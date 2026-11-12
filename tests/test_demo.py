from tests.web_fixture import post_submission
"""Demo acceptance: real application boundaries, fake mail transport and AI provider."""
import datetime as dt
import re
import time
from dataclasses import replace

import openpyxl
import pytest
from koushi import application as A, advisor_confirmation as AC, clock, committee_requests as CR
from koushi import committee_verification as CV, demo, ingest, settings
from koushi.classifier import ClassificationResult
from koushi.engines.confirmation import CONFIRMED
from koushi.web import app as W


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "demo"
    monkeypatch.setenv("KOUSHI_DEMO_ROOT", str(root))
    monkeypatch.setenv("KOUSHI_OFFICE_PASSWORD", "demo-test")
    monkeypatch.setenv("KOUSHI_PUBLIC_URL", "http://127.0.0.1:5000")
    from tests.test_generators_schema4 import _attachment4_template
    template = tmp_path / "official-shape-test.docx"
    _attachment4_template(template)
    monkeypatch.setattr("koushi.config.TPL_ATT4", template)
    demo.initialize(root)
    cfg = demo.web_config(root)
    monkeypatch.setitem(W.app.config, "_web", cfg)
    monkeypatch.setitem(W.app.config, "TESTING", True)
    monkeypatch.setitem(W.app.config, "SECRET_KEY", "test-secret")
    sent = []
    def transport(**kwargs):
        sent.append(kwargs)
        return True
    return root, cfg, W.app.test_client(), sent, transport


def csrf(client):
    with client.session_transaction() as session:
        session.setdefault("_csrf_token", "test-csrf")
        return session["_csrf_token"]


def login(client):
    client.post("/login/office", data={"password": "demo-test", "csrf_token": csrf(client)})


def automate(cfg, transport, mails=(), intent="confirm"):
    return A.run_daily_automation(submission_paths=ingest.student_submission_paths(cfg.submissions_dir),
        master_path=cfg.master_path, state_dir=cfg.state_dir, registries=cfg.build_registries,
        submissions_dir=cfg.submissions_dir, send=True, transport=transport,
        date=clock.today(), now=clock.now(), templates=cfg.templates,
        fetch_replies=lambda: list(mails),
        classifier=lambda mail, req: ClassificationResult(intent=intent, evidence=mail.reply_text,
                                                         model="test-provider", prompt_version="test"))


def approve(cfg):
    A.clear_case_review(master_path=cfg.master_path, state_dir=cfg.state_dir,
                        case_key=f"{demo.TERM}-{demo.SID}", actor="展示專員")


@pytest.mark.parametrize("scenario", [k for k in demo.SCENARIOS if k != "ai"])   # ai 情境另有專用測試
def test_four_scenarios_are_isolated_and_obey_dates(env, scenario):
    root, _, client, sent, transport = env
    old_run, _ = demo.active(root)
    demo.initialize(root, scenario)
    cfg = demo.web_config(root)
    W.configure_app(W.app, cfg)
    assert cfg.state_dir.parent != old_run
    automate(cfg, transport)
    assert not sent  # Filling period never sends.
    clock.advance(root)
    result = automate(cfg, transport)
    if scenario == "deficiency":
        assert len(result.process.sent) == 1
        assert result.process.sent[0]["to"] == demo.fallback_template().format(學號=demo.SID)
        # The student completes the seeded draft through the actual browser route.
        W.configure_app(W.app, cfg)
        client.post("/login/student", data={"student_id": demo.SID, "csrf_token": csrf(client)})
        draft = A.load_draft(cfg.submissions_dir, f"{demo.TERM}-{demo.SID}")
        # Draft contains normalized submission values; UI presents it for resubmission.
        page = client.get(f"/form?term={demo.TERM}").get_data(as_text=True)
        assert "模擬會議室" in page
        fields = {"term": demo.TERM, "student_email": demo.MAILBOXES["student"],
            "exam_date": (clock.today()+dt.timedelta(days=30)).isoformat(), "exam_location": "模擬會議室",
            "exam_start_time": "10:00", "exam_end_time": "12:00", "exam_mode": "實體",
            "advisor_0": "模擬指導教授", "advisor_0_email": demo.MAILBOXES["advisor"], "csrf_token": csrf(client)}
        for i, m in enumerate(demo.COMMITTEES):
            for key in ("name", "unit", "title", "email"):
                fields[f"committee_{i}_{key}"] = m[key]
        response = post_submission(client, data=fields)
        assert response.status_code == 302
        assert (cfg.submissions_dir / f"{demo.TERM}-{demo.SID}.json").is_file()
        automate(cfg, transport)
    approve(cfg)
    result = automate(cfg, transport)
    # 2026-10-12 起附件4 不等放行：資料齊全就寄（可能在放行之前），整個流程只寄一封
    advisor_mails = [m for m in sent if m["to"] == demo.MAILBOXES["advisor"] and "口試附件4" in m["subject"]]
    assert len(advisor_mails) == 1 and "不需回覆" in advisor_mails[0]["body"]
    assert len(result.committee_sent) == 1
    assert {m["to"] for m in sent} <= set(demo.MAILBOXES.values()) | {demo.fallback_template().format(學號=demo.SID)}
    count = len(sent)
    automate(cfg, transport)
    assert len(sent) == count  # A repeat is idempotent.
    request = A.tracking_snapshot(master=A.load_current_master(cfg.master_path), state_dir=cfg.state_dir).confirmation.requests[0]
    version = AC.load_mail_state(cfg.state_dir)[AC.ref_code(request)]["version"]
    mail = AC.InboundMail(message_id="demo-reply", from_addr=demo.MAILBOXES["advisor"],
        subject="Re: " + advisor_mails[0]["subject"], received_at=(clock.now()+dt.timedelta(minutes=1)).replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).isoformat(),
        in_reply_to=AC.load_mail_state(cfg.state_dir)[AC.ref_code(request)]["message_id"], received_at_trusted=True,
        body=f"確認無誤\n附件4 版本：{version}", reply_text="確認無誤" if scenario != "modify" else "可以，但日期請修改")
    result = automate(cfg, transport, [mail], intent="modify" if scenario == "modify" else "confirm")
    inbox = AC.load_inbox(cfg.state_dir)
    if scenario == "modify":
        assert inbox[0].status == AC.INBOX_PENDING
        assert not result.tracking.confirmation.confirmed
        # The office applies the requested change and obtains a fresh confirmation.
        login(client)
        client.post("/office/advisor-reply", data={"message_id":mail.message_id,
            "action":"modified", "handler":"展示專員", "note":"教授要求修改日期", "csrf_token":csrf(client)})
        changed_date = (clock.today()+dt.timedelta(days=40)).isoformat()
        client.post(f"/office/case/{demo.TERM}-{demo.SID}/correction", data={
            "field":"口試日期", "value":changed_date, "handler":"展示專員",
            "reason":"依教授回信修正", "csrf_token":csrf(client)})
        assert changed_date in client.get("/office/overview").get_data(as_text=True)
        result = automate(cfg, transport)
        assert len(result.advisor.sent) == 1
        current = result.tracking.confirmation.requests[0]
        new_version = AC.load_mail_state(cfg.state_dir)[AC.ref_code(current)]["version"]
        fresh = AC.InboundMail(message_id="fresh-confirm", from_addr=demo.MAILBOXES["advisor"],
            subject="Re: "+result.advisor.sent[0]["subject"], received_at=(clock.now()+dt.timedelta(minutes=5)).replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).isoformat(),
            in_reply_to=AC.load_mail_state(cfg.state_dir)[AC.ref_code(current)]["message_id"], received_at_trusted=True,
            body=f"新版確認無誤\n附件4 版本：{new_version}", reply_text="新版確認無誤")
        result = automate(cfg, transport, [fresh])
        inbox = AC.load_inbox(cfg.state_dir)
    assert inbox[-1].status == AC.INBOX_APPLIED
    assert result.tracking.confirmation.confirmed
    blocked = A.generate_artifacts(master_path=cfg.master_path, state_dir=cfg.state_dir,
        templates=cfg.templates, output_dir=cfg.doc_output_root / "post", phase="post-exam")
    assert not blocked.attachment6_files
    previews = A.generate_fee_preview(master_path=cfg.master_path, state_dir=cfg.state_dir,
        output_dir=cfg.doc_output_root / "preview", template=cfg.templates.attachment6)
    assert previews and "資料未齊" in openpyxl.load_workbook(previews[0]).active.cell(1,1).value
    # Complete the actual email challenge and submission routes.
    W.configure_app(W.app, cfg)
    req = CR.plan(A.load_current_master(cfg.master_path), cfg.state_dir)[0]
    token = CR.link_token(cfg.state_dir, req.email, request=req)
    path = f"/committee/reply/{token}"
    assert "信箱驗證碼" in client.get(path).get_data(as_text=True)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(demo, "mail_transport", lambda root: transport)
        client.post(path, data={"action":"send-code", "csrf_token":csrf(client)})
    code = re.search(r"驗證碼是：(\d{6})", sent[-1]["body"]).group(1)
    client.post(path, data={"action":"verify-code", "code":code, "csrf_token":csrf(client)})
    response = client.post(path, data={"身分證字號":"A123456789", "身分證字號_確認":"A123456789",
        "金融機構":"模擬銀行", "帳號":"1234567890", "帳號_確認":"1234567890", "戶籍地址":"虛構地址", "csrf_token":csrf(client)})
    assert "已收到" in response.get_data(as_text=True)
    # Committee identity reconciliation alone must not invalidate the professor's approval.
    official = A.generate_artifacts(master_path=cfg.master_path, state_dir=cfg.state_dir,
        templates=cfg.templates, output_dir=cfg.doc_output_root / "post", phase="post-exam")
    assert official.attachment6_files
    # Download the actual file through the office UI, not only check disk existence.
    login(client)
    client.post("/office/documents/generate", data={"phase":"post-exam", "csrf_token":csrf(client)})
    status = W._load_doc_status()["post-exam"]
    assert status["status"] == "built"
    for name in status["attachment6_files"]:
        response = client.get("/office/documents/download/post-exam/" + name)
        assert response.status_code == 200 and response.data.startswith(b"PK")


def test_demo_controls_require_login_and_csrf_and_reset_safely(env):
    root, cfg, client, sent, transport = env
    assert client.get("/office/demo").status_code == 302
    login(client)
    assert client.post("/office/demo", data={"action":"advance"}).status_code == 400
    old = clock.today()
    response = client.post("/office/demo", data={"action":"advance","days":"1","csrf_token":csrf(client)})
    assert response.status_code == 302 and clock.today() == old + dt.timedelta(days=1)
    old_run, _ = demo.active(root)
    client.post("/office/demo", data={"action":"reset","scenario":"modify","csrf_token":csrf(client)})
    assert old_run.is_dir() and demo.active(root)[0] != old_run
    assert "教授要求修改" in client.get("/office/demo").get_data(as_text=True)


def test_otp_expiry_retries_and_one_time_use(env):
    _, cfg, _, sent, transport = env
    CV.send_code(cfg.state_dir,token="token",email="test@example.org",secret="secret",transport=transport,now=1000)
    code = re.search(r"(\d{6})", sent[-1]["body"]).group(1)
    with pytest.raises(ValueError):
        CV.send_code(cfg.state_dir,token="token",email="test@example.org",secret="secret",transport=transport,now=1001)
    for i in range(5):
        with pytest.raises(ValueError): CV.verify_code(cfg.state_dir,token="token",code="invalid",secret="secret",now=1010)
    with pytest.raises(ValueError): CV.verify_code(cfg.state_dir,token="token",code=code,secret="secret",now=1011)
    CV.send_code(cfg.state_dir,token="fresh",email="test@example.org",secret="secret",transport=transport,now=1000)
    code = re.search(r"(\d{6})", sent[-1]["body"]).group(1)
    assert CV.verify_code(cfg.state_dir,token="fresh",code=code,secret="secret",now=1001) == 1901
    with pytest.raises(ValueError): CV.verify_code(cfg.state_dir,token="fresh",code=code,secret="secret",now=1002)
    CV.send_code(cfg.state_dir,token="expired",email="test@example.org",secret="secret",transport=transport,now=1000)
    with pytest.raises(ValueError): CV.verify_code(cfg.state_dir,token="expired",code=code,secret="secret",now=1600)


def test_demo_mail_guard_blocks_other_recipients(env, monkeypatch):
    root, cfg, client, sent, transport = env
    monkeypatch.setattr("koushi.mail.email_sender.sender_address", lambda: demo.MAILBOXES["student"])
    monkeypatch.setattr("koushi.mail.email_sender.send_email", transport)
    guarded = demo.mail_transport(root)
    with pytest.raises(ValueError): guarded(to="outsider@example.net",subject="test",body="test")
    assert not sent
    guarded(to=demo.MAILBOXES["advisor"], subject="test",body="test")
    assert demo.active(root)[1]["run_id"] in sent[0]["subject"]


def test_committee_link_expires_in_real_time(env, monkeypatch):
    _, cfg, _, _, _ = env
    monkeypatch.setattr(CR.time, "time", lambda: 1000)
    token = CR.link_token(cfg.state_dir, "test@example.org")
    clock.advance(cfg.demo_root, 7)  # Advancing business time does not expire the link.
    assert CR.email_for_token(cfg.state_dir, token) == "test@example.org"
    monkeypatch.setattr(CR.time, "time", lambda: 1000+7*86400)
    assert CR.email_for_token(cfg.state_dir, token) is None


@pytest.mark.parametrize("raw", ['{"intent":"confirm"}', '[]', '{"intent":"confirm","evidence":"OK","has_reservation":"false","modifications":[],"questions":[]}'])
def test_incomplete_model_output_never_confirms(raw):
    from koushi.classifier import parse_model_output, ClassifierError
    with pytest.raises(ClassifierError): parse_model_output(raw,model="test",prompt_ver="test")


def test_hard_deadline_stops_student_mail_and_office_can_extend_one_case(env):
    root, _, client, sent, transport = env
    demo.initialize(root, "deficiency")
    cfg = demo.web_config(root)
    W.configure_app(W.app, cfg)
    clock.advance(root)
    automate(cfg, transport)
    clock.advance(root, 7)
    clock.advance(root)
    before = len(sent)
    automate(cfg, transport)
    assert len(sent) == before
    key = f"{demo.TERM}-{demo.SID}"
    assert A.submission_block_reason(key, cfg.state_dir)
    login(client)
    client.post(f"/office/case/{key}/unlock", data={"until":(clock.today()+dt.timedelta(days=1)).isoformat(),
        "handler":"展示專員", "reason":"模擬專員同意個案延長", "csrf_token":csrf(client)})
    assert not A.submission_block_reason(key, cfg.state_dir)


def test_mailbox_agent_failure_does_not_stop_daily_run(env, monkeypatch):
    """信箱 agent 不是核心流程：出錯只略過並留紀錄，每日自動化照常完成。"""
    root, cfg, _, _, transport = env
    clock.advance(root)  # 填寫期會提早返回，推進到補件期才會跑到信箱 agent
    def boom(**kwargs):
        raise RuntimeError("mailbox state corrupted")
    monkeypatch.setattr(A, "run_mailbox_agent", boom)
    result = automate(cfg, transport)
    assert result.mailbox == ()
    assert any("信箱 agent 本輪略過" in note for note in result.notes)
    from koushi import audit_log
    events = audit_log.load(cfg.state_dir / "audit_log.json").by_source("mailbox_agent")
    assert events[-1].action == "run_failed" and "corrupted" in events[-1].detail["error"]



def test_ai_scenario_produces_affiliation_and_title_proposals_that_need_staff(env, monkeypatch):
    """「AI 協助判讀」情境：服務單位簡稱與客座教授會產生 AI 建議；核准對應前查無距離，核准後才算交通費。"""
    from koushi import affiliation_agent, ai_proposals as P, offline_llm
    root, _, client, sent, transport = env
    demo.initialize(root, "ai")
    cfg = demo.web_config(root)
    W.configure_app(W.app, cfg)
    clock.advance(root)
    result = A.run_daily_automation(submission_paths=ingest.student_submission_paths(cfg.submissions_dir),
        master_path=cfg.master_path, state_dir=cfg.state_dir, registries=cfg.build_registries,
        submissions_dir=cfg.submissions_dir, send=True, transport=transport,
        date=clock.today(), now=clock.now(), templates=cfg.templates, fetch_replies=lambda: [],
        llm=offline_llm.make_offline_llm())
    agents = {p["agent"] for p in result.proposals}
    assert {"affiliation", "title"} <= agents, agents
    master = A.load_current_master(cfg.master_path)
    external = next(m for e in master.exams for m in e.委員 if m.委員姓名 == "模擬校外委員")
    assert external.交通費 in (None, 0)          # 未確認前維持查無距離
    proposal = next(p for p in P.pending(cfg.state_dir, "affiliation"))
    assert proposal["suggestion"] == demo.EXTERNAL_UNIT
    affiliation_agent.decide(cfg.state_dir, proposal["id"], action="approve", actor="展示專員",
                             distance_units=[demo.EXTERNAL_UNIT])
    A.build_current_master(submission_paths=ingest.student_submission_paths(cfg.submissions_dir),
                           master_path=cfg.master_path, registries=cfg.build_registries,
                           submissions_dir=cfg.submissions_dir)
    master = A.load_current_master(cfg.master_path)
    external = next(m for e in master.exams for m in e.委員 if m.委員姓名 == "模擬校外委員")
    assert external.交通費 and external.交通費 > 0
