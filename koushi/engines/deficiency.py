# -*- coding: utf-8 -*-
"""補件追蹤引擎 —— 偵測缺件 → 分類責任 → 追蹤通知 → 計算截止日。

每次完整重算，不做增量。上一輪的追蹤狀態透過 previous 參數帶入，
引擎比對後自動判定 new / notified / overdue / resolved。
"""

from __future__ import annotations

import datetime as _dt
from .. import clock
from dataclasses import dataclass, replace

from .. import validation
from ..deadline import Deadline, deficiency_deadline
from ..model import Master
from ..responsibility import classify as _classify


@dataclass(frozen=True)
class DeficiencyItem:
    """一筆缺件追蹤紀錄。"""
    case_key: str
    term: str
    student_id: str
    student_name: str
    issue_code: str
    field: str
    description: str
    owner: str
    blocks_att4: bool
    blocks_att6: bool
    notified_date: _dt.date | None = None
    deadline: Deadline | None = None
    reminder_count: int = 0
    last_reminder_date: _dt.date | None = None

    @property
    def tracking_key(self) -> tuple[str, str, str]:
        return (self.case_key, self.issue_code, self.field)

    def status_on(self, day: _dt.date) -> str:
        """用明確日期判定狀態，避免排程注入時鐘後仍偷讀系統日期。"""
        if self.deadline and self.deadline.is_overdue_on(day):
            return "overdue"
        if self.notified_date:
            return "notified"
        return "new"

    @property
    def status(self) -> str:
        return self.status_on(clock.today())


@dataclass(frozen=True)
class DeficiencyReport:
    """完整的缺件追蹤報告。"""
    items: tuple[DeficiencyItem, ...]
    resolved_keys: frozenset[tuple[str, str, str]]

    @property
    def new(self) -> list[DeficiencyItem]:
        return [i for i in self.items if i.status == "new"]

    @property
    def notified(self) -> list[DeficiencyItem]:
        return [i for i in self.items if i.status == "notified"]

    @property
    def overdue(self) -> list[DeficiencyItem]:
        return [i for i in self.items if i.status == "overdue"]

    @property
    def by_owner(self) -> dict[str, list[DeficiencyItem]]:
        out: dict[str, list[DeficiencyItem]] = {}
        for item in self.items:
            out.setdefault(item.owner, []).append(item)
        return out

    @property
    def by_case(self) -> dict[str, list[DeficiencyItem]]:
        out: dict[str, list[DeficiencyItem]] = {}
        for item in self.items:
            out.setdefault(item.case_key, []).append(item)
        return out


def _item_from_issue(issue: dict) -> DeficiencyItem:
    return DeficiencyItem(
        case_key=str(issue.get("案件鍵", "")),
        term=str(issue.get("學期", "")),
        student_id=str(issue.get("學號", "")),
        student_name=str(issue.get("姓名", "")),
        issue_code=str(issue.get("問題代碼", "")),
        field=str(issue.get("欄位", "")),
        description=str(issue.get("說明", "")),
        owner=_classify(issue),
        blocks_att4=issue.get("阻擋附件4") == "是",
        blocks_att6=issue.get("阻擋附件6") == "是",
    )


def scan(
    master: Master,
    *,
    previous: list[DeficiencyItem] | None = None,
    holidays: set[_dt.date] | None = None,
) -> DeficiencyReport:
    """掃描主檔，產生缺件追蹤報告。

    previous 帶入上一輪的追蹤紀錄；已通知或已有期限的項目會保留 notified_date 與 deadline。
    不在本次掃描結果中的 previous 項目視為已解決，列入 resolved_keys。
    """
    issues = validation.check(master)
    blocking = [i for i in issues if i.get("阻擋產出") == "是"]

    prev_map: dict[tuple[str, str, str], DeficiencyItem] = {}
    if previous:
        for item in previous:
            prev_map[item.tracking_key] = item

    items: list[DeficiencyItem] = []
    current_keys: set[tuple[str, str, str]] = set()
    for issue in blocking:
        item = _item_from_issue(issue)
        current_keys.add(item.tracking_key)
        prev = prev_map.get(item.tracking_key)
        # 尚未寄出通知但已套上全系統補件截止日的學生缺件也要保留期限，
        # 截止後才會以逾期出現在系辦待處理清單（例如查無收件人的學生）。
        if prev and (prev.notified_date or prev.deadline):
            item = replace(
                item, notified_date=prev.notified_date, deadline=prev.deadline,
                reminder_count=prev.reminder_count, last_reminder_date=prev.last_reminder_date,
            )
        items.append(item)

    resolved = frozenset(k for k in prev_map if k not in current_keys)
    return DeficiencyReport(items=tuple(items), resolved_keys=resolved)


def mark_notified(
    items: list[DeficiencyItem],
    *,
    date: _dt.date | None = None,
    holidays: set[_dt.date] | None = None,
) -> list[DeficiencyItem]:
    """標記為已通知，附上補件截止日。只更新尚未通知的項目。"""
    date = date or clock.today()
    dl = deficiency_deadline(date, holidays)
    return [
        replace(item, notified_date=date, deadline=dl) if item.notified_date is None else item
        for item in items
    ]


def mark_reminded(
    item: DeficiencyItem,
    *,
    date: _dt.date | None = None,
) -> DeficiencyItem:
    """記錄一次期限前催件；同一曆日只應呼叫一次，由呼叫端保證。"""
    date = date or clock.today()
    return replace(item, reminder_count=item.reminder_count + 1, last_reminder_date=date)


# ── 序列化（持久化用）──────────────────────────────────

_DATE_FMT = "%Y-%m-%d"


def item_to_dict(item: DeficiencyItem) -> dict:
    d = {
        "case_key": item.case_key,
        "term": item.term,
        "student_id": item.student_id,
        "student_name": item.student_name,
        "issue_code": item.issue_code,
        "field": item.field,
        "description": item.description,
        "owner": item.owner,
        "blocks_att4": item.blocks_att4,
        "blocks_att6": item.blocks_att6,
        "reminder_count": item.reminder_count,
    }
    if item.notified_date:
        d["notified_date"] = item.notified_date.strftime(_DATE_FMT)
    if item.deadline:
        d["deadline_base"] = item.deadline.base_date.strftime(_DATE_FMT)
        d["deadline_days"] = item.deadline.business_days
        d["deadline_date"] = item.deadline.computed_date.strftime(_DATE_FMT)
    if item.last_reminder_date:
        d["last_reminder_date"] = item.last_reminder_date.strftime(_DATE_FMT)
    return d


def item_from_dict(d: dict) -> DeficiencyItem:
    notified = None
    if d.get("notified_date"):
        notified = _dt.datetime.strptime(d["notified_date"], _DATE_FMT).date()
    deadline = None
    if d.get("deadline_date"):
        deadline = Deadline(
            base_date=_dt.datetime.strptime(d["deadline_base"], _DATE_FMT).date(),
            business_days=int(d["deadline_days"]),
            computed_date=_dt.datetime.strptime(d["deadline_date"], _DATE_FMT).date(),
        )
    last_reminder = None
    if d.get("last_reminder_date"):
        last_reminder = _dt.datetime.strptime(d["last_reminder_date"], _DATE_FMT).date()
    return DeficiencyItem(
        case_key=d["case_key"],
        term=d["term"],
        student_id=d["student_id"],
        student_name=d["student_name"],
        issue_code=d["issue_code"],
        field=d["field"],
        description=d["description"],
        owner=d["owner"],
        blocks_att4=d.get("blocks_att4", True),
        blocks_att6=d.get("blocks_att6", d.get("blocks_att56", True)),  # 相容附件5 取消前存的狀態
        notified_date=notified,
        deadline=deadline,
        reminder_count=d.get("reminder_count", 0),
        last_reminder_date=last_reminder,
    )
