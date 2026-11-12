# -*- coding: utf-8 -*-
"""Phase 2 tests: deficiency tracking engine."""

import datetime as _dt

from koushi.engines.deficiency import (
    DeficiencyItem,
    DeficiencyReport,
    item_from_dict,
    item_to_dict,
    mark_notified,
    scan,
)
from koushi.deadline import Deadline
from koushi.model import Committee, Exam, Master, Member
from koushi.responsibility import DEPARTMENT, POST_EXAM, STUDENT


def _exam(key_suffix="A", advisor="王教授", has_att3=True, committee_ids=None):
    members = []
    for cid in (committee_ids or ["C0001"]):
        members.append(Member(committee_id=cid, 服務單位="台灣大學", 職稱="教授",
                              委員資格="校外委員", 口試費=1000, 交通費=100))
    e = Exam(
        學期="1142", 學號=f"111502{key_suffix}", 姓名=f"學生{key_suffix}",
        學院="工學院", 系所="資訊工程學系碩士班", 案件種類="學位考試申請",
        審核狀態="簽核完成", 有附件2=True, 有附件3=has_att3, 介面委員已填=has_att3,
        指導教授=advisor, 委員=members,
    )
    e.derive()
    return e


def _committee(cid="C0001", bank_complete=True):
    return Committee(
        committee_id=cid, 委員姓名="張委員", 服務單位="台灣大學",
        身分證字號="A123456789" if bank_complete else "",
        金融機構="台銀" if bank_complete else "",
        帳號="12345" if bank_complete else "",
        戶籍地址="台北市" if bank_complete else "",
        Email="zhang@example.org",
    )


def _master(exams, committees):
    return Master(exams=exams, committees=committees)


# ── scan ─────────────────────────────────────────────

class TestScan:
    def test_clean_master_yields_empty_report(self):
        exam = _exam()
        master = _master([exam], {"C0001": _committee()})
        report = scan(master)
        assert len(report.items) == 0
        assert len(report.resolved_keys) == 0

    def test_blocking_issues_become_items(self):
        exam = _exam(advisor="")
        master = _master([exam], {"C0001": _committee()})
        report = scan(master)
        assert len(report.items) > 0
        codes = {i.issue_code for i in report.items}
        assert "ADVISOR_SOURCE_UNAVAILABLE" in codes

    def test_non_blocking_issues_excluded(self):
        exam = _exam(has_att3=False)
        master = _master([exam], {"C0001": _committee()})
        report = scan(master)
        codes = {i.issue_code for i in report.items}
        assert "MISSING_SUBMISSION" not in codes

    def test_item_fields_populated(self):
        exam = _exam(advisor="")
        master = _master([exam], {"C0001": _committee()})
        report = scan(master)
        item = next(i for i in report.items if i.issue_code == "ADVISOR_SOURCE_UNAVAILABLE")
        assert item.case_key == exam.案件鍵
        assert item.term == "1142"
        assert item.student_id == exam.學號
        assert item.owner == STUDENT  # 2026-09-23：指導教授由學生在介面填寫
        assert item.blocks_att4 is True
        assert item.blocks_att6 is True
        assert item.status == "new"

    def test_bank_issue_owner_is_post_exam(self):
        exam = _exam()
        master = _master([exam], {"C0001": _committee(bank_complete=False)})
        report = scan(master)
        bank = next(i for i in report.items if i.issue_code == "MISSING_BANK_INFO")
        assert bank.owner == POST_EXAM
        assert bank.blocks_att4 is False
        assert bank.blocks_att6 is True


# ── scan with previous state ─────────────────────────

class TestScanWithPrevious:
    def test_carries_forward_notified_date(self):
        exam = _exam(advisor="")
        master = _master([exam], {"C0001": _committee()})
        report1 = scan(master)
        notified = mark_notified(list(report1.items), date=_dt.date(2026, 9, 1))
        report2 = scan(master, previous=notified)
        item = next(i for i in report2.items if i.issue_code == "ADVISOR_SOURCE_UNAVAILABLE")
        assert item.notified_date == _dt.date(2026, 9, 1)
        assert item.deadline is not None

    def test_resolved_when_issue_disappears(self):
        exam_bad = _exam(advisor="")
        master_bad = _master([exam_bad], {"C0001": _committee()})
        report1 = scan(master_bad)
        notified = mark_notified(list(report1.items), date=_dt.date(2026, 9, 1))

        exam_fixed = _exam(advisor="王教授")
        master_fixed = _master([exam_fixed], {"C0001": _committee()})
        report2 = scan(master_fixed, previous=notified)
        assert len(report2.resolved_keys) > 0
        resolved_codes = {k[1] for k in report2.resolved_keys}
        assert "ADVISOR_SOURCE_UNAVAILABLE" in resolved_codes

    def test_new_issue_stays_new(self):
        exam = _exam(advisor="")
        master = _master([exam], {"C0001": _committee()})
        prev_item = DeficiencyItem(
            case_key="other-key", term="1142", student_id="999",
            student_name="X", issue_code="OTHER", field="f",
            description="d", owner=DEPARTMENT,
            blocks_att4=True, blocks_att6=True,
            notified_date=_dt.date(2026, 9, 1),
        )
        report = scan(master, previous=[prev_item])
        item = next(i for i in report.items if i.issue_code == "ADVISOR_SOURCE_UNAVAILABLE")
        assert item.status == "new"
        assert item.notified_date is None


# ── mark_notified ────────────────────────────────────

class TestMarkNotified:
    def test_sets_date_and_deadline(self):
        item = DeficiencyItem(
            case_key="1142-A", term="1142", student_id="111502A",
            student_name="學生A", issue_code="NO_COMMITTEE", field="委員名單",
            description="test", owner=STUDENT,
            blocks_att4=True, blocks_att6=True,
        )
        today = _dt.date.today()
        result = mark_notified([item], date=today)
        assert len(result) == 1
        assert result[0].notified_date == today
        assert result[0].deadline is not None
        assert result[0].deadline.business_days == 5
        assert result[0].status == "notified"

    def test_skips_already_notified(self):
        dl = Deadline(
            base_date=_dt.date(2026, 9, 1),
            business_days=5,
            computed_date=_dt.date(2026, 9, 8),
        )
        item = DeficiencyItem(
            case_key="1142-A", term="1142", student_id="111502A",
            student_name="學生A", issue_code="NO_COMMITTEE", field="委員名單",
            description="test", owner=STUDENT,
            blocks_att4=True, blocks_att6=True,
            notified_date=_dt.date(2026, 9, 1),
            deadline=dl,
        )
        result = mark_notified([item], date=_dt.date(2026, 9, 3))
        assert result[0].notified_date == _dt.date(2026, 9, 1)


# ── status ───────────────────────────────────────────

class TestStatus:
    def test_new_status(self):
        item = DeficiencyItem(
            case_key="k", term="t", student_id="s", student_name="n",
            issue_code="c", field="f", description="d", owner="student",
            blocks_att4=True, blocks_att6=True,
        )
        assert item.status == "new"

    def test_notified_status(self):
        dl = Deadline(
            base_date=_dt.date(2099, 1, 1),
            business_days=5,
            computed_date=_dt.date(2099, 1, 8),
        )
        item = DeficiencyItem(
            case_key="k", term="t", student_id="s", student_name="n",
            issue_code="c", field="f", description="d", owner="student",
            blocks_att4=True, blocks_att6=True,
            notified_date=_dt.date(2099, 1, 1), deadline=dl,
        )
        assert item.status == "notified"

    def test_overdue_status(self):
        dl = Deadline(
            base_date=_dt.date(2020, 1, 1),
            business_days=5,
            computed_date=_dt.date(2020, 1, 8),
        )
        item = DeficiencyItem(
            case_key="k", term="t", student_id="s", student_name="n",
            issue_code="c", field="f", description="d", owner="student",
            blocks_att4=True, blocks_att6=True,
            notified_date=_dt.date(2020, 1, 1), deadline=dl,
        )
        assert item.status == "overdue"


# ── report convenience properties ────────────────────

class TestReportProperties:
    def test_by_owner(self):
        items = [
            DeficiencyItem(case_key="a", term="t", student_id="s", student_name="n",
                           issue_code="c1", field="f", description="d", owner="student",
                           blocks_att4=True, blocks_att6=True),
            DeficiencyItem(case_key="b", term="t", student_id="s", student_name="n",
                           issue_code="c2", field="f", description="d", owner="office",
                           blocks_att4=True, blocks_att6=True),
        ]
        report = DeficiencyReport(items=tuple(items), resolved_keys=frozenset())
        assert len(report.by_owner["student"]) == 1
        assert len(report.by_owner["office"]) == 1

    def test_by_case(self):
        items = [
            DeficiencyItem(case_key="1142-A", term="t", student_id="s", student_name="n",
                           issue_code="c1", field="f1", description="d", owner="student",
                           blocks_att4=True, blocks_att6=True),
            DeficiencyItem(case_key="1142-A", term="t", student_id="s", student_name="n",
                           issue_code="c2", field="f2", description="d", owner="office",
                           blocks_att4=True, blocks_att6=True),
        ]
        report = DeficiencyReport(items=tuple(items), resolved_keys=frozenset())
        assert len(report.by_case["1142-A"]) == 2


# ── serialization ────────────────────────────────────

class TestSerialization:
    def test_round_trip_without_notification(self):
        item = DeficiencyItem(
            case_key="1142-A", term="1142", student_id="111502A",
            student_name="學生A", issue_code="NO_COMMITTEE", field="委員名單",
            description="附件2 查無委員資料", owner="student",
            blocks_att4=True, blocks_att6=True,
        )
        restored = item_from_dict(item_to_dict(item))
        assert restored.case_key == item.case_key
        assert restored.issue_code == item.issue_code
        assert restored.owner == item.owner
        assert restored.notified_date is None
        assert restored.deadline is None

    def test_round_trip_with_notification(self):
        dl = Deadline(
            base_date=_dt.date(2026, 9, 1),
            business_days=5,
            computed_date=_dt.date(2026, 9, 8),
        )
        item = DeficiencyItem(
            case_key="1142-A", term="1142", student_id="111502A",
            student_name="學生A", issue_code="NO_COMMITTEE", field="委員名單",
            description="test", owner="student",
            blocks_att4=True, blocks_att6=False,
            notified_date=_dt.date(2026, 9, 1), deadline=dl,
        )
        restored = item_from_dict(item_to_dict(item))
        assert restored.notified_date == _dt.date(2026, 9, 1)
        assert restored.deadline.computed_date == _dt.date(2026, 9, 8)
        assert restored.deadline.business_days == 5
        assert restored.blocks_att4 is True
        assert restored.blocks_att6 is False
