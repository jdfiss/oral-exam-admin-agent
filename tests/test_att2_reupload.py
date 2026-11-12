# -*- coding: utf-8 -*-
"""附件2 上傳紀錄與重傳（2026-10-01 Q4–Q6）：第一次上傳日決定補件截止日、重傳不重算，
重傳後新出現的學生標「補件時間較短」，消失但已寄過補件信的學生交專員確認，
首次補件信晚寄只警示、不自動順延。全程使用假資料。"""
import datetime as dt

import pytest

from koushi import send_log, settings as office_settings
from koushi.application import (
    acknowledge_att2_removed, att2_changes, first_deficiency_mail_delay, record_att2_upload,
)
from koushi.mail import notify

from tests.att2_fixture import case, write_att2

FILL = dt.date(2026, 10, 1)


@pytest.fixture
def state(tmp_path):
    office_settings.save(office_settings.OfficeSettings(current_term="1141", fill_deadline=FILL), tmp_path)
    return tmp_path


def _att2(tmp_path, *student_ids):
    return write_att2(tmp_path / f"att2-{'-'.join(student_ids)}.xlsx",
                      [case(student_id=sid, name=f"學生{sid}") for sid in student_ids])


def _mark_notified(state_dir, case_key, sent_at="2026-10-02 09:00:00"):
    event = send_log.SendEvent(
        idempotency_key=f"k-{case_key}", type=notify.TYPE_STUDENT_DEFICIENCY, to="s@example.org",
        subject="補件", status=send_log.SENT, tracking={"case_key": case_key},
        created_at=sent_at, sent_at=sent_at,
    )
    log = send_log.load(state_dir / "send_log.json")
    log._events[event.idempotency_key] = event
    send_log.save(log, state_dir / "send_log.json")


def test_reupload_keeps_first_date_and_flags_late_added_cases(state, tmp_path):
    record_att2_upload(state, _att2(tmp_path, "A1", "A2"), day=FILL)
    settings = record_att2_upload(state, _att2(tmp_path, "A1", "A2", "A3"), day=dt.date(2026, 10, 5))
    assert settings.att2_first_uploaded == FILL
    assert settings.check_start == dt.date(2026, 10, 2)
    changes = att2_changes(state)
    assert changes.late_added == {"1141-A3"}  # 正式檢核開始後才出現：補件時間較短
    assert changes.removed_notified == ()


def test_reupload_before_check_start_is_not_late(state, tmp_path):
    record_att2_upload(state, _att2(tmp_path, "A1"), day=dt.date(2026, 9, 25))
    record_att2_upload(state, _att2(tmp_path, "A1", "A2"), day=FILL)
    assert att2_changes(state).late_added == frozenset()


def test_removed_case_with_notice_needs_office_confirmation(state, tmp_path):
    record_att2_upload(state, _att2(tmp_path, "A1", "A2", "A3"), day=FILL)
    _mark_notified(state, "1141-A2")
    record_att2_upload(state, _att2(tmp_path, "A1"), day=dt.date(2026, 10, 5))
    # A3 沒寄過補件信，消失就消失；A2 寄過，要專員確認
    assert att2_changes(state).removed_notified == ("1141-A2",)

    with pytest.raises(ValueError, match="處理人"):
        acknowledge_att2_removed(state, "1141-A2", actor=" ")
    acknowledge_att2_removed(state, "1141-A2", actor="王專員", note="學生撤銷申請")
    assert att2_changes(state).removed_notified == ()


def test_switching_term_starts_fresh_upload_history(state, tmp_path):
    record_att2_upload(state, _att2(tmp_path, "A1"), day=FILL)
    current = office_settings.load(state)
    office_settings.save(office_settings.from_form(
        {"current_term": "1142", "fill_deadline": "2027-03-01"}, base=current), state)
    assert att2_changes(state).late_added == frozenset()
    settings = record_att2_upload(state, _att2(tmp_path, "A1"), day=dt.date(2027, 2, 20))
    assert settings.att2_first_uploaded == dt.date(2027, 2, 20)


def test_late_first_notice_is_reported_but_deadline_not_extended(state, tmp_path):
    record_att2_upload(state, _att2(tmp_path, "A1"), day=FILL)
    before = office_settings.load(state).deficiency_deadline
    assert first_deficiency_mail_delay(state) is None  # 還沒寄過
    _mark_notified(state, "1141-A1", sent_at="2026-10-05 09:00:00")
    assert first_deficiency_mail_delay(state) == 3  # 10/2 該寄，10/5 才寄
    assert office_settings.load(state).deficiency_deadline == before


def test_on_time_first_notice_has_no_warning(state, tmp_path):
    record_att2_upload(state, _att2(tmp_path, "A1"), day=FILL)
    _mark_notified(state, "1141-A1", sent_at="2026-10-02 09:00:00")
    assert first_deficiency_mail_delay(state) is None
