# -*- coding: utf-8 -*-
"""截止日管理器 —— 統一管理補件截止日與教授確認截止日，支援工作日計算。"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

from . import config as C


def add_business_days(
    base: _dt.date,
    days: int,
    holidays: set[_dt.date] | None = None,
) -> _dt.date:
    """從 base 往後加 days 個工作日（排除週末與指定假日）。"""
    holidays = holidays or set()
    added = 0
    current = base
    while added < days:
        current += _dt.timedelta(days=1)
        if current.weekday() < 5 and current not in holidays:
            added += 1
    return current


def business_days_between(
    start: _dt.date,
    end: _dt.date,
    holidays: set[_dt.date] | None = None,
) -> int:
    """計算 start 到 end 之間的工作日數（不含 start，含 end）。"""
    holidays = holidays or set()
    if end <= start:
        return 0
    count = 0
    current = start
    while current < end:
        current += _dt.timedelta(days=1)
        if current.weekday() < 5 and current not in holidays:
            count += 1
    return count


@dataclass(frozen=True)
class Deadline:
    """一個截止日：從基準日算起 N 個工作日。"""
    base_date: _dt.date
    business_days: int
    computed_date: _dt.date

    def is_overdue_on(self, day: _dt.date) -> bool:
        """回傳指定日期是否已超過截止日，供排程與測試共用同一時鐘。"""
        return day > self.computed_date

    def remaining_business_days_on(
        self,
        day: _dt.date,
        holidays: set[_dt.date] | None = None,
    ) -> int:
        return business_days_between(day, self.computed_date, holidays)

    @property
    def is_overdue(self) -> bool:
        return self.is_overdue_on(_dt.date.today())

    @property
    def remaining_business_days(self) -> int:
        return self.remaining_business_days_on(_dt.date.today())


def deficiency_deadline(
    base: _dt.date | None = None,
    holidays: set[_dt.date] | None = None,
) -> Deadline:
    """補件截止日。"""
    base = base or _dt.date.today()
    computed = add_business_days(base, C.DEFICIENCY_DEADLINE_DAYS, holidays)
    return Deadline(base_date=base, business_days=C.DEFICIENCY_DEADLINE_DAYS, computed_date=computed)


def confirmation_deadline(
    base: _dt.date | None = None,
    holidays: set[_dt.date] | None = None,
) -> Deadline:
    """教授確認截止日。"""
    base = base or _dt.date.today()
    computed = add_business_days(base, C.CONFIRMATION_DEADLINE_DAYS, holidays)
    return Deadline(base_date=base, business_days=C.CONFIRMATION_DEADLINE_DAYS, computed_date=computed)
