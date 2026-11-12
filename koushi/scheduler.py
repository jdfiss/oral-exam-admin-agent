# -*- coding: utf-8 -*-
"""通知排程器（ReminderScheduler）—— 讀三個追蹤引擎目前的截止日狀態，
決定哪些項目現在該催，並觸發草稿產生器（drafts.DraftTemplateEngine）。

只讀狀態、只產生草稿；不寄信、不改主檔、不寫回引擎狀態。是否核准實際
寄送由呼叫端（系辦／CLI）決定。

逾期後不是每次重算都重催：附件6催繳有 reminder_count／last_reminder_date
可追蹤，同一追蹤鍵在 interval_days 個工作日內只排一次，避免洗版教授信箱。
缺件、確認目前沒有節流欄位，逾期期間每次掃描都會排入（由呼叫端或未來的
寄送記錄依 idempotency_key 去重）。

收件人預設一律用 drafts.test_recipient() 產生的 @example.invalid 佔位地址；
要接上真實通訊錄，呼叫端可傳入 resolve_recipient(kind, obj) -> str | None。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
from dataclasses import dataclass
from typing import Callable

from . import config as C
from .deadline import business_days_between
from .drafts import DraftTemplateEngine, NotificationDraft, group_deficiency_by_case, placeholder_recipient
from .engines.collection import CollectionItem, CollectionReport
from .engines.confirmation import ConfirmationReport, ConfirmationRequest
from .engines.deficiency import DeficiencyItem, DeficiencyReport
from .responsibility import STUDENT

REMINDER_INTERVAL_DAYS = 2  # 逾期後至少間隔幾個工作日才能再排一次催繳提醒

RecipientResolver = Callable[[str, object], "str | None"]


@dataclass(frozen=True)
class ScheduledReminder:
    """一次排定的提醒事件；draft 尚未核准寄送。"""

    kind: str  # deficiency / confirmation / collection
    tracking_key: tuple
    due_date: _dt.date | None
    draft: NotificationDraft


def _slug(*parts: str) -> str:
    return "-".join(str(p) for p in parts if p)


class ReminderScheduler:
    """依截止日狀態決定何時該催件，觸發草稿產生器。"""

    def __init__(
        self,
        draft_engine: DraftTemplateEngine | None = None,
        *,
        today: Callable[[], _dt.date] | None = None,
        resolve_recipient: RecipientResolver | None = None,
        interval_days: int = REMINDER_INTERVAL_DAYS,
        deficiency_lead_days: int = C.DEFICIENCY_REMINDER_LEAD_DAYS,
        confirmation_interval_days: int = REMINDER_INTERVAL_DAYS,
        holidays: set[_dt.date] | None = None,
        business_days: bool = True,
    ):
        self._engine = draft_engine or DraftTemplateEngine()
        self._today = today or clock.today
        self._resolve = resolve_recipient
        self._interval_days = interval_days
        self._confirmation_interval_days = max(confirmation_interval_days, 1)
        self._deficiency_lead_days = deficiency_lead_days
        self._holidays = holidays
        self._business_days = business_days

    def _recipient(self, kind: str, obj, local_part: str) -> str | None:
        if self._resolve is None:
            return placeholder_recipient(local_part)
        return self._resolve(kind, obj)

    # ── 缺件追蹤 ──────────────────────────────────────

    def scan_deficiency(self, report: DeficiencyReport) -> list[ScheduledReminder]:
        """截止前若干工作日起、每天最多排一次補件催繳提醒；到期即停止。

        只處理責任在學生、已通知但尚未逾期的缺件——系辦責任的缺件不寄信，
        改由 internal_todos() 提供待辦清單；已逾期的不再催（決策：到期後
        不設逾期催件，該轉交系辦或人工處理，不是繼續寄信給學生）。
        """
        today = self._today()
        student_items = [
            i for i in report.items
            if i.status_on(today) == "notified"
            and i.owner == STUDENT
            and self._due_for_deficiency_reminder(i, today=today)
        ]
        scheduled = []
        for case_key, items in group_deficiency_by_case(student_items).items():
            to = self._recipient("deficiency", items, case_key)
            draft = self._engine.deficiency_draft(case_key, items[0].student_name, items, to=to,
                                                 reminder_date=today)
            due = max((i.deadline.computed_date for i in items if i.deadline), default=None)
            scheduled.append(ScheduledReminder(
                kind="deficiency", tracking_key=(case_key,), due_date=due, draft=draft,
            ))
        return scheduled

    def _due_for_deficiency_reminder(self, item: DeficiencyItem, *, today: _dt.date) -> bool:
        if item.deadline is None:
            return False
        if self._business_days:
            remaining = item.deadline.remaining_business_days_on(today, self._holidays)
        else:
            remaining = max((item.deadline.computed_date - today).days, 0)
        if remaining > self._deficiency_lead_days:
            return False  # 還沒進入截止前的催件窗口
        return item.last_reminder_date != today  # 同一曆日不重寄

    def internal_todos(self, report: DeficiencyReport) -> list[DeficiencyItem]:
        """系辦責任、目前逾期的缺件——內部待辦清單，不產生草稿、不寄信。"""
        today = self._today()
        return [
            i for i in report.items
            if i.status_on(today) == "overdue" and i.owner != STUDENT
        ]

    # ── 教授確認 ──────────────────────────────────────

    def scan_confirmation(self, report: ConfirmationReport) -> list[ScheduledReminder]:
        """逾期未回覆的確認請求，逐場次排一次催辦提醒。"""
        scheduled = []
        today = self._today()
        for req in report.requests:
            if req.status_on(today) != "overdue":
                continue
            if req.last_reminder_date:
                elapsed = business_days_between(req.last_reminder_date, today, self._holidays) if self._business_days else (today - req.last_reminder_date).days
                if elapsed < self._confirmation_interval_days:
                    continue
            to = self._recipient("confirmation", req, _slug(*req.request_key))
            draft = self._engine.confirmation_draft(req, to=to)
            scheduled.append(ScheduledReminder(
                kind="confirmation",
                tracking_key=req.request_key,
                due_date=req.deadline.computed_date if req.deadline else None,
                draft=draft,
            ))
        return scheduled

    # ── 附件6催繳 ─────────────────────────────────────

    def scan_collection(self, report: CollectionReport) -> list[ScheduledReminder]:
        """逾期未收回的附件6，依節流間隔決定是否再排一次催繳。"""
        scheduled = []
        today = self._today()
        for item in report.items:
            if item.status_on(today) != "overdue":
                continue
            if not self._due_for_reminder(item, today=today):
                continue
            to = self._recipient("collection", item, _slug(*item.request_key))
            draft = self._engine.collection_draft(item, to=to)
            scheduled.append(ScheduledReminder(
                kind="collection",
                tracking_key=item.request_key,
                due_date=item.deadline.computed_date if item.deadline else None,
                draft=draft,
            ))
        return scheduled

    def _due_for_reminder(self, item: CollectionItem, *, today: _dt.date) -> bool:
        if item.last_reminder_date is None:
            return True
        elapsed = business_days_between(item.last_reminder_date, today, self._holidays) if self._business_days else (today - item.last_reminder_date).days
        return elapsed >= self._interval_days

    # ── 一次掃描三個引擎 ────────────────────────────────

    def scan_all(
        self,
        *,
        deficiency: DeficiencyReport | None = None,
        confirmation: ConfirmationReport | None = None,
        collection: CollectionReport | None = None,
    ) -> list[ScheduledReminder]:
        out: list[ScheduledReminder] = []
        if deficiency is not None:
            out.extend(self.scan_deficiency(deficiency))
        if confirmation is not None:
            out.extend(self.scan_confirmation(confirmation))
        if collection is not None:
            out.extend(self.scan_collection(collection))
        return out
