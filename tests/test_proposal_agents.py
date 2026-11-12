# -*- coding: utf-8 -*-
"""提案型 agent：AI 提案、專員決定（docs/提案型agent設計.md）。

全部用假模型，不呼叫真 API。重點：
- 未確認的服務單位建議不影響交通費；確認後寫入對照表，下次不再問模型。
- 送模型的內容沒有身分證、銀行帳號、地址、Email、學生姓名與學號。
- 修正建議只有專員按套用才寫入專員修正層，並記錄處理人。
- 職稱建議不改變資格檢核結果。
- 模型錯誤逐項隔離，該項留給專員。
"""
import json

import pytest

from koushi import (affiliation_agent, agent_trace, ai_proposals as P, corrections, correction_agent,
                    title_agent, validation)
from koushi.application import load_current_master
from koushi.workflow import RegistryPaths, build_master

from tests.att2_fixture import case, write_att2

NTU = {"name": "外校委員", "unit": "台大資訊系", "title": "兼任副教授"}
HOME = {"name": "校內委員", "unit": "國立範例大學資訊工程學系", "title": "教授"}
DISTANCES = "服務單位,單程公里\n國立臺灣大學資訊工程學系,50\n國立臺灣大學電機工程學系,50\n國立清華大學資訊工程學系,30\n"
PII = ("A123456789", "0123456789012", "台北市大安區羅斯福路四段1號", "far@example.org", "王小明", "111900001")


class FakeLLM:
    model = "fake-model"

    def __init__(self, responses=None, error=None):
        self.responses = dict(responses or {})
        self.error = error
        self.calls = []

    def complete_json(self, system, user, *, schema_name):
        self.calls.append({"system": system, "user": user, "schema": schema_name})
        if self.error:
            raise self.error
        return self.responses[schema_name]


def _submission(path, committees, place="S1-101"):
    path.write_text(json.dumps({
        "term": "1141", "student_id": "111900001", "version": 1, "exam_date": "2026-11-10",
        "exam_location": place, "advisors": ["林指導"],
        "committees": [{"name": c["name"], "unit": c["unit"], "title": c["title"], "email": c["email"]}
                       for c in committees],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _build(tmp_path, *, portal=(NTU, HOME), student=None):
    state = tmp_path / "state"
    (tmp_path / "distances.csv").write_text(DISTANCES, encoding="utf-8")
    student = student or [{**c, "email": f"m{i}@example.org"} for i, c in enumerate(portal)]
    sub = _submission(tmp_path / "a.json", student)
    registries = RegistryPaths(
        members=tmp_path / "none.xlsx", distances=tmp_path / "distances.csv",
        members_output=tmp_path / "out" / "members.xlsx",
        attachment2=write_att2(tmp_path / "att2.xlsx", [case(student_id="111900001", committees=list(portal))]),
        corrections=state / corrections.FILENAME,
    )
    result = build_master(submission_paths=[sub], output_path=tmp_path / "out" / "master.xlsx",
                          registries=registries)
    return result, state


def _member(master, unit):
    return next(m for e in master.exams for m in e.委員 if m.服務單位 == unit)


def _distance_table(tmp_path, state):
    from koushi.registry import DistanceTable
    table = DistanceTable.load(tmp_path / "distances.csv")
    table.aliases = P.alias_map(state)
    return table


# ── A：服務單位對應 ──

def test_unconfirmed_affiliation_proposal_never_changes_fee_until_staff_confirms(tmp_path):
    result, state = _build(tmp_path)
    assert _member(result.master, "台大資訊系").交通費 is None
    llm = FakeLLM({"affiliation_match": {"match_id": "D1", "confidence": 0.9, "rationale": "台大資訊系即臺大資訊工程學系"}})
    created = affiliation_agent.run(result.master, state, distance_table=_distance_table(tmp_path, state), llm=llm)
    assert len(created) == 1 and created[0]["suggestion"] == "國立臺灣大學資訊工程學系"
    assert created[0]["status"] == P.PENDING

    # 有待確認建議，重建主檔交通費仍是查無距離，例外照列
    rebuilt, _ = _build(tmp_path)
    assert _member(rebuilt.master, "台大資訊系").交通費 is None
    assert any(i["問題代碼"] == "NO_DISTANCE" for i in rebuilt.issues)

    affiliation_agent.decide(state, created[0]["id"], action="approve", actor="專員甲",
                             distance_units=list(_distance_table(tmp_path, state).rows))
    confirmed, _ = _build(tmp_path)
    member = _member(confirmed.master, "台大資訊系")
    assert member.交通費 == 300 and "專員確認對照" in member.交通費說明
    aliases = P.load_aliases(state)
    entry = aliases["aliases"]["台大資訊系"]
    assert entry["actor"] == "專員甲" and entry["target"] == "國立臺灣大學資訊工程學系" and aliases["version"] == 1


def test_confirmed_alias_is_used_without_calling_llm_again(tmp_path):
    result, state = _build(tmp_path)
    P.set_alias(state, unit="台大資訊系", target="國立臺灣大學資訊工程學系", actor="專員甲", source="staff_override")
    llm = FakeLLM({"affiliation_match": {"match_id": "D1", "confidence": 0.9, "rationale": "x"}})
    rebuilt, _ = _build(tmp_path)
    assert _member(rebuilt.master, "台大資訊系").交通費 == 300
    assert affiliation_agent.run(rebuilt.master, state, distance_table=_distance_table(tmp_path, state), llm=llm) == []
    assert llm.calls == []


def test_existing_proposal_is_not_re_asked(tmp_path):
    result, state = _build(tmp_path)
    llm = FakeLLM({"affiliation_match": {"match_id": None, "confidence": 0.2, "rationale": "無"}})
    table = _distance_table(tmp_path, state)
    affiliation_agent.run(result.master, state, distance_table=table, llm=llm)
    affiliation_agent.run(result.master, state, distance_table=table, llm=llm)
    assert len(llm.calls) == 1


def test_rule_match_needs_no_llm_but_still_waits_for_staff(tmp_path):
    result, state = _build(tmp_path, portal=({**NTU, "unit": "國立台灣大學 資訊工程系"}, HOME))
    llm = FakeLLM()
    created = affiliation_agent.run(result.master, state, distance_table=_distance_table(tmp_path, state), llm=llm)
    assert llm.calls == []
    assert created[0]["method"] == "rule" and created[0]["suggestion"] == "國立臺灣大學資訊工程學系"
    assert created[0]["confidence"] is None  # 規則比對不編造信心分數
    assert created[0]["status"] == P.PENDING and P.alias_map(state) == {}


def test_seed_aliases_cover_english_abbreviations():
    units = ["國立臺灣大學資訊工程學系", "國立清華大學資訊工程學系"]
    assert affiliation_agent.search("NTU CSIE", units)[0]["score"] == 1.0
    assert affiliation_agent.search("Dept. of CSIE, National Tsing Hua University", units)[0]["name"] == units[1]


def test_llm_choice_outside_candidates_is_rejected_and_left_for_staff(tmp_path):
    result, state = _build(tmp_path)
    llm = FakeLLM({"affiliation_match": {"match_id": "D99", "confidence": 1, "rationale": "x"}})
    created = affiliation_agent.run(result.master, state, distance_table=_distance_table(tmp_path, state), llm=llm)
    assert created[0]["suggestion"] is None and "不在候選" in created[0]["error"]


def test_override_and_reject_affiliation(tmp_path):
    result, state = _build(tmp_path)
    llm = FakeLLM({"affiliation_match": {"match_id": "D1", "confidence": 0.9, "rationale": "x"}})
    table = _distance_table(tmp_path, state)
    proposal = affiliation_agent.run(result.master, state, distance_table=table, llm=llm)[0]
    with pytest.raises(ValueError):
        affiliation_agent.decide(state, proposal["id"], action="override", actor="專員甲", target="不存在的單位",
                                 distance_units=list(table.rows))
    with pytest.raises(ValueError):
        affiliation_agent.decide(state, proposal["id"], action="approve", actor=" ", distance_units=list(table.rows))
    decided = affiliation_agent.decide(state, proposal["id"], action="reject", actor="專員甲")
    assert decided["status"] == P.REJECTED and P.alias_map(state) == {}
    steps = [t["step"] for t in agent_trace.load_trace(state, tracking_key=proposal["subject_key"])]
    assert steps == ["retrieve_candidates", "llm_call", "propose", "human_decision"]


# ── 個資不送模型 ──

def test_no_pii_in_any_llm_prompt(tmp_path):
    portal = (NTU, HOME, {"name": "另一委員", "unit": "國立清華大學資訊工程學系", "title": "Adjunct Professor"})
    student = [{**c, "email": "far@example.org"} for c in portal[:2]]  # 名單不一致 → COMMITTEE_MISMATCH
    result, state = _build(tmp_path, portal=portal, student=student)
    master = result.master
    for committee in master.committees.values():  # 假設資料庫已有委員個資
        committee.身分證字號, committee.帳號, committee.戶籍地址 = PII[0], PII[1], PII[2]
    llm = FakeLLM({
        "affiliation_match": {"match_id": "D1", "confidence": 0.9, "rationale": "x"},
        "source_correction": {"action": "none", "rationale": "需系辦確認名單"},
        "title_level": {"level": "副教授級", "confidence": 0.6, "rationale": "x"},
    })
    affiliation_agent.run(master, state, distance_table=_distance_table(tmp_path, state), llm=llm)
    correction_agent.run(master, state, llm=llm)
    title_agent.run(master, state, llm=llm)
    assert {c["schema"] for c in llm.calls} == {"affiliation_match", "source_correction", "title_level"}
    for call in llm.calls:
        prompt = call["system"] + call["user"]
        for secret in PII + ("外校委員", "另一委員", "校內委員", "1141-111900001"):
            assert secret not in prompt, (secret, call["schema"])


def test_scrub_masks_ids_accounts_and_emails():
    text = P.scrub("身分證 A123456789 帳號 0123-4567-8901 信箱 a@b.org")
    assert "A123456789" not in text and "0123-4567-8901" not in text and "a@b.org" not in text


# ── B：來源異常修正建議 ──

def test_correction_applies_only_through_staff_action_with_handler(tmp_path):
    student = [{**NTU, "email": "a@example.org"}]  # 學生名單少一位 → COMMITTEE_MISMATCH（案件層）
    result, state = _build(tmp_path, student=student)
    llm = FakeLLM({"source_correction": {"action": "correct", "field": "口試地點", "member_ref": None,
                                         "new_value": "S1-204", "confidence": 0.7, "rationale": "依附件2"}})
    created = correction_agent.run(result.master, state, llm=llm)
    proposal = next(p for p in created if p["suggestion"])
    assert proposal["suggestion"]["old"] == "S1-101" and proposal["suggestion"]["new"] == "S1-204"
    assert corrections.load(state) == []  # 從不自動套用

    with pytest.raises(ValueError):
        correction_agent.decide(state, proposal["id"], action="approve", actor="",
                                master_path=tmp_path / "out" / "master.xlsx")
    assert corrections.load(state) == []
    correction_agent.decide(state, proposal["id"], action="approve", actor="專員乙",
                            master_path=tmp_path / "out" / "master.xlsx")
    [saved] = corrections.load(state)
    assert (saved.field, saved.value, saved.actor) == ("口試地點", "S1-204", "專員乙")
    assert proposal["id"] in saved.reason
    assert P.get(state, proposal["id"])["status"] == P.APPROVED


def test_invalid_correction_suggestion_is_not_offered(tmp_path):
    student = [{**NTU, "email": "a@example.org"}]
    result, state = _build(tmp_path, student=student)
    llm = FakeLLM({"source_correction": {"action": "correct", "field": "Email", "member_ref": "委員1",
                                         "new_value": "x@example.org", "confidence": 0.9, "rationale": "x"}})
    created = correction_agent.run(result.master, state, llm=llm)
    assert created and all(p["suggestion"] is None and p["error"] for p in created)


# ── C：職稱輔助判讀 ──

def test_title_suggestion_does_not_change_qualification_state(tmp_path):
    result, state = _build(tmp_path)
    master = load_current_master(tmp_path / "out" / "master.xlsx")
    before = [i for i in validation.check(master) if i["問題代碼"] == "COMMITTEE_RANK_EXCEPTION"]
    assert before
    llm = FakeLLM({"title_level": {"level": "副教授級", "confidence": 0.6, "rationale": "兼任仍具副教授資格"}})
    [proposal] = title_agent.run(master, state, llm=llm)
    title_agent.decide(state, proposal["id"], action="approve", actor="專員丙")
    rebuilt, _ = _build(tmp_path)
    after = [i for i in rebuilt.issues if i["問題代碼"] == "COMMITTEE_RANK_EXCEPTION"]
    assert [i["說明"] for i in after] == [i["說明"] for i in before]
    assert not (state / "case_reviews.json").exists()
    assert P.load_title_levels(state)["titles"]["兼任副教授"]["level"] == "副教授級"
    # 已確認的職稱下次不再問模型
    assert title_agent.run(master, state, llm=llm) == [] and len(llm.calls) == 1


# ── 錯誤隔離與開關 ──

def test_llm_error_is_isolated_per_item_and_retried_later(tmp_path):
    portal = (NTU, {"name": "委員乙", "unit": "清華資訊所", "title": "教授"}, HOME)
    result, state = _build(tmp_path, portal=portal)
    broken = FakeLLM(error=TimeoutError("timeout"))
    created = affiliation_agent.run(result.master, state, distance_table=_distance_table(tmp_path, state), llm=broken)
    assert len(created) == 2 and all(p["error"] and p["suggestion"] is None for p in created)
    assert _member(result.master, "台大資訊系").交通費 is None
    good = FakeLLM({"affiliation_match": {"match_id": "D1", "confidence": 0.8, "rationale": "x"}})
    retried = affiliation_agent.run(result.master, state, distance_table=_distance_table(tmp_path, state), llm=good)
    assert len(retried) == 2 and all(not p["error"] for p in retried)
    assert len(P.pending(state, "affiliation")) == 2


def test_disabled_agent_does_nothing(tmp_path):
    result, state = _build(tmp_path)
    P.save_settings(state, {"affiliation": False}, actor="專員甲")
    llm = FakeLLM()
    assert affiliation_agent.run(result.master, state, distance_table=_distance_table(tmp_path, state), llm=llm) == []
    assert P.load_settings(state) == {"affiliation": False, "correction": True, "title": True}


# ── 網頁 ──

def test_office_page_lists_and_decides_proposals(tmp_path, monkeypatch):
    from koushi import llm as llm_module
    from koushi.web import app as webapp
    from tests.test_web_app import _csrf

    result, state = _build(tmp_path)
    config = webapp.WebConfig(
        submissions_dir=tmp_path / "subs", master_path=tmp_path / "out" / "master.xlsx", state_dir=state,
        registries=RegistryPaths(members=tmp_path / "none.xlsx", distances=tmp_path / "distances.csv",
                                 attachment2=tmp_path / "att2.xlsx"),
        doc_output_root=tmp_path / "docs", office_password="pw",
    )
    monkeypatch.setitem(webapp.app.config, "_web", config)
    monkeypatch.setitem(webapp.app.config, "TESTING", True)
    fake = FakeLLM({"affiliation_match": {"match_id": "D1", "confidence": 0.9, "rationale": "同校同系"},
                    "source_correction": {"action": "none", "rationale": "x"},
                    "title_level": {"level": "副教授級", "confidence": 0.5, "rationale": "x"}})
    monkeypatch.setattr(llm_module, "make_llm", lambda: fake)
    with webapp.app.test_client() as client:
        client.post("/login/office", data={"password": "pw", "csrf_token": _csrf(client)})
        client.post("/office/ai-proposals/run", data={"agent": "all", "csrf_token": _csrf(client)})
        page = client.get("/office/ai-proposals").get_data(as_text=True)
        assert "台大資訊系" in page and "AI 建議" in page and "信心 90%" in page and "模型判讀" in page
        proposal = P.pending(state, "affiliation")[0]
        client.post(f"/office/ai-proposals/{proposal['id']}/decide",
                    data={"action": "approve", "handler": "專員甲", "csrf_token": _csrf(client)})
    assert P.alias_map(state) == {"台大資訊系": "國立臺灣大學資訊工程學系"}
    assert P.get(state, proposal["id"])["decided_by"] == "專員甲"


def test_add_returns_existing_pending_proposal_instead_of_duplicate(tmp_path):
    """兩輪同時產生建議時（網頁不再用全域鎖包住整輪），同一對象只留一筆待處理建議。"""
    base = {"agent": "title", "subject_key": "客座教授", "subject": "客座教授", "suggestion": "教授級"}
    first = P.add(tmp_path, dict(base))
    second = P.add(tmp_path, dict(base))
    assert second["id"] == first["id"] and len(P.load(tmp_path)) == 1


def test_ai_proposal_run_does_not_hold_global_state_lock():
    from koushi.web import app as webapp
    assert "run_ai_proposals" in webapp._UNLOCKED_POSTS


def test_busy_state_lock_shows_retry_page_not_500(tmp_path, monkeypatch):
    from koushi import util as U
    from koushi.web import app as webapp
    from koushi.workflow import RegistryPaths
    cfg = webapp.WebConfig(submissions_dir=tmp_path / "subs", master_path=tmp_path / "out" / "master.xlsx",
                           state_dir=tmp_path / "state", doc_output_root=tmp_path / "docs", office_password="pw",
                           registries=RegistryPaths(attachment2=tmp_path / "a2.xlsx", members=tmp_path / "m.xlsx",
                                                    distances=tmp_path / "d.csv"))
    monkeypatch.setitem(webapp.app.config, "_web", cfg)
    monkeypatch.setitem(webapp.app.config, "TESTING", True)
    client = webapp.app.test_client()
    with client.session_transaction() as s:
        s["_csrf_token"] = "t"
    client.post("/login/office", data={"password": "pw", "csrf_token": "t"})

    def busy(*a, **k):
        raise U.BuildLockTimeout("無法在 120s 內取得狀態鎖")
    monkeypatch.setattr(webapp.U, "state_lock", busy)
    response = client.post("/office/ai-switch", data={"disabled": "1", "handler": "甲", "reason": "x", "csrf_token": "t"})
    assert response.status_code == 503 and "請稍候一分鐘再試" in response.get_data(as_text=True)


def test_invalid_level_is_fed_back_and_repaired_once(tmp_path):
    """模型回了白名單外的級別：把允許的級別回饋給模型修正一次，修正後才成為建議（docs/自我修正設計.md）。"""
    result, state = _build(tmp_path)
    master = load_current_master(tmp_path / "out" / "master.xlsx")
    replies = iter([{"level": "超級教授級", "confidence": 1, "rationale": "x"},
                    {"level": "副教授級", "confidence": 0.6, "rationale": "兼任仍具副教授資格"}])

    class Repairable(FakeLLM):
        def complete_json(self, system, user, *, schema_name):
            self.calls.append({"user": user})
            return next(replies)

    llm = Repairable()
    [proposal] = title_agent.run(master, state, llm=llm)
    assert len(llm.calls) == 2 and "只能是：教授級" in llm.calls[1]["user"]
    assert proposal["suggestion"] == "副教授級" and not proposal["error"]
    calls = [t for t in agent_trace.load_trace(state, tracking_key=proposal["subject_key"]) if t["step"] == "llm_call"]
    assert [t["decision"] for t in calls] == ["repair", "pass"]


def test_unrepairable_candidate_id_stays_an_error_for_staff(tmp_path):
    result, state = _build(tmp_path)
    llm = FakeLLM({"affiliation_match": {"match_id": "D99", "confidence": 1, "rationale": "x"}})
    [proposal] = affiliation_agent.run(result.master, state, distance_table=_distance_table(tmp_path, state), llm=llm)
    assert len(llm.calls) == 2 and proposal["error"] and proposal["suggestion"] is None


def test_identity_confirmation_issues_are_not_sent_for_correction():
    """「以姓名沿用舊資料，請確認同一人」要人確認身分，改欄位解決不了：不產生「無建議」的雜訊建議。"""
    from types import SimpleNamespace
    master = SimpleNamespace(by_key=lambda: {"K1": object()}, source_issues=[
        {"案件鍵": "K1", "責任": "office", "問題代碼": "COMMITTEE_MATCHED_BY_NAME"},
        {"案件鍵": "K1", "責任": "office", "問題代碼": "UNVERIFIED_COMMITTEE_IDENTITY"},
        {"案件鍵": "K1", "責任": "office", "問題代碼": "COMMITTEE_MISMATCH"},
    ])
    assert [i["問題代碼"] for i in correction_agent.candidate_issues(master)] == ["COMMITTEE_MISMATCH"]


def test_proposal_made_without_model_is_retried_when_model_returns(tmp_path):
    """審查 F08：沒有模型時的空建議，模型恢復後補分析，不永久卡住。"""
    result, state = _build(tmp_path)
    master = load_current_master(tmp_path / "out" / "master.xlsx")
    [empty] = title_agent.run(master, state, llm=None)
    assert empty["suggestion"] is None and empty["method"] == "none"
    assert title_agent.run(master, state, llm=None) == []          # 沒模型時不重複建立
    llm = FakeLLM({"title_level": {"level": "副教授級", "confidence": 0.6, "rationale": "兼任仍具副教授資格"}})
    [filled] = title_agent.run(master, state, llm=llm)
    assert filled["suggestion"] == "副教授級" and len(P.pending(state, title_agent.AGENT)) == 1


def test_stale_correction_proposal_is_rejected_not_applied(tmp_path):
    """審查 F04：AI 建議提出後，專員已改過同一欄位，再核准舊建議要拒絕。"""
    from koushi.application import save_case_correction
    student = [{**NTU, "email": "a@example.org"}]
    result, state = _build(tmp_path, student=student)
    llm = FakeLLM({"source_correction": {"action": "correct", "field": "口試地點", "member_ref": None,
                                         "new_value": "舊AI教室", "confidence": 0.7, "rationale": "依附件2"}})
    proposal = next(p for p in correction_agent.run(result.master, state, llm=llm) if p["suggestion"])
    master_path = tmp_path / "out" / "master.xlsx"
    save_case_correction(master_path=master_path, state_dir=state, case_key=proposal["case_key"], field="口試地點",
                         value="最新專員教室", actor="專員甲", reason="電話確認")
    with pytest.raises(ValueError, match="資料已變更"):
        correction_agent.decide(state, proposal["id"], action="approve", actor="專員乙", master_path=master_path)
    assert {c.field: c.value for c in corrections.load(state)}["口試地點"] == "最新專員教室"
