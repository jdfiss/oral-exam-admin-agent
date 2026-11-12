"""Run the field-trial entry point in a clean process, including the real approval routes."""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_trial_tasks_can_be_completed_without_network(tmp_path):
    script = r'''
import json, socket, sys
from pathlib import Path
from koushi import trial
trial.configure_environment()
def deny_network(*args, **kwargs):
    raise AssertionError("Field trial must not access the network")
socket.socket.connect = deny_network
from koushi import config as C
official = Path(sys.argv[1]) / "official.doc"
official.write_bytes(b"official sentinel: never read or overwrite")
C.TPL_ATT4 = C.TPL_ATT4_OFFICIAL = official
cfg = trial.prepare(Path(sys.argv[1]) / "trial")
assert trial.check_ready(cfg)["ok"]
assert official.read_bytes() == b"official sentinel: never read or overwrite"
assert cfg.templates.attachment4.is_relative_to(cfg.demo_root)
assert cfg.templates.attachment6.is_relative_to(cfg.demo_root)
from koushi import application, ai_proposals as P, ai_switch, demo, mailbox_agent as MA
from koushi.web import app as W
client = W.app.test_client()
with client.session_transaction() as session:
    session["_csrf_token"] = "t"
login_page = client.get("/").get_data(as_text=True)
assert "系辦登入" in login_page and "評審" not in login_page
assert client.post("/login/judge", data={"password": cfg.judge_password, "csrf_token": "t"}).status_code == 404
client.post("/login/office", data={"password": cfg.office_password, "csrf_token": "t"})
for page in ("/office/overview", "/office/settings", "/office/judge/outbox", "/office/demo",
             "/office/communications", "/office/import", "/office/ai-proposals", "/office/archives"):
    html = client.get(page).get_data(as_text=True)
    assert "評審" not in html, page
mailbox = client.get("/office/mailbox").get_data(as_text=True)
for badge in ("疑似夾帶指令", "已遮蔽且未送模型", "AI 判讀失敗"):
    assert badge in mailbox, badge
assert "T100000099" not in mailbox and "000123409999" not in mailbox
def post(path, **data):
    response = client.post(path, data={**data, "csrf_token": "t", "handler": "試用者"})
    assert response.status_code == 302, (path, response.status_code)
master = application.load_current_master(cfg.master_path)
old_date = master.exams[0].口試日期
mail, action = next((m, a) for m in MA.load_mails(cfg.state_dir) for a in m["actions"]
                    if a["type"] == MA.A_APPLY_CORRECTION and a["status"] == "pending")
new_date = next(c["after"] for c in action["params"]["changes"] if c["status"] == "mapped")
assert str(old_date) != new_date
post("/office/mailbox/approve-all", message_id=mail["message_id"])
mail = next(m for m in MA.load_mails(cfg.state_dir) if m["message_id"] == mail["message_id"])
assert all(a["status"] != "pending" for a in mail["actions"]), mail["actions"]
assert any(a["type"] == "reply_draft" and a["status"] == "done" for a in mail["actions"])
master = application.load_current_master(cfg.master_path)
assert str(master.exams[0].口試日期) == new_date
post("/office/demo", action="send")
assert application.load_automation_status(cfg.state_dir)["ok"]
proposal = next(p for p in P.pending(cfg.state_dir, "affiliation") if not p.get("error"))
post(f"/office/ai-proposals/{proposal['id']}/decide", action="approve")
assert P.get(cfg.state_dir, proposal["id"])["status"] == P.APPROVED
master = application.load_current_master(cfg.master_path)
external = next(m for e in master.exams for m in e.委員 if m.委員姓名 == "模擬校外委員")
assert external.交通費 == 180
post("/office/ai-switch", disabled="1", reason="試用緊急停止")
assert ai_switch.is_disabled(cfg.state_dir)
answer = client.post("/office/assistant/ask", data={"question": "附件4 已經寄給哪些教授？", "csrf_token": "t"})
assert answer.status_code == 403
first_run = cfg.state_dir
fresh = trial.prepare(cfg.demo_root)
assert fresh.state_dir != first_run and first_run.is_dir()
assert not ai_switch.is_disabled(fresh.state_dir)
assert trial.check_ready(fresh)["ok"]
assert official.read_bytes() == b"official sentinel: never read or overwrite"
print(json.dumps({"ok": True, "tasks": ["date_approval", "resend", "affiliation_approval", "emergency_stop", "fresh_restart", "safety_mails"]}))
'''
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(tmp_path)],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.strip())["ok"]


def test_trial_cli_check_and_refuse_existing_unmarked_data(tmp_path):
    root = tmp_path / "trial"
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "koushi.trial", "--check", "--root", str(root)],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((root / "trial-check.json").read_text(encoding="utf-8"))
    assert report["ok"] and report["model"] == "offline-replay"
    assert {"affiliation", "title"} <= set(report["pending_agents"])
    assert report["pending_corrections"] >= 1
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "koushi.trial", "--check", "--root", str(existing)],
                            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode != 0 and "示範資料夾" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_trial_refuses_production_before_creating_data(tmp_path):
    root = tmp_path / "trial"
    result = subprocess.run([sys.executable, "-X", "utf8", "-m", "koushi.trial", "--check", "--root", str(root)],
                            cwd=ROOT, env={**os.environ, "KOUSHI_ENV": "production"},
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode != 0 and "正式環境" in result.stderr
    assert not root.exists()


def test_live_recording_reads_only_model_keys(tmp_path, monkeypatch):
    import pytest
    from koushi import trial
    from koushi.mail import email_sender
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-test\nKOUSHI_UNIV_NAME=真實大學\nMAIL_PASSWORD=secret\n", encoding="utf-8")
    monkeypatch.setattr(email_sender, "ENV_PATHS", (env,))
    for key in trial.LIVE_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert trial.live_model_env() == {"OPENAI_API_KEY": "sk-test"}
    env.write_text("KOUSHI_UNIV_NAME=真實大學\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        trial.live_model_env()


def test_live_recording_accepts_gemini_key(tmp_path, monkeypatch):
    """免費的 Gemini 金鑰也能錄真模型（之前 --live 只認 OPENAI_API_KEY，還會刪掉 GEMINI_API_KEY）。"""
    from koushi import trial
    from koushi.mail import email_sender
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=AIza-test\nMAIL_PASSWORD=secret\n", encoding="utf-8")
    monkeypatch.setattr(email_sender, "ENV_PATHS", (env,))
    for key in trial.LIVE_KEYS:
        monkeypatch.delenv(key, raising=False)
    assert trial.live_model_env() == {"GEMINI_API_KEY": "AIza-test"}


def test_fault_wrappers_forward_real_token_usage():
    from koushi.llm import LLMError
    from koushi.offline_llm import FlakyLLM, SloppyOnceLLM

    class Inner:
        model_name, last_usage = "gpt-test", {"prompt_tokens": 10, "completion_tokens": 2}

        def complete_json(self, system, user, *, schema_name):
            return {"evidence": ["x"]}

    llm = FlakyLLM(SloppyOnceLLM(Inner(), marker="投影機"), fail_marker="停車")
    llm.complete_json("s", "一般來信", schema_name="mailbox_analysis")
    assert llm.last_usage == Inner.last_usage and llm.model_name == "gpt-test"
    try:
        llm.complete_json("s", "停車", schema_name="mailbox_analysis")
    except LLMError:
        pass
    assert llm.last_usage is None  # 模擬逾時那次不沿用上一次的用量
