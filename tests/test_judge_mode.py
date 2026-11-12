# -*- coding: utf-8 -*-
"""評審模式：示範資料、不寄真實信件、評審帳號權限在伺服器端受控且全程留稽核。"""
from dataclasses import replace

import pytest

from koushi import application, audit_log, demo, judge
from koushi import mailbox_agent as MA
from koushi import settings as office_settings
from koushi.mail import email_sender
from koushi.web import app as W

KEY = f"{demo.TERM}-{demo.SID}"


@pytest.fixture
def jenv(tmp_path, monkeypatch):
    root = tmp_path / "judge"
    monkeypatch.setenv("KOUSHI_DEMO_ROOT", str(root))
    monkeypatch.setenv("KOUSHI_OFFICE_PASSWORD", "office-pw")
    monkeypatch.setenv(judge.MODE_ENV, "1")
    monkeypatch.setenv(judge.PASSWORD_ENV, "judge-pw")
    monkeypatch.delenv("KOUSHI_ENV", raising=False)
    from tests.test_generators_schema4 import _attachment4_template
    template = tmp_path / "official-shape-test.docx"
    _attachment4_template(template)
    monkeypatch.setattr("koushi.config.TPL_ATT4", template)
    real_smtp = []  # 任何走到真實 SMTP 函式的寄信都記在這裡（應該永遠是空的）
    monkeypatch.setattr(email_sender, "send_email", lambda **kw: real_smtp.append(kw) or True)
    demo.initialize(root)
    cfg = demo.web_config(root)
    assert cfg.judge_mode
    monkeypatch.setitem(W.app.config, "_web", cfg)
    monkeypatch.setitem(W.app.config, "TESTING", True)
    client = W.app.test_client()
    with client.session_transaction() as s:
        s["_csrf_token"] = "t"
    return {"root": root, "cfg": cfg, "client": client, "real_smtp": real_smtp}


def _login_judge(client, password="judge-pw"):
    return client.post("/login/judge", data={"password": password, "csrf_token": "t"})


def _cfg():
    return W.app.config["_web"]


def _judge_events(state_dir, action=None):
    events = audit_log.load(state_dir / "audit_log.json").by_source("judge")
    return [e for e in events if action is None or e.action == action]


# ── 只能用在示範資料 ──

def test_refused_in_production(jenv, monkeypatch):
    monkeypatch.setenv("KOUSHI_ENV", "production")
    with pytest.raises(RuntimeError, match="正式環境"):
        demo.web_config(jenv["root"])


def test_refused_on_real_data_root_or_unmarked_folder(tmp_path, monkeypatch):
    real = tmp_path / "real"
    monkeypatch.setattr("koushi.config.STATE_DIR", real / "_state")
    with pytest.raises(RuntimeError, match="正式資料目錄"):
        judge.ensure_allowed(real)
    other = tmp_path / "someone-else"
    other.mkdir()
    (other / "口試主檔.xlsx").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="示範資料夾"):
        judge.ensure_allowed(other)
    judge.ensure_allowed(tmp_path / "fresh")  # 不存在的新資料夾可以


def test_web_refuses_judge_mode_without_demo_data(monkeypatch):
    monkeypatch.setenv(judge.MODE_ENV, "1")
    monkeypatch.delenv("KOUSHI_DEMO_ROOT", raising=False)
    with pytest.raises(RuntimeError, match="示範資料"):
        W.WebConfig.from_env()


def test_judge_password_must_differ_from_office(jenv, monkeypatch):
    monkeypatch.setenv(judge.PASSWORD_ENV, "office-pw")
    with pytest.raises(RuntimeError, match="不可與系辦密碼相同"):
        demo.web_config(jenv["root"])


# ── 登入與標示 ──

def test_separate_judge_login_and_banner(jenv):
    client, cfg = jenv["client"], jenv["cfg"]
    assert "評審登入" in client.get("/").get_data(as_text=True)
    assert judge.BANNER in client.get("/").get_data(as_text=True)
    # 兩組密碼不能互通
    client.post("/login/office", data={"password": "judge-pw", "csrf_token": "t"})
    with client.session_transaction() as s:
        assert "role" not in s
    _login_judge(client, "office-pw")
    with client.session_transaction() as s:
        assert "role" not in s
    assert _judge_events(cfg.state_dir, "login_failed")
    assert _login_judge(client).status_code == 302
    page = client.get("/office/overview").get_data(as_text=True)
    assert judge.BANNER in page and "評審（權限受控）" in page and "模擬寄件匣" in page
    assert _judge_events(cfg.state_dir, "login")


def test_judge_login_absent_outside_judge_mode(jenv, monkeypatch):
    plain = replace(jenv["cfg"], judge_password=None)
    monkeypatch.setitem(W.app.config, "_web", plain)
    client = jenv["client"]
    assert "評審登入" not in client.get("/").get_data(as_text=True)
    assert _login_judge(client).status_code == 404
    client.post("/login/office", data={"password": "office-pw", "csrf_token": "t"})
    assert client.get("/office/judge/outbox").status_code == 404


# ── 權限：伺服器端白名單 ──

@pytest.mark.parametrize("path,data", [
    ("/office/settings", {"send_hour": "23", "current_term": demo.TERM}),
    ("/office/ai-proposals/settings", {"affiliation": "0", "handler": "x"}),
    ("/office/ai-proposals/alias/remove", {"key": "x", "handler": "x", "reason": "x"}),
    ("/office/upload-registries", {}),
])
def test_judge_cannot_change_settings_or_upload(jenv, path, data):
    client, cfg = jenv["client"], jenv["cfg"]
    _login_judge(client)
    before = office_settings.load(cfg.state_dir)
    response = client.post(path, data={**data, "csrf_token": "t"})
    assert response.status_code == 403
    assert office_settings.load(cfg.state_dir) == before
    [denied] = _judge_events(cfg.state_dir, "denied")
    assert denied.detail["actor"] == "評審" and denied.detail["path"] == path


@pytest.mark.parametrize("path", [
    "/office/overview/export", "/office/communications/export",
    f"/office/archives/{demo.TERM}/files/x.json", f"/office/archives/{demo.TERM}/manifest",
])  # 本輪產生的附件4／附件6 是示範假資料，評審可下載（tests/test_agent_trace_pages.py）
def test_judge_cannot_export_or_download(jenv, path):
    client, cfg = jenv["client"], jenv["cfg"]
    _login_judge(client)
    assert client.get(path).status_code == 403
    assert _judge_events(cfg.state_dir, "denied")[-1].detail["path"] == path


def test_judge_can_view_every_office_page(jenv):
    client = jenv["client"]
    _login_judge(client)
    for path in ("/office/overview", "/office/import", "/office/documents", "/office/communications",
                 "/office/mailbox", "/office/committees", "/office/ai-proposals", "/office/settings",
                 "/office/archives", "/office/demo", "/office/judge/outbox", "/office/assistant",
                 "/office/agent-trace", "/office/agent-metrics"):
        response = client.get(path)
        assert response.status_code == 200, path
    settings_page = client.get("/office/settings").get_data(as_text=True)
    assert "評審帳號僅可檢視" in settings_page


# ── 完整流程：人工關卡、模擬寄件匣、模擬回信、重設 ──

def test_judge_runs_scenario_without_sending_real_mail(jenv):
    client, cfg = jenv["client"], jenv["cfg"]
    _login_judge(client)
    client.post("/office/demo", data={"action": "advance", "days": "1", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    assert application.load_automation_status(cfg.state_dir)["ok"]
    # 人工關卡：處理人欄位填什麼都一律記為「評審」
    assert client.post(f"/office/case/{KEY}/clear", data={"handler": "冒名專員", "note": "評審放行",
                                                          "csrf_token": "t"}).status_code == 302
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    assert application.load_automation_status(cfg.state_dir)["ok"]

    outbox = judge.outbox(cfg.state_dir)
    advisor_mail = next(m for m in outbox if m["to"] == demo.MAILBOXES["advisor"])
    assert {m["to"] for m in outbox} >= {demo.MAILBOXES["advisor"], demo.MAILBOXES["committee"]}
    assert jenv["real_smtp"] == []
    page = client.get("/office/judge/outbox").get_data(as_text=True)
    assert W._rename_attachments(advisor_mail["subject"]) in page and "未實際寄出" in page

    # 評審模擬教授回信，下一輪由信箱 agent 讀取（沒有模型＝全部交人工）
    client.post("/office/judge/outbox/reply", data={"outbox_id": advisor_mail["id"],
                                                    "text": "請問口試當天有投影機嗎？", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    [record] = MA.load_mails(cfg.state_dir)
    assert record["sender"]["role"] == MA.ROLE_ADVISOR and record["sender"]["verified"]
    assert record["status"] == MA.MAIL_AWAITING
    assert client.post("/office/mailbox/mark", data={"message_id": record["message_id"], "status": "handled",
                                                     "handler": "隨便填", "csrf_token": "t"}).status_code == 302
    assert MA.load_mails(cfg.state_dir)[0]["status"] == MA.MAIL_HANDLED
    assert jenv["real_smtp"] == []

    # 每一個評審動作都留稽核，且業務紀錄的處理人一律是「評審」
    actions = [e.detail["endpoint"] for e in _judge_events(cfg.state_dir, "action")]
    assert {"office_demo", "clear_case", "judge_outbox_reply", "mailbox_mark"} <= set(actions)
    actors = {e.detail.get("actor") for e in audit_log.load(cfg.state_dir / "audit_log.json").all()
              if e.source != "judge" and isinstance(e.detail, dict) and e.detail.get("actor")}
    assert "評審" in actors and "冒名專員" not in actors and "隨便填" not in actors

    # 重設成乾淨的指定情境：新的一輪，寄件匣清空
    old_state = cfg.state_dir
    client.post("/office/demo", data={"action": "reset", "scenario": "committee", "csrf_token": "t"})
    assert _cfg().state_dir != old_state and _cfg().judge_mode
    assert demo.active(jenv["root"])[1]["scenario"] == "committee"
    assert judge.outbox(_cfg().state_dir) == []
    assert "本輪還沒有寄出任何信" in client.get("/office/judge/outbox").get_data(as_text=True)


def test_judge_can_engage_ai_emergency_stop_recorded_as_judge(jenv):
    from koushi import ai_switch
    client, cfg = jenv["client"], jenv["cfg"]
    _login_judge(client)
    client.post("/office/ai-switch", data={"disabled": "1", "handler": "別人", "reason": "評審測試緊急停止",
                                           "csrf_token": "t"})
    assert ai_switch.is_disabled(cfg.state_dir)
    [event] = audit_log.load(cfg.state_dir / "audit_log.json").by_source("ai_switch")
    assert event.detail["actor"] == "評審"


def test_judge_session_is_void_outside_judge_mode(jenv, monkeypatch):
    client = jenv["client"]
    _login_judge(client)
    monkeypatch.setitem(W.app.config, "_web", replace(jenv["cfg"], judge_password=None))
    assert client.get("/office/overview").status_code == 403
    with client.session_transaction() as s:
        assert "role" not in s


# ── 寄信防線 ──

def test_capture_transport_and_real_smtp_block(tmp_path, monkeypatch):
    send = judge.capture_transport(tmp_path)
    assert send(to="advisor@example.org", subject="主旨", body="內容", message_id="<m@x>") is True
    [item] = judge.outbox(tmp_path)
    assert item["to"] == "advisor@example.org" and item["message_id"] == "<m@x>"
    reply = judge.simulate_reply(tmp_path, outbox_id=item["id"], text="確認無誤", stranger=True)
    assert reply["from_addr"] == judge.STRANGER and reply["in_reply_to"] == "<m@x>"
    [mail] = judge.take_replies(tmp_path)
    assert mail.reply_text == "確認無誤" and "> 內容" in mail.body
    assert judge.take_replies(tmp_path) == []  # 只讀取一次
    with pytest.raises(ValueError):
        judge.simulate_reply(tmp_path, outbox_id="nope", text="x")

    monkeypatch.setattr(email_sender, "send_email", email_sender.send_email)  # 測試結束後還原
    judge.block_real_smtp()
    with pytest.raises(RuntimeError, match="評審模式不寄出真實信件"):
        email_sender.send_email(to="a@example.org", subject="s", body="b")
