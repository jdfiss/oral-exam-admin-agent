# -*- coding: utf-8 -*-
"""離線備援 LLM（koushi/offline_llm.py）：格式要能通過各 agent 自己的檢核，且不連網。"""
import json

import pytest

from koushi import affiliation_agent, assistant as A, llm as llm_module, mailbox_agent as MA, title_agent
from koushi.llm import LLMError, StructuredLLM
from koushi.offline_llm import ScriptedLLM, make_offline_llm

from tests.test_assistant import _snapshot


def test_scripted_llm_protocol_and_lookup():
    llm = ScriptedLLM({
        "a": {"x": 1},
        "b": [("改成", {"kind": "modify"}), ("", {"kind": "other"})],
        "c": LLMError("boom"),
        "d": [("只有這段", {"kind": "only"})],
    })
    assert isinstance(llm, StructuredLLM) and llm.model_name == llm.model == "offline-replay"
    first = llm.complete_json("s", "u", schema_name="a")
    first["x"] = 2
    assert llm.complete_json("s", "u", schema_name="a") == {"x": 1}   # 回傳副本
    assert llm.complete_json("s", "日期改成 10/1", schema_name="b") == {"kind": "modify"}
    assert llm.complete_json("s", "其他", schema_name="b") == {"kind": "other"}
    with pytest.raises(LLMError):
        llm.complete_json("s", "u", schema_name="c")
    with pytest.raises(LLMError):
        llm.complete_json("s", "u", schema_name="d")
    with pytest.raises(LLMError):
        llm.complete_json("s", "u", schema_name="unknown")
    assert len(llm.calls) == 7


def test_make_llm_offline_provider(monkeypatch):
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "offline")
    llm = llm_module.make_llm()
    assert isinstance(llm, ScriptedLLM)
    monkeypatch.delenv("KOUSHI_LLM_PROVIDER")
    assert llm_module.make_llm() is None   # conftest 已清掉所有金鑰


def _mailbox_user(text, role="advisor"):
    context = json.dumps({"sender_role": role, "sender_verified": True, "linked_case_count": 1,
                          "attachment4_request_state": "none", "has_attachments": False}, ensure_ascii=False)
    return MA.USER_TEMPLATE.format(context=context, subject="來信", reply_text=text)


@pytest.mark.parametrize("text, role, category", [
    ("您好，口試日期改成 10/12，地點改到 工程館 B201。", "advisor", "modify"),
    ("老師臨時出國，請問可以把口試延後到下週嗎？", "student", "student_request"),
    ("附件4 內容確認無誤，謝謝。", "advisor", "confirm"),
    ("請問口試當天停車要怎麼申請？", "advisor", "question"),
    ("祝順心", "advisor", "other"),
])
def test_mailbox_analysis_passes_agent_validation(text, role, category):
    raw = make_offline_llm().complete_json(MA.SYSTEM_PROMPT, _mailbox_user(text, role), schema_name="mailbox_analysis")
    analysis, dropped = MA._validate_analysis(raw, text, set())
    assert analysis["category"] == category and not dropped
    assert all(e in text for e in analysis["evidence"])
    if category == "modify":
        assert [m["field"] for m in analysis["modifications"]] == ["口試日期", "口試地點"]


def test_affiliation_and_title_outputs_are_valid():
    llm = make_offline_llm()
    user = json.dumps({"服務單位": "範例科大電子系", "候選": [{"id": "D1", "單位": "國立範例科技大學電子工程學系"},
                                                         {"id": "D2", "單位": "國立樣本大學電機工程學系"}]},
                      ensure_ascii=False)
    out = llm.complete_json(affiliation_agent.SYSTEM_PROMPT, user, schema_name="affiliation_match")
    assert out["match_id"] == "D1" and 0 < out["confidence"] <= 1
    none = llm.complete_json("", json.dumps({"服務單位": "某某研究中心", "候選": [{"id": "D1", "單位": "國立樣本大學電機工程學系"}]},
                                            ensure_ascii=False), schema_name="affiliation_match")
    assert none["match_id"] is None
    for title, level in (("客座教授", "教授級"), ("兼任副研究員", "副教授級"), ("技術長", "無法判斷")):
        out = llm.complete_json(title_agent.SYSTEM_PROMPT, json.dumps({"職稱": title}, ensure_ascii=False),
                                schema_name="title_level")
        assert out["level"] == level and out["level"] in title_agent.LEVELS
    assert llm.complete_json("", "{}", schema_name="source_correction")["action"] == "none"


@pytest.mark.parametrize("question, tool", [
    ("哪些案件卡在哪一關？", "list_cases"),
    ("還有哪些教授沒確認附件4？", "list_confirmations"),
    ("目前有哪些缺件？", "list_deficiencies"),
    ("總共有幾件案件？", "overview_counts"),
])
def test_assistant_answers_pass_grounding_check(tmp_path, question, tool):
    ans = A.ask(question, snapshot=_snapshot(), llm=make_offline_llm(), state_dir=tmp_path)
    assert [s.tool for s in ans.steps] == [tool]
    assert ans.kind == "answer", ans.problems
