# -*- coding: utf-8 -*-
"""deadline.py 的工作日計算與截止日判定。"""

import datetime as dt

from koushi.deadline import (
    add_business_days, business_days_between, Deadline,
    deficiency_deadline, confirmation_deadline,
)


class TestAddBusinessDays:
    def test_simple_weekdays(self):
        # 2026-09-21 is Monday
        mon = dt.date(2026, 9, 21)
        assert add_business_days(mon, 1) == dt.date(2026, 9, 22)
        # 5 business days from Monday = next Monday (Tue-Fri=4, skip weekend, Mon=5)
        assert add_business_days(mon, 5) == dt.date(2026, 9, 28)

    def test_skips_weekend(self):
        fri = dt.date(2026, 9, 25)
        assert add_business_days(fri, 1) == dt.date(2026, 9, 28)  # Monday

    def test_skips_holidays(self):
        mon = dt.date(2026, 9, 21)
        holidays = {dt.date(2026, 9, 22)}  # Tuesday is holiday
        assert add_business_days(mon, 1, holidays) == dt.date(2026, 9, 23)

    def test_zero_days_returns_base(self):
        mon = dt.date(2026, 9, 21)
        assert add_business_days(mon, 0) == mon

    def test_weekend_and_holiday_combined(self):
        # Friday + 2 business days, Monday is holiday
        fri = dt.date(2026, 9, 25)
        holidays = {dt.date(2026, 9, 28)}
        assert add_business_days(fri, 2) == dt.date(2026, 9, 29)  # no holiday
        assert add_business_days(fri, 2, holidays) == dt.date(2026, 9, 30)


class TestBusinessDaysBetween:
    def test_same_day_is_zero(self):
        d = dt.date(2026, 9, 21)
        assert business_days_between(d, d) == 0

    def test_end_before_start_is_zero(self):
        assert business_days_between(dt.date(2026, 9, 22), dt.date(2026, 9, 21)) == 0

    def test_one_weekday(self):
        assert business_days_between(dt.date(2026, 9, 21), dt.date(2026, 9, 22)) == 1

    def test_across_weekend(self):
        # Friday to Monday = 1 business day (only Monday counts)
        assert business_days_between(dt.date(2026, 9, 25), dt.date(2026, 9, 28)) == 1

    def test_full_week(self):
        assert business_days_between(dt.date(2026, 9, 21), dt.date(2026, 9, 28)) == 5

    def test_with_holidays(self):
        # Mon→Fri = 4 biz days (Tue Wed Thu Fri), minus Wed holiday = 3
        holidays = {dt.date(2026, 9, 23)}
        assert business_days_between(dt.date(2026, 9, 21), dt.date(2026, 9, 25), holidays) == 3


class TestDeadline:
    def test_is_overdue_on(self):
        dl = Deadline(base_date=dt.date(2026, 9, 1), business_days=5,
                      computed_date=dt.date(2026, 9, 8))
        assert not dl.is_overdue_on(dt.date(2026, 9, 8))
        assert dl.is_overdue_on(dt.date(2026, 9, 9))

    def test_remaining_business_days_on(self):
        # Mon 9/21, computed_date = Fri 9/25 → 4 biz days between
        dl = Deadline(base_date=dt.date(2026, 9, 21), business_days=4,
                      computed_date=dt.date(2026, 9, 25))
        assert dl.remaining_business_days_on(dt.date(2026, 9, 21)) == 4
        assert dl.remaining_business_days_on(dt.date(2026, 9, 25)) == 0

    def test_deficiency_deadline_factory(self):
        dl = deficiency_deadline(base=dt.date(2026, 9, 21))
        assert dl.base_date == dt.date(2026, 9, 21)
        assert dl.business_days > 0
        assert dl.computed_date > dl.base_date

    def test_confirmation_deadline_factory(self):
        dl = confirmation_deadline(base=dt.date(2026, 9, 21))
        assert dl.base_date == dt.date(2026, 9, 21)
        assert dl.business_days > 0
        assert dl.computed_date > dl.base_date
