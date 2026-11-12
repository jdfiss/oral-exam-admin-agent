# -*- coding: utf-8 -*-
"""F06: interrupted deliveries recover from persisted SENT evidence, without SMTP."""
import datetime as dt

import pytest

from koushi import advisor_confirmation as AC, application, case_review, committee_requests, send_log
from koushi.engines.state import load_from_dir
from koushi.mail import notify
from tests.test_advisor_confirmation import env
from tests.test_application_generate import _clean_exam, _save_master

TODAY = dt.date(2026, 10, 1)


@pytest.fixture(params=["advisor", "committee"])
def delivery_env(env, request, tmp_path, monkeypatch):
    import json
    exams = [_clean_exam("1141", sid, "教授" + sid, dt.date(2026, 11, i + 1))
             for i, sid in enumerate("ABC")]
    path, master = _save_master(tmp_path, exams)
    for i, exam in enumerate(exams):
        member = exam.委員[0]
        member.服務單位 = "外校大學"
        master.committees[member.committee_id].Email = f"member{i}@example.org"
        (env["subs"] / f"1141-{exam.學號}.json").write_text(json.dumps({
            "case_key": exam.案件鍵, "term": "1141", "student_id": exam.學號,
            "version": 1, "advisor_emails": {exam.指導教授: f"advisor{i}@example.org"},
        }), encoding="utf-8")
        case_review.clear(env["state"], exam=exam, master=master)
    application.master_workbook.save(master, [], path=path)
    from koushi import source_state
    source_state.publish(path, {})
    env.update(master=master, master_path=path, kind=request.param)
    monkeypatch.setattr(send_log, "_now_str", lambda: "2026-10-01 10:00:00")
    return env


def run(env, *, transport, date=TODAY):
    if env["kind"] == "advisor":
        return application.process_advisor_confirmations(
            master=env["master"], state_dir=env["state"], submissions_dir=env["subs"],
            send=True, transport=transport, date=date, template="unused.doc",
        )
    return application.process_committee_requests(
        master=env["master"], state_dir=env["state"], send=True, transport=transport, date=date,
    )


def sent_dates(env):
    if env["kind"] == "advisor":
        return {r.sent_date for r in load_from_dir(env["state"]).confirmation if r.sent_date}
    return {r.sent_date for r in committee_requests.plan(env["master"], env["state"]) if r.sent_date}


@pytest.mark.parametrize("fail_at", [1, 2, 3])
@pytest.mark.parametrize("failure", ["exception", "false"])
def test_batch_failure_then_reload_never_resends_success(delivery_env, fail_at, failure):
    env = delivery_env
    deliveries, attempts = [], []

    def failing_transport(**kw):
        attempts.append(kw["to"])
        if len(attempts) == fail_at:
            if failure == "exception":
                raise RuntimeError("SMTP failure")
            return False
        deliveries.append(kw["to"])
        return True

    if failure == "exception":
        with pytest.raises(RuntimeError, match="SMTP failure"):
            run(env, transport=failing_transport)
    else:
        run(env, transport=failing_transport)
    log = send_log.load(env["state"] / "send_log.json")
    assert len(log.by_status(send_log.SENT)) == len(deliveries)
    assert len(log.by_status(send_log.FAILED)) == 1
    if env["kind"] == "advisor":
        assert sum(r.sent_date is not None for r in load_from_dir(env["state"]).confirmation) == len(deliveries)
    else:
        assert sum(r.sent_date is not None for r in committee_requests.plan(env["master"], env["state"])) == len(deliveries)
    run(env, transport=lambda **kw: deliveries.append(kw["to"]) or True,
        date=TODAY + dt.timedelta(days=1))
    assert len(deliveries) == len(set(deliveries)) == 3
    assert len(send_log.load(env["state"] / "send_log.json").by_status(send_log.SENT)) == 3
    run(env, transport=lambda **kw: pytest.fail("already delivered"))


def test_legacy_sent_only_recovers_before_generating_attachments(delivery_env, monkeypatch):
    env = delivery_env
    if env["kind"] == "advisor":
        drafts = application.process_advisor_confirmations(
            master=env["master"], state_dir=env["state"], submissions_dir=env["subs"],
            send=False, date=TODAY,
        ).drafts
    else:
        drafts, _ = application.process_committee_requests(
            master=env["master"], state_dir=env["state"], send=False, date=TODAY,
        )
    log = send_log.SendEventLog()
    notify.send_drafts(drafts, log=log, transport=lambda **kw: True)
    send_log.save(log, env["state"] / "send_log.json")
    monkeypatch.setattr(AC, "write_term_document", lambda *a, **kw: pytest.fail("recovery needs no new attachment"))
    run(env, transport=lambda **kw: pytest.fail("SENT must not be resent"),
        date=TODAY + dt.timedelta(days=2))
    assert sent_dates(env) == {TODAY}
    if env["kind"] == "advisor":
        assert len(load_from_dir(env["state"]).confirmation) == 3
        assert all(r.deadline.base_date == TODAY for r in load_from_dir(env["state"]).confirmation)
        assert all(r["rounds"] == 1 for r in AC.load_mail_state(env["state"]).values())
    run(env, transport=lambda **kw: pytest.fail("repeated recovery must be idempotent"))


@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize("write_target", ["mail", "engine"])
def test_business_write_interruption_recovers_without_resending(delivery_env, monkeypatch, after_write, write_target):
    env = delivery_env
    if env["kind"] == "advisor":
        target, name = (AC, "_save_mail_state") if write_target == "mail" else (application, "save_to_dir")
    else:
        target, name = committee_requests, "mark_sent"
    original = getattr(target, name)
    interrupted = []

    def fail_once(*args, **kwargs):
        if not interrupted:
            interrupted.append(True)
            if after_write:
                original(*args, **kwargs)
            raise OSError("state write interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, fail_once)
    deliveries = []
    with pytest.raises(OSError, match="state write interrupted"):
        run(env, transport=lambda **kw: deliveries.append(kw["to"]) or True)
    run(env, transport=lambda **kw: deliveries.append(kw["to"]) or True,
        date=TODAY + dt.timedelta(days=2))
    assert len(deliveries) == len(set(deliveries)) == 3
    assert TODAY in sent_dates(env)
    if env["kind"] == "advisor":
        assert all(r["rounds"] == 1 for r in AC.load_mail_state(env["state"]).values())


def test_recovery_survives_a_new_python_process(delivery_env):
    import subprocess
    import sys
    env = delivery_env
    if env["kind"] == "advisor":
        drafts = application.process_advisor_confirmations(
            master=env["master"], state_dir=env["state"], submissions_dir=env["subs"],
            send=False, date=TODAY,
        ).drafts
    else:
        drafts, _ = application.process_committee_requests(master=env["master"], state_dir=env["state"])
    log = send_log.SendEventLog()
    notify.send_drafts(drafts, log=log, transport=lambda **kw: True)
    send_log.save(log, env["state"] / "send_log.json")
    code = '''
import datetime as dt
import sys
from koushi import application as A
master = A.load_current_master(sys.argv[1])
def no_send(**kwargs):
    raise AssertionError("recovery must not send")
kwargs = dict(master=master, state_dir=sys.argv[2], send=True, transport=no_send, date=dt.date(2026, 10, 3))
if sys.argv[3] == "advisor":
    assert A.process_advisor_confirmations(**kwargs).drafts == []
else:
    assert A.process_committee_requests(**kwargs) == ([], [])
'''
    result = subprocess.run([sys.executable, "-c", code, str(env["master_path"]), str(env["state"]), env["kind"]],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert sent_dates(env) == {TODAY}


def test_old_version_sent_evidence_does_not_recover_new_version(env):
    for exam in env["master"].exams:
        case_review.clear(env["state"], exam=exam, master=env["master"])
    drafts = application.process_advisor_confirmations(
        master=env["master"], state_dir=env["state"], submissions_dir=env["subs"], send=False, date=TODAY,
    ).drafts
    log = send_log.SendEventLog()
    notify.send_drafts(drafts, log=log, transport=lambda **kw: True)
    send_log.save(log, env["state"] / "send_log.json")
    env["master"].exams[0].口試方式 = "視訊"
    result = application.process_advisor_confirmations(
        master=env["master"], state_dir=env["state"], submissions_dir=env["subs"],
        send=True, transport=env["transport"], date=TODAY,
    )
    assert len(result.sent) == 1
    assert result.sent[0]["to"] == "jia@example.org"
    assert "視訊" in env["sent"][0]["body"]
