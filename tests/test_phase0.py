# -*- coding: utf-8 -*-
"""Phase 0 tests: deadline + responsibility."""

import datetime as _dt

from koushi.deadline import (
    Deadline,
    add_business_days,
    business_days_between,
    confirmation_deadline,
    deficiency_deadline,
)
from koushi.responsibility import (
    DEPARTMENT,
    POST_EXAM,
    STUDENT,
    classify,
    partition,
)


# ── deadline ──────────────────────────────────────────

class TestAddBusinessDays:
    def test_skips_weekend(self):
        # 2026-09-04 is Friday
        friday = _dt.date(2026, 9, 4)
        result = add_business_days(friday, 1)
        assert result == _dt.date(2026, 9, 7)  # Monday

    def test_multiple_days_across_weekend(self):
        # 2026-09-03 is Thursday
        thursday = _dt.date(2026, 9, 3)
        result = add_business_days(thursday, 3)
        assert result == _dt.date(2026, 9, 8)  # Tuesday (Thu+1=Fri, skip Sat/Sun, +2=Mon, +3=Tue)

    def test_skips_holidays(self):
        monday = _dt.date(2026, 9, 7)
        holidays = {_dt.date(2026, 9, 8)}  # Tuesday is holiday
        result = add_business_days(monday, 2, holidays)
        assert result == _dt.date(2026, 9, 10)  # Thursday (Mon+1=skip Tue, +1=Wed, +2=Thu)

    def test_zero_days(self):
        day = _dt.date(2026, 9, 3)
        assert add_business_days(day, 0) == day

    def test_full_week(self):
        monday = _dt.date(2026, 9, 7)
        result = add_business_days(monday, 5)
        assert result == _dt.date(2026, 9, 14)  # next Monday


class TestBusinessDaysBetween:
    def test_same_week(self):
        mon = _dt.date(2026, 9, 7)
        fri = _dt.date(2026, 9, 11)
        assert business_days_between(mon, fri) == 4

    def test_across_weekend(self):
        fri = _dt.date(2026, 9, 4)
        mon = _dt.date(2026, 9, 7)
        assert business_days_between(fri, mon) == 1

    def test_same_day(self):
        day = _dt.date(2026, 9, 3)
        assert business_days_between(day, day) == 0

    def test_end_before_start(self):
        assert business_days_between(_dt.date(2026, 9, 5), _dt.date(2026, 9, 3)) == 0


class TestDeadlineFactories:
    def test_deficiency_deadline_days(self):
        base = _dt.date(2026, 9, 3)
        dl = deficiency_deadline(base)
        assert dl.base_date == base
        assert dl.business_days == 5
        assert dl.computed_date == add_business_days(base, 5)

    def test_confirmation_deadline_days(self):
        base = _dt.date(2026, 9, 3)
        dl = confirmation_deadline(base)
        assert dl.base_date == base
        assert dl.business_days == 3
        assert dl.computed_date == add_business_days(base, 3)

    def test_is_overdue(self):
        past = Deadline(
            base_date=_dt.date(2020, 1, 1),
            business_days=1,
            computed_date=_dt.date(2020, 1, 2),
        )
        assert past.is_overdue

    def test_not_overdue(self):
        future = Deadline(
            base_date=_dt.date(2099, 1, 1),
            business_days=1,
            computed_date=_dt.date(2099, 1, 2),
        )
        assert not future.is_overdue


# ── responsibility ────────────────────────────────────

class TestClassify:
    def test_missing_attachment3_is_student(self):
        assert classify({"問題代碼": "MISSING_SUBMISSION"}) == STUDENT

    def test_missing_bank_info_is_post_exam(self):
        assert classify({"問題代碼": "MISSING_BANK_INFO"}) == POST_EXAM

    def test_no_exam_date_is_student(self):
        assert classify({"問題代碼": "NO_EXAM_DATE"}) == STUDENT

    def test_unverified_identity_is_post_exam(self):
        # 委員個資不經學生：系統向委員本人索取，口試現場可用附件6 補齊。
        assert classify({"問題代碼": "UNVERIFIED_COMMITTEE_IDENTITY"}) == POST_EXAM

    def test_advisor_unavailable_is_student(self):
        # 2026-09-23：指導教授由學生在介面填寫。
        assert classify({"問題代碼": "ADVISOR_SOURCE_UNAVAILABLE"}) == STUDENT

    def test_committee_exceptions_are_department(self):
        for code in ("COMMITTEE_MISMATCH", "SUBMISSION_WITHOUT_PORTAL_CASE"):
            assert classify({"問題代碼": code}) == DEPARTMENT

    def test_missing_committee_is_student(self):
        # 委員名單與 E-mail 必填（資工系為準）
        for code in ("NO_COMMITTEE", "MISSING_COMMITTEE_LIST", "MISSING_COMMITTEE_EMAIL"):
            assert classify({"問題代碼": code}) == STUDENT

    def test_no_distance_is_department(self):
        assert classify({"問題代碼": "NO_DISTANCE"}) == DEPARTMENT

    def test_unknown_blocking_defaults_to_department(self):
        assert classify({"問題代碼": "SOME_NEW_CODE", "阻擋產出": "是"}) == DEPARTMENT

    def test_unknown_warning_defaults_to_department(self):
        assert classify({"問題代碼": "SOME_WARNING"}) == DEPARTMENT


class TestPartition:
    def test_splits_correctly(self):
        issues = [
            {"問題代碼": "MISSING_SUBMISSION", "案件鍵": "1142-A"},
            {"問題代碼": "NO_DISTANCE", "案件鍵": "1142-B"},
            {"問題代碼": "MISSING_BANK_INFO", "案件鍵": "1142-C"},
        ]
        student, dept, post = partition(issues)
        assert len(student) == 1
        assert len(dept) == 1
        assert len(post) == 1
        assert student[0]["問題代碼"] == "MISSING_SUBMISSION"
        assert dept[0]["問題代碼"] == "NO_DISTANCE"
        assert post[0]["問題代碼"] == "MISSING_BANK_INFO"

    def test_empty(self):
        student, dept, post = partition([])
        assert student == []
        assert dept == []
        assert post == []
