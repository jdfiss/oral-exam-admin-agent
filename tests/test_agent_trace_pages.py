# -*- coding: utf-8 -*-
"""Agent 軌跡查核頁、Agent 指標儀表板、評審模式預設（離線模型、可下載附件4／6 與軌跡 JSON）。

全程假資料：示範資料夾、離線備援模型（不連網）、模擬寄件匣（不寄信）。
"""
import json

import pytest

from koushi import agent_runs, agent_trace, audit_log, demo, judge, llm, runtime
from koushi import mailbox_agent as MA
from koushi.mail import email_sender
from koushi.web import app as W

KEY = f"{demo.TERM}-{demo.SID}"


def _make_env(tmp_path, monkeypatch, *, judge_mode: bool):
    root = tmp_path / ("judge" if judge_mode else "demo")
    monkeypatch.setenv("KOUSHI_DEMO_ROOT", str(root))
    monkeypatch.setenv("KOUSHI_OFFICE_PASSWORD", "office-pw")
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "")   # 讓 monkeypatch 在結束時還原（configure_llm 會直接改環境變數）
    monkeypatch.delenv("KOUSHI_ENV", raising=False)
    if judge_mode:
        monkeypatch.setenv(judge.MODE_ENV, "1")
        monkeypatch.setenv(judge.PASSWORD_ENV, "judge-pw")
    else:
        monkeypatch.delenv(judge.MODE_ENV, raising=False)
    from tests.test_generators_schema4 import _attachment4_template
    template = tmp_path / "official-shape-test.docx"
    _attachment4_template(template)
    monkeypatch.setattr("koushi.config.TPL_ATT4", template)
    real_smtp = []
    monkeypatch.setattr(email_sender, "send_email", lambda **kw: real_smtp.append(kw) or True)
    demo.initialize(root)
    cfg = demo.web_config(root)
    monkeypatch.setitem(W.app.config, "_web", cfg)
    monkeypatch.setitem(W.app.config, "TESTING", True)
    client = W.app.test_client()
    with client.session_transaction() as s:
        s["_csrf_token"] = "t"
    return {"root": root, "cfg": cfg, "client": client, "real_smtp": real_smtp}


@pytest.fixture
def jenv(tmp_path, monkeypatch):
    env = _make_env(tmp_path, monkeypatch, judge_mode=True)
    env["client"].post("/login/judge", data={"password": "judge-pw", "csrf_token": "t"})
    return env


@pytest.fixture
def oenv(tmp_path, monkeypatch):
    env = _make_env(tmp_path, monkeypatch, judge_mode=False)
    env["client"].post("/login/office", data={"password": "office-pw", "csrf_token": "t"})
    return env


def _cfg():
    return W.app.config["_web"]


def _seed_trace(state_dir):
    """一封信（含學號與信箱）＋一筆模型逾時的職稱建議＋專員決定。"""
    common = {"agent": "mailbox", "run_id": "run-mail-1", "tracking_key": ("<m1@example.org>",)}
    agent_trace.trace_step(state_dir, step="parse", tool="mail.inbox.parse_message",
                           input_summary="主旨 4 字", output={"reply_chars": 10}, **common)
    agent_trace.trace_step(state_dir, step="identify_sender", tool="mailbox_agent.identify_sender",
                           input_summary="寄件者 a***@example.org",
                           output={"role": "advisor", "case_keys": [KEY], "email": "advisor@example.org"}, **common)
    agent_trace.trace_step(state_dir, step="analyze", tool="llm.complete_json", model="offline-replay",
                           prompt_version="mailbox-v1", latency_ms=12,
                           output={"category": "question", "summary": f"學號 {demo.SID} 詢問"},
                           usage={"prompt_tokens": 100, "completion_tokens": 20}, **common)
    agent_trace.trace_step(state_dir, step="done", output={"status": "awaiting_staff"}, human_required=True, **common)
    agent_trace.trace_step(state_dir, step="staff_mark", input_summary="handled",
                           output={"actor": "評審", "note": ""}, **common)
    title = {"agent": "title", "run_id": "run-title-1", "tracking_key": "title:客座教授"}
    agent_trace.trace_step(state_dir, step="llm_call", tool="llm.complete_json:title_level", latency_ms=5,
                           model="offline-replay", prompt_version="title-v1",
                           output={"error": "TimeoutError: timeout"}, human_required=True, **title)
    agent_trace.trace_step(state_dir, step="propose", tool="ai_proposals.add", output={"suggestion": None},
                           decision="reference_only", human_required=True, **title)


# ── 遮蔽與整理 ──

def test_sanitize_masks_long_digits_and_emails_recursively():
    out = agent_runs.sanitize({"k": [f"{KEY} 寄給 advisor@example.org", ("12345678",)], "n": 3, "x": None})
    assert out == {"k": [f"{demo.TERM}-{'*' * len(demo.SID)} 寄給 a***@example.org", ["********"]], "n": 3, "x": None}


def test_list_and_get_runs_group_by_task_and_link_staff_decisions(tmp_path):
    _seed_trace(tmp_path)
    runs = agent_runs.list_runs(tmp_path)
    assert {r["agent"] for r in runs} == {"mailbox", "title"}
    mail = next(r for r in runs if r["agent"] == "mailbox")
    assert mail["steps"] == 4 and mail["staff_steps"] == 1 and mail["staff_decision"] == "handled"
    assert mail["final_decision"] == "awaiting_staff" and mail["human_required"] and not mail["has_error"]
    assert mail["model"] == "offline-replay" and mail["prompt_tokens"] == 100 and mail["latency_ms"] == 12
    assert mail["task_id"] == agent_runs.task_id("mailbox", "run-mail-1", ["<m1@example.org>"])
    assert agent_runs.list_runs(tmp_path, has_error=True)[0]["agent"] == "title"
    assert agent_runs.list_runs(tmp_path, agent="title")[0]["errors"] == ["TimeoutError: timeout"]
    assert agent_runs.list_runs(tmp_path, date_from="2999-01-01") == []

    run = agent_runs.get_run(tmp_path, mail["task_id"])
    assert [s["step"] for s in run["steps"]] == ["parse", "identify_sender", "analyze", "done"]
    assert [s["step"] for s in run["staff_decisions"]] == ["staff_mark"]
    text = json.dumps(agent_runs.export_run(run), ensure_ascii=False)
    assert demo.SID not in text and "advisor@example.org" not in text and "m1@example.org" not in text
    assert agent_runs.get_run(tmp_path, "0" * 16) is None


# ── 頁面：空狀態與一般系辦 ──

def test_pages_render_with_empty_state(oenv):
    client = oenv["client"]
    page = client.get("/office/agent-trace").get_data(as_text=True)
    assert "目前還沒有任何 agent 執行紀錄" in page and "Agent 軌跡" in page
    page = client.get("/office/agent-metrics").get_data(as_text=True)
    assert "目前還沒有任何 agent 執行紀錄，指標無法計算" in page and "估計值" in page
    assert client.get("/office/agent-trace?agent=bogus&date_from=x&needs_human=1&has_error=1").status_code == 200
    assert "沒有符合篩選條件" in client.get("/office/agent-trace?needs_human=1").get_data(as_text=True)
    assert client.get("/office/agent-trace/" + "0" * 16).status_code == 404
    assert client.get("/office/agent-trace/not-a-task/export.json").status_code == 404


def test_trace_pages_require_login(tmp_path, monkeypatch):
    env = _make_env(tmp_path, monkeypatch, judge_mode=False)
    for path in ("/office/agent-trace", "/office/agent-metrics"):
        assert env["client"].get(path).status_code == 302


def test_trace_list_detail_metrics_show_masked_content(oenv):
    client, state = oenv["client"], oenv["cfg"].state_dir
    _seed_trace(state)
    page = client.get("/office/agent-trace").get_data(as_text=True)
    assert "信箱 agent" in page and "職稱判讀" in page and "待專員處理" in page and "有錯誤" in page
    assert "m***@example.org" in page and "m1@example.org" not in page
    tid = agent_runs.task_id("mailbox", "run-mail-1", ["<m1@example.org>"])
    detail = client.get(f"/office/agent-trace/{tid}").get_data(as_text=True)
    for word in ("identify_sender", "mail.inbox.parse_message", "offline-replay", "mailbox-v1", "專員決定", "staff_mark"):
        assert word in detail
    assert demo.SID not in detail and "advisor@example.org" not in detail
    metrics = client.get("/office/agent-metrics").get_data(as_text=True)
    assert "人工介入率" in metrics and "模型失敗→轉人工" in metrics and "逾時 1" in metrics
    assert "100.0%" in metrics and "2026-09" in metrics   # 價目核對日期
    assert "人工介入率：" in metrics   # 每個指標都附定義


# ── 評審模式 ──

def test_judge_can_view_trace_and_metrics_and_export_run_json(jenv):
    client, state = jenv["client"], jenv["cfg"].state_dir
    assert client.get("/office/agent-trace").status_code == 200
    assert client.get("/office/agent-metrics").status_code == 200
    _seed_trace(state)
    tid = agent_runs.task_id("title", "run-title-1", ["title:客座教授"])
    assert client.get(f"/office/agent-trace/{tid}").status_code == 200
    response = client.get(f"/office/agent-trace/{tid}/export.json")
    assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"]
    data = json.loads(response.get_data(as_text=True))
    assert data["format"] == agent_runs.EXPORT_FORMAT and data["summary"]["agent"] == "title"
    assert [s["step"] for s in data["steps"]] == ["llm_call", "propose"]
    [event] = [e for e in audit_log.load(state / "audit_log.json").by_source("judge") if e.action == "export_trace"]
    assert event.detail["task_id"] == tid and event.detail["actor"] == judge.ACTOR


def test_judge_banner_shows_active_model_and_switch_scope(jenv, monkeypatch):
    client = jenv["client"]
    assert "模型：未設定（全部轉人工）" in client.get("/office/overview").get_data(as_text=True)
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "offline")
    assert "模型：離線備援（規則替身）" in client.get("/office/overview").get_data(as_text=True)
    settings = client.get("/office/ai-settings").get_data(as_text=True)
    assert "只影響本輪示範資料" in settings


def test_judge_offline_flow_produces_linked_traces(jenv, monkeypatch):
    """評審模式＋離線備援：信箱 agent 真的跑出分類與軌跡，各頁「查看完整軌跡」連得到。"""
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "offline")
    client, cfg = jenv["client"], jenv["cfg"]
    client.post("/office/demo", data={"action": "advance", "days": "1", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    client.post(f"/office/case/{KEY}/clear", data={"handler": "x", "note": "放行", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    advisor_mail = next(m for m in judge.outbox(cfg.state_dir) if m["to"] == demo.MAILBOXES["advisor"])
    client.post("/office/judge/outbox/reply", data={"outbox_id": advisor_mail["id"],
                                                    "text": "請問口試當天有投影機嗎？", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    [record] = MA.load_mails(cfg.state_dir)
    assert not record.get("llm_error")
    tid = agent_runs.task_id("mailbox", record["run_id"], [record["message_id"]])
    mailbox_page = client.get("/office/mailbox").get_data(as_text=True)
    assert f"/office/agent-trace/{tid}" in mailbox_page and "AI 處理紀錄" in mailbox_page
    detail = client.get(f"/office/agent-trace/{tid}").get_data(as_text=True)
    assert "offline-replay" in detail and "analyze" in detail
    exported = client.get(f"/office/agent-trace/{tid}/export.json").get_data(as_text=True)
    assert demo.SID not in exported and demo.MAILBOXES["advisor"] not in exported
    metrics = client.get("/office/agent-metrics").get_data(as_text=True)
    assert "信箱 agent" in metrics and "offline-replay" in metrics
    assert jenv["real_smtp"] == []


def test_ai_proposals_page_links_full_trace(oenv):
    from koushi import ai_proposals as P
    state = oenv["cfg"].state_dir
    saved = P.add(state, {"agent": "title", "subject_key": "title:客座教授", "subject": "客座教授", "run_id": "r-t",
                          "count": 1, "rule_reason": "x", "suggestion": None, "confidence": None, "rationale": "",
                          "method": "none", "model": None, "prompt_version": None, "error": ""})
    agent_trace.trace_step(state, agent="title", run_id="r-t", step="propose", tracking_key="title:客座教授",
                           tool="ai_proposals.add", output={"id": saved["id"]}, decision="reference_only")
    tid = agent_runs.task_id("title", "r-t", ["title:客座教授"])
    page = oenv["client"].get("/office/ai-proposals").get_data(as_text=True)
    assert f"/office/agent-trace/{tid}" in page
    assert oenv["client"].get(f"/office/agent-trace/{tid}").status_code == 200


def test_judge_can_download_generated_documents_but_not_archives_or_exports(jenv):
    client, cfg = jenv["client"], jenv["cfg"]
    client.post("/office/demo", data={"action": "advance", "days": "1", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    client.post(f"/office/case/{KEY}/clear", data={"handler": "x", "note": "放行", "csrf_token": "t"})
    client.post("/office/documents/generate", data={"phase": "pre-exam", "csrf_token": "t"})   # 附件4
    client.post("/office/documents/generate", data={"phase": "preview", "csrf_token": "t"})    # 附件6（預覽版）
    status = json.loads((cfg.doc_output_root / "documents_status.json").read_text(encoding="utf-8"))
    files = [("pre-exam", name) for name in status["pre-exam"]["attachment4_files"]]
    files += [("preview", name) for name in status["preview"]["attachment6_files"]]
    assert any("附件4" in name for _, name in files) and any(phase == "preview" for phase, _ in files)
    for phase, name in files:
        response = client.get(f"/office/documents/download/{phase}/{name}")
        assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"], name
    downloads = [(e.detail["phase"], e.detail["filename"]) for e in
                 audit_log.load(cfg.state_dir / "audit_log.json").by_source("judge") if e.action == "download_document"]
    assert downloads == files   # 評審每次下載都留稽核
    for path in ("/office/overview/export", "/office/communications/export",
                 f"/office/archives/{demo.TERM}/files/x.json", f"/office/archives/{demo.TERM}/manifest"):
        assert client.get(path).status_code == 403, path
    assert client.post("/office/upload-registries", data={"csrf_token": "t"}).status_code == 403


# ── 評審模式的模型預設 ──

def test_configure_llm_defaults_to_offline_without_key(monkeypatch):
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert judge.configure_llm() == llm.OFFLINE_LABEL == "離線備援（規則替身）"
    assert llm.make_llm().model_name == "offline-replay"


def test_configure_llm_keeps_real_model_when_key_exists_unless_forced(monkeypatch):
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")   # 不會發出任何請求
    assert judge.configure_llm() == "gpt-4o-mini（openai）"
    assert not llm.offline_selected()
    assert judge.configure_llm(force_offline=True) == llm.OFFLINE_LABEL
    assert llm.make_llm().model_name == "offline-replay"
    classifier = runtime.make_classifier()     # 強制離線時回信判讀器也不連外：改用離線備援規則
    assert getattr(classifier, "offline", False)


def test_configure_llm_respects_explicit_provider(monkeypatch):
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "offline")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert judge.configure_llm() == llm.OFFLINE_LABEL


def test_eval_wrapper_reuses_core_metrics():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))
    import agent_metrics as wrapper
    from koushi import agent_metrics as core
    assert wrapper.compute is core.compute and wrapper.DEFAULT_PRICES is core.DEFAULT_PRICES
