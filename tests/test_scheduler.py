# -*- coding: utf-8 -*-
"""通知排程器測試：逾期判定、附件6催繳節流、觸發草稿產生器。"""

import datetime as _dt
from dataclasses import replace

import pytest

from koushi.deadline import Deadline, add_business_days
from koushi.drafts import TEST_RECIPIENT_DOMAIN
from koushi.engines.collection import CollectionItem, CollectionReport
from koushi.engines.confirmation import ConfirmationReport, ConfirmationRequest
from koushi.engines.deficiency import DeficiencyItem, DeficiencyReport
from koushi.scheduler import ReminderScheduler


def _past_deadline(days_overdue=1):
    today = _dt.date.today()
    base = today - _dt.timedelta(days=days_overdue + 10)
    return Deadline(base_date=base, business_days=5,
                     computed_date=today - _dt.timedelta(days=days_overdue))


def _future_deadline(days_left=10):
    today = _dt.date.today()
    return Deadline(base_date=today, business_days=days_left,
                     computed_date=today + _dt.timedelta(days=days_left))


def _near_deadline(business_days_left=1):
    """尚未逾期，且落在預設催件提前工作日窗口內的截止日（見 scan_deficiency）。"""
    today = _dt.date.today()
    computed = add_business_days(today, business_days_left) if business_days_left > 0 else today
    return Deadline(base_date=today - _dt.timedelta(days=10), business_days=5, computed_date=computed)


def _def_item(case_key="1142-A", overdue=True, owner="student", deadline=None,
              last_reminder_date=None):
    return DeficiencyItem(
        case_key=case_key, term="1142", student_id="1115020",
        student_name="學生甲", issue_code="NO_COMMITTEE", field="委員名單",
        description="測試", owner=owner,
        blocks_att4=True, blocks_att6=True,
        notified_date=_dt.date(2026, 1, 1),
        deadline=deadline if deadline is not None else (_past_deadline() if overdue else _future_deadline()),
        last_reminder_date=last_reminder_date,
    )


def _confirmation_req(overdue=True):
    return ConfirmationRequest(
        group_key=("1142", "碩", "王教授", "9/10"),
        term="1142", advisor="王教授", degree="碩",
        date_text="9/10", student_names=("學生甲",), case_keys=("1142-A",),
        sent_date=_dt.date(2026, 1, 1),
        deadline=_past_deadline() if overdue else _future_deadline(),
    )


def _collection_item(overdue=True, reminder_count=0, last_reminder_date=None):
    return CollectionItem(
        group_key=("1142", "碩", "王教授", "9/10"),
        term="1142", advisor="王教授", degree="碩",
        date_text="9/10", student_names=("學生甲",), case_keys=("1142-A",),
        sent_date=_dt.date(2026, 1, 1),
        deadline=_past_deadline() if overdue else _future_deadline(),
        reminder_count=reminder_count,
        last_reminder_date=last_reminder_date,
    )


def _weekday(target_weekday: int, near: _dt.date | None = None) -> _dt.date:
    """回傳離 near（預設今天）最近、weekday() 等於 target_weekday 的日期。"""
    d = near or _dt.date.today()
    while d.weekday() != target_weekday:
        d += _dt.timedelta(days=1)
    return d


# ── scan_deficiency ───────────────────────────────────

class TestScanDeficiency:
    """決策：截止前若干工作日起每日催件一次，到期即停止——不是只催逾期。"""

    def test_schedules_items_within_lead_window(self):
        report = DeficiencyReport(
            items=(_def_item(deadline=_near_deadline(1)),
                   _def_item(case_key="1142-B", deadline=_future_deadline(10))),
            resolved_keys=frozenset(),
        )
        scheduler = ReminderScheduler()
        scheduled = scheduler.scan_deficiency(report)
        assert len(scheduled) == 1
        assert scheduled[0].kind == "deficiency"
        assert scheduled[0].tracking_key == ("1142-A",)

    def test_injected_clock_controls_deadline_status(self):
        """排程器不可一半使用注入日期、一半偷讀作業系統今天。"""
        today = _dt.date(2020, 1, 8)
        deadline = Deadline(
            base_date=_dt.date(2020, 1, 1), business_days=5,
            computed_date=add_business_days(today, 1),
        )
        report = DeficiencyReport(
            items=(_def_item(deadline=deadline),), resolved_keys=frozenset(),
        )

        scheduled = ReminderScheduler(today=lambda: today).scan_deficiency(report)

        assert len(scheduled) == 1

    def test_daily_reminders_send_again_next_day_but_not_same_day(self):
        """決策八-3：截止前每日催一次；同一天重跑不重寄，隔天要能再寄。"""
        from koushi import send_log
        from koushi.mail.notify import send_scheduled

        day1 = _dt.date(2020, 1, 6)
        deadline = Deadline(base_date=_dt.date(2020, 1, 1), business_days=5,
                            computed_date=add_business_days(day1, 2))
        report = DeficiencyReport(items=(_def_item(deadline=deadline),), resolved_keys=frozenset())
        log = send_log.SendEventLog()
        mails = []
        transport = lambda **kw: mails.append(kw)

        for day, expected in ((day1, 1), (day1, 1), (day1 + _dt.timedelta(days=1), 2)):
            scheduled = ReminderScheduler(
                today=lambda d=day: d, resolve_recipient=lambda *_: "s@example.edu",
            ).scan_deficiency(report)
            send_scheduled(scheduled, log=log, transport=transport)
            assert len(mails) == expected

    def test_does_not_schedule_already_overdue_items(self):
        """到期即停止：逾期缺件轉系辦處理，不再繼續寄信催學生。"""
        report = DeficiencyReport(items=(_def_item(deadline=_past_deadline()),), resolved_keys=frozenset())
        scheduled = ReminderScheduler().scan_deficiency(report)
        assert scheduled == []

    def test_does_not_resend_within_same_calendar_day(self):
        today = _dt.date.today()
        item = _def_item(deadline=_near_deadline(1), last_reminder_date=today)
        report = DeficiencyReport(items=(item,), resolved_keys=frozenset())
        scheduled = ReminderScheduler().scan_deficiency(report)
        assert scheduled == []

    def test_resends_on_a_new_calendar_day(self):
        yesterday = _dt.date.today() - _dt.timedelta(days=1)
        item = _def_item(deadline=_near_deadline(1), last_reminder_date=yesterday)
        report = DeficiencyReport(items=(item,), resolved_keys=frozenset())
        scheduled = ReminderScheduler().scan_deficiency(report)
        assert len(scheduled) == 1

    def test_groups_multiple_issues_same_case_into_one_reminder(self):
        near = _near_deadline(1)
        items = (
            DeficiencyItem(case_key="1142-A", term="1142", student_id="1", student_name="甲",
                           issue_code="A", field="f", description="d", owner="student",
                           blocks_att4=True, blocks_att6=True,
                           notified_date=_dt.date(2026, 1, 1), deadline=near),
            DeficiencyItem(case_key="1142-A", term="1142", student_id="1", student_name="甲",
                           issue_code="B", field="f", description="d", owner="student",
                           blocks_att4=True, blocks_att6=True,
                           notified_date=_dt.date(2026, 1, 1), deadline=near),
        )
        report = DeficiencyReport(items=items, resolved_keys=frozenset())
        scheduled = ReminderScheduler().scan_deficiency(report)
        assert len(scheduled) == 1
        assert scheduled[0].draft.tracking["issue_codes"] == ["A", "B"]

    def test_uses_placeholder_recipient_by_default(self):
        report = DeficiencyReport(items=(_def_item(deadline=_near_deadline(1)),), resolved_keys=frozenset())
        scheduled = ReminderScheduler().scan_deficiency(report)
        assert scheduled[0].draft.to.endswith(f"@{TEST_RECIPIENT_DOMAIN}")

    def test_custom_resolver_is_used(self):
        report = DeficiencyReport(items=(_def_item(deadline=_near_deadline(1)),), resolved_keys=frozenset())
        scheduler = ReminderScheduler(resolve_recipient=lambda kind, obj: "real@example.edu")
        scheduled = scheduler.scan_deficiency(report)
        assert scheduled[0].draft.to == "real@example.edu"

    def test_office_owned_issue_is_not_scheduled_as_email(self):
        report = DeficiencyReport(
            items=(_def_item(owner="office", deadline=_near_deadline(1)),), resolved_keys=frozenset(),
        )
        scheduled = ReminderScheduler().scan_deficiency(report)
        assert scheduled == []


# ── internal_todos ────────────────────────────────────

class TestInternalTodos:
    def test_returns_only_non_student_overdue_items(self):
        report = DeficiencyReport(
            items=(_def_item(case_key="A", owner="student"),
                   _def_item(case_key="B", owner="office")),
            resolved_keys=frozenset(),
        )
        todos = ReminderScheduler().internal_todos(report)
        assert len(todos) == 1
        assert todos[0].case_key == "B"

    def test_excludes_not_overdue(self):
        report = DeficiencyReport(
            items=(_def_item(owner="office", overdue=False),), resolved_keys=frozenset(),
        )
        assert ReminderScheduler().internal_todos(report) == []

    def test_no_draft_or_email_involved(self):
        """internal_todos 回傳原始 DeficiencyItem，不是 NotificationDraft。"""
        report = DeficiencyReport(items=(_def_item(owner="office"),), resolved_keys=frozenset())
        todos = ReminderScheduler().internal_todos(report)
        assert isinstance(todos[0], DeficiencyItem)


# ── scan_confirmation ─────────────────────────────────

class TestScanConfirmation:
    def test_never_schedules_advisor_reminders(self):
        """2026-10-12 起取消教授確認：過了期限也不會變逾期，不催教授。"""
        report = ConfirmationReport(requests=(_confirmation_req(overdue=True),
                                              _confirmation_req(overdue=False)))
        assert ReminderScheduler().scan_confirmation(report) == []

    def test_ignores_confirmed_even_if_deadline_passed(self):
        from koushi.engines.confirmation import CONFIRMED
        from dataclasses import replace
        req = replace(_confirmation_req(overdue=True), response=CONFIRMED)
        report = ConfirmationReport(requests=(req,))
        scheduled = ReminderScheduler().scan_confirmation(report)
        assert scheduled == []

    def test_injected_clock_prevents_early_overdue_reminder(self):
        today = _dt.date(2020, 1, 8)
        deadline = Deadline(
            base_date=_dt.date(2020, 1, 1), business_days=5,
            computed_date=add_business_days(today, 1),
        )
        request = _confirmation_req(overdue=False)
        request = replace(request, deadline=deadline)

        scheduled = ReminderScheduler(today=lambda: today).scan_confirmation(
            ConfirmationReport(requests=(request,)),
        )

        assert scheduled == []


# ── scan_collection：節流 ─────────────────────────────

class TestScanCollectionThrottle:
    def test_first_reminder_always_scheduled(self):
        report = CollectionReport(items=(_collection_item(reminder_count=0,
                                                            last_reminder_date=None),))
        scheduled = ReminderScheduler().scan_collection(report)
        assert len(scheduled) == 1
        assert scheduled[0].draft.tracking["reminder_ord"] == 1

    def test_within_interval_is_skipped(self):
        today = _weekday(3)  # 固定在週四，避開跨週末誤差
        last = today - _dt.timedelta(days=1)  # 週三，1 個工作日前
        item = _collection_item(reminder_count=1, last_reminder_date=last)
        report = CollectionReport(items=(item,))
        scheduler = ReminderScheduler(today=lambda: today, interval_days=2)
        assert scheduler.scan_collection(report) == []

    def test_past_interval_is_scheduled(self):
        today = _weekday(3)  # 週四
        last = today - _dt.timedelta(days=2)  # 週二，2 個工作日前
        item = _collection_item(reminder_count=1, last_reminder_date=last)
        report = CollectionReport(items=(item,))
        scheduler = ReminderScheduler(today=lambda: today, interval_days=2)
        scheduled = scheduler.scan_collection(report)
        assert len(scheduled) == 1
        assert scheduled[0].draft.tracking["reminder_ord"] == 2

    def test_not_overdue_never_scheduled_regardless_of_throttle(self):
        report = CollectionReport(items=(_collection_item(overdue=False),))
        scheduled = ReminderScheduler().scan_collection(report)
        assert scheduled == []

    def test_injected_clock_prevents_early_overdue_reminder(self):
        today = _dt.date(2020, 1, 8)
        deadline = Deadline(
            base_date=_dt.date(2020, 1, 1), business_days=5,
            computed_date=add_business_days(today, 1),
        )
        item = _collection_item(overdue=False)
        item = replace(item, deadline=deadline)

        scheduled = ReminderScheduler(today=lambda: today).scan_collection(
            CollectionReport(items=(item,)),
        )

        assert scheduled == []


# ── scan_all ──────────────────────────────────────────

class TestScanAll:
    def test_combines_all_three_engines(self):
        deficiency = DeficiencyReport(items=(_def_item(deadline=_near_deadline(1)),), resolved_keys=frozenset())
        confirmation = ConfirmationReport(requests=(_confirmation_req(),))
        collection = CollectionReport(items=(_collection_item(),))
        scheduled = ReminderScheduler().scan_all(
            deficiency=deficiency, confirmation=confirmation, collection=collection,
        )
        kinds = {s.kind for s in scheduled}
        assert kinds == {"deficiency", "collection"}          # 教授不用回信，不再有 confirmation 催辦

    def test_omitted_engine_is_skipped(self):
        deficiency = DeficiencyReport(items=(_def_item(deadline=_near_deadline(1)),), resolved_keys=frozenset())
        scheduled = ReminderScheduler().scan_all(deficiency=deficiency)
        assert scheduled and all(s.kind == "deficiency" for s in scheduled)


# ── _reminder_recipient_resolver：接上真實通訊錄 ─────────────

class TestReminderRecipientResolver:
    """`remind --send` 用的 (kind, obj) → email 轉接器；預覽模式不會用到它。"""

    def _contacts(self):
        return {"students": {"1142-A": "s@example.edu"}, "advisors": {"王教授": "wang@example.edu"}, "office": None}

    def test_deficiency_resolves_by_case_key_of_first_item(self):
        from koushi.application import _reminder_recipient_resolver
        resolve = _reminder_recipient_resolver(self._contacts())
        assert resolve("deficiency", [_def_item()]) == "s@example.edu"

    def test_deficiency_empty_items_resolves_none(self):
        from koushi.application import _reminder_recipient_resolver
        resolve = _reminder_recipient_resolver(self._contacts())
        assert resolve("deficiency", []) is None

    def test_confirmation_resolves_by_advisor_collection_by_committee(self):
        from koushi.application import _reminder_recipient_resolver
        resolve = _reminder_recipient_resolver(self._contacts())
        assert resolve("confirmation", _confirmation_req()) == "wang@example.edu"
        assert resolve("collection", _collection_item()) is None  # 附件6 提醒不寄指導教授（2026-10-13）
        item = _collection_item()
        contacts = {**self._contacts(), "case_committees": {k: ["c1@x.tw", "c2@x.tw"] for k in item.case_keys}}
        assert _reminder_recipient_resolver(contacts)("collection", item) == "c1@x.tw, c2@x.tw"

    def test_unknown_kind_resolves_none(self):
        from koushi.application import _reminder_recipient_resolver
        resolve = _reminder_recipient_resolver(self._contacts())
        assert resolve("unknown", object()) is None
