# -*- coding: utf-8 -*-
"""錄影用的展示腳本（python -m show）不能再因 koushi API 改動而默默壞掉。

- test_show_demo_flow_in_process：一律執行。十五個步驟照實際程式跑，只替換「寫出附件4／6
  檔案」那一段（正式範本不進版控、附件4 需要 LibreOffice），納入／排除案件的關卡邏輯照跑。
  步驟 11-16 的 agent 用離線備援模型（koushi/offline_llm.py），不連網、不寄信。
- test_show_demo_cli_with_real_templates：本機有正式範本與 LibreOffice 時，以子程序完整跑
  `python -m show --no-pause`；缺任一項就略過。
"""
from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from koushi import config as C
from koushi import affiliation_agent, document_conversion

# 真實校名（取縮寫種子表裡的大學名）不得出現在錄影畫面與產出檔
REAL_SCHOOLS = [s for s in affiliation_agent._KNOWN_SCHOOLS if s.endswith("大學")]

ROOT = Path(__file__).resolve().parent.parent
_INSTITUTION_KEYS = ("KOUSHI_UNIV_NAME", "KOUSHI_DEPT_NAME", "KOUSHI_OFFICE_SIGNATURE",
                     "KOUSHI_HOME_DEPT_KEYS", "KOUSHI_HOME_UNIV_KEYS", "KOUSHI_HOME_DEPT_LABEL")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def demo(tmp_path, monkeypatch):
    # show.demo 載入時會改寫機構識別環境變數；先登記原值，測試結束後還原，
    # 避免影響其他以子程序執行的測試。
    for key in _INSTITUTION_KEYS:
        if key in os.environ:
            monkeypatch.setenv(key, os.environ[key])
        else:
            monkeypatch.delenv(key, raising=False)
    from show import demo as module

    # koushi.config 已由 conftest 以正式校系載入；展示資料以匿名示範學系為本系。
    monkeypatch.setattr(C, "UNIV_NAME", "示範大學")
    monkeypatch.setattr(C, "DEPT_NAME", "示範學系")
    monkeypatch.setattr(C, "OFFICE_SIGNATURE", "示範學系系辦")
    monkeypatch.setattr(C, "HOME_DEPT_KEYS", ("示範大學示範學系",))
    monkeypatch.setattr(C, "HOME_UNIV_KEYS", ("示範大學",))
    monkeypatch.setattr(C, "HOME_DEPT_LABEL", "本校示範學系")

    original_output = module.OUTPUT
    module.set_output_dir(tmp_path / "show-output")
    yield module
    module.set_output_dir(original_output)


def _fake_writers(monkeypatch):
    from koushi import workflow

    @contextlib.contextmanager
    def fake_template(path, **kwargs):
        yield path

    def fake_att4(master, output, *, template, term=None):
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text("附件4替身：" + "、".join(e.案件鍵 for e in master.exams), encoding="utf-8")

    def fake_att6(master, out_dir, *, template, term=None, prefill=False):
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        summaries = []
        for exam in master.exams:
            path = out_dir / f"附件6-{exam.案件鍵}.xlsx"
            path.write_text("附件6替身", encoding="utf-8")
            summaries.append({"輸出": str(path)})
        return summaries

    monkeypatch.setattr(document_conversion, "docx_template", fake_template)
    monkeypatch.setattr(workflow, "_validate_templates", lambda *a, **k: None)
    monkeypatch.setattr(workflow.attachment4_generator, "generate", fake_att4)
    monkeypatch.setattr(workflow.attachment6_generator, "generate", fake_att6)


def test_show_demo_flow_in_process(demo, monkeypatch, capsys):
    _fake_writers(monkeypatch)

    demo.select_llm(False)
    demo._run_up_to(demo.LAST_STEP)
    out = _ANSI.sub("", capsys.readouterr().out)

    # 步驟 2：學生乙未填委員 E-mail 屬學生責任、阻擋附件6
    assert "[1151-S002] MISSING_COMMITTEE_EMAIL" in out
    # 步驟 4：截止前催件排得出學生乙的補件信
    assert "[口試資料補正通知] 展示用學生乙（1151-S002）" in out
    # 步驟 5：補件後缺件解除
    assert "已解決：案件 1151-S002 / 問題 MISSING_COMMITTEE_EMAIL" in out
    # 步驟 6：系辦上傳資料後兩案放行
    assert "[1151-S001] 已放行" in out and "[1151-S002] 已放行" in out
    assert "兩案皆無資料面阻擋項目" in out
    # 步驟 7：附件4 與兩份預填附件6
    assert "預填附件6（銀行欄留白）：2 份" in out
    # 步驟 8：附件4 寄給兩位教授參考；一位沒回信（不用處理）、一位回信要求修改
    assert "已寄出：2　教授回信要求修改：1" in out
    # 步驟 9：教授有沒有回信都不擋口試後附件6；回收先不合格再補正
    assert "完整附件6：2 份" in out
    assert "尚未確認附件4" not in out
    assert "狀態：needs_correction" in out and "狀態：collected" in out
    # 步驟 10：信件草稿
    assert "學生補件信草稿範例" in out

    # 步驟 11：信箱 agent——修改要求成為待核准草稿、注入降級、個資不送模型、模型失敗轉人工
    assert "模型：offline-replay" in out
    assert "[草稿待核准] 依教授回信修正資料" in out
    assert "一般來信 → 記錄異動申請：自動" in out
    assert "夾帶注入 → 記錄異動申請：建議" in out
    assert "整封不送模型（模型呼叫 0 次）" in out
    assert "模型呼叫失敗：模擬：模型連線逾時" in out
    for step in ("identify_sender", "link_case", "pii_guard", "analyze", "plan_actions", "policy"):
        assert step in out
    # 步驟 11e：模型引用不精確 → 檢核擋下 → 回饋修正一次後通過
    assert "自我修正：第 1 次判讀未通過檢核（判讀依據不是原信逐字引用）" in out and "第 2 次通過檢核" in out
    # 步驟 12：專員核准後才寫入修正，並留下 staff_review 軌跡
    assert "[1151-S002] 口試日期：2026-10-05 → 2026-10-12" in out
    assert "專員修正筆數：0 → 2" in out and "staff_review" in out
    # 步驟 13：規則比對與模型排序並陳，確認前查無距離、確認後才查得到
    assert "[規則比對（不呼叫模型）]" in out and "[模型排序]" in out
    assert "「範例科大電子系」查距離表：查無距離" in out
    assert "「範例科大電子系」查距離表：查得到 30.0 公里" in out
    assert "再跑一次服務單位 agent：新增建議 0 筆" in out
    # 步驟 14：問答助理選工具、通過 grounding check；亂答被攔、要求做事被拒且不呼叫模型
    assert "工具 list_cases" in out and "grounding check：通過" in out
    assert "代號 CASE_03 不在引用的工具結果中" in out
    assert "沒有呼叫模型（模型呼叫 0 次）" in out
    # 步驟 15：稽核軌跡總覽
    assert "agent_trace 共" in out and "對照比賽門檻（第三條）" in out
    # 步驟 16：AI 總開關——停用後不建立模型、模型呼叫 0 次、恢復後留稽核紀錄
    assert "make_llm() → None" in out and "模型呼叫 0 次（傳入的模型也不會被使用）" in out
    assert "AI 總開關：已恢復運作" in out

    from koushi import agent_trace
    agents = {e["agent"] for e in agent_trace.load_trace(demo.STATE_DIR)}
    assert {"mailbox", "affiliation", "title", "assistant"} <= agents

    # 不得出現真實校系名稱（錄影畫面）
    for real in REAL_SCHOOLS:
        assert real not in out


def test_live_without_key_falls_back_to_offline(demo, monkeypatch, capsys):
    monkeypatch.setattr("koushi.llm.provider_config", lambda: None)
    llm, label = demo.select_llm(True)
    assert getattr(llm, "model_name", "") == "offline-replay" and "離線備援" in label
    assert "改用離線備援" in _ANSI.sub("", capsys.readouterr().out)


def test_live_uses_real_llm_factory(demo, monkeypatch):
    sentinel = type("Fake", (), {"model_name": "fake-live", "complete_json": lambda *a, **k: {}})()
    monkeypatch.setattr("koushi.llm.make_llm", lambda: sentinel)
    llm, label = demo.select_llm(True)
    assert llm is sentinel and "fake-live" in label
    demo.select_llm(False)


def _real_templates_available() -> bool:
    return (Path(C.TPL_ATT4).is_file() and Path(C.TPL_ATT6).is_file()
            and document_conversion.find_soffice() is not None)


@pytest.mark.skipif(not _real_templates_available(),
                    reason="需要 resource/template 的正式附件4／6 範本（不進版控）與 LibreOffice")
def test_show_demo_cli_with_real_templates(tmp_path):
    env = {**os.environ, "KOUSHI_SHOW_OUTPUT": str(tmp_path / "show-output"),
           "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, "-m", "show", "--no-pause"], cwd=ROOT, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=600,
    )
    out = _ANSI.sub("", proc.stdout)
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "展示完成" in out
    assert list((tmp_path / "show-output").rglob("附件4-學位口試一覽.docx"))
    att6 = list((tmp_path / "show-output" / "artifacts" / "post-exam").rglob("附件6-*.xlsx"))
    # 2026-10-12 起取消教授確認關卡：兩位教授的案件都產生完整附件6
    assert sorted(p.name for p in att6) == ["附件6-1151-碩-展示委員C-1001.xlsx", "附件6-1151-碩-展示委員F-1005.xlsx"]
    import openpyxl
    for path in att6:
        text = " ".join(str(v) for ws in openpyxl.load_workbook(path) for row in ws.iter_rows(values_only=True)
                        for v in row if v is not None)
        assert "一一五學年度" in text
        for real in REAL_SCHOOLS:
            assert real not in text
