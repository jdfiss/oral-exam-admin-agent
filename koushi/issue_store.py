# -*- coding: utf-8 -*-
"""缺件紀錄庫（IssueRecordStore）—— 每筆缺件的完整生命週期記錄。

`koushi/engines/deficiency.py` 的 DeficiencyItem 只有粗粒度的
new/notified/overdue 三態，是「這次掃描看到什麼」的快照。這裡疊加一層
更細的狀態機，記錄「這筆缺件走到哪一步」：

    detected → draft_ready → request_sent → resubmitted → revalidated → resolved

不取代 deficiency.py，只是在它輸出的追蹤鍵（case_key, issue_code, field）
上疊加流程狀態；resolved 之後若同一追蹤鍵又被偵測到（復發），可以轉回
detected。history 保留每次轉移，供事件時間線串接。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import util as U
from .deadline import Deadline
from .engines.deficiency import DeficiencyItem

DETECTED = "detected"
DRAFT_READY = "draft_ready"
REQUEST_SENT = "request_sent"
RESUBMITTED = "resubmitted"
REVALIDATED = "revalidated"
RESOLVED = "resolved"

# 允許的狀態轉移；同一追蹤鍵 resolved 後可能復發，故 resolved → detected 也合法。
_ALLOWED_TRANSITIONS = {
    DETECTED: {DRAFT_READY, RESOLVED},
    DRAFT_READY: {REQUEST_SENT, RESOLVED},
    REQUEST_SENT: {RESUBMITTED, RESOLVED},
    RESUBMITTED: {REVALIDATED, RESOLVED},
    REVALIDATED: {RESOLVED, REQUEST_SENT},  # 複驗仍未過 → 退回通知循環
    RESOLVED: {DETECTED},
}

_TIMESTAMP_FIELD = {
    DETECTED: "detected_at",
    DRAFT_READY: "draft_ready_at",
    REQUEST_SENT: "request_sent_at",
    RESUBMITTED: "resubmitted_at",
    REVALIDATED: "revalidated_at",
    RESOLVED: "resolved_at",
}

_DATE_FMT = "%Y-%m-%d"


@dataclass(frozen=True)
class IssueRecord:
    """一筆缺件的生命週期記錄。tracking_key 對齊 DeficiencyItem.tracking_key。"""

    case_key: str
    issue_code: str
    field: str
    term: str
    student_id: str
    student_name: str
    description: str
    owner: str
    status: str = DETECTED
    detected_at: _dt.date | None = None
    draft_ready_at: _dt.date | None = None
    request_sent_at: _dt.date | None = None
    resubmitted_at: _dt.date | None = None
    revalidated_at: _dt.date | None = None
    resolved_at: _dt.date | None = None
    deadline: Deadline | None = None
    # 解決時對應的資料版本；本專案目前沒有內容版本／revision 機制
    # （見 docs/review-2026-09-07.md 第2點），先留欄位、預設 None，
    # 不臆造假的版本號。
    resolved_version: str | None = None
    history: tuple[dict, ...] = ()

    @property
    def tracking_key(self) -> tuple[str, str, str]:
        return (self.case_key, self.issue_code, self.field)


def from_deficiency_item(item: DeficiencyItem, *, date: _dt.date | None = None) -> IssueRecord:
    """從補件追蹤引擎的一筆缺件建立初始 detected 記錄。"""
    date = date or clock.today()
    entry = {"from": None, "to": DETECTED, "at": date.strftime(_DATE_FMT), "note": ""}
    return IssueRecord(
        case_key=item.case_key, issue_code=item.issue_code, field=item.field,
        term=item.term, student_id=item.student_id, student_name=item.student_name,
        description=item.description, owner=item.owner,
        status=DETECTED, detected_at=date, history=(entry,),
    )


def transition(
    record: IssueRecord,
    to_status: str,
    *,
    date: _dt.date | None = None,
    note: str = "",
    deadline: Deadline | None = None,
    resolved_version: str | None = None,
) -> IssueRecord:
    """把一筆記錄轉移到新狀態。不允許的轉移會拋例外，不靜默忽略。"""
    allowed = _ALLOWED_TRANSITIONS.get(record.status, set())
    if to_status not in allowed:
        raise ValueError(f"不允許的狀態轉移：{record.status} → {to_status}")

    date = date or clock.today()
    entry = {"from": record.status, "to": to_status, "at": date.strftime(_DATE_FMT), "note": note}
    updates: dict = {"status": to_status, "history": record.history + (entry,)}
    updates[_TIMESTAMP_FIELD[to_status]] = date
    if deadline is not None:
        updates["deadline"] = deadline
    if to_status == RESOLVED and resolved_version is not None:
        updates["resolved_version"] = resolved_version
    if to_status == DETECTED and record.status == RESOLVED:
        # 復發：清掉舊的解決資訊，重新走一輪生命週期。
        updates["resolved_at"] = None
        updates["resolved_version"] = None
    return replace(record, **updates)


@dataclass
class IssueRecordStore:
    """以 tracking_key 為鍵的缺件記錄集合。"""

    records: dict[tuple[str, str, str], IssueRecord]

    @classmethod
    def empty(cls) -> "IssueRecordStore":
        return cls(records={})

    def get(self, tracking_key: tuple[str, str, str]) -> IssueRecord | None:
        return self.records.get(tracking_key)

    def put(self, record: IssueRecord) -> None:
        self.records[record.tracking_key] = record

    def all(self) -> list[IssueRecord]:
        return list(self.records.values())

    def by_status(self, status: str) -> list[IssueRecord]:
        return [r for r in self.records.values() if r.status == status]

    def by_owner(self, owner: str) -> list[IssueRecord]:
        return [r for r in self.records.values() if r.owner == owner]

    def by_case(self, case_key: str) -> list[IssueRecord]:
        return [r for r in self.records.values() if r.case_key == case_key]

    def active(self) -> list[IssueRecord]:
        """尚未解決的記錄。"""
        return [r for r in self.records.values() if r.status != RESOLVED]


def reconcile(
    previous: IssueRecordStore,
    items: list[DeficiencyItem],
    *,
    date: _dt.date | None = None,
) -> IssueRecordStore:
    """用本次掃描的缺件清單對帳。

    新追蹤鍵 → 建立 detected 記錄（或替已 resolved 的記錄轉回 detected，
    視為復發）。本次沒出現、但先前還沒 resolved 的追蹤鍵 → 轉 resolved。
    其餘保留原狀態不動。
    """
    date = date or clock.today()
    current_keys = {item.tracking_key for item in items}
    result: dict[tuple[str, str, str], IssueRecord] = dict(previous.records)

    for item in items:
        key = item.tracking_key
        existing = result.get(key)
        if existing is None:
            result[key] = from_deficiency_item(item, date=date)
        elif existing.status == RESOLVED:
            result[key] = transition(existing, DETECTED, date=date, note="復發")

    for key, record in list(result.items()):
        if key not in current_keys and record.status not in (RESOLVED, RESUBMITTED):
            result[key] = transition(record, RESOLVED, date=date, note="本次掃描未再偵測到")

    return IssueRecordStore(records=result)


# ── 複驗（resubmitted → revalidated → resolved / request_sent）────

@dataclass(frozen=True)
class RevalidationVerdict:
    """一筆 resubmitted 缺件的複驗結論。passed=True 表示問題已解決。"""

    passed: bool
    note: str = ""


Reviewer = Callable[[IssueRecord, bool], RevalidationVerdict]
"""reviewer(record, still_detected) → RevalidationVerdict。

`still_detected` 是確定性檢核的結果（該追蹤鍵是否仍出現在本次掃描）；
reviewer 可以純粹依據這個旗標決定（automated_reviewer 就是），也可以
進一步分析學生提交的內容再判斷（未來的 LLM reviewer）。
"""


def automated_reviewer(record: IssueRecord, still_detected: bool) -> RevalidationVerdict:
    """預設複驗邏輯：問題從掃描結果消失即視為通過。"""
    if not still_detected:
        return RevalidationVerdict(passed=True, note="自動檢核：問題已不存在")
    return RevalidationVerdict(passed=False, note="自動檢核：問題仍存在")


def revalidate(
    store: IssueRecordStore,
    current_keys: set[tuple[str, str, str]],
    *,
    reviewer: Reviewer = automated_reviewer,
    date: _dt.date | None = None,
) -> IssueRecordStore:
    """對所有 resubmitted 記錄執行複驗，依結論推進狀態。

    通過 → revalidated → resolved；未通過 → revalidated → request_sent
    （退回通知循環）。reviewer 決定每筆是否通過；預設用
    `automated_reviewer`（問題消失＝通過），未來可替換為 LLM reviewer。
    """
    date = date or clock.today()
    result = dict(store.records)
    for key, record in list(result.items()):
        if record.status != RESUBMITTED:
            continue
        still_detected = key in current_keys
        verdict = reviewer(record, still_detected)
        updated = transition(record, REVALIDATED, date=date, note=verdict.note)
        if verdict.passed:
            updated = transition(updated, RESOLVED, date=date, note="複驗通過")
        else:
            updated = transition(updated, REQUEST_SENT, date=date, note="複驗未通過，退回通知循環")
        result[key] = updated
    return IssueRecordStore(records=result)


# ── 序列化 ──────────────────────────────────────────

def _date_to_str(d: _dt.date | None) -> str | None:
    return d.strftime(_DATE_FMT) if d else None


def _str_to_date(s: str | None) -> _dt.date | None:
    return _dt.datetime.strptime(s, _DATE_FMT).date() if s else None


def record_to_dict(r: IssueRecord) -> dict:
    d = {
        "case_key": r.case_key, "issue_code": r.issue_code, "field": r.field,
        "term": r.term, "student_id": r.student_id, "student_name": r.student_name,
        "description": r.description, "owner": r.owner, "status": r.status,
        "detected_at": _date_to_str(r.detected_at),
        "draft_ready_at": _date_to_str(r.draft_ready_at),
        "request_sent_at": _date_to_str(r.request_sent_at),
        "resubmitted_at": _date_to_str(r.resubmitted_at),
        "revalidated_at": _date_to_str(r.revalidated_at),
        "resolved_at": _date_to_str(r.resolved_at),
        "resolved_version": r.resolved_version,
        "history": list(r.history),
    }
    if r.deadline:
        d["deadline_base"] = r.deadline.base_date.strftime(_DATE_FMT)
        d["deadline_days"] = r.deadline.business_days
        d["deadline_date"] = r.deadline.computed_date.strftime(_DATE_FMT)
    return d


def record_from_dict(d: dict) -> IssueRecord:
    deadline = None
    if d.get("deadline_date"):
        deadline = Deadline(
            base_date=_str_to_date(d["deadline_base"]),
            business_days=int(d["deadline_days"]),
            computed_date=_str_to_date(d["deadline_date"]),
        )
    return IssueRecord(
        case_key=d["case_key"], issue_code=d["issue_code"], field=d["field"],
        term=d["term"], student_id=d["student_id"], student_name=d["student_name"],
        description=d["description"], owner=d["owner"], status=d.get("status", DETECTED),
        detected_at=_str_to_date(d.get("detected_at")),
        draft_ready_at=_str_to_date(d.get("draft_ready_at")),
        request_sent_at=_str_to_date(d.get("request_sent_at")),
        resubmitted_at=_str_to_date(d.get("resubmitted_at")),
        revalidated_at=_str_to_date(d.get("revalidated_at")),
        resolved_at=_str_to_date(d.get("resolved_at")),
        resolved_version=d.get("resolved_version"),
        deadline=deadline,
        history=tuple(d.get("history", [])),
    )


def load(path: str | Path) -> IssueRecordStore:
    """從 JSON 載入。檔案不存在時回傳空的 store。"""
    path = Path(path)
    if not path.is_file():
        return IssueRecordStore.empty()
    raw = U.read_json_state(path)
    records = {}
    for d in raw.get("records", []):
        record = record_from_dict(d)
        records[record.tracking_key] = record
    return IssueRecordStore(records=records)


def save(store: IssueRecordStore, path: str | Path) -> Path:
    raw = {"records": [record_to_dict(r) for r in store.all()]}
    return U.atomic_write_json(raw, path)
