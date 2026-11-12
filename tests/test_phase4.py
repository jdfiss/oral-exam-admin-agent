# -*- coding: utf-8 -*-
"""Phase 4 tests: collection (附件6催繳) engine."""

import datetime as _dt

from koushi.engines.collection import (
    COLLECTED,
    NEEDS_CORRECTION,
    OUTSTANDING,
    OVERDUE,
    CollectionItem,
    apply_collected,
    create_items,
    item_from_dict,
    item_to_dict,
    mark_collected,
    mark_reminded,
    mark_sent,
    reconcile,
    report,
)
from koushi.deadline import Deadline
from koushi.model import Committee, Exam, Master, Member


def _exam(key_suffix="A", advisor="王教授", date_text="9/10"):
    members = [Member(committee_id="C0001", 服務單位="台灣大學", 職稱="教授",
                      委員資格="校外委員", 口試費=1000, 交通費=100)]
    e = Exam(
        學期="1142", 學號=f"111502{key_suffix}", 姓名=f"學生{key_suffix}",
        學院="工學院", 系所="資訊工程學系碩士班", 案件種類="學位考試申請",
        審核狀態="簽核完成", 有附件2=True, 有附件3=True,
        指導教授=advisor, 委員=members,
        口試日期原文=date_text,
    )
    e.derive()
    return e


def _master(exams):
    c = Committee(committee_id="C0001", 委員姓名="張委員", 服務單位="台灣大學",
                  身分證字號="A123456789", 金融機構="台銀", 帳號="12345", 戶籍地址="台北市")
    return Master(exams=exams, committees={"C0001": c})


# ── create_items ─────────────────────────────────────

class TestCreateItems:
    def test_one_group(self):
        master = _master([_exam()])
        items = create_items(master)
        assert len(items) == 1
        assert items[0].advisor == "王教授"
        assert items[0].status == "draft"

    def test_groups_match_confirmation(self):
        e1 = _exam(key_suffix="A", advisor="王教授", date_text="9/10")
        e2 = _exam(key_suffix="B", advisor="王教授", date_text="9/10")
        e3 = _exam(key_suffix="C", advisor="李教授", date_text="9/10")
        master = _master([e1, e2, e3])
        items = create_items(master)
        assert len(items) == 2
        wang = next(i for i in items if i.advisor == "王教授")
        assert len(wang.student_names) == 2

    def test_term_filter(self):
        master = _master([_exam()])
        items = create_items(master, term="1141")
        assert len(items) == 0


# ── mark_sent ────────────────────────────────────────

class TestMarkSent:
    def test_sets_date_and_deadline(self):
        items = create_items(_master([_exam()]))
        today = _dt.date.today()
        sent = mark_sent(items, date=today, deadline_days=5)
        assert sent[0].sent_date == today
        assert sent[0].deadline.business_days == 5
        assert sent[0].status == OUTSTANDING

    def test_skips_already_sent(self):
        items = create_items(_master([_exam()]))
        sent1 = mark_sent(items, date=_dt.date(2026, 9, 1))
        sent2 = mark_sent(sent1, date=_dt.date(2026, 9, 4))
        assert sent2[0].sent_date == _dt.date(2026, 9, 1)


# ── mark_collected ───────────────────────────────────

class TestMarkCollected:
    def test_marks_collected(self):
        items = mark_sent(create_items(_master([_exam()])), date=_dt.date(2026, 9, 1))
        updated = mark_collected(items[0], date=_dt.date(2026, 9, 5))
        assert updated.status == COLLECTED
        assert updated.collected_date == _dt.date(2026, 9, 5)
        assert updated.qualified is True

    def test_unqualified_marks_needs_correction_not_collected(self):
        """已收到但資料或簽章不齊全：不算完成，跟真正收齊分開狀態。"""
        items = mark_sent(create_items(_master([_exam()])), date=_dt.date(2026, 9, 1))
        updated = mark_collected(items[0], date=_dt.date(2026, 9, 5),
                                  qualified=False, note="缺委員簽名")
        assert updated.status == NEEDS_CORRECTION
        assert updated.status != COLLECTED
        assert updated.correction_note == "缺委員簽名"

    def test_correction_note_cleared_when_qualified(self):
        items = mark_sent(create_items(_master([_exam()])), date=_dt.date(2026, 9, 1))
        updated = mark_collected(items[0], date=_dt.date(2026, 9, 5),
                                  qualified=True, note="這段不該被存")
        assert updated.correction_note == ""


# ── mark_reminded ────────────────────────────────────

class TestMarkReminded:
    def test_increments_count(self):
        items = mark_sent(create_items(_master([_exam()])), date=_dt.date(2026, 9, 1))
        r1 = mark_reminded(items[0], date=_dt.date(2026, 9, 8))
        assert r1.reminder_count == 1
        assert r1.last_reminder_date == _dt.date(2026, 9, 8)
        r2 = mark_reminded(r1, date=_dt.date(2026, 9, 10))
        assert r2.reminder_count == 2
        assert r2.last_reminder_date == _dt.date(2026, 9, 10)


# ── apply_collected ──────────────────────────────────

class TestApplyCollected:
    def test_batch_collect(self):
        e1 = _exam(key_suffix="A", advisor="王教授")
        e2 = _exam(key_suffix="B", advisor="李教授")
        master = _master([e1, e2])
        today = _dt.date.today()
        items = mark_sent(create_items(master), date=today)
        wang_key = next(i for i in items if i.advisor == "王教授").request_key
        updated = apply_collected(items, {wang_key}, date=today)
        statuses = {i.advisor: i.status for i in updated}
        assert statuses["王教授"] == COLLECTED
        assert statuses["李教授"] == OUTSTANDING


# ── status logic ─────────────────────────────────────

class TestStatus:
    def test_draft(self):
        item = create_items(_master([_exam()]))[0]
        assert item.status == "draft"

    def test_outstanding(self):
        dl = Deadline(base_date=_dt.date(2099, 1, 1), business_days=5,
                      computed_date=_dt.date(2099, 1, 8))
        item = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            sent_date=_dt.date(2099, 1, 1), deadline=dl,
        )
        assert item.status == OUTSTANDING

    def test_overdue(self):
        dl = Deadline(base_date=_dt.date(2020, 1, 1), business_days=5,
                      computed_date=_dt.date(2020, 1, 8))
        item = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            sent_date=_dt.date(2020, 1, 1), deadline=dl,
        )
        assert item.status == OVERDUE

    def test_collected_overrides_overdue(self):
        dl = Deadline(base_date=_dt.date(2020, 1, 1), business_days=5,
                      computed_date=_dt.date(2020, 1, 8))
        item = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            sent_date=_dt.date(2020, 1, 1), deadline=dl,
            collected_date=_dt.date(2020, 1, 10),
        )
        assert item.status == COLLECTED


# ── report ───────────────────────────────────────────

class TestReport:
    def test_all_collected(self):
        item = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            collected_date=_dt.date(2026, 9, 5),
        )
        r = report([item])
        assert r.all_collected is True
        assert len(r.needs_action) == 0

    def test_needs_action(self):
        dl = Deadline(base_date=_dt.date(2020, 1, 1), business_days=5,
                      computed_date=_dt.date(2020, 1, 8))
        overdue = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            sent_date=_dt.date(2020, 1, 1), deadline=dl,
        )
        collected = CollectionItem(
            group_key=("1142", "碩", "李教授", "9/15", ""),
            term="1142", advisor="李教授", degree="碩", date_text="9/15",
            student_names=("B",), case_keys=("1142-B",),
            collected_date=_dt.date(2026, 9, 5),
        )
        r = report([overdue, collected])
        assert r.all_collected is False
        assert len(r.needs_action) == 1
        assert len(r.overdue) == 1
        assert len(r.collected) == 1

    def test_empty_not_all_collected(self):
        r = report([])
        assert r.all_collected is False

    def test_needs_correction_is_not_collected_and_needs_action(self):
        items = mark_sent(create_items(_master([_exam()])), date=_dt.date(2026, 9, 1))
        needs_fix = mark_collected(items[0], date=_dt.date(2026, 9, 5), qualified=False)
        r = report([needs_fix])
        assert r.all_collected is False
        assert len(r.collected) == 0
        assert len(r.needs_correction) == 1
        assert needs_fix in r.needs_action


# ── reconcile ────────────────────────────────────────

class TestReconcile:
    def test_preserves_sent_state(self):
        master = _master([_exam()])
        items = mark_sent(create_items(master), date=_dt.date(2026, 9, 1))
        reconciled = reconcile(master, items)
        assert len(reconciled) == 1
        assert reconciled[0].sent_date == _dt.date(2026, 9, 1)

    def test_adds_new_group(self):
        e1 = _exam(key_suffix="A", advisor="王教授")
        master1 = _master([e1])
        items = mark_sent(create_items(master1), date=_dt.date(2026, 9, 1))

        e2 = _exam(key_suffix="B", advisor="李教授")
        master2 = _master([e1, e2])
        reconciled = reconcile(master2, items)
        assert len(reconciled) == 2
        wang = next(i for i in reconciled if i.advisor == "王教授")
        li = next(i for i in reconciled if i.advisor == "李教授")
        assert wang.sent_date == _dt.date(2026, 9, 1)
        assert li.status == "draft"

    def test_preserves_collected_state(self):
        master = _master([_exam()])
        items = mark_sent(create_items(master), date=_dt.date(2026, 9, 1))
        items = [mark_collected(items[0], date=_dt.date(2026, 9, 5))]
        reconciled = reconcile(master, items)
        assert reconciled[0].status == COLLECTED

    def test_case_keys_changed_invalidates_collected_state(self):
        """收回附件6之後同組又換委員或加學生，舊的收件狀態不能沿用。"""
        e1 = _exam(key_suffix="A", advisor="王教授")
        master1 = _master([e1])
        items = mark_sent(create_items(master1), date=_dt.date(2026, 9, 1))
        items = [mark_collected(items[0], date=_dt.date(2026, 9, 5))]
        assert items[0].status == COLLECTED

        e2 = _exam(key_suffix="B", advisor="王教授")
        master2 = _master([e1, e2])
        reconciled = reconcile(master2, items)

        assert len(reconciled) == 1
        assert len(reconciled[0].student_names) == 2
        assert reconciled[0].status == "draft"
        assert reconciled[0].collected_date is None
        assert reconciled[0].sent_date is None


# ── serialization ────────────────────────────────────

class TestSerialization:
    def test_round_trip_draft(self):
        item = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("學生A",), case_keys=("1142-A",),
        )
        restored = item_from_dict(item_to_dict(item))
        assert restored.group_key == item.group_key
        assert restored.student_names == item.student_names
        assert restored.sent_date is None
        assert restored.collected_date is None

    def test_round_trip_full(self):
        dl = Deadline(base_date=_dt.date(2026, 9, 1), business_days=5,
                      computed_date=_dt.date(2026, 9, 8))
        item = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("學生A",), case_keys=("1142-A",),
            sent_date=_dt.date(2026, 9, 1), deadline=dl,
            collected_date=_dt.date(2026, 9, 5),
            reminder_count=2, last_reminder_date=_dt.date(2026, 9, 4),
        )
        restored = item_from_dict(item_to_dict(item))
        assert restored.sent_date == _dt.date(2026, 9, 1)
        assert restored.deadline.computed_date == _dt.date(2026, 9, 8)
        assert restored.collected_date == _dt.date(2026, 9, 5)
        assert restored.status == COLLECTED  # 未指定 qualified 時視同合格，向下相容舊資料
        assert restored.reminder_count == 2
        assert restored.last_reminder_date == _dt.date(2026, 9, 4)

    def test_round_trip_preserves_unqualified_correction(self):
        item = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("學生A",), case_keys=("1142-A",),
            collected_date=_dt.date(2026, 9, 5),
            qualified=False, correction_note="缺委員簽名",
        )
        restored = item_from_dict(item_to_dict(item))
        assert restored.status == NEEDS_CORRECTION
        assert restored.qualified is False
        assert restored.correction_note == "缺委員簽名"
