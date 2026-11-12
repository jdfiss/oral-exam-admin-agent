# -*- coding: utf-8 -*-
"""專員修正層的套用規則（2026-10-01 Q11）與個案開放修改（Q13）。"""
import datetime as dt

import pytest

from koushi import case_access, corrections
from koushi import settings as office_settings
from koushi.application import submission_block_reason
from koushi.model import Committee, Exam, Member


def _exam(place="S1-101", title="教授"):
    return Exam(學期="1141", 學號="A", 姓名="學生A", 口試地點=place,
                委員=[Member(committee_id="C1", 職稱=title)])


COMMITTEES = {"C1": Committee(committee_id="C1", 委員姓名="陳大文")}


def _set(tmp_path, **kw):
    values = dict(case_key="1141-A", field="口試地點", value="S1-204", original="S1-101",
                  actor="專員甲", reason="教授更正")
    values.update(kw)
    return corrections.set_correction(tmp_path, **values)


def test_correction_applies_over_source(tmp_path):
    _set(tmp_path)
    exams = {"1141-A": _exam()}
    assert corrections.apply(exams, COMMITTEES, corrections.load(tmp_path)) == []
    assert exams["1141-A"].口試地點 == "S1-204"


def test_source_catching_up_is_not_a_conflict(tmp_path):
    _set(tmp_path)
    exams = {"1141-A": _exam(place="S1-204")}
    assert corrections.apply(exams, COMMITTEES, corrections.load(tmp_path)) == []


def test_source_changed_to_third_value_is_flagged_but_correction_kept(tmp_path):
    _set(tmp_path)
    exams = {"1141-A": _exam(place="S1-999")}
    issues = corrections.apply(exams, COMMITTEES, corrections.load(tmp_path))
    assert [i["問題代碼"] for i in issues] == [corrections.ISSUE_SOURCE_CHANGED]
    assert issues[0]["責任"] == "office" and issues[0]["來源值"] == "S1-999"
    assert exams["1141-A"].口試地點 == "S1-204"
    # 專員重新確認：以新來源值為原值，之後不再報例外
    corrections.save_conflicts(tmp_path, issues)
    original = corrections.original_for(tmp_path, case_key="1141-A", field="口試地點", member="",
                                        shown_value="S1-204")
    assert original == "S1-999"
    _set(tmp_path, original=original)
    assert corrections.apply({"1141-A": _exam(place="S1-999")}, COMMITTEES, corrections.load(tmp_path)) == []


def test_member_title_correction_by_name(tmp_path):
    _set(tmp_path, field="職稱", member="陳大文", value="副教授", original="教授")
    exams = {"1141-A": _exam()}
    corrections.apply(exams, COMMITTEES, corrections.load(tmp_path))
    assert exams["1141-A"].委員[0].職稱 == "副教授"


def test_member_gone_is_flagged(tmp_path):
    _set(tmp_path, field="職稱", member="王小華", value="副教授", original="教授")
    issues = corrections.apply({"1141-A": _exam()}, COMMITTEES, corrections.load(tmp_path))
    assert [i["問題代碼"] for i in issues] == [corrections.ISSUE_TARGET_MISSING]


@pytest.mark.parametrize("kw, message", [
    ({"actor": " "}, "處理人"),
    ({"reason": ""}, "修正原因"),
    ({"field": "姓名"}, "不能在案件頁修正"),
    ({"field": "口試日期", "value": "12/3"}, "口試日期格式錯誤"),
    ({"field": "口試方式", "value": "線上"}, "口試方式"),
])
def test_bad_corrections_rejected(tmp_path, kw, message):
    with pytest.raises(ValueError, match=message):
        _set(tmp_path, **kw)


def test_unlock_overrides_closed_period_until_date(tmp_path):
    """2026-10-01 Q13：補件截止後，專員可對單一案件延長修改期限。"""
    long_ago = dt.date(2026, 9, 1)
    office_settings.save(office_settings.OfficeSettings(
        current_term="1141", fill_deadline=long_ago, att2_first_uploaded=long_ago), tmp_path)
    today = dt.date.today()
    assert "補件截止日已過" in submission_block_reason("1141-A", tmp_path, today=today)
    case_access.grant_unlock(tmp_path, case_key="1141-A", until=today + dt.timedelta(days=2),
                             actor="專員甲", reason="逾期延長")
    assert submission_block_reason("1141-A", tmp_path, today=today) is None
    assert submission_block_reason("1141-A", tmp_path, today=today + dt.timedelta(days=3))
    assert submission_block_reason("1141-B", tmp_path, today=today)  # 只開放這一案


def test_unlock_requires_handler_reason_and_future_date(tmp_path):
    today = dt.date.today()
    with pytest.raises(ValueError, match="處理人"):
        case_access.grant_unlock(tmp_path, case_key="1141-A", until=today, actor="", reason="x")
    with pytest.raises(ValueError, match="原因"):
        case_access.grant_unlock(tmp_path, case_key="1141-A", until=today, actor="甲", reason="")
    with pytest.raises(ValueError, match="早於今天"):
        case_access.grant_unlock(tmp_path, case_key="1141-A", until=today - dt.timedelta(days=1),
                                 actor="甲", reason="x")


def test_overdue_incomplete_case_is_listed_for_office(tmp_path):
    """2026-10-01 Q13：補件截止後仍有學生責任問題的案件，在人工關卡列「逾期未完成」例外。"""
    from koushi.application import review_queue, sync_deficiency_deadlines
    from koushi.model import Master
    long_ago = dt.date(2026, 9, 1)
    office_settings.save(office_settings.OfficeSettings(
        current_term="1141", fill_deadline=long_ago, att2_first_uploaded=long_ago), tmp_path)
    exam = Exam(學期="1141", 學號="A", 姓名="學生A", 系所="資訊工程學系碩士班", 指導教授="",
                口試日期=dt.date(2026, 11, 1), 有附件3=True, 介面委員已填=True,
                委員=[Member(committee_id="C-A", 服務單位="國立範例大學資訊工程學系")]).derive()
    master = Master(exams=[exam])
    sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=long_ago + dt.timedelta(days=1))
    rows = review_queue(master, tmp_path, today=dt.date.today())
    codes = [i["問題代碼"] for i in rows[0].exceptions]
    assert "STUDENT_OVERDUE_INCOMPLETE" in codes


def test_committee_exception_lead_days_from_settings():
    """2026-10-01 Q12：委員不催件；口試前幾天未交個資列例外，天數由系辦設定。"""
    from koushi.committee_requests import CommitteeRequest
    request = CommitteeRequest(email="x@example.org", name="委員", case_keys=("1141-A",),
                               earliest_exam=dt.date(2026, 12, 10), sent_date=None, received=False)
    assert not request.missing_before_exam(dt.date(2026, 12, 6))
    assert request.missing_before_exam(dt.date(2026, 12, 6), lead_days=5)
    assert request.missing_before_exam(dt.date(2026, 12, 7))  # 預設 3 天
