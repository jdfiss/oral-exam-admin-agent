"""操作入口對完整狀態對帳，篩選查詢不刪除其他學期資料。"""

import datetime as dt
import json
from dataclasses import replace

from koushi.application import (
    plan_reminders, submission_block_reason, sync_deficiency_deadlines, tracking_snapshot,
)
from koushi.deadline import Deadline
from koushi.engines.confirmation import CONFIRMED, create_requests, record_response
from koushi.engines.state import EngineState, load_from_dir, save_to_dir
from koushi.model import Exam, Master, Member


def _exam(term: str, student_id: str) -> Exam:
    return Exam(
        學期=term, 學號=student_id, 姓名=f"學生{student_id}",
        系所="資訊工程學系碩士班", 指導教授="測試教授", 審核狀態="簽核完成",
        口試日期=dt.date(2026, 10, 1), 有附件3=True, 介面委員已填=True,
        # 校內服務單位避開 MISSING_BANK_INFO／NO_DISTANCE；正式身分（C-）避開
        # UNVERIFIED_COMMITTEE_IDENTITY。master 不帶委員資料，所以委員 E-mail
        # 空白，作為唯一的學生責任缺件（MISSING_COMMITTEE_EMAIL）。
        委員=[Member(committee_id=f"C-{student_id}", 服務單位="國立範例大學資訊工程學系")],
    ).derive()


def test_term_status_persists_all_terms(tmp_path):
    master = Master(exams=[_exam("1141", "A"), _exam("1142", "B")])
    previous = create_requests(master)
    prior_other_term = record_response(
        next(item for item in previous if item.term == "1142"),
        CONFIRMED, date=dt.date(2026, 9, 1),
    )
    save_to_dir(
        EngineState(deficiency=[], confirmation=[prior_other_term], collection=[]),
        tmp_path,
    )

    result = tracking_snapshot(master=master, state_dir=tmp_path, term="1141", persist=True)

    assert {item.term for item in result.confirmation.requests} == {"1141"}
    saved = load_from_dir(tmp_path)
    assert {item.term for item in saved.confirmation} == {"1141", "1142"}
    assert next(item for item in saved.confirmation if item.term == "1142").response == CONFIRMED


def test_sync_deficiency_deadlines_starts_clock_and_persists(tmp_path):
    # _exam() 的委員沒有 E-mail，會觸發學生責任的 MISSING_COMMITTEE_EMAIL，
    # 足以驗證起算與持久化。
    master = Master(exams=[_exam("1141", "A")])
    today = dt.date.today()

    report = sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=today)

    assert len(report.items) == 1
    item = report.items[0]
    assert item.owner == "student"
    assert item.notified_date == today
    assert item.deadline is not None
    assert not item.deadline.is_overdue

    saved = load_from_dir(tmp_path)
    assert saved.deficiency[0].notified_date == today


def test_sync_deficiency_deadlines_does_not_reset_existing_notified_date(tmp_path):
    master = Master(exams=[_exam("1141", "A")])
    today = dt.date.today()
    sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=today)

    report = sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=today + dt.timedelta(days=9))
    assert report.items[0].notified_date == today  # 沿用第一次起算的日期


def test_unset_settings_keep_student_side_closed(tmp_path):
    """2026-10-01 Q1：系辦設好本期學期與填寫截止日之前，學生端一律不開放。"""
    master = Master(exams=[_exam("1141", "A")])
    case_key = master.exams[0].案件鍵
    sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=dt.date.today())
    assert submission_block_reason(case_key, tmp_path) == "本學期尚未開放填寫"


def test_office_owned_overdue_does_not_block_student_during_filling(tmp_path):
    """系辦責任（如距離表查無服務單位）逾期不擋學生填寫，兩邊責任各自獨立。"""
    from koushi import settings as office_settings
    office_settings.save(office_settings.OfficeSettings(
        current_term="1141", fill_deadline=dt.date.today() + dt.timedelta(days=10)), tmp_path)
    exam = Exam(
        學期="1141", 學號="A", 姓名="學生A", 系所="資訊工程學系碩士班",
        指導教授="測試教授", 審核狀態="簽核完成",
        口試日期=dt.date(2026, 10, 1),
        # 校外單位且交通費未知 → office 責任的 NO_DISTANCE
        委員=[Member(committee_id="C0001", 服務單位="遠方大學")],
    ).derive()
    master = Master(exams=[exam])
    case_key = exam.案件鍵

    sync_deficiency_deadlines(master=master, state_dir=tmp_path, date=dt.date.today())
    state = load_from_dir(tmp_path)
    assert any(item.owner == "office" for item in state.deficiency)
    assert not any(item.owner == "student" for item in state.deficiency)
    past = Deadline(base_date=dt.date(2020, 1, 1), business_days=5, computed_date=dt.date(2020, 1, 10))
    state.deficiency = [replace(item, deadline=past) for item in state.deficiency]
    save_to_dir(state, tmp_path)

    assert submission_block_reason(case_key, tmp_path) is None


def test_plan_reminders_suppresses_resubmitted_case(tmp_path):
    """已送出新版本、等待下一輪重檢的案件（issue_store 為 resubmitted）不再收到「尚未提交」催件。"""
    from koushi import issue_store
    from koushi.application import mark_case_resubmitted

    state_dir = tmp_path / "state"
    master = Master(exams=[_exam("1141", "A")])
    notified_on = dt.date(2026, 9, 17)  # 週四；五個工作日後為 9/24
    planning_on = dt.date(2026, 9, 22)  # 週二；距截止日正好兩個工作日
    sync_deficiency_deadlines(master=master, state_dir=state_dir, date=notified_on)
    tracking_snapshot(master=master, state_dir=state_dir, persist=True)

    without_resubmission = plan_reminders(master=master, state_dir=state_dir, date=planning_on)
    assert any(s.kind == "deficiency" for s in without_resubmission.scheduled)

    path = state_dir / "issue_records.json"
    store = issue_store.load(path)
    for record in store.all():
        record = issue_store.transition(record, issue_store.DRAFT_READY, date=notified_on)
        store.put(issue_store.transition(record, issue_store.REQUEST_SENT, date=notified_on))
    issue_store.save(store, path)
    mark_case_resubmitted("1141-A", state_dir, date=planning_on)

    with_resubmission = plan_reminders(master=master, state_dir=state_dir, date=planning_on)
    assert not any(s.kind == "deficiency" for s in with_resubmission.scheduled)


def test_plan_reminders_resolves_real_recipients_when_sending(tmp_path, monkeypatch):
    """resolve_recipients=True（remind --send 用）改用通訊錄真實地址；
    預設仍是 placeholder，預覽模式不會意外印出真實個資。"""
    from koushi import config as C

    monkeypatch.setattr(C, "OUT_DIR", tmp_path / "outputs_today")

    state_dir = tmp_path / "state"
    submissions_dir = tmp_path / "submissions"
    submissions_dir.mkdir()

    master = Master(exams=[_exam("1141", "A")])
    notified_on = dt.date(2026, 9, 17)
    planning_on = dt.date(2026, 9, 22)
    sync_deficiency_deadlines(master=master, state_dir=state_dir, date=notified_on)

    (submissions_dir / "1141-A.json").write_text(json.dumps({
        "term": "1141", "student_id": "A", "version": 1,
        "student_email": "a@example.edu",
    }), encoding="utf-8")

    preview = plan_reminders(
        master=master, state_dir=state_dir, submissions_dir=submissions_dir, date=planning_on,
    )
    preview_item = next(s for s in preview.scheduled if s.kind == "deficiency")
    assert preview_item.draft.to != "a@example.edu"

    resolved = plan_reminders(master=master, state_dir=state_dir, submissions_dir=submissions_dir,
                               resolve_recipients=True, date=planning_on)
    resolved_item = next(s for s in resolved.scheduled if s.kind == "deficiency")
    assert resolved_item.draft.to == "a@example.edu"
