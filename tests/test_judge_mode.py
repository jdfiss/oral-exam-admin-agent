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


# ── 公開評審環境：免密碼入口、真模型每日上限 ──

def test_open_judge_login_needs_no_password_but_office_still_does(jenv, monkeypatch):
    monkeypatch.setenv(judge.OPEN_ENV, "1")
    client, cfg = jenv["client"], jenv["cfg"]
    home = client.get("/").get_data(as_text=True)
    assert "免登入" in home and 'id="judge_password"' not in home
    assert home.index("免登入") < home.index("學生登入")  # 評審入口放最上面
    assert _login_judge(client, "").status_code == 302
    with client.session_transaction() as s:
        assert s.get("judge") and s.get("role") == "office"
        sandbox = judge.sandbox_root(cfg.demo_root, s["judge_sandbox"])
    from koushi import demo as D
    assert _judge_events(D.web_config(sandbox).state_dir, "login")  # 登入紀錄寫在這位評審自己的沙盒
    other = W.app.test_client()
    with other.session_transaction() as s:
        s["_csrf_token"] = "t"
    other.post("/login/office", data={"password": "", "csrf_token": "t"})
    with other.session_transaction() as s:
        assert "role" not in s


def test_judge_password_required_when_not_open(jenv, monkeypatch):
    monkeypatch.delenv(judge.OPEN_ENV, raising=False)
    client = jenv["client"]
    assert 'id="judge_password"' in client.get("/").get_data(as_text=True)
    _login_judge(client, "")
    with client.session_transaction() as s:
        assert "role" not in s


def test_daily_cap_switches_to_offline_after_limit(monkeypatch):
    from koushi import llm as L

    class Real:
        model = "gpt-4o-mini"
        last_usage = {"prompt_tokens": 1}
        calls = 0

        def complete_json(self, system, user, *, schema_name):
            Real.calls += 1
            return {"real": True}

    L.reset_daily_cap()
    day = ["d1"]
    capped = L.DailyCapLLM(Real(), 2, today=lambda: day[0])
    sentinel = object()

    class Offline:
        model = L.OFFLINE_LABEL
        def complete_json(self, system, user, *, schema_name):
            return sentinel

    capped._offline = Offline()
    assert capped.complete_json("s", "u", schema_name="x") == {"real": True}
    assert capped.complete_json("s", "u", schema_name="x") == {"real": True}
    assert L.model_name(capped) == "gpt-4o-mini"
    assert capped.complete_json("s", "u", schema_name="x") is sentinel
    assert Real.calls == 2 and L.model_name(capped) == L.OFFLINE_LABEL and capped.last_usage is None
    day[0] = "d2"  # 隔天重新計數
    assert capped.complete_json("s", "u", schema_name="x") == {"real": True}
    L.reset_daily_cap()


def test_daily_cap_env(monkeypatch):
    from koushi import llm as L
    monkeypatch.setenv(L.DAILY_CAP_ENV, "500")
    assert L.daily_cap() == 500
    for raw in ("", "0", "abc"):
        monkeypatch.setenv(L.DAILY_CAP_ENV, raw)
        assert L.daily_cap() is None


def test_judge_guide_only_in_judge_mode(jenv, monkeypatch):
    client = jenv["client"]
    resp = _login_judge(client)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/office/guide")
    page = client.get("/office/guide").get_data(as_text=True)
    assert "評審試用導覽" in page and "受保護版本" in page and "每天的工作順序" in page and "模擬寄件匣" in page and "推進一天" in page
    assert W.app.test_client().get("/office/guide").status_code in (302, 401, 403)
    # 一般（非評審）模式：系辦介面維持原樣，沒有導覽連結，網址也不存在
    plain = replace(jenv["cfg"], judge_password=None)
    monkeypatch.setitem(W.app.config, "_web", plain)
    office = W.app.test_client()
    with office.session_transaction() as s:
        s["_csrf_token"] = "t"
    office.post("/login/office", data={"password": "office-pw", "csrf_token": "t"})
    assert "試用導覽" not in office.get("/office/overview").get_data(as_text=True)
    assert office.get("/office/guide").status_code == 404


def test_password_judge_env_still_can_reset(jenv, monkeypatch):
    monkeypatch.delenv(judge.OPEN_ENV, raising=False)
    client = jenv["client"]
    _login_judge(client)
    assert client.post("/office/demo", data={"csrf_token": "t", "action": "reset", "scenario": "ai"}).status_code == 302



def _fresh_client():
    c = W.app.test_client()
    with c.session_transaction() as s:
        s["_csrf_token"] = "t"
    return c


def test_open_judges_get_isolated_sandboxes(jenv, monkeypatch):
    """公開評審環境：每位評審一份示範資料，B 重設或按 AI 緊急停止不影響 A，也不改全域設定。"""
    from koushi import clock
    monkeypatch.setenv(judge.OPEN_ENV, "1")
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "offline")
    base = W.app.config["_web"]
    base_clock = clock.now()
    a, b = _fresh_client(), _fresh_client()
    _login_judge(a, ""); _login_judge(b, "")
    with a.session_transaction() as s:
        sa = s["judge_sandbox"]
    with b.session_transaction() as s:
        sb = s["judge_sandbox"]
    assert sa != sb
    assert a.post("/office/demo", data={"csrf_token": "t", "action": "advance", "days": "7"}).status_code == 302
    a.get("/office/demo")  # 先讓「已推進」提示訊息顯示掉
    a_demo = a.get("/office/demo").get_data(as_text=True)
    # B 重設、按 AI 緊急停止：都允許
    assert b.post("/office/demo", data={"csrf_token": "t", "action": "reset", "scenario": "normal"}).status_code == 302
    assert b.post("/office/ai-switch", data={"csrf_token": "t", "disabled": "1", "reason": "試緊急停止",
                                             "handler": "評審"}).status_code == 302
    assert "AI 已緊急停止" in b.get("/office/overview").get_data(as_text=True)
    # A 不受影響
    assert "AI 已緊急停止" not in a.get("/office/overview").get_data(as_text=True)
    assert a.get("/office/demo").get_data(as_text=True) == a_demo
    # 全域設定與模擬時鐘沒被評審動到
    assert W.app.config["_web"] is base and clock.now() == base_clock
    # 稽核寫在各自的沙盒
    root_a = judge.sandbox_root(base.demo_root, sa)
    assert (root_a / "active.json").is_file()


def test_sandbox_rebuilt_if_missing_and_cleanup(jenv, monkeypatch, tmp_path):
    import shutil
    monkeypatch.setenv(judge.OPEN_ENV, "1")
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "offline")
    a = _fresh_client()
    _login_judge(a, "")
    with a.session_transaction() as s:
        root = judge.sandbox_root(W.app.config["_web"].demo_root, s["judge_sandbox"])
    shutil.rmtree(root)  # 模擬伺服器重啟、沙盒被清掉
    assert a.get("/office/overview").status_code == 200
    assert (root / "active.json").is_file()
    # 清理：超過期限的刪掉
    assert judge.cleanup_sandboxes(W.app.config["_web"].demo_root, now=root.stat().st_mtime + 49 * 3600) >= 1
    assert not root.exists()
    with pytest.raises(ValueError):
        judge.sandbox_root(W.app.config["_web"].demo_root, "../etc")


def test_no_sandbox_in_password_mode(jenv, monkeypatch):
    monkeypatch.delenv(judge.OPEN_ENV, raising=False)
    client = jenv["client"]
    _login_judge(client)
    with client.session_transaction() as s:
        assert "judge_sandbox" not in s



def test_sandboxes_isolated_under_concurrent_threads(jenv, monkeypatch):
    """多執行緒同時處理兩位評審：各自的模擬日期不會互相串（設定與時鐘資料夾都是每個請求各自一份）。"""
    import threading
    from koushi import clock
    monkeypatch.setenv(judge.OPEN_ENV, "1")
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "offline")
    a, b = _fresh_client(), _fresh_client()
    _login_judge(a, ""); _login_judge(b, "")
    a.post("/office/demo", data={"csrf_token": "t", "action": "advance", "days": "7"})
    with a.session_transaction() as s:
        sa = s["judge_sandbox"]

    seen, lock, start = {"a": [], "b": []}, threading.Lock(), threading.Barrier(6)

    def recorder():  # 排在 _enter_judge_sandbox 之後：記下這個請求看到的日期與設定
        if W.g.get("_web") is not None:
            who = "a" if W.session.get("judge_sandbox") == sa else "b"
            with lock:
                seen[who].append((clock.now().date(), W.cfg().demo_root))

    W.app.before_request_funcs.setdefault(None, []).append(recorder)
    errors = []

    def hammer(client):
        try:
            start.wait()
            for _ in range(10):
                assert client.get("/office/demo").status_code == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    try:
        threads = [threading.Thread(target=hammer, args=(c,)) for c in (a, b) * 3]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        W.app.before_request_funcs[None].remove(recorder)
    assert not errors
    assert len(seen["a"]) == 30 and len(seen["b"]) == 30
    assert len(set(seen["a"])) == 1 and len(set(seen["b"])) == 1   # 每位評審每次都看到自己的日期與資料夾
    (day_a, root_a), (day_b, root_b) = seen["a"][0], seen["b"][0]
    assert day_a != day_b and root_a != root_b                      # A 推進過 7 天，B 沒有
    assert clock._request_root.get() is None                         # 請求結束都有還原
