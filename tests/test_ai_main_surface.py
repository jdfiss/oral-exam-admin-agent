# -*- coding: utf-8 -*-
"""AI 進主畫面：每日自動化跑提案型 agent、工作總覽「AI 今日動態／待你決定」、案件頁「AI 協作」。

全程假模型（或離線備援），不連網、不寄信。
"""
import threading

import pytest

from koushi import ai_proposals as P, ai_switch, application, audit_log, clock, correction_agent, demo, ingest
from koushi import mailbox_agent as MA, title_agent, util as U
from koushi.cli import _print_proposals
from koushi.offline_llm import make_offline_llm
from koushi.web import app as W

from tests.test_mailbox_agent import mail
from tests.test_proposal_agents import FakeLLM, _build


def _answers():
    return {"affiliation_match": {"match_id": "D1", "confidence": 0.9, "rationale": "同校同系"},
            "source_correction": {"action": "none", "rationale": "原值即可"},
            "title_level": {"level": title_agent.LEVELS[0], "confidence": 0.6, "rationale": "依職稱"}}


# ── 提案型 agent（單元）──

def test_proposal_agents_create_pending_and_dedupe(tmp_path):
    result, state = _build(tmp_path)
    registries = type("R", (), {"distances": tmp_path / "distances.csv"})()
    llm = FakeLLM(_answers())
    created, notes = application.run_proposal_agents(master=result.master, state_dir=state, llm=llm,
                                                     registries=registries)
    assert notes == [] and created
    assert {p["agent"] for p in created} >= {"affiliation"}
    assert all(p["status"] == P.PENDING for p in created)
    calls = len(llm.calls)
    again, _ = application.run_proposal_agents(master=result.master, state_dir=state, llm=llm,
                                               registries=registries)
    assert again == [] and len(llm.calls) == calls   # 已有待決建議：不重問模型、不重複新增


def test_proposal_agents_respect_switches_and_missing_model(tmp_path):
    result, state = _build(tmp_path)
    llm = FakeLLM(_answers())
    assert application.run_proposal_agents(master=result.master, state_dir=state, llm=None) == ([], [])
    P.save_settings(state, {name: False for name in P.AGENTS}, actor="專員甲")
    assert application.run_proposal_agents(master=result.master, state_dir=state, llm=llm) == ([], [])
    P.save_settings(state, {name: True for name in P.AGENTS}, actor="專員甲")
    ai_switch.set_disabled(state, disabled=True, actor="專員甲", reason="測試緊急停止")
    assert application.run_proposal_agents(master=result.master, state_dir=state, llm=llm) == ([], [])
    assert llm.calls == [] and P.load(state) == []


def test_one_proposal_agent_failure_is_isolated_and_audited(tmp_path, monkeypatch):
    result, state = _build(tmp_path)
    ran = []
    monkeypatch.setattr(correction_agent, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("資料檔損毀")))
    monkeypatch.setattr(title_agent, "run", lambda *a, **k: ran.append("title") or [])
    registries = type("R", (), {"distances": tmp_path / "distances.csv"})()
    created, notes = application.run_proposal_agents(master=result.master, state_dir=state,
                                                     llm=FakeLLM(_answers()), registries=registries)
    assert ran == ["title"]
    assert any("來源異常修正" in n and "本輪略過" in n and "資料檔損毀" in n for n in notes)
    [event] = audit_log.load(state / "audit_log.json").by_source("ai_proposals")
    assert event.action == "run_failed" and event.detail["agent"] == "correction"


def test_cli_prints_proposal_counts(capsys):
    _print_proposals(())
    assert "AI 建議：本輪沒有新建議" in capsys.readouterr().out
    _print_proposals(({"agent": "correction", "error": ""}, {"agent": "title", "error": "Timeout"}))
    out = capsys.readouterr().out
    assert "新增 2 筆" in out and "來源異常修正建議 1" in out and "1 筆模型失敗" in out


# ── 每日自動化（示範情境）──

@pytest.fixture
def demo_cfg(tmp_path, monkeypatch):
    from tests.test_generators_schema4 import _attachment4_template
    root = tmp_path / "demo"
    template = tmp_path / "a4.docx"
    _attachment4_template(template)
    monkeypatch.setattr("koushi.config.TPL_ATT4", template)
    monkeypatch.setenv("KOUSHI_DEMO_ROOT", str(root))
    monkeypatch.setenv("KOUSHI_OFFICE_PASSWORD", "pw")
    demo.initialize(root)
    cfg = demo.web_config(root)
    clock.advance(root)
    return cfg


def _automate(cfg, llm, mails=None):
    return application.run_daily_automation(
        submission_paths=ingest.student_submission_paths(cfg.submissions_dir), master_path=cfg.master_path,
        state_dir=cfg.state_dir, registries=cfg.build_registries, submissions_dir=cfg.submissions_dir,
        send=False, date=clock.today(), now=clock.now(), templates=cfg.templates, llm=llm,
        fetch_replies=(lambda: list(mails)) if mails is not None else None)


class LockCheckingLLM(FakeLLM):
    """每次被呼叫時，從另一個執行緒確認全域狀態鎖沒有被佔住。"""

    def __init__(self, state_dir, responses):
        super().__init__(responses)
        self.state_dir = state_dir
        self.lock_free = []

    def complete_json(self, system, user, *, schema_name):
        result = {}

        def probe():
            try:
                with U.state_lock(self.state_dir, timeout=0.2):
                    result["free"] = True
            except U.BuildLockTimeout:
                result["free"] = False
        worker = threading.Thread(target=probe)
        worker.start()
        worker.join()
        self.lock_free.append(result["free"])
        return super().complete_json(system, user, schema_name=schema_name)


def test_daily_automation_runs_proposal_agents_outside_state_lock(demo_cfg, monkeypatch):
    # 測接線而非篩選：示範情境的來源異常屬「確認同一人」類，正式篩選會略過，這裡放行讓它產生建議
    monkeypatch.setattr(correction_agent, "NOT_CORRECTABLE", frozenset())
    llm = LockCheckingLLM(demo_cfg.state_dir, _answers())
    result = _automate(demo_cfg, llm)
    assert result.proposals and all(p["status"] == P.PENDING for p in result.proposals)
    assert {p["id"] for p in result.proposals} == {p["id"] for p in P.pending(demo_cfg.state_dir)}
    assert llm.lock_free and all(llm.lock_free)
    # 第二輪：同一主題已有待決建議，不再新增、不再問模型
    calls = len(llm.calls)
    assert _automate(demo_cfg, llm).proposals == ()
    assert len(llm.calls) == calls


def test_daily_automation_without_model_or_when_stopped_creates_no_proposals(demo_cfg):
    assert _automate(demo_cfg, None).proposals == ()
    ai_switch.set_disabled(demo_cfg.state_dir, disabled=True, actor="專員甲", reason="測試")
    llm = FakeLLM(_answers())
    result = _automate(demo_cfg, llm)
    assert result.proposals == () and llm.calls == []
    assert P.load(demo_cfg.state_dir) == []


def test_daily_automation_survives_proposal_failure(demo_cfg, monkeypatch):
    def boom(*a, **k):
        raise OSError("磁碟錯誤")
    monkeypatch.setattr(correction_agent, "run", boom)
    result = _automate(demo_cfg, FakeLLM(_answers()))
    assert result.phase and result.tracking is not None
    assert any("AI 建議（來源異常修正建議）本輪略過" in n for n in result.notes)


# ── 網頁：工作總覽與案件頁 ──

def _client(cfg, monkeypatch):
    monkeypatch.setitem(W.app.config, "_web", cfg)
    monkeypatch.setitem(W.app.config, "TESTING", True)
    client = W.app.test_client()
    with client.session_transaction() as s:
        s["_csrf_token"] = "t"
    client.post("/login/office", data={"password": "pw", "csrf_token": "t"})
    return client


def test_overview_and_case_panel_empty_state(demo_cfg, monkeypatch):
    client = _client(demo_cfg, monkeypatch)
    page = client.get("/office/overview").get_data(as_text=True)
    assert "待辦清單" in page and "AI 今日動態" in page
    assert "未設定（全部轉人工）" in page and "運作中" not in page
    assert "今天 agent 尚未處理來信或產生建議" in page
    case_key = next(p.stem for p in demo_cfg.submissions_dir.glob("*.json"))
    panel = client.get(f"/office/case/{case_key}").get_data(as_text=True)
    assert "AI 協作" in panel and "本案目前沒有 AI 處理紀錄" in panel


def test_overview_and_case_panel_populated(demo_cfg, monkeypatch):
    monkeypatch.setattr(correction_agent, "NOT_CORRECTABLE", frozenset())   # 測畫面而非篩選
    advisor_mail = mail("<m1@example.org>", demo.MAILBOXES["advisor"],
                        "口試地點請改到 S1-A204，其餘無誤。", subject="口試一覽表修改")
    result = _automate(demo_cfg, make_offline_llm(), mails=[advisor_mail])
    [record] = result.mailbox
    assert result.proposals
    case_key = record["link"]["case_keys"][0]
    client = _client(demo_cfg, monkeypatch)
    ai_switch.set_disabled(demo_cfg.state_dir, disabled=True, actor="專員甲", reason="展示停用")
    page = client.get("/office/overview").get_data(as_text=True)
    assert "已關閉（緊急停止）" in page and "AI 已停止（全部轉人工）" in page
    ai_switch.set_disabled(demo_cfg.state_dir, disabled=False, actor="專員甲", reason="恢復")
    page = client.get("/office/overview").get_data(as_text=True)
    assert "今天 agent 尚未處理" not in page
    assert "信箱 agent 分析來信" in page and MA.CATEGORY_LABELS[record["analysis"]["category"]] in page
    assert "新增 AI 建議" in page and "來源異常修正" in page
    assert "/office/ai-proposals" in page and "status=awaiting_staff" in page and "has_error=1" in page

    panel = client.get(f"/office/case/{case_key}").get_data(as_text=True)
    assert "相關來信 1" in panel and W._rename_attachments(record["analysis"]["summary"]) in panel
    assert "/office/mailbox#mail-" in panel and "查看完整軌跡" in panel
    for action in record["actions"]:
        if action["type"] != MA.A_NO_ACTION:
            assert MA.ACT_STATUS_LABELS[action["status"]] in panel
    assert "@" not in panel.split("AI 協作")[1].split("</section>")[0]
    # 軌跡連結可開
    import re
    for tid in set(re.findall(r"/office/agent-trace/([0-9a-f]{16})", panel)):
        assert client.get(f"/office/agent-trace/{tid}").status_code == 200

    # 本案的來源異常修正建議出現在案件頁，並連到 AI 建議頁的錨點
    correction = next((p for p in result.proposals if p["agent"] == "correction"), None)
    if correction is not None:
        other = client.get(f"/office/case/{correction['case_key']}").get_data(as_text=True)
        assert f"#proposal-{correction['id']}" in other
        assert f'id="proposal-{correction["id"]}"' in client.get("/office/ai-proposals").get_data(as_text=True)
