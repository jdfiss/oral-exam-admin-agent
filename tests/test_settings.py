# -*- coding: utf-8 -*-
"""系辦期限與催件設定：截止日、時段，以及依時段運作的送出限制與每日自動化。"""
import datetime as dt
import json

import pytest

from koushi import settings as office_settings
from koushi.application import run_daily_automation, submission_block_reason, sync_deficiency_deadlines
from koushi.engines.state import load_from_dir
from koushi.model import Exam, Master, Member
from koushi.workflow import RegistryPaths

from tests.att2_fixture import case, write_att2

FILL = dt.date(2026, 10, 1)  # 週四


def _settings(**kw):
    """預設：本期 1141、附件2 在填寫截止日當天上傳（正式檢核從 10/2 週五開始）。"""
    values = {"fill_deadline": FILL, "send_hour": 0, "current_term": "1141", "att2_first_uploaded": FILL}
    values.update(kw)
    return office_settings.OfficeSettings(**values)


class TestOfficeSettings:
    def test_defaults_when_no_file(self, tmp_path):
        loaded = office_settings.load(tmp_path)
        assert loaded.fill_deadline is None
        assert loaded.phase_on(dt.date(2030, 1, 1)) == office_settings.PHASE_UNSET

    def test_business_day_deficiency_deadline_and_phases(self):
        s = _settings(deficiency_days=5)
        assert s.check_start == dt.date(2026, 10, 2)
        assert s.deficiency_deadline == dt.date(2026, 10, 9)  # 正式檢核開始日＋5 個工作日，跳過週末
        assert s.phase_on(FILL) == office_settings.PHASE_FILLING  # 截止日當天仍可填
        assert s.phase_on(FILL + dt.timedelta(days=1)) == office_settings.PHASE_DEFICIENCY
        assert s.phase_on(dt.date(2026, 10, 9)) == office_settings.PHASE_DEFICIENCY
        assert s.phase_on(dt.date(2026, 10, 12)) == office_settings.PHASE_CLOSED

    def test_calendar_days(self):
        assert _settings(deficiency_days=5, business_days=False).deficiency_deadline == dt.date(2026, 10, 7)

    def test_late_att2_upload_pushes_check_start_and_deadline(self):
        """2026-10-01 Q3：系辦晚傳附件2，補件截止日從上傳隔天起算，學生的補件天數不被壓縮。"""
        s = _settings(deficiency_days=7, business_days=False, att2_first_uploaded=dt.date(2026, 10, 5))
        assert s.check_start == dt.date(2026, 10, 6)
        assert s.deficiency_deadline == dt.date(2026, 10, 13)
        assert s.phase_on(dt.date(2026, 10, 5)) == office_settings.PHASE_AWAITING_ATT2  # 上傳當天還不檢核
        assert s.phase_on(dt.date(2026, 10, 6)) == office_settings.PHASE_DEFICIENCY

    def test_early_att2_upload_does_not_shorten_deficiency_period(self):
        """2026-10-01 Q5：提早上傳附件2，正式檢核仍從填寫截止日隔天開始。"""
        early = _settings(deficiency_days=7, business_days=False, att2_first_uploaded=dt.date(2026, 9, 25))
        on_time = _settings(deficiency_days=7, business_days=False)
        assert early.check_start == on_time.check_start == dt.date(2026, 10, 2)
        assert early.deficiency_deadline == on_time.deficiency_deadline == dt.date(2026, 10, 9)

    def test_no_att2_after_fill_deadline_is_awaiting(self):
        s = _settings(att2_first_uploaded=None)
        assert s.check_start is None and s.deficiency_deadline is None
        assert s.phase_on(dt.date(2026, 12, 31)) == office_settings.PHASE_AWAITING_ATT2

    def test_manual_override_replaces_formula(self):
        """2026-10-01 Q6：補件截止日可手動覆寫；自動化失敗時不會自動順延。"""
        s = _settings(deficiency_days=5, deficiency_deadline_override=dt.date(2026, 10, 20))
        assert s.computed_deficiency_deadline == dt.date(2026, 10, 9)
        assert s.deficiency_deadline == dt.date(2026, 10, 20)
        assert s.phase_on(dt.date(2026, 10, 20)) == office_settings.PHASE_DEFICIENCY
        assert s.phase_on(dt.date(2026, 10, 21)) == office_settings.PHASE_CLOSED

    def test_reupload_keeps_first_upload_date(self, tmp_path):
        """2026-10-01 Q4：附件2 重傳不重算補件截止日。"""
        office_settings.save(_settings(att2_first_uploaded=None), tmp_path)
        office_settings.record_att2_upload(tmp_path, day=dt.date(2026, 10, 1))
        office_settings.record_att2_upload(tmp_path, day=dt.date(2026, 10, 6))
        assert office_settings.load(tmp_path).att2_first_uploaded == dt.date(2026, 10, 1)

    def test_switching_term_clears_upload_date_and_override(self):
        base = _settings(deficiency_deadline_override=dt.date(2026, 10, 20))
        switched = office_settings.from_form({"fill_deadline": "2027-03-01", "current_term": "1142"}, base=base)
        assert switched.att2_first_uploaded is None and switched.deficiency_deadline_override is None
        same = office_settings.from_form({"fill_deadline": "2026-10-01", "current_term": "1141",
                                          "deficiency_deadline_override": "2026-10-20"}, base=base)
        assert same.att2_first_uploaded == FILL
        assert office_settings.term_changing({"current_term": "1142"}, base)
        assert not office_settings.term_changing({"current_term": "1141"}, base)

    def test_save_load_round_trip_and_audit(self, tmp_path):
        office_settings.save(_settings(deficiency_days=7), tmp_path)
        assert office_settings.load(tmp_path) == _settings(deficiency_days=7)
        audit = json.loads((tmp_path / "audit_log.json").read_text(encoding="utf-8"))
        event = audit["events"][-1]
        assert event["source"] == "settings"
        assert "deficiency_days" in event["detail"]["changed"]

    def test_corrupt_file_raises_instead_of_silent_default(self, tmp_path):
        (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="無法讀取"):
            office_settings.load(tmp_path)

    @pytest.mark.parametrize("form, message", [
        ({"fill_deadline": "2026-13-01"}, "填寫截止日格式錯誤"),
        ({"deficiency_days": "abc"}, "必須是整數"),
        ({"deficiency_days": "-1"}, "0 以上"),
        ({"send_hour": "24"}, "0–23"),
    ])
    def test_from_form_rejects_bad_values(self, form, message):
        with pytest.raises(ValueError, match=message):
            office_settings.from_form(form)

    def test_from_form_parses_values(self):
        s = office_settings.from_form({
            "fill_deadline": "2026-10-01", "deficiency_days": "3", "business_days": "0", "send_hour": "8",
        })
        assert (s.fill_deadline, s.deficiency_days, s.business_days, s.send_hour) == \
            (FILL, 3, False, 8)


def _exam_missing_advisor():
    return Exam(
        學期="1141", 學號="A", 姓名="學生A", 系所="資訊工程學系碩士班", 指導教授="",
        口試日期=dt.date(2026, 11, 1), 有附件3=True, 介面委員已填=True,
        委員=[Member(committee_id="C-A", 服務單位="國立範例大學資訊工程學系")],
    ).derive()


class TestSubmissionWindow:
    def test_free_during_filling(self, tmp_path):
        office_settings.save(_settings(), tmp_path)
        assert submission_block_reason("1141-A", tmp_path, today=FILL) is None

    def test_deficiency_period_only_cases_with_student_issue(self, tmp_path):
        office_settings.save(_settings(), tmp_path)
        master = Master(exams=[_exam_missing_advisor()])
        sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=FILL + dt.timedelta(days=1))
        during = FILL + dt.timedelta(days=2)
        assert submission_block_reason("1141-A", tmp_path, today=during) is None
        assert "只有收到補件通知" in submission_block_reason("1141-B", tmp_path, today=during)

    def test_closed_after_deficiency_deadline(self, tmp_path):
        office_settings.save(_settings(deficiency_days=5), tmp_path)
        assert "補件截止日已過" in submission_block_reason("1141-A", tmp_path, today=dt.date(2026, 10, 12))

    def test_locked_while_awaiting_att2(self, tmp_path):
        office_settings.save(_settings(att2_first_uploaded=None), tmp_path)
        assert "尚未開始檢核" in submission_block_reason("1141-A", tmp_path, today=dt.date(2026, 10, 5))

    def test_not_open_without_term_or_fill_deadline(self, tmp_path):
        """2026-10-01 Q1：系辦設好本期學期與填寫截止日之前，學生端不開放。"""
        office_settings.save(_settings(current_term=""), tmp_path)
        assert "尚未開放" in submission_block_reason("1141-A", tmp_path, today=dt.date(2026, 9, 1))
        office_settings.save(office_settings.OfficeSettings(current_term="1141"), tmp_path)
        assert "尚未開放" in submission_block_reason("1141-A", tmp_path, today=dt.date(2026, 9, 1))

    def test_student_items_use_shared_deficiency_deadline(self, tmp_path):
        office_settings.save(_settings(deficiency_days=5), tmp_path)
        master = Master(exams=[_exam_missing_advisor()])
        sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=dt.date(2026, 10, 5))
        items = [i for i in load_from_dir(tmp_path).deficiency if i.owner == "student"]
        assert items and all(i.deadline.computed_date == dt.date(2026, 10, 9) for i in items)


def _automation_env(tmp_path, *, with_att2=True):
    submissions = tmp_path / "submissions"
    submissions.mkdir()
    (submissions / "1141-111900001.json").write_text(json.dumps({
        "term": "1141", "student_id": "111900001", "version": 1, "student_email": "s@example.org",
        "exam_date": "2026-11-01", "advisors": [],  # 缺指導教授 → 學生缺件
        "committees": [{"name": "陳大文", "unit": "國立範例大學資訊工程學系", "email": "d@example.org"}],
    }, ensure_ascii=False), encoding="utf-8")
    registries = RegistryPaths(
        members=tmp_path / "none.xlsx", distances=tmp_path / "none.csv",
        members_output=tmp_path / "out" / "members.xlsx",
        attachment2=write_att2(tmp_path / "att2.xlsx", [case()]) if with_att2 else tmp_path / "missing.xlsx",
    )
    return dict(
        submission_paths=list(submissions.glob("*.json")), master_path=tmp_path / "out" / "master.xlsx",
        state_dir=tmp_path / "state", registries=registries, submissions_dir=submissions,
    )


def _never_send(**kwargs):
    pytest.fail("此時段不應寄信")


class TestDailyAutomation:
    def test_filling_period_builds_preview_without_mail_or_tracking(self, tmp_path):
        env = _automation_env(tmp_path)
        result = run_daily_automation(**env, send=True, transport=_never_send,
                                      now=dt.datetime(2026, 9, 30, 10), settings=_settings())
        assert result.phase == office_settings.PHASE_FILLING
        assert result.process.drafts == [] and result.tracking is None
        assert env["master_path"].is_file()
        assert not (env["state_dir"] / "engines_state.json").exists()

    def test_deficiency_period_requires_attachment2(self, tmp_path):
        env = _automation_env(tmp_path, with_att2=False)
        with pytest.raises(ValueError, match="尚未上傳附件2"):
            run_daily_automation(**env, now=dt.datetime(2026, 10, 2, 10), settings=_settings())

    def test_awaiting_att2_only_previews(self, tmp_path):
        """填寫截止後、本期附件2 還沒上傳：只建預覽、不寄信，也不當成自動化失敗。"""
        env = _automation_env(tmp_path)
        result = run_daily_automation(**env, send=True, transport=_never_send,
                                      now=dt.datetime(2026, 10, 5, 10),
                                      settings=_settings(att2_first_uploaded=None))
        assert result.phase == office_settings.PHASE_AWAITING_ATT2
        assert result.process.drafts == [] and result.tracking is None
        assert any("尚未上傳附件2" in n for n in result.notes)

    def test_deficiency_period_sends_first_notice_with_shared_deadline(self, tmp_path):
        env = _automation_env(tmp_path)
        sent = []
        result = run_daily_automation(
            **env, send=True, transport=lambda **kw: sent.append(kw) or True,
            now=dt.datetime(2026, 10, 2, 10), settings=_settings(deficiency_days=5),
        )
        assert result.phase == office_settings.PHASE_DEFICIENCY
        assert len(sent) == 1
        student = [i for i in result.tracking.deficiency.items if i.owner == "student"]
        assert student and all(i.deadline.computed_date == dt.date(2026, 10, 9) for i in student)

    def test_before_send_hour_only_previews(self, tmp_path):
        env = _automation_env(tmp_path)
        result = run_daily_automation(
            **env, send=True, transport=_never_send,
            now=dt.datetime(2026, 10, 2, 7), settings=_settings(send_hour=9),
        )
        assert result.process.sent == []
        assert any("每日發送時刻" in n for n in result.notes)

    def test_closed_period_sends_no_student_mail(self, tmp_path):
        env = _automation_env(tmp_path)
        result = run_daily_automation(**env, send=True, transport=_never_send,
                                      now=dt.datetime(2026, 10, 20, 10), settings=_settings())
        assert result.phase == office_settings.PHASE_CLOSED
        assert result.process.drafts == []
        assert not [s for s in result.reminders.scheduled if s.kind == "deficiency"]


class TestTermHolidaysEmail:
    """review-2026-09-23 P1-6／P1-9／P1-13。"""

    def test_holiday_pushes_business_day_deadline(self):
        assert _settings(deficiency_days=5).deficiency_deadline == dt.date(2026, 10, 9)
        # 補件期間內遇到假日（10/5），截止日順延一個工作日
        with_holiday = _settings(deficiency_days=5, holidays=(dt.date(2026, 10, 5),))
        assert with_holiday.deficiency_deadline == dt.date(2026, 10, 12)

    def test_form_parses_term_holidays_and_email_template(self):
        s = office_settings.from_form({
            "fill_deadline": "2026-10-01", "current_term": "1141",
            "holidays": "2026-10-10\n2026-10-09", "student_email_template": "{學號}@cc.example.edu",
        })
        assert s.current_term == "1141"
        assert s.holidays == (dt.date(2026, 10, 9), dt.date(2026, 10, 10))
        assert s.fallback_student_email("111900001") == "111900001@cc.example.edu"

    @pytest.mark.parametrize("form, message", [
        ({"holidays": "10/10"}, "國定假日格式錯誤"),
        ({"current_term": "114-1"}, "本期學期僅限英數字"),
        ({"student_email_template": "cc.example.edu"}, "需包含 {學號}"),
    ])
    def test_form_rejects_bad_values(self, form, message):
        with pytest.raises(ValueError, match=message):
            office_settings.from_form(form)

    def test_round_trip_keeps_new_fields(self, tmp_path):
        s = _settings(current_term="1141", holidays=(dt.date(2026, 10, 10),),
                      student_email_template="{學號}@cc.example.edu")
        office_settings.save(s, tmp_path)
        assert office_settings.load(tmp_path) == s

    def test_never_filled_student_gets_notice_at_fallback_address(self, tmp_path):
        env = _automation_env(tmp_path)
        for path in env["submission_paths"]:
            path.unlink()  # 學生從未填寫：沒有留任何 Email
        env["submission_paths"] = []
        sent = []
        result = run_daily_automation(
            **env, send=True, transport=lambda **kw: sent.append(kw) or True,
            now=dt.datetime(2026, 10, 2, 10),
            settings=_settings(student_email_template="{學號}@cc.example.edu"),
        )
        assert [kw["to"] for kw in sent] == ["111900001@cc.example.edu"]
        assert result.process.sent


def test_case_stays_editable_after_fixing_during_deficiency_period(tmp_path):
    """review-2026-09-23 P1-10：本輪收到過補件通知的案件，修好一項後仍可在補件截止日前修改。"""
    office_settings.save(_settings(), tmp_path)
    master = Master(exams=[_exam_missing_advisor()])
    sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=FILL + dt.timedelta(days=1))
    from koushi.model import Committee
    fixed = _exam_missing_advisor()
    fixed.指導教授 = "林指導"
    committees = {"C-A": Committee(committee_id="C-A", 委員姓名="委員", Email="c@example.org")}
    sync_deficiency_deadlines(master=Master(exams=[fixed], committees=committees), state_dir=tmp_path,
                              date=FILL + dt.timedelta(days=2))
    assert not [i for i in load_from_dir(tmp_path).deficiency if i.owner == "student"]
    assert submission_block_reason("1141-A", tmp_path, today=FILL + dt.timedelta(days=3)) is None


def test_cli_automate_records_failure_status(tmp_path, monkeypatch):
    """review-2026-09-23 P1-12：自動化失敗要留下狀態，讓系辦總覽顯示。"""
    from koushi import cli, config as C
    from koushi.application import load_automation_status
    state = tmp_path / "state"
    office_settings.save(office_settings.OfficeSettings(
        fill_deadline=dt.date.today() - dt.timedelta(days=2), send_hour=0, current_term="1141",
        att2_first_uploaded=dt.date.today() - dt.timedelta(days=2)), state)
    monkeypatch.setattr(C, "PORTAL_ATT2", tmp_path / "missing.xlsx")
    monkeypatch.setattr(C, "REG_MEMBER", tmp_path / "none.xlsx")
    monkeypatch.setattr(C, "DISTANCE_CSV", tmp_path / "none.csv")
    (tmp_path / "subs").mkdir()
    with pytest.raises(ValueError, match="尚未上傳附件2"):
        cli.main(["automate", "--submissions-dir", str(tmp_path / "subs"),
                  "--master", str(tmp_path / "m.xlsx"), "--state-dir", str(state)])
    status = load_automation_status(state)
    assert status["ok"] is False and "附件2" in status["error"]
