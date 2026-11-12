# -*- coding: utf-8 -*-
"""分階段產檔的兩道關卡：專員人工關卡放行（決策第四節）與教授確認附件4（第六節）。

附件4寄出後有一個等待教授對附件4回信的確認關卡；不能只因檔案已
產出就自動進入口試後步驟。這裡只測 application.generate_artifacts()
的確認關卡邏輯本身，不重新驗證 generate_post_exam 的產檔細節（見
test_phase1.py／test_generators_schema4.py）。
"""
import datetime as dt

import pytest

from koushi import config as C
from koushi import case_review
from koushi import application
from koushi.application import generate_artifacts, review_queue
from koushi.engines.confirmation import CONFIRMED, MODIFIED, create_requests, record_response
from koushi.engines.state import EngineState, save_to_dir
from koushi.model import Committee, Exam, Master, Member
from koushi.workflow import GenerationResult, TemplatePaths

TEMPLATES = TemplatePaths(
    attachment4=C.ROOT / "unused.doc",  # post-exam 不檢查附件4範本，不需要真的存在
    attachment6=C.TPL_ATT6,
)


def _clean_exam(term, student_id, advisor, date) -> Exam:
    """一場不含任何阻擋問題的口試（校內委員、已簽核、指導教授與日期齊全）。"""
    return Exam(
        學期=term, 學號=student_id, 姓名=f"學生{student_id}",
        系所="資訊工程學系碩士班", 指導教授=advisor, 審核狀態="簽核完成",
        口試日期=date,
        委員=[Member(committee_id=f"C-{advisor}", 服務單位="國立範例大學資訊工程學系")],
    ).derive()


def _save_master(tmp_path, exams):
    from koushi import master as master_workbook
    committees = {
        member.committee_id: Committee(committee_id=member.committee_id, 委員姓名=member.committee_id)
        for exam in exams for member in exam.委員
    }
    master = Master(exams=exams, committees=committees)
    path = tmp_path / "master.xlsx"
    master_workbook.save(master, [], path=path)
    from koushi import source_state
    source_state.publish(path, {})
    return path, master


def _clear_all(state_dir, master):
    for exam in master.exams:
        case_review.clear(state_dir, exam=exam, master=master, note="測試放行")


def test_post_exam_requires_state_dir(tmp_path):
    master_path, _ = _save_master(tmp_path, [_clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))])
    with pytest.raises(ValueError, match="state_dir"):
        generate_artifacts(
            master_path=master_path, output_dir=tmp_path / "out",
            templates=TEMPLATES, phase="post-exam",
        )


def test_post_exam_includes_groups_regardless_of_advisor_reply(tmp_path):
    """2026-10-12 起取消教授確認：教授回不回信、要求修改與否，都不擋口試後附件6。"""
    confirmed_exam = _clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))
    pending_exam = _clean_exam("1141", "B", "教授乙", dt.date(2026, 10, 5))
    master_path, master = _save_master(tmp_path, [confirmed_exam, pending_exam])

    requests = create_requests(master)
    confirmed_req = next(r for r in requests if r.advisor == "教授甲")
    pending_req = next(r for r in requests if r.advisor == "教授乙")
    updated = [
        record_response(confirmed_req, CONFIRMED, date=dt.date(2026, 9, 20)),
        record_response(pending_req, MODIFIED, date=dt.date(2026, 9, 20)),
    ]
    state_dir = tmp_path / "state"
    save_to_dir(EngineState(deficiency=[], confirmation=updated, collection=[]), state_dir)
    _clear_all(state_dir, master)

    result = generate_artifacts(
        master_path=master_path, output_dir=tmp_path / "out",
        templates=TEMPLATES, phase="post-exam", state_dir=state_dir,
    )

    names = " ".join(f.name for f in result.attachment6_files)
    assert "教授甲" in names and "教授乙" in names
    assert not any(i["問題代碼"] == "CONFIRMATION_PENDING" for i in result.issues)


def test_post_exam_generates_even_when_advisor_never_replied(tmp_path):
    exam = _clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))
    master_path, master = _save_master(tmp_path, [exam])
    # 從沒建過確認請求：預設就是 pending，不是 CONFIRMED。
    state_dir = tmp_path / "state"
    _clear_all(state_dir, master)

    result = generate_artifacts(
        master_path=master_path, output_dir=tmp_path / "out",
        templates=TEMPLATES, phase="post-exam", state_dir=state_dir,
    )
    assert len(result.attachment6_files) == 1
    assert not any(i["問題代碼"] == "CONFIRMATION_PENDING" for i in result.issues)


PRE_TEMPLATES = TemplatePaths(attachment4=C.TPL_ATT4, attachment6=C.TPL_ATT6)


def test_pre_exam_requires_state_dir(tmp_path):
    master_path, _ = _save_master(tmp_path, [_clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))])
    with pytest.raises(ValueError, match="state_dir"):
        generate_artifacts(master_path=master_path, output_dir=tmp_path / "out",
                           templates=PRE_TEMPLATES, phase="pre-exam")


def test_pre_exam_only_includes_cases_cleared_by_office(tmp_path, monkeypatch):
    """只測關卡本身：傳給 generate_pre_exam 的主檔只含已放行案件（不測 Word 轉檔）。"""
    from koushi import application
    from koushi.workflow import PreExamResult

    cleared = _clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))
    pending = _clean_exam("1141", "B", "教授乙", dt.date(2026, 10, 5))
    master_path, master = _save_master(tmp_path, [cleared, pending])
    state_dir = tmp_path / "state"
    case_review.clear(state_dir, exam=cleared, master=master)

    seen = {}

    def fake_generate_pre_exam(allowed, **kwargs):
        seen["keys"] = [e.案件鍵 for e in allowed.exams]
        path = kwargs["output_dir"] / "a4.docx"
        path.write_bytes(b"fixture attachment4")
        return PreExamResult(attachment4=path, attachment6_files=[], issues=[])

    monkeypatch.setattr(application, "generate_pre_exam", fake_generate_pre_exam)
    result = generate_artifacts(master_path=master_path, output_dir=tmp_path / "out",
                                templates=PRE_TEMPLATES, phase="pre-exam", state_dir=state_dir)
    assert seen["keys"] == ["1141-A"]
    review_pending = [i for i in result.issues if i["問題代碼"] == "CASE_REVIEW_PENDING"]
    assert [i["案件鍵"] for i in review_pending] == ["1141-B"]


def test_review_queue_lists_exceptions_and_detects_committee_change(tmp_path):
    exam = _clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))
    exam.委員[0].職稱 = "講師"  # 碩士需助理教授以上 → 職級例外
    _, master = _save_master(tmp_path, [exam])
    state_dir = tmp_path / "state"

    row = review_queue(master, state_dir)[0]
    assert row.review is None
    codes = [i["問題代碼"] for i in row.exceptions]
    assert "COMMITTEE_RANK_EXCEPTION" in codes
    assert "COMMITTEE_COUNT_BELOW_MIN" in codes  # 只有 1 位委員

    case_review.clear(state_dir, exam=exam, master=master, actor="專員甲", note="業界專家，系主任同意")
    row = review_queue(master, state_dir)[0]
    assert row.review.cleared_by == "專員甲" and not row.changed_since_clear

    exam.委員[0].服務單位 = "國立清華大學資訊工程學系"  # 放行後名單變動：只標示、不自動撤銷
    row = review_queue(master, state_dir)[0]
    assert row.review is not None and row.changed_since_clear


def test_review_queue_flags_exceptions_that_appear_after_clear(tmp_path):
    """review-2026-09-23 P2-23：放行後新出現的例外要標示，放行當下已知的不算。"""
    exam = _clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))
    exam.委員[0].職稱 = "教授"
    master_path, master = _save_master(tmp_path, [exam])
    state_dir = tmp_path / "state"
    known = [i["問題代碼"] for i in review_queue(master, state_dir)[0].exceptions]
    assert "COMMITTEE_RANK_EXCEPTION" not in known

    application.clear_case_review(master_path=master_path, state_dir=state_dir, case_key="1141-A",
                                  actor="專員甲")
    row = review_queue(master, state_dir)[0]
    assert row.new_exceptions == []
    assert set(case_review.load(state_dir)["1141-A"].exception_keys) == {
        case_review.exception_key(i) for i in row.exceptions}

    exam.委員[0].職稱 = "講師"
    row = review_queue(master, state_dir)[0]
    assert [i["問題代碼"] for i in row.new_exceptions] == ["COMMITTEE_RANK_EXCEPTION"]


def test_old_review_without_exception_keys_reports_no_new_exceptions(tmp_path):
    import json
    exam = _clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))
    exam.委員[0].職稱 = "講師"
    _, master = _save_master(tmp_path, [exam])
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "case_reviews.json").write_text(json.dumps({"cases": {"1141-A": {
        "case_key": "1141-A", "cleared_at": "2026-09-20 10:00:00", "cleared_by": "專員甲",
        "note": "", "committee_signature": case_review.committee_signature(exam, master),
    }}}), encoding="utf-8")
    row = review_queue(master, state_dir)[0]
    assert row.review.exception_keys is None and row.new_exceptions == []

def test_co_advised_group_is_not_blocked_by_missing_reply(tmp_path):
    """共同指導：附件4 寄給全部指導教授，每位各一筆；2026-10-12 起有人沒回信也不擋口試後產檔。"""
    exam = _clean_exam("1141", "A", "教授甲、教授乙", dt.date(2026, 10, 1))
    master_path, master = _save_master(tmp_path, [exam])
    requests = create_requests(master)
    assert sorted(r.advisor for r in requests) == ["教授乙", "教授甲"]

    state_dir = tmp_path / "state"
    _clear_all(state_dir, master)
    only_one = [record_response(r, CONFIRMED, date=dt.date(2026, 9, 20)) if r.advisor == "教授甲" else r
                for r in requests]
    save_to_dir(EngineState(deficiency=[], confirmation=only_one, collection=[]), state_dir)
    result = generate_artifacts(master_path=master_path, output_dir=tmp_path / "out",
                                templates=TEMPLATES, phase="post-exam", state_dir=state_dir)
    assert len(result.attachment6_files) == 1

    both = [record_response(r, CONFIRMED, date=dt.date(2026, 9, 20)) for r in requests]
    save_to_dir(EngineState(deficiency=[], confirmation=both, collection=[]), state_dir)
    result = generate_artifacts(master_path=master_path, output_dir=tmp_path / "out2",
                                templates=TEMPLATES, phase="post-exam", state_dir=state_dir)
    assert len(result.attachment6_files) == 1


def test_resolve_advisor_handles_co_advisors():
    from koushi.mail.recipients import resolve_advisor
    contacts = {"advisors": {"教授乙": "b@example.org"}, "students": {}, "office": None}
    assert resolve_advisor("教授甲、教授乙", contacts) == "b@example.org"
    assert resolve_advisor("教授丙", contacts) is None


def test_resolve_advisor_prefers_email_filled_by_students_of_that_group():
    from koushi.mail.recipients import resolve_advisor
    contacts = {
        "case_advisors": {"1141-A": {"教授甲": "a-from-student@example.org"}, "1141-B": {}},
        "advisors": {"教授甲": "a-office@example.org", "教授乙": "b-office@example.org"},
    }
    assert resolve_advisor("教授甲", contacts, case_keys=("1141-B", "1141-A")) == "a-from-student@example.org"
    assert resolve_advisor("教授甲", contacts, case_keys=("1141-B",)) == "a-office@example.org"
    assert resolve_advisor("教授乙", contacts, case_keys=("1141-A",)) == "b-office@example.org"


def test_submission_contacts_collect_advisor_emails_per_case(tmp_path):
    import json
    path = tmp_path / "1141-A.json"
    path.write_text(json.dumps({
        "case_key": "1141-A", "term": "1141", "student_id": "A", "version": 1,
        "student_email": "s@example.org",
        "advisor_emails": {"教授甲": "a@example.org", "教授乙": "not-an-email"},
    }), encoding="utf-8")
    contacts, _ = application._submission_contacts([path], {"1141-A"})
    assert contacts["case_advisors"] == {"1141-A": {"教授甲": "a@example.org"}}


@pytest.mark.parametrize("state_present", [False, True])
def test_generate_without_phase_cannot_bypass_gates(tmp_path, state_present):
    master_path, _ = _save_master(
        tmp_path, [_clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))],
    )
    output_dir = tmp_path / "out"
    with pytest.raises(ValueError, match="階段"):
        generate_artifacts(
            master_path=master_path, output_dir=output_dir, templates=TEMPLATES,
            state_dir=tmp_path / "empty-state" if state_present else None,
        )
    assert not output_dir.exists()


def test_cli_generate_requires_phase_before_calling_application(tmp_path, monkeypatch, capsys):
    from koushi import cli, runtime

    monkeypatch.setattr(runtime, "load_local_env", lambda: None)
    called = []
    def fake_generate(**kwargs):
        called.append(kwargs)
        return GenerationResult(
            attachment4=tmp_path / "a4.docx", attachment6_files=[], issues=[],
        )

    monkeypatch.setattr(cli, "generate_artifacts", fake_generate)
    with pytest.raises(SystemExit) as error:
        cli.main(["generate", "--master", str(tmp_path / "missing.xlsx")])
    assert error.value.code == 2
    assert "--phase" in capsys.readouterr().err
    assert called == []


@pytest.mark.parametrize("reviewed", [False, True])
def test_cli_post_exam_enforces_gates(tmp_path, monkeypatch, capsys, reviewed):
    from koushi import cli, runtime

    monkeypatch.setattr(runtime, "load_local_env", lambda: None)
    master_path, master = _save_master(
        tmp_path, [_clean_exam("1141", "A", "教授甲", dt.date(2026, 10, 1))],
    )
    state_dir = tmp_path / "state"
    output_dir = tmp_path / "out"
    if reviewed:
        _clear_all(state_dir, master)

    assert cli.main([
        "generate", "--phase", "post-exam", "--master", str(master_path),
        "--state-dir", str(state_dir), "--output-dir", str(output_dir),
    ]) == 0
    output = capsys.readouterr().out
    assert "CONFIRMATION_PENDING" not in output              # 2026-10-12 起不再有教授確認關卡
    if not reviewed:
        assert "CASE_REVIEW_PENDING" in output
        assert not list(output_dir.rglob("*.xlsx"))
