# -*- coding: utf-8 -*-
"""AI 總開關（緊急停止）：一個開關停掉所有 LLM 呼叫與 agent 自動動作，退回人工流程。

全程假 LLM、假 transport、假資料；任何模型被呼叫都會讓測試失敗。
"""
import json

import pytest

from koushi import advisor_confirmation as AC
from koushi import ai_switch, application, audit_log, llm as llm_module, runtime
from koushi import mailbox_agent as MA
from koushi import settings as office_settings
from koushi.classifier import ClassificationResult
from koushi.web import app as W

from tests.test_advisor_confirmation import _sent_and_reply, env  # noqa: F401
from tests.test_mailbox_agent import DAY2, by_type, mail


class ExplodingLLM:
    """被呼叫就讓測試失敗的假模型。"""
    model_name = "must-not-be-called"

    def __init__(self):
        self.calls = 0

    def complete_json(self, system, user, *, schema_name):
        self.calls += 1
        raise AssertionError("AI 已緊急停止，不應呼叫模型")


def _stop(state, **kwargs):
    return ai_switch.set_disabled(state, disabled=True, actor=kwargs.get("actor", "專員甲"),
                                  reason=kwargs.get("reason", "疑似提示注入，先停用"))


# ── 開關本身 ──

def test_default_enabled_and_toggle_is_audited_with_handler_and_reason(tmp_path):
    assert not ai_switch.is_disabled(tmp_path)
    _stop(tmp_path)
    reason = ai_switch.disabled_reason(tmp_path)
    assert "專員甲" in reason and "疑似提示注入" in reason
    ai_switch.set_disabled(tmp_path, disabled=False, actor="專員乙", reason="已排除問題")
    assert not ai_switch.is_disabled(tmp_path)
    events = audit_log.load(tmp_path / "audit_log.json").by_source("ai_switch")
    assert [(e.action, e.detail["actor"], e.detail["reason"]) for e in events] == [
        ("disabled", "專員甲", "疑似提示注入，先停用"), ("enabled", "專員乙", "已排除問題")]


@pytest.mark.parametrize("actor,reason", [("", "原因"), ("專員甲", "  ")])
def test_toggle_requires_handler_and_reason(tmp_path, actor, reason):
    with pytest.raises(ValueError):
        ai_switch.set_disabled(tmp_path, disabled=True, actor=actor, reason=reason)
    assert not ai_switch.is_disabled(tmp_path)
    assert not (tmp_path / "audit_log.json").exists()


def test_corrupt_switch_file_fails_closed(tmp_path):
    (tmp_path / ai_switch.FILENAME).write_text("{not json", encoding="utf-8")
    assert ai_switch.is_disabled(tmp_path)


def test_env_override_disables_everywhere(tmp_path, monkeypatch):
    monkeypatch.setenv(ai_switch.ENV, "1")
    assert "KOUSHI_AI_DISABLED" in ai_switch.disabled_reason(tmp_path)
    assert ai_switch.is_disabled()  # 不帶狀態目錄也生效


# ── 集中式關閉：make_llm／make_classifier ──

def test_make_llm_and_classifier_return_none_when_stopped(tmp_path, monkeypatch):
    built = []
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-never-used")
    monkeypatch.setattr(llm_module, "OpenAIStructuredLLM", lambda **kw: built.append(kw) or "llm")
    monkeypatch.setattr("koushi.adapters.openai_adapter.OpenAIAdapter", lambda **kw: built.append(kw) or object())
    assert llm_module.make_llm(tmp_path).primary == "llm"  # 未停止時照常建立（外層是重試／備援）
    assert runtime.make_classifier(tmp_path) is not None
    built.clear()
    _stop(tmp_path)
    assert llm_module.make_llm(tmp_path) is None
    assert runtime.make_mailbox_llm(tmp_path) is None
    assert runtime.make_classifier(tmp_path) is None
    assert built == []  # 連用戶端都不建立



def test_stop_overrides_offline_provider(tmp_path, monkeypatch):
    """離線備援也是 AI：總開關關閉時一樣不建立。"""
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "offline")
    assert llm_module.make_llm(tmp_path) is not None
    _stop(tmp_path)
    assert llm_module.make_llm(tmp_path) is None

def test_make_llm_without_state_dir_uses_registered_web_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-never-used")
    monkeypatch.setattr(llm_module, "OpenAIStructuredLLM", lambda **kw: "llm")
    monkeypatch.setattr(ai_switch, "_state_dir_provider", lambda: tmp_path)
    assert llm_module.make_llm().primary == "llm"
    _stop(tmp_path)
    assert llm_module.make_llm() is None


# ── agent 退回人工 ──

def test_mailbox_agent_never_calls_model_and_auto_actions_become_suggestions(env):
    office_settings.save(office_settings.OfficeSettings(mailbox_auto_reply_enabled=True), env["state"])
    reply = _sent_and_reply(env, body="請問可以改期嗎？")
    sent_before = len(env["sent"])
    # 判讀在停止前完成（收件匣記為「提問」），信箱 agent 在停止後才執行：仍不得自動回信
    application.ingest_advisor_replies(
        master=env["master"], state_dir=env["state"], mails=[reply], date=DAY2,
        classifier=lambda m, r: ClassificationResult(intent="question", evidence=m.reply_text,
                                                     model="fake", prompt_version="t"))
    _stop(env["state"])
    fake = ExplodingLLM()
    [record] = application.run_mailbox_agent(master=env["master"], state_dir=env["state"], mails=[reply],
                                             llm=fake, send=True, transport=env["transport"], date=DAY2)
    assert fake.calls == 0
    assert record["analysis"]["summary"].startswith("AI 總開關已關閉")
    assert not [a for a in record["actions"] if a["policy"] == MA.AUTO]
    ack = by_type(record, MA.A_ACK)[0]
    assert ack["policy"] == MA.SUGGEST and "AI 總開關" in ack["policy_reason"]
    assert len(env["sent"]) == sent_before and record["status"] == MA.MAIL_AWAITING
    traces = MA.traces_by_run(env["state"])[record["run_id"]]
    assert any(t.get("tool") == "skipped_ai_stopped" for t in traces)


def test_advisor_reply_classifier_skipped_so_no_auto_confirm(env):
    office_settings.save(office_settings.OfficeSettings(auto_confirm_enabled=True, auto_confirm_shadow=False,
                                                        auto_confirm_whitelist=("教授甲",)), env["state"])
    reply = _sent_and_reply(env)
    _stop(env["state"])

    def classifier(m, r):
        raise AssertionError("AI 已緊急停止，不應判讀")
    application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[reply],
                                       classifier=classifier)
    [item] = AC.load_inbox(env["state"])
    assert item.status == AC.INBOX_PENDING and item.intent is None


# ── 網頁：橫幅、切換、問答助理 ──

@pytest.fixture
def office(tmp_path, monkeypatch):
    from koushi.workflow import RegistryPaths
    cfg = W.WebConfig(submissions_dir=tmp_path / "subs", master_path=tmp_path / "out" / "master.xlsx",
                      state_dir=tmp_path / "state", doc_output_root=tmp_path / "docs", office_password="pw",
                      registries=RegistryPaths(attachment2=tmp_path / "a2.xlsx", members=tmp_path / "m.xlsx",
                                               distances=tmp_path / "d.csv"))
    monkeypatch.setitem(W.app.config, "_web", cfg)
    monkeypatch.setitem(W.app.config, "TESTING", True)
    client = W.app.test_client()
    with client.session_transaction() as s:
        s["_csrf_token"] = "t"
    client.post("/login/office", data={"password": "pw", "csrf_token": "t"})
    return client, cfg


def test_web_toggle_shows_banner_and_is_audited(office):
    client, cfg = office
    assert "AI 總開關已關閉" not in client.get("/office/overview").get_data(as_text=True)
    response = client.post("/office/ai-switch", data={"disabled": "1", "handler": "專員甲", "reason": "模型異常",
                                                      "csrf_token": "t"})
    assert response.status_code == 302 and ai_switch.is_disabled(cfg.state_dir)
    for page in ("/office/overview", "/office/mailbox", "/office/settings", "/office/ai-proposals"):
        assert "AI 總開關已關閉（緊急停止）" in client.get(page).get_data(as_text=True)
    # 缺原因不能恢復
    client.post("/office/ai-switch", data={"disabled": "0", "handler": "專員甲", "reason": "", "csrf_token": "t"})
    assert ai_switch.is_disabled(cfg.state_dir)
    client.post("/office/ai-switch", data={"disabled": "0", "handler": "專員乙", "reason": "已恢復", "csrf_token": "t"})
    assert not ai_switch.is_disabled(cfg.state_dir)
    events = audit_log.load(cfg.state_dir / "audit_log.json").by_source("ai_switch")
    assert [(e.action, e.detail["actor"]) for e in events] == [("disabled", "專員甲"), ("enabled", "專員乙")]


def test_assistant_does_not_call_model_when_stopped(office, monkeypatch):
    client, cfg = office
    fake = ExplodingLLM()
    monkeypatch.setattr(llm_module, "make_llm", lambda *a, **k: fake)  # 即使取得模型的路徑被替換也擋得住
    _stop(cfg.state_dir)
    response = client.post("/office/assistant/ask", data={"question": "有幾件逾期？", "csrf_token": "t"})
    assert response.status_code == 403 and "AI 總開關已關閉" in response.get_data(as_text=True)
    assert "AI 總開關已關閉" in client.get("/office/assistant").get_data(as_text=True)
    assert fake.calls == 0


def test_daily_automation_skips_all_model_use_when_stopped(tmp_path, monkeypatch):
    from koushi import clock, demo, ingest
    from tests.test_generators_schema4 import _attachment4_template
    root = tmp_path / "demo"
    template = tmp_path / "a4.docx"
    _attachment4_template(template)
    monkeypatch.setattr("koushi.config.TPL_ATT4", template)
    monkeypatch.setenv("KOUSHI_DEMO_ROOT", str(root))
    monkeypatch.setenv("KOUSHI_OFFICE_PASSWORD", "x")
    demo.initialize(root)
    cfg = demo.web_config(root)
    clock.advance(root)
    _stop(cfg.state_dir)
    fake, sent = ExplodingLLM(), []

    def classifier(m, r):
        raise AssertionError("AI 已緊急停止，不應判讀")
    stray = mail("<x1>", demo.MAILBOXES["advisor"], "請問口試幾點開始？")
    result = application.run_daily_automation(
        submission_paths=ingest.student_submission_paths(cfg.submissions_dir), master_path=cfg.master_path,
        state_dir=cfg.state_dir, registries=cfg.build_registries, submissions_dir=cfg.submissions_dir,
        send=True, transport=lambda **kw: sent.append(kw) or True, date=clock.today(), now=clock.now(),
        templates=cfg.templates, fetch_replies=lambda: [stray], classifier=classifier, llm=fake)
    assert fake.calls == 0
    assert any("AI 總開關已關閉" in note for note in result.notes)
    assert result.proposals == ()   # 提案型 agent 也不跑
    [record] = result.mailbox
    assert record["status"] == MA.MAIL_AWAITING
    assert not [a for a in record["actions"] if a["policy"] == MA.AUTO]
    assert json.loads((cfg.state_dir / ai_switch.FILENAME).read_text(encoding="utf-8"))["disabled"] is True
