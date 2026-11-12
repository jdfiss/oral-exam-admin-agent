# -*- coding: utf-8 -*-
"""系辦問答助理：唯讀工具、代號化、回答檢查、動作拒絕、步數上限、錯誤處理、網頁。

全程用假模型（依腳本回傳 JSON），不呼叫任何真實 LLM。
"""
import datetime as _dt
import json

import pytest

from koushi import agent_trace, assistant as A, assistant_tools as T
from koushi import settings as office_settings
from koushi.application import ReviewRow, TrackingResult
from koushi.case_review import CaseReview
from koushi.committee_requests import CommitteeRequest
from koushi.deadline import Deadline
from koushi.engines.collection import CollectionReport
from koushi.engines.confirmation import ConfirmationReport, ConfirmationRequest
from koushi.engines.deficiency import DeficiencyItem, DeficiencyReport
from koushi.model import Committee, Exam, Master, Member

K1, K2 = "1141-111900001", "1141-111900002"
PII = ("A123456789", "0123456789012", "台北市某路", "dawen@example.org", "s1@example.org",
       "王小明", "李小華", "111900001", "111900002", "陳大文", "林指導", "周教授")
PAST = _dt.date(2000, 1, 1)


class FakeLLM:
    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def complete_json(self, system, user, *, schema_name):
        self.calls.append((system, user, schema_name))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item(user) if callable(item) else item


def _snapshot(today=_dt.date(2026, 10, 8)):
    committee = Committee(committee_id="C0001", 委員姓名="陳大文", 服務單位="國立清華大學資訊工程學系",
                          身分證字號="A123456789", 金融機構="某銀行", 帳號="0123456789012",
                          戶籍地址="台北市某路1號", Email="dawen@example.org")
    master = Master(
        exams=[
            Exam(學期="1141", 學號="111900001", 姓名="王小明", 指導教授="林指導",
                 委員=[Member(committee_id="C0001", 委員姓名="陳大文")]),
            Exam(學期="1141", 學號="111900002", 姓名="李小華", 指導教授="周教授",
                 委員=[Member(committee_id="C0001", 委員姓名="陳大文")]),
        ],
        committees={"C0001": committee},
    )
    rows = [
        {"id": K1, "term": "1141", "student_id": "111900001", "student_name": "王小明",
         "exam_date": "2026-11-01", "status": "已填寫", "version": 2, "hints": [],
         "student_email": "s1@example.org", "no_email": False, "deficiency_count": 1, "late_added": False},
        {"id": K2, "term": "1141", "student_id": "111900002", "student_name": "李小華",
         "exam_date": "2026-11-02", "status": "已填寫", "version": 1, "hints": [],
         "student_email": "s2@example.org", "no_email": False, "deficiency_count": 0, "late_added": False},
    ]
    reviews = [
        ReviewRow(case_key=K1, student_id="111900001", student_name="王小明",
                  exceptions=[{"說明": "委員陳大文職級不符（身分證 A123456789）"}], review=None,
                  changed_since_clear=False),
        ReviewRow(case_key=K2, student_id="111900002", student_name="李小華", exceptions=[],
                  review=CaseReview(case_key=K2, cleared_at="2026-10-05 10:00:00", cleared_by="專員",
                                    note="", committee_signature="x"),
                  changed_since_clear=False),
    ]
    deficiency = DeficiencyItem(case_key=K1, term="1141", student_id="111900001", student_name="王小明",
                                issue_code="NO_EXAM_DATE", field="口試日期", description="王小明未填口試日期",
                                owner="student", blocks_att4=True, blocks_att6=True, notified_date=PAST,
                                deadline=Deadline(base_date=PAST, business_days=7, computed_date=PAST))
    confirmation = ConfirmationRequest(group_key=("1141", "碩", "周教授", "11/2"), term="1141", advisor="周教授",
                                       degree="碩", date_text="11/2", student_names=("李小華",), case_keys=(K2,),
                                       sent_date=PAST,
                                       deadline=Deadline(base_date=PAST, business_days=5, computed_date=PAST))
    tracking = TrackingResult(
        deficiency=DeficiencyReport(items=(deficiency,), resolved_keys=frozenset()),
        confirmation=ConfirmationReport(requests=(confirmation,)),
        collection=CollectionReport(items=()),
    )
    plan = [CommitteeRequest(email="dawen@example.org", name="陳大文", case_keys=(K2,),
                             earliest_exam=_dt.date(2026, 11, 2), sent_date=None, received=False)]
    return T.Snapshot(
        today=today,
        settings=office_settings.OfficeSettings(current_term="1141", fill_deadline=_dt.date(2026, 10, 1),
                                                att2_first_uploaded=_dt.date(2026, 10, 1), deficiency_days=7,
                                                business_days=False),
        overview_rows=rows, reviews=reviews, tracking=tracking, master=master, committee_plan=plan,
    )


def _tool(name, **args):
    return {"type": "tool_call", "tool": name, "args": args}


def _answer(text, sources=(1,)):
    return {"type": "answer", "answer": text, "sources": list(sources)}


def _ask(question, script, tmp_path=None, snap=None):
    llm = FakeLLM(script)
    ans = A.ask(question, snapshot=snap or _snapshot(), llm=llm, state_dir=tmp_path)
    return ans, llm


# ── 工具：資料與畫面同源、不含個資 ─────────────────────────

def test_case_tokens_are_stable_and_sorted():
    aliaser = T.Aliaser(_snapshot())
    assert aliaser.token(K1) == "CASE_01" and aliaser.token(K2) == "CASE_02"
    assert aliaser.resolve_case("CASE_02") == K2
    assert aliaser.resolve_case(aliaser.token("王小明")) == K1


def test_stage_follows_pipeline_order_using_ui_data():
    snap = _snapshot()
    idx = T.make_index(snap)
    # 案件 1：學生缺件在人工關卡之前；案件 2 已放行，附件4 已寄出但教授沒回信
    # ——2026-10-12 起教授不用回信，不算卡住，下一關是委員個資
    assert [b["stage"] for b in idx.blockers(K1)][:2] == [T.STAGE_DEFICIENCY, T.STAGE_REVIEW]
    assert idx.stage(K2) == T.STAGE_COMMITTEE
    assert T.STAGE_CONFIRMATION not in [b["stage"] for b in idx.blockers(K2)]
    counts = T.tool_overview_counts(snap, idx)
    assert counts["review_pending"] == 1 and counts["student_deficiency_overdue"] == 1
    assert counts["total_cases"] == len(snap.overview_rows)


def test_no_tool_output_contains_pii():
    snap = _snapshot()
    aliaser = T.Aliaser(snap)
    outputs = [T.run_tool(name, {}, snap, aliaser) for name in T.TOOLS]
    outputs += [T.run_tool("get_case", {"case": tok}, snap, aliaser) for tok in ("CASE_01", "CASE_02")]
    outputs.append(T.run_tool("list_committee_data", {"missing_only": False}, snap, aliaser))
    text = json.dumps(outputs, ensure_ascii=False)
    for secret in PII:
        assert secret not in text, secret
    assert "未回填" in text and "CASE_01" in text and "PER_" in text


def test_tool_errors_are_reported_not_raised():
    snap = _snapshot()
    aliaser = T.Aliaser(snap)
    assert "error" in T.run_tool("no_such_tool", {}, snap, aliaser)
    assert "error" in T.run_tool("list_cases", {"stage": "亂寫"}, snap, aliaser)
    assert "error" in T.run_tool("get_case", {"case": "CASE_99"}, snap, aliaser)


# ── 問答流程 ────────────────────────────────────────────

def test_grounded_answer_matches_tool_data(tmp_path):
    ans, llm = _ask("1141-111900001 王小明卡在哪？", [
        _tool("get_case", case="CASE_01"),
        _answer("CASE_01 目前卡在學生缺件：口試日期尚未補，補件逾期，共 1 項。"),
    ], tmp_path)
    assert ans.kind == "answer", ans.problems
    assert ans.steps[0].output["stage"] == T.STAGE_DEFICIENCY
    # 模型看到的問題與工具結果都沒有個資
    sent = json.dumps(llm.calls, ensure_ascii=False)
    for secret in PII:
        assert secret not in sent, secret
    assert "CASE_01" in llm.calls[0][1]
    # 顯示時代號換回真實案件鍵
    assert ("case", K1) in A.segments(ans.text, ans.aliaser)


def test_hallucinated_case_key_falls_back_to_raw_table():
    ans, _ = _ask("哪些案件待專員放行？", [
        _tool("list_cases", blocker=T.STAGE_REVIEW),
        _answer("CASE_07 待專員放行。"),
    ])
    assert ans.kind == "unverified"
    assert ans.note == A.UNVERIFIED_NOTE
    assert any("CASE_07" in p for p in ans.problems)
    assert ans.steps and ans.steps[0].output["rows"]


def test_invented_number_falls_back():
    ans, _ = _ask("有幾件待專員放行？", [_tool("overview_counts"), _answer("共有 42 件待專員放行。")])
    assert ans.kind == "unverified" and any("42" in p for p in ans.problems)


def test_wrong_status_for_case_falls_back():
    """LLM前置研究的疑慮：模型把案件狀態說錯。CASE_01 其實是待專員放行，不是已放行。"""
    ans, _ = _ask("CASE_01 放行了嗎？", [_tool("get_case", case="CASE_01"), _answer("CASE_01 已放行。")])
    assert ans.kind == "unverified"
    assert any("已放行" in p for p in ans.problems)


def test_answer_without_sources_falls_back():
    ans, _ = _ask("有幾件待專員放行？", [_tool("overview_counts"), _answer("共有 1 件待專員放行。", sources=())])
    assert ans.kind == "unverified"


def test_model_scope_reply_becomes_out_of_scope(tmp_path):
    ans, _ = _ask("今天天氣如何？", [_answer("我只能回答口試案件相關的資料問題。", sources=())], tmp_path)
    assert ans.kind == "out_of_scope" and ans.text == A.SCOPE_REPLY
    assert [t["step"] for t in agent_trace.load_trace(tmp_path, run_id=ans.run_id)] == ["question", "out_of_scope"]


def test_model_policy_reply_links_policy_pages():
    ans, _ = _ask("口試委員要怎麼安排比較好？", [_answer("我只能查詢口試案件資料，規則請看系辦操作說明或期限設定頁", sources=())])
    assert ans.kind == "out_of_scope" and "office_settings_page" in [e for _, e, _ in ans.links]


def test_other_answer_without_facts_still_passes():
    ans, _ = _ask("你好", [_answer("您好，請問想查哪個案件？", sources=())])
    assert ans.kind == "answer"


def test_dates_and_list_ordinals_are_not_false_alarms():
    ans, _ = _ask("補件截止日？", [
        _tool("get_deadlines"),
        _answer("1. 補件截止日是 2026-10-08。\n2. 目前階段：補件期間", sources=[1]),
    ])
    assert ans.kind == "answer", ans.problems


def test_action_request_is_refused_without_calling_model():
    ans, llm = _ask("幫我放行 1141-111900001", [_answer("不該被呼叫")])
    assert ans.kind == "refused"
    assert not llm.calls
    assert ("office_overview" in [e for _, e, _ in ans.links])
    assert any(anchor == "review-queue" for _, _, anchor in ans.links)


def test_model_detected_action_request_links_to_page():
    ans, llm = _ask("可以幫我寄催辦信給逾期的教授嗎？",
                    [{"type": "action_request", "action": "confirmation", "case": "CASE_02"}])
    assert ans.kind == "refused" and llm.calls
    assert ans.links[0][1] == "office_documents"
    assert ans.link_case == K2


def test_answer_claiming_to_have_acted_is_rejected():
    ans, _ = _ask("CASE_01 狀態？", [_tool("get_case", case="CASE_01"), _answer("我已幫您放行 CASE_01。")])
    assert ans.kind == "unverified"


@pytest.mark.parametrize("question", ["哪些案件待放行？", "幫我查哪些案件待專員放行", "請問教授確認狀態"])
def test_queries_are_not_mistaken_for_actions(question):
    assert A.detect_action(question) is None


def test_step_limit():
    ans, llm = _ask("全部列出來", [_tool("overview_counts")])
    assert len(ans.steps) == A.MAX_TOOL_STEPS
    assert len(llm.calls) == A.MAX_TOOL_STEPS + 1
    assert "已達查詢上限" in llm.calls[-1][1]
    assert ans.kind == "unverified"


def test_llm_error_is_graceful(tmp_path):
    ans, _ = _ask("有幾件待專員放行？", [RuntimeError("timeout")], tmp_path)
    assert ans.kind == "error"
    assert "暫時無法使用" in ans.text
    steps = [t["step"] for t in agent_trace.load_trace(tmp_path, run_id=ans.run_id)]
    assert steps == ["question", "llm_error"]


def test_llm_error_after_tools_shows_raw_data():
    ans, _ = _ask("有幾件待專員放行？", [_tool("overview_counts"), RuntimeError("boom")])
    assert ans.kind == "error" and ans.steps and ans.note == A.UNVERIFIED_NOTE


def test_trace_is_written_without_pii(tmp_path):
    ans, _ = _ask("王小明 111900001 卡在哪？", [
        _tool("get_case", case="CASE_01"), _answer("CASE_01 目前卡在學生缺件。"),
    ], tmp_path)
    trace = agent_trace.load_trace(tmp_path, run_id=ans.run_id)
    assert [t["step"] for t in trace] == ["question", "tool_call", "grounding_check"]
    assert all(t["agent"] == "assistant" and t["prompt_version"] == A.PROMPT_VERSION for t in trace)
    assert trace[1]["tool"] == "get_case" and trace[1]["latency_ms"] is not None
    assert trace[2]["decision"] == "pass"
    raw = (tmp_path / "audit_log.json").read_text(encoding="utf-8")
    for secret in PII:
        assert secret not in raw, secret
    assert agent_trace.load_trace(tmp_path, tracking_key=ans.run_id) == trace


def test_mask_digits():
    assert agent_trace.mask_digits("學號 111900001 第 3 項") == "學號 ********* 第 3 項"


def test_output_tables_flatten_nested_rows():
    snap = _snapshot()
    out = T.run_tool("list_cases", {}, snap, T.Aliaser(snap))
    tables = A.output_tables(out)
    assert tables[0]["headers"] == ["欄位", "值"]
    assert tables[1]["title"] == "rows" and "stage" in tables[1]["headers"]


# ── 網頁 ────────────────────────────────────────────────

@pytest.fixture
def client(tmp_path, monkeypatch):
    from koushi.web import app as webapp
    from koushi.workflow import RegistryPaths
    from tests.att2_fixture import case as att2_case, write_att2
    from tests.period_fixture import open_filling_period
    cfg = webapp.WebConfig(
        submissions_dir=tmp_path / "submissions", master_path=tmp_path / "output" / "master.xlsx",
        state_dir=tmp_path / "state",
        registries=RegistryPaths(attachment2=write_att2(tmp_path / "office" / "att2.xlsx", [att2_case(term="1141")]),
                                 members=tmp_path / "office" / "missing.xlsx",
                                 distances=tmp_path / "office" / "missing.csv"),
        doc_output_root=tmp_path / "doc_output", office_password="pw",
    )
    monkeypatch.setitem(webapp.app.config, "_web", cfg)
    monkeypatch.setitem(webapp.app.config, "TESTING", True)
    open_filling_period(cfg.state_dir)
    fake = {"llm": None}
    monkeypatch.setattr("koushi.llm.make_llm", lambda: fake["llm"])
    with webapp.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["_csrf_token"] = "t"
        c.post("/login/office", data={"password": "pw", "csrf_token": "t"})
        (cfg.submissions_dir).mkdir(parents=True, exist_ok=True)
        (cfg.submissions_dir / f"{K1}.json").write_text(json.dumps(
            {"term": "1141", "student_id": "111900001", "student_email": "s1@example.org",
             "exam_date": "2026-11-01", "committees": [], "version": 1}, ensure_ascii=False), encoding="utf-8")
        yield c, fake, cfg


def test_page_hidden_and_disabled_without_llm(client):
    c, fake, _ = client
    assert "問答助理" not in c.get("/office/overview").get_data(as_text=True)
    page = c.get("/office/assistant").get_data(as_text=True)
    assert "尚未設定 LLM" in page and 'id="ask-form"' not in page
    assert "assistant_enabled" not in c.get("/office/settings").get_data(as_text=True)
    assert c.post("/office/assistant/ask", data={"question": "x", "csrf_token": "t"}).status_code == 403


def test_page_and_answer_render_case_links(client):
    c, fake, cfg = client
    fake["llm"] = FakeLLM([
        _tool("list_cases"),
        _answer("CASE_01 目前是等待附件2上傳。"),
    ])
    page = c.get("/office/assistant").get_data(as_text=True)
    assert 'href="/office/assistant"' in page and "哪些案件卡在待專員放行？" in page
    resp = c.post("/office/assistant/ask", data={"question": "1141-111900001 卡在哪？", "csrf_token": "t"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert f'data-case="{K1}"' in html and "查詢軌跡" in html and "list_cases" in html
    # 工具結果與工作總覽同源：總覽只有這一案
    assert fake["llm"].calls and "111900001" not in json.dumps(fake["llm"].calls, ensure_ascii=False)
    trace = agent_trace.load_trace(cfg.state_dir)
    assert {t["step"] for t in trace} >= {"question", "tool_call", "grounding_check"}


def test_settings_toggle_disables_assistant(client):
    c, fake, cfg = client
    fake["llm"] = FakeLLM([_answer("x", sources=())])
    assert "assistant_enabled" in c.get("/office/ai-settings").get_data(as_text=True)
    form = {"csrf_token": "t", "current_term": "1141",
            "fill_deadline": office_settings.load(cfg.state_dir).fill_deadline.isoformat(),
            "assistant_enabled": "0"}
    c.post("/office/settings", data=form)
    assert office_settings.load(cfg.state_dir).assistant_enabled is False
    assert "已在「期限設定」頁關閉" in c.get("/office/assistant").get_data(as_text=True)
    assert 'href="/office/assistant"' not in c.get("/office/overview").get_data(as_text=True)
    # 沒有 LLM 時表單不送這個欄位，原值保留
    form.pop("assistant_enabled")
    c.post("/office/settings", data=form)
    assert office_settings.load(cfg.state_dir).assistant_enabled is False


def test_refusal_and_fallback_render(client):
    c, fake, _ = client
    fake["llm"] = FakeLLM([_answer("不該被呼叫")])
    html = c.post("/office/assistant/ask",
                  data={"question": "幫我放行 1141-111900001", "csrf_token": "t"}).get_data(as_text=True)
    assert 'href="/office/overview#review-queue"' in html and "不能寄信、放行" in html
    assert not fake["llm"].calls
    fake["llm"] = FakeLLM([_tool("list_cases"), _answer("CASE_05 待專員放行，共 9 件。")])
    html = c.post("/office/assistant/ask", data={"question": "哪些待放行？", "csrf_token": "t"}).get_data(as_text=True)
    assert A.UNVERIFIED_NOTE in html and "<table>" in html and f'data-case="{K1}"' in html
    assert "CASE_05" in html  # 檢查原因照實列出模型編造的代號


# ── 反思（docs/自我修正設計.md）──────────────────────────

def test_rejected_answer_is_reflected_once_and_can_recover(tmp_path):
    ans, llm = _ask("1141-111900001 王小明卡在哪？", [
        _tool("get_case", case="CASE_01"),
        _answer("CASE_01 已放行。"),                                                  # 狀態說錯 → 被檢查擋下
        _answer("CASE_01 目前卡在學生缺件：口試日期尚未補，補件逾期，共 1 項。"),       # 依回饋改寫
    ], tmp_path)
    assert ans.kind == "answer", ans.problems
    assert len(llm.calls) == 3 and "沒有通過系統檢查" in llm.calls[2][1] and "已放行" in llm.calls[2][1]
    steps = [(t["step"], t.get("decision")) for t in agent_trace.load_trace(tmp_path, run_id=ans.run_id)]
    assert ("grounding_check", "repair") in steps and ("reflect", "repair") in steps
    assert steps[-1] == ("grounding_check", "pass")


def test_reflection_is_bounded():
    ans, llm = _ask("有幾件待專員放行？", [_tool("overview_counts"), _answer("共有 42 件待專員放行。")])
    assert ans.kind == "unverified" and len(llm.calls) == 3   # 1 次工具＋原回答＋1 次反思後的回答


# ── 評審實測的錯答（2026-10-09）：選錯工具／篩選、狀態對不上、規則與修改請求 ─────────────

def _released_snapshot():
    """案件 1 也已放行（沒有待專員放行），教授確認已寄出、期限未到（不是逾期）。"""
    from koushi.engines.confirmation import ConfirmationRequest as CR
    snap = _snapshot()
    snap.reviews[0] = ReviewRow(case_key=K1, student_id="111900001", student_name="王小明", exceptions=[],
                                review=CaseReview(case_key=K1, cleared_at="2026-10-06 10:00:00", cleared_by="專員",
                                                  note="", committee_signature="x"),
                                changed_since_clear=False)
    future = _dt.date(2099, 1, 1)
    pending = CR(group_key=("1141", "碩", "周教授", "11/2"), term="1141", advisor="周教授", degree="碩",
                 date_text="11/2", student_names=("李小華",), case_keys=(K2,), sent_date=_dt.date(2026, 10, 7),
                 deadline=Deadline(base_date=_dt.date(2026, 10, 7), business_days=5, computed_date=future))
    snap.tracking = TrackingResult(deficiency=DeficiencyReport(items=(), resolved_keys=frozenset()),
                                   confirmation=ConfirmationReport(requests=(pending,)),
                                   collection=CollectionReport(items=()))
    return snap


def _offline_ask(question, snap, tmp_path=None):
    from koushi.offline_llm import make_offline_llm
    llm = make_offline_llm()
    return A.ask(question, snapshot=snap, llm=llm, state_dir=tmp_path), llm


def test_offline_review_question_uses_filter_and_reports_empty():
    ans, _ = _offline_ask("哪些案件卡在待專員放行？", _released_snapshot())
    assert [(s.tool, s.args) for s in ans.steps] == [("list_cases", {"blocker": T.STAGE_REVIEW})]
    assert ans.kind == "answer", ans.problems
    assert ans.text == "目前沒有待專員放行的案件。"


def test_offline_review_question_lists_only_matching_cases():
    ans, _ = _offline_ask("哪些案件卡在待專員放行？", _snapshot())
    assert ans.kind == "answer", ans.problems
    assert "CASE_01" in ans.text and "CASE_02" not in ans.text


def test_offline_overdue_confirmation_question_is_not_answered_with_pending_advisor():
    ans, _ = _offline_ask("有哪些教授確認已經逾期未回？", _released_snapshot())
    assert [(s.tool, s.args) for s in ans.steps] == [("list_confirmations", {"status": "overdue"})]
    assert ans.kind == "answer", ans.problems
    assert ans.text.startswith("附件4 只寄給指導教授參考，教授不用回信")
    assert "PER_" not in ans.text


def test_offline_case_question_uses_get_case_and_document_numbers_pass():
    ans, _ = _offline_ask("王小明的口試是哪一天？教授確認了嗎？", _released_snapshot())
    assert ans.steps[0].tool == "get_case"
    assert ans.kind == "answer", ans.problems
    assert "2026-11-01" in ans.text and "附件4" in ans.text


@pytest.mark.parametrize("question, tool, args", [
    ("目前有哪些缺件？", "list_deficiencies", {}),
    ("學生缺件有幾件已經逾期？", "list_deficiencies", {"status": "overdue", "owner": "student"}),
    ("還有哪些委員沒有回填個資？", "list_committee_data", {"missing_only": True}),
    ("總共有幾件案件？", "overview_counts", {}),
    ("這學期的補件截止日是哪天？", "get_deadlines", {}),
    ("最近 7 天寄出幾封信？有失敗的嗎？", "communication_counts", {"days": 7}),
])
def test_offline_intents_pick_tool_and_pass_check(question, tool, args):
    ans, _ = _offline_ask(question, _snapshot())
    assert [(s.tool, s.args) for s in ans.steps] == [(tool, args)]
    assert ans.kind == "answer", ans.problems


def test_listing_answer_with_wrong_status_is_rejected():
    """問待專員放行，卻列出目前在別關的案件（評審實測：列出「附件6尚未交付」的案件仍通過）。"""
    ans, _ = _ask("哪些案件卡在待專員放行？", [
        _tool("list_cases"),
        _answer("- CASE_02：目前在「待教授確認」"),
    ])
    assert ans.kind == "unverified"
    assert any("待專員放行" in p and "CASE_02" in p for p in ans.problems)


def test_listing_pending_advisor_as_overdue_answer_is_rejected():
    ans, _ = _ask("有哪些教授確認已經逾期未回？", [
        _tool("list_confirmations"),
        _answer("- PER_03（CASE_02）：已寄出待回覆"),
    ], snap=_released_snapshot())
    assert ans.kind == "unverified"
    assert any("逾期未回" in p for p in ans.problems)


def test_claiming_overdue_when_tool_says_pending_is_rejected():
    ans, _ = _ask("有哪些教授確認已經逾期未回？", [
        _tool("list_confirmations"),
        _answer("- PER_03（CASE_02）：逾期未回"),
    ], snap=_released_snapshot())
    assert ans.kind == "unverified"


def test_listing_check_skips_specific_case_and_negation():
    ans, _ = _ask("CASE_01 待專員放行嗎？", [_tool("get_case", case="CASE_01"),
                                         _answer("CASE_01 目前卡在學生缺件。")])
    assert ans.kind == "answer", ans.problems
    assert A.requested_statuses("哪些案件還沒走完流程？", T.status_vocabulary()) == set()
    assert A.requested_statuses("哪些案件卡在待專員放行？", T.status_vocabulary()) == {T.STAGE_REVIEW}


def test_document_labels_are_not_numbers():
    ans, _ = _ask("CASE_02 的附件4 確認了嗎？", [
        _tool("get_case", case="CASE_02"),
        _answer("CASE_02 的附件4：已寄出；附件6 尚未產生。"),
    ])
    assert ans.kind == "answer", ans.problems
    assert A._numbers("附件2、附件 4、附件6 共 3 份") == {"3"}


@pytest.mark.parametrize("question", ["校外委員的交通費怎麼算？", "口試費的計算方式是什麼？", "補件期限為什麼是 7 天？"])
def test_policy_questions_answered_without_model(question, tmp_path):
    ans, llm = _ask(question, [_answer("不該被呼叫")], tmp_path)
    assert ans.kind == "out_of_scope" and not llm.calls
    assert "只查詢口試案件資料" in ans.text
    assert "office_settings_page" in [e for _, e, _ in ans.links]
    assert [t["step"] for t in agent_trace.load_trace(tmp_path, run_id=ans.run_id)] == ["question", "out_of_scope"]


@pytest.mark.parametrize("question", ["這學期的補件截止日是哪天？", "哪些委員交了撥帳資料？", "今天天氣如何？"])
def test_data_questions_are_not_policy(question):
    assert not A.detect_policy(question)


@pytest.mark.parametrize("question", [
    "請幫我把王小明的口試日期改到12月1日",
    "幫我把 1141-111900001 的口試日期改為 11/15",
    "可以幫我把王小明的口試延後一週嗎？",
    "請將 CASE_01 標記為已確認",
])
def test_modification_requests_are_refused_without_model(question):
    ans, llm = _ask(question, [_answer("不該被呼叫")])
    assert ans.kind == "refused" and not llm.calls
    assert ans.link_case == K1   # 問題裡的案件鍵／學生姓名 → 附上該案件詳情連結


def test_refusal_render_links_case_page(client):
    c, fake, _ = client
    fake["llm"] = FakeLLM([_answer("不該被呼叫")])
    html = c.post("/office/assistant/ask", data={"question": "請幫我把 1141-111900001 的口試日期改到12月1日",
                                                 "csrf_token": "t"}).get_data(as_text=True)
    assert "不能寄信、放行、修改" in html and f'data-case="{K1}"' in html and not fake["llm"].calls
    html = c.post("/office/assistant/ask", data={"question": "校外委員的交通費怎麼算？", "csrf_token": "t"}
                  ).get_data(as_text=True)
    assert "只查詢口試案件資料" in html and 'href="/office/settings"' in html and not fake["llm"].calls


def test_swapped_case_values_fail_grounding():
    """審查 F05：兩案日期對調不能通過；數字要來自同一句提到的那個案件。"""
    step = A.Step(1, "list_cases", {}, {"rows": [{"case": "CASE_01", "exam_date": "2026-11-01"},
                                                 {"case": "CASE_02", "exam_date": "2026-11-02"}]}, 0)
    ok = A.verify_answer("CASE_01 的口試日期是 2026-11-01；CASE_02 是 2026-11-02。", [1], [step], "口試日期", "2026-10-13")
    swapped = A.verify_answer("CASE_01 的口試日期是 2026-11-02；CASE_02 是 2026-11-01。", [1], [step], "口試日期",
                              "2026-10-13")
    assert ok == [] and swapped


def test_conclusion_without_any_tool_result_fails():
    """審查 F05：沒有查資料就下結論（「全部案件皆可立即產檔。」）不能通過；反問可以。"""
    assert A.verify_answer("全部案件皆可立即產檔。", [], [], "可以產檔嗎", "2026-10-13")
    assert A.verify_answer("您好，請問想查哪個案件？", [], [], "你好", "2026-10-13") == []
