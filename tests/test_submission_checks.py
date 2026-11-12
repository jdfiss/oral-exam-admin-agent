# -*- coding: utf-8 -*-
"""送出時檢查、學生自我檢查與口試日期例外（review-2026-09-23 P1-14／P1-15）。"""
import datetime as dt

import pytest

from koushi import settings as office_settings
from koushi.application import (
    earliest_feasible_exam_date, review_queue, submission_self_check, submit_application,
)
from koushi.model import Committee, Exam, Master, Member


def _fields(**overrides):
    fields = {
        "term": "1141", "student_email": "s@example.org", "exam_date": "2026-11-01",
        "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        **{f"committee_{i}_{k}": v for i in range(3) for k, v in
           (("name", f"委員{i}"), ("unit", "某大學"), ("email", f"c{i}@example.org"))},
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize("overrides, message", [
    ({"student_email": "not-an-email"}, "學生 Email"),
    ({"advisor_0": ""}, "指導教授"),
    ({"advisor_0_email": ""}, "指導教授「林指導」的 E-mail"),
    ({"committee_2_name": ""}, "至少 3 位"),
    ({"committee_1_email": "bad"}, "委員「委員1」的 E-mail"),
    ({"committee_0_unit": ""}, "缺服務單位"),
    ({"exam_date": ""}, "口試日期"),
])
def test_invalid_submission_is_rejected_without_saving(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        submit_application(student_id="111900001", fields=_fields(**overrides), submissions_dir=tmp_path)
    assert not list(tmp_path.glob("*.json"))


def test_rejected_resubmission_does_not_archive_previous_version(tmp_path):
    submit_application(student_id="111900001", fields=_fields(), submissions_dir=tmp_path)
    with pytest.raises(ValueError):
        submit_application(student_id="111900001", fields=_fields(advisor_0=""), submissions_dir=tmp_path)
    assert not (tmp_path / "versions").exists()


def test_self_check_hints():
    settings = office_settings.OfficeSettings(fill_deadline=dt.date(2026, 10, 1), deficiency_days=5,
                                              confirmation_days=3)
    # 附件2 未上傳時以準時上傳推估：補件截止 10/9（10/2 起 5 個工作日）＋3 個工作日
    assert earliest_feasible_exam_date(settings) == dt.date(2026, 10, 14)
    hints = submission_self_check({"exam_date": "2026-10-09", "committees": [{}] * 3},
                                  today=dt.date(2026, 9, 20), settings=settings)
    assert any("來不及" in h for h in hints)
    assert any("博士班口試委員至少 5 位" in h for h in hints)
    assert any("已經過去" in h for h in submission_self_check(
        {"exam_date": "2026-09-01", "committees": [{}] * 5}, today=dt.date(2026, 9, 20)))


def test_exam_date_too_early_is_office_exception(tmp_path):
    office_settings.save(office_settings.OfficeSettings(fill_deadline=dt.date(2026, 10, 1)), tmp_path)
    exam = Exam(學期="1141", 學號="A", 姓名="學生A", 系所="資訊工程學系碩士班", 指導教授="林指導",
                口試日期=dt.date(2026, 10, 5),
                委員=[Member(committee_id=f"C{i}", 服務單位="國立範例大學資訊工程學系") for i in range(3)]).derive()
    master = Master(exams=[exam], committees={f"C{i}": Committee(committee_id=f"C{i}", 委員姓名=f"委員{i}")
                                               for i in range(3)})
    codes = [i["問題代碼"] for i in review_queue(master, tmp_path)[0].exceptions]
    assert "EXAM_DATE_TOO_EARLY" in codes


def test_doctor_needs_five_committees():
    from koushi import validation
    exam = Exam(學期="1141", 學號="A", 姓名="學生A", 系所="資訊工程學系博士班", 指導教授="林指導",
                委員=[Member(committee_id=f"C{i}", 服務單位="國立範例大學資訊工程學系") for i in range(3)]).derive()
    master = Master(exams=[exam], committees={f"C{i}": Committee(committee_id=f"C{i}", 委員姓名=f"委員{i}")
                                               for i in range(3)})
    issue = next(i for i in validation.check(master) if i["問題代碼"] == "COMMITTEE_COUNT_BELOW_MIN")
    assert "博士口試委員至少 5 位" in issue["說明"] and issue["責任"] == validation.OWNER_OFFICE


def test_advisor_emails_are_stored_by_name(tmp_path):
    data = submit_application(student_id="111900001", submissions_dir=tmp_path, fields=_fields(
        advisor_1="王共同", advisor_1_email="wang@example.org"))
    assert data["advisors"] == ["林指導", "王共同"]
    assert data["advisor_emails"] == {"林指導": "lin@example.org", "王共同": "wang@example.org"}
