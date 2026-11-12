# -*- coding: utf-8 -*-
"""測試用的系辦時段設定（2026-10-01：本期學期＋填寫截止日都設好，學生端才開放）。"""
import datetime as _dt

from koushi import settings as office_settings


def open_filling_period(state_dir, term="1141"):
    """填寫期間：填寫截止日在 30 天後。"""
    office_settings.save(
        office_settings.OfficeSettings(current_term=term,
                                       fill_deadline=_dt.date.today() + _dt.timedelta(days=30)),
        state_dir,
    )


def start_deficiency_period(state_dir, term="1141"):
    """填寫截止日與附件2 第一次上傳都在昨天：今天是正式檢核開始日，系統才會寄補件信。"""
    yesterday = _dt.date.today() - _dt.timedelta(days=1)
    office_settings.save(
        office_settings.OfficeSettings(fill_deadline=yesterday, att2_first_uploaded=yesterday,
                                       current_term=term, send_hour=0),
        state_dir,
    )
