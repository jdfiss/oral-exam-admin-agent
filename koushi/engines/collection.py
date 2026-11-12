# -*- coding: utf-8 -*-
"""附件6催繳引擎 —— 口試後追蹤附件6回收，循環直到全部到齊。

口試後流程：
1. 口試前把交付版附件6（委員個資四欄留白）交給指導教授／學生帶去口試，委員現場簽名、
   補填未交的個資；完整版（含個資）只讓系辦下載核撥，不寄出（2026-10-01 Q14）
2. 教授收齊委員簽名後繳回；現場手寫的個資由專員在「委員個資」頁登錄（Q15）
3. 追蹤每份附件6的回收狀態：outstanding → collected（合格）/ needs_correction（不合格）/ overdue
4. 逾期或不合格觸發催繳／補正通知

每份追蹤項目對應一張附件6（同清冊組 = 同學期+學位別+指導教授+日期）。

「合格」目前是系辦收到文件後的人工判斷（有沒有簽章、資料是否齊全），
不是系統自動核對檔案內容——這裡沒有文件上傳或內容比對能力，只提供狀態
機讓系辦的判斷結果可被追蹤、不會跟「已收到」混成同一種狀態。
"""

from __future__ import annotations

import datetime as _dt
from .. import clock
from dataclasses import dataclass, replace
import hashlib
import json

from ..deadline import Deadline, add_business_days
from ..generators.grouping import roster_groups
from ..model import Master


OUTSTANDING = "outstanding"
COLLECTED = "collected"
NEEDS_CORRECTION = "needs_correction"
OVERDUE = "overdue"


@dataclass(frozen=True)
class CollectionItem:
    """一份附件6的催繳追蹤紀錄。"""
    group_key: tuple
    term: str
    advisor: str
    degree: str
    date_text: str
    student_names: tuple[str, ...]
    case_keys: tuple[str, ...]
    sent_date: _dt.date | None = None
    deadline: Deadline | None = None
    collected_date: _dt.date | None = None
    qualified: bool | None = None  # 只在 collected_date 有值時才有意義
    correction_note: str = ""
    reminder_count: int = 0
    last_reminder_date: _dt.date | None = None
    content_version: str = ""
    history: tuple[dict, ...] = ()

    @property
    def request_key(self) -> tuple[str, ...]:
        """（學期, 學位, 指導教授組合, 日期）；缺指導教授或日期而被隔離的案件再附隔離鍵，避免撞號。"""
        key = (self.term, self.degree, self.advisor, self.date_text)
        fallback = self.group_key[4] if len(self.group_key) > 4 else ""
        return key + (fallback,) if fallback else key

    def status_on(self, day: _dt.date) -> str:
        """用明確日期判定狀態，讓排程測試與批次執行可固定時鐘。"""
        if self.collected_date:
            return COLLECTED if self.qualified is not False else NEEDS_CORRECTION
        if self.deadline and self.deadline.is_overdue_on(day):
            return OVERDUE
        if self.sent_date:
            return OUTSTANDING
        return "draft"

    @property
    def status(self) -> str:
        return self.status_on(clock.today())


@dataclass(frozen=True)
class CollectionReport:
    """附件6催繳彙總報告。"""
    items: tuple[CollectionItem, ...]

    @property
    def draft(self) -> list[CollectionItem]:
        return [i for i in self.items if i.status == "draft"]

    @property
    def outstanding(self) -> list[CollectionItem]:
        return [i for i in self.items if i.status == OUTSTANDING]

    @property
    def collected(self) -> list[CollectionItem]:
        return [i for i in self.items if i.status == COLLECTED]

    @property
    def needs_correction(self) -> list[CollectionItem]:
        """已收到，但系辦判斷資料或簽章不齊全，需要補正、不算完成。"""
        return [i for i in self.items if i.status == NEEDS_CORRECTION]

    @property
    def overdue(self) -> list[CollectionItem]:
        return [i for i in self.items if i.status == OVERDUE]

    @property
    def all_collected(self) -> bool:
        return bool(self.items) and all(i.status == COLLECTED for i in self.items)

    @property
    def needs_action(self) -> list[CollectionItem]:
        return [i for i in self.items if i.status in (OUTSTANDING, OVERDUE, NEEDS_CORRECTION)]


def create_items(
    master: Master,
    *,
    term: str | None = None,
) -> list[CollectionItem]:
    """從主檔的清冊分組建立催繳追蹤項目。"""
    groups = roster_groups(master.active(term))
    items: list[CollectionItem] = []
    for group_key, exams in groups.items():
        first = exams[0]
        items.append(CollectionItem(
            group_key=group_key,
            term=str(first.學期),
            advisor=first.指導教授 or "",
            degree=first.學位別,
            date_text=first.日期字串,
            student_names=tuple(e.姓名 for e in exams),
            case_keys=tuple(e.案件鍵 for e in exams),
            content_version=hashlib.sha256(json.dumps([
                [e.案件鍵, e.姓名, e.指導教授, e.口試方式,
                 [(m.committee_id, m.委員姓名 or getattr(master.committees.get(m.committee_id), "委員姓名", ""), m.服務單位,
                   m.職稱, m.口試費, m.交通費, m.交通費說明) for m in e.委員]]
                for e in sorted(exams, key=lambda e: e.案件鍵)
            ], ensure_ascii=False).encode("utf-8")).hexdigest(),
        ))
    return items


def reconcile(
    master: Master,
    previous: list[CollectionItem],
    *,
    term: str | None = None,
) -> list[CollectionItem]:
    """用目前主檔對帳：保留已有追蹤狀態的項目，補上新增的分組。

    同組案件鍵變了（換委員、新增／移除學生）代表已收回的附件6內容跟現在
    的清冊對不上，舊的收件狀態不能沿用——重設回 draft，等於要重新產生、
    重新寄一次才能再走一次回收流程。
    """
    current = create_items(master, term=term)
    prev_map = {i.request_key: i for i in previous}
    result: list[CollectionItem] = []
    for item in current:
        prev = prev_map.get(item.request_key)
        if prev is None or prev.case_keys != item.case_keys or prev.content_version != item.content_version:
            if prev is not None:
                evidence = item_to_dict(prev)
                evidence.pop("history", None)
                item = replace(item, history=prev.history + (evidence,))
            result.append(item)  # 新分組，或內容已變動：本來就是預設的 draft 狀態
        else:
            result.append(replace(prev, student_names=item.student_names,
                                  case_keys=item.case_keys))
    return result


def mark_sent(
    items: list[CollectionItem],
    *,
    date: _dt.date | None = None,
    deadline_days: int = 5,
    holidays: set[_dt.date] | None = None,
    business_days: bool = True,
    exam_date: _dt.date | None = None,
) -> list[CollectionItem]:
    """標記已寄出，附上回收截止日。只更新尚未寄出的項目。"""
    date = date or clock.today()
    base = max(date, exam_date) if exam_date else date
    computed = add_business_days(base, deadline_days, holidays) if business_days else base + _dt.timedelta(days=deadline_days)
    dl = Deadline(base_date=base, business_days=deadline_days, computed_date=computed)
    return [
        replace(i, sent_date=date, deadline=dl) if i.sent_date is None else i
        for i in items
    ]


def mark_collected(
    item: CollectionItem,
    *,
    date: _dt.date | None = None,
    qualified: bool = True,
    note: str = "",
) -> CollectionItem:
    """標記已收回；qualified=False 表示收到但資料或簽章不齊全，需要補正。

    合格與否是系辦收到文件後的人工判斷，這裡只記錄結果，不核對檔案內容。
    """
    return replace(
        item, collected_date=date or clock.today(),
        qualified=qualified, correction_note=note if not qualified else "",
    )


def mark_reminded(
    item: CollectionItem,
    *,
    date: _dt.date | None = None,
) -> CollectionItem:
    """記錄一次催繳。"""
    date = date or clock.today()
    return replace(item, reminder_count=item.reminder_count + 1, last_reminder_date=date)


def apply_collected(
    items: list[CollectionItem],
    collected_keys: set[tuple[str, ...]],
    *,
    date: _dt.date | None = None,
) -> list[CollectionItem]:
    """批次標記已收回。collected_keys 為 request_key 集合。"""
    date = date or clock.today()
    return [
        mark_collected(i, date=date) if i.request_key in collected_keys else i
        for i in items
    ]


def report(items: list[CollectionItem]) -> CollectionReport:
    return CollectionReport(items=tuple(items))


# ── 序列化 ──────────────────────────────────────────

_DATE_FMT = "%Y-%m-%d"


def item_to_dict(item: CollectionItem) -> dict:
    d: dict = {
        "group_key": list(item.group_key),
        "term": item.term,
        "advisor": item.advisor,
        "degree": item.degree,
        "date_text": item.date_text,
        "student_names": list(item.student_names),
        "case_keys": list(item.case_keys),
        "reminder_count": item.reminder_count,
        "content_version": item.content_version, "history": list(item.history),
    }
    if item.sent_date:
        d["sent_date"] = item.sent_date.strftime(_DATE_FMT)
    if item.deadline:
        d["deadline_base"] = item.deadline.base_date.strftime(_DATE_FMT)
        d["deadline_days"] = item.deadline.business_days
        d["deadline_date"] = item.deadline.computed_date.strftime(_DATE_FMT)
    if item.collected_date:
        d["collected_date"] = item.collected_date.strftime(_DATE_FMT)
        d["qualified"] = item.qualified
        if item.correction_note:
            d["correction_note"] = item.correction_note
    if item.last_reminder_date:
        d["last_reminder_date"] = item.last_reminder_date.strftime(_DATE_FMT)
    return d


def item_from_dict(d: dict) -> CollectionItem:
    sent = None
    if d.get("sent_date"):
        sent = _dt.datetime.strptime(d["sent_date"], _DATE_FMT).date()
    deadline = None
    if d.get("deadline_date"):
        deadline = Deadline(
            base_date=_dt.datetime.strptime(d["deadline_base"], _DATE_FMT).date(),
            business_days=int(d["deadline_days"]),
            computed_date=_dt.datetime.strptime(d["deadline_date"], _DATE_FMT).date(),
        )
    collected = None
    if d.get("collected_date"):
        collected = _dt.datetime.strptime(d["collected_date"], _DATE_FMT).date()
    last_reminder = None
    if d.get("last_reminder_date"):
        last_reminder = _dt.datetime.strptime(d["last_reminder_date"], _DATE_FMT).date()
    return CollectionItem(
        group_key=tuple(d["group_key"]),
        term=d["term"],
        advisor=d["advisor"],
        degree=d["degree"],
        date_text=d["date_text"],
        student_names=tuple(d["student_names"]),
        case_keys=tuple(d["case_keys"]),
        sent_date=sent,
        deadline=deadline,
        collected_date=collected,
        qualified=d.get("qualified", True) if collected else None,
        correction_note=d.get("correction_note", ""),
        reminder_count=d.get("reminder_count", 0),
        last_reminder_date=last_reminder,
        content_version=d.get("content_version", ""), history=tuple(d.get("history", [])),
    )
