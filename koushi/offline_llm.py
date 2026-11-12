# -*- coding: utf-8 -*-
"""離線備援 LLM（offline-replay）：不連網、不需 API key 的預錄／規則替身。

用途（競賽辦法：Demo 應準備離線備援）：
- `python -m show` 預設使用；`--live` 才改用 `koushi.llm.make_llm()` 的真實模型。
- 設定 `KOUSHI_LLM_PROVIDER=offline` 時，`make_llm()` 也回傳這裡的替身，網頁展示
  （python -m koushi 展示模式、python -m show.demo --web）同樣可以離線操作。

`ScriptedLLM` 實作 `StructuredLLM` Protocol：依 schema_name 取回應，可再依 user 內容的
子字串挑選；回應可以是 dict（回傳副本）、callable(system, user) -> dict 或 Exception（拋出）。

`make_offline_llm()` 的預設回應是**很笨的關鍵字規則**，不是模型：只求輸出格式正確、
能通過各 agent 自己的檢核（逐字引用、候選 id、級別白名單、回答 grounding），讓整條
agent 管線（任務分解、工具調用、政策閘門、人工核准、軌跡）能在無網路時完整展示。
它看不懂提示注入——正好示範「就算模型被騙，確定性政策仍會把動作降級」。
"""

from __future__ import annotations

import copy
import json
import re
from typing import Callable

from .llm import LLMError

OFFLINE_MODEL = "offline-replay"


class ScriptedLLM:
    """依 schema_name（與可選的 user 子字串）回傳預錄的 JSON。

    responses 範例::

        {"title_level": {"level": "教授級", "confidence": 0.6, "rationale": "…"},
         "mailbox_analysis": [("改成", modify_json), ("", fallback_json)],
         "assistant_step": lambda system, user: {...}}

    清單形式依序比對，子字串為空字串時一律符合；都不符合就拋 LLMError（呼叫端轉人工）。
    schema_name 查不到時改用 "*" 的回應。
    """

    def __init__(self, responses: dict, *, model_name: str = OFFLINE_MODEL):
        self.responses = dict(responses)
        self.model = self.model_name = model_name
        self.last_usage: dict | None = None
        self.calls: list[tuple[str, str]] = []   # (schema_name, user)

    def complete_json(self, system: str, user: str, *, schema_name: str) -> dict:
        self.calls.append((schema_name, user))
        entry = self.responses.get(schema_name, self.responses.get("*"))
        if entry is None:
            raise LLMError(f"離線備援沒有 {schema_name} 的預錄回應")
        if isinstance(entry, list):
            entry = next((resp for key, resp in entry if not key or key in user), None)
            if entry is None:
                raise LLMError(f"離線備援沒有符合這段內容的 {schema_name} 回應")
        if isinstance(entry, Exception):
            raise entry
        result = entry(system, user) if callable(entry) else copy.deepcopy(entry)
        if not isinstance(result, dict):
            raise LLMError("離線備援回應必須是 JSON 物件")
        return result


# ── 共用 ────────────────────────────────────────────────

_CLAUSE_SPLIT = re.compile(r"[，,。；;！!？?\n]")


def _clauses(text: str) -> list[str]:
    """切成子句；每段都是原文的連續子字串（只去頭尾空白），可當逐字引用。"""
    return [c.strip() for c in _CLAUSE_SPLIT.split(text or "") if c.strip()]


def _json(user: str) -> dict:
    try:
        data = json.loads(user)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# ── 信箱 agent：mailbox_analysis ─────────────────────────

_MAIL_FIELDS = (("口試日期", ("日期", "改期")), ("時間", ("時間", "時段")),
                ("口試地點", ("地點", "教室", "會議室")), ("方式", ("方式", "線上", "視訊", "實體")))
_CHANGE_VERBS = ("改成", "改到", "改為", "更改為", "換到")


def _mail_body(user: str) -> tuple[str, dict]:
    head = "來信內容（已去除引用與個資）：\n---\n"
    start = user.find(head)
    end = user.rfind("\n---\n")
    text = user[start + len(head):end] if start >= 0 and end > start else ""
    context = {}
    match = re.search(r"來信背景[^\n]*\n(\{.*?\})\n", user)
    if match:
        try:
            context = json.loads(match.group(1))
        except json.JSONDecodeError:
            context = {}
    return text, context


def _hits(clauses: list[str], words) -> list[str]:
    return [c for c in clauses if any(w in c for w in words)]


def mailbox_analysis(system: str, user: str) -> dict:
    text, context = _mail_body(user)
    role = context.get("sender_role", "")
    clauses = _clauses(text)
    result = {"category": "other", "summary": "", "evidence": [], "modifications": [],
              "questions": [], "suggested_actions": [], "suspicious": False}

    if hits := _hits(clauses, ("自動回覆", "休假中", "out of office")):
        result.update(category="auto_reply", summary="（離線備援）自動回覆信件。", evidence=hits[:1])
        return result
    changes = _hits(clauses, _CHANGE_VERBS)
    if role == "advisor" and changes:
        mods = []
        for clause in changes:
            verb = next(v for v in _CHANGE_VERBS if v in clause)
            field = next((name for name, words in _MAIL_FIELDS if any(w in clause for w in words)), "")
            new_value = clause.split(verb, 1)[1].strip()
            if field and new_value:
                mods.append({"row": "", "field": field, "new_value": new_value, "quote": clause})
        if mods:
            listed = "；".join(f"{m['field']} → {m['new_value']}" for m in mods)
            result.update(category="modify", summary=f"（離線備援）教授要求修改附件4：{listed}。",
                          evidence=[m["quote"] for m in mods], modifications=mods)
            return result
    if role == "student" and (hits := _hits(clauses, ("延後", "延期", "改期", "異動", "開放修改", "更正"))):
        result.update(category="student_request", summary="（離線備援）學生來信申請異動口試資料。",
                      evidence=hits[:2])
        return result
    if hits := _hits(clauses, ("確認無誤", "無誤", "沒問題", "同意如期")):
        result.update(category="confirm", summary="（離線備援）來信表示附件4 內容無誤。", evidence=hits[:1])
        return result
    if hits := _hits(clauses, ("撥款", "撥帳", "帳戶", "身分證")):
        result.update(category="committee_data", summary="（離線備援）委員來信提供或詢問撥帳資料。",
                      evidence=hits[:1])
        return result
    questions = [c for c in clauses if any(w in c for w in ("請問", "嗎", "如何", "怎麼"))]
    if questions:
        result.update(category="question", summary="（離線備援）來信提出疑問。",
                      evidence=questions[:1], questions=questions[:3])
        return result
    if clauses:
        result.update(summary="（離線備援）無法以關鍵字歸類，請專員查看。", evidence=clauses[:1])
    else:
        result.update(category="unknown", summary="（離線備援）沒有可判讀的文字。")
    return result


# ── 附件4 回信判讀（classifier）────────────────────────────

OFFLINE_CLASSIFIER_PROMPT = "offline-rules-v1"
_CONFIRM_WORDS = ("確認無誤", "無誤", "沒問題", "沒有問題", "同意", "確認", "OK", "ok", "Ok", "可以")
_RESERVATION_WORDS = ("但", "不過", "只是", "另外", "好像", "似乎")
_QUESTION_WORDS = ("請問", "嗎", "如何", "怎麼", "？", "?")


def classify_advisor_reply(reply_text: str):
    """離線備援的附件4 回信判讀：關鍵字規則，不是模型（不連網、不需金鑰）。

    輸出與真實模型相同的 ClassificationResult（evidence 一律是原文逐字子句），
    model 標為 offline-replay、prompt_version 標為 offline-rules-v1，畫面據此標示「離線備援」。
    是否自動確認仍由既有閘門決定（核對全過、開關、白名單、版本、疑似注入），這裡只判讀意圖。
    """
    from .classifier import ClassificationResult, Modification
    from .mailbox_agent import looks_like_injection
    clauses = _clauses(reply_text)
    base = {"model": OFFLINE_MODEL, "prompt_version": OFFLINE_CLASSIFIER_PROMPT}
    if not clauses:
        raise LLMError("（離線備援）回信沒有可判讀的文字")
    if looks_like_injection(reply_text):  # 與真實提示詞的安全規則一致：疑似注入一律 unknown
        return ClassificationResult(intent="unknown", evidence=clauses[0], **base)
    if hits := _hits(clauses, ("自動回覆", "休假中", "out of office", "代為回覆", "代回")):
        return ClassificationResult(intent="auto_reply", evidence=hits[0], **base)
    changes = _hits(clauses, _CHANGE_VERBS)
    mods = []
    for clause in changes:
        verb = next(v for v in _CHANGE_VERBS if v in clause)
        field = next((name for name, words in _MAIL_FIELDS if any(w in clause for w in words)), "")
        new_value = clause.split(verb, 1)[1].strip()
        if field and new_value:
            mods.append(Modification(field=field, new_value=new_value, quote=clause))
    confirms = _hits(clauses, _CONFIRM_WORDS)
    questions = [c for c in clauses if any(w in c for w in _QUESTION_WORDS)]
    # 「確認」出現在問句裡（「想確認一下…嗎」）不算確認
    plain_confirms = [c for c in confirms if c not in questions and c not in changes]
    if mods and not plain_confirms:
        return ClassificationResult(intent="modify", evidence=mods[0].quote, modifications=tuple(mods), **base)
    if plain_confirms:
        reservation = bool(mods or questions or changes) or any(
            w in c for c in clauses for w in _RESERVATION_WORDS)
        return ClassificationResult(intent="confirm", evidence=plain_confirms[0], has_reservation=reservation,
                                    modifications=tuple(mods), questions=tuple(questions[:3]), **base)
    if questions:
        return ClassificationResult(intent="question", evidence=questions[0], questions=tuple(questions[:3]), **base)
    if changes:
        return ClassificationResult(intent="question", evidence=changes[0], **base)
    return ClassificationResult(intent="unknown", evidence=clauses[0], **base)


def make_offline_classifier():
    """runtime.make_classifier() 在 KOUSHI_LLM_PROVIDER=offline 時用的回信判讀器。"""
    def classify(mail, request):
        return classify_advisor_reply(mail.reply_text)
    classify.offline = True
    return classify


# ── 提案型 agent ────────────────────────────────────────

_UNIT_ABBR = (("科大", "科技大學"), ("師大", "師範大學"), ("電子系", "電子工程學系"),
              ("電機系", "電機工程學系"), ("資工系", "資訊工程學系"), ("臺", "台"))


def _unit_key(unit: str) -> str:
    s = str(unit or "")
    for short, long in _UNIT_ABBR:
        if long not in s:
            s = s.replace(short, long)
    for word in ("國立", "私立", " ", "　"):
        s = s.replace(word, "")
    return s


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def affiliation_match(system: str, user: str) -> dict:
    data = _json(user)
    unit = _unit_key(data.get("服務單位", ""))
    best, best_score = None, 0.0
    for cand in data.get("候選") or []:
        a, b = _bigrams(unit), _bigrams(_unit_key(cand.get("單位", "")))
        score = len(a & b) / len(a | b) if a and b else 0.0
        if score > best_score:
            best, best_score = cand, score
    if best is None or best_score < 0.5:
        return {"match_id": None, "confidence": 0.3,
                "rationale": "（離線備援）候選中沒有明顯相同的學校與系所，建議專員手動指定或補距離表。"}
    return {"match_id": best["id"], "confidence": round(min(0.85, best_score), 2),
            "rationale": f"（離線備援）展開常見簡稱（如「科大」＝「科技大學」）並去掉「國立」等字後，與「{best['單位']}」"
                         f"的相似度為 {best_score:.2f}（候選清單的分數是展開簡稱前的規則比對，所以較低），"
                         "推測為同一學校同一系所。"}


_TITLE_LEVELS = (("助理教授", "助理教授級"), ("副教授", "副教授級"), ("教授", "教授級"),
                 ("助研究員", "助理教授級"), ("副研究員", "副教授級"), ("研究員", "教授級"),
                 ("assistant professor", "助理教授級"), ("associate professor", "副教授級"),
                 ("professor", "教授級"))
_TITLE_MODIFIERS = ("兼任", "客座", "退休", "名譽", "約聘", "專案", "合聘", "adjunct", "visiting", "emeritus")


def title_level(system: str, user: str) -> dict:
    title = str(_json(user).get("職稱", ""))
    lowered = title.lower()
    level = next((lv for word, lv in _TITLE_LEVELS if word in lowered), None)
    modifier = next((m for m in _TITLE_MODIFIERS if m in lowered), "")
    if level is None:
        return {"level": "無法判斷", "confidence": 0.5,
                "rationale": f"（離線備援）「{title}」不是學術職稱，無法對應職級，請專員依學經歷判斷。"}
    note = f"「{modifier}」是聘任類別，不改變職級本身；" if modifier else ""
    return {"level": level, "confidence": 0.6 if modifier else 0.8,
            "rationale": f"（離線備援）{note}職稱本身相當於{level}，是否符合資格仍由專員核對聘書。"}


def source_correction(system: str, user: str) -> dict:
    return {"action": "none", "field": None, "member_ref": None, "new_value": None, "confidence": 0.0,
            "rationale": "（離線備援）無法離線判斷正確值，建議專員在案件頁核對附件2 原檔後修正。"}


# ── 問答助理：assistant_step ─────────────────────────────
# 依常見問句意圖挑工具與篩選參數，回答只用工具結果的欄位組句（含「目前沒有…」的空結果）。
# 規則／政策問題與「幫我改…」由 assistant.py 在呼叫模型前就攔下，這裡不會收到。

_CASE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(?:CASE|STU)_\d+(?![0-9])")
_STEP_RE = re.compile(r"^步驟 (\d+)：(\w+)\((.*?)\) → (.*)$", re.MULTILINE)
_REVIEW_WORDS = ("待專員放行", "待放行", "放行")
_DEFICIENCY_WORDS = ("缺件", "補件")
_ASSIST_CONFIRM_WORDS = ("教授確認", "確認", "附件4", "學位口試一覽", "回信", "回覆", "教授")
_COMMITTEE_WORDS = ("委員個資", "委員資料", "個資", "撥帳", "回填", "身分", "帳號")
_DEADLINE_WORDS = ("期限", "截止", "學期", "階段")
_MAIL_WORDS = ("寄出", "寄了", "寄信", "幾封", "通訊", "信件", "寄送")
_SOURCE_WORDS = ("來源異常", "附件2查無", "查無")
_CASES_WORDS = ("卡", "關", "進度", "哪些案件", "還沒", "流程", "案件狀態")
_COUNT_WORDS = ("幾件", "幾個", "多少", "總共", "總數", "統計", "幾")
_LIST_WORDS = ("哪些", "哪幾", "列出", "名單", "是誰", "有誰")
_OUT_OF_SCOPE = "我只能回答口試案件相關的資料問題。"
_STAGE_NAMES = ("已撤銷", "附件2查無", "未填寫", "等待附件2上傳", "學生缺件", "待專員放行", "待教授確認",
                "委員個資未齊", "附件6尚未交付", "附件6待回收", "附件6需補正", "流程完成")
_CONFIRM_STATUS = (("逾期", "overdue"), ("需修改", "modified"), ("要求修改", "modified"),
                   ("還沒寄", "draft"), ("尚未寄", "draft"), ("未寄出", "draft"), ("已確認", "confirmed"))
_CONFIRM_LABEL = {"overdue": "逾期未回", "modified": "需修改", "draft": "尚未寄出", "confirmed": "已確認",
                  "pending": "已寄出"}   # 2026-10-12 起教授不用回信：沒有「待回覆／逾期」


def _has(text: str, words) -> bool:
    return any(w in text for w in words)


def _plan(question: str):
    """問題 → (工具, 參數)；看不出是案件資料問題回 None。"""
    q = question
    tokens = sorted(_CASE_TOKEN_RE.findall(q), key=lambda t: not t.startswith("CASE_"))
    if tokens:
        return "get_case", {"case": tokens[0]}
    if _has(q, _SOURCE_WORDS):
        return "list_source_issues", {}
    if _has(q, ("截止", "期限")) and not _has(q, _LIST_WORDS):   # 「補件截止日是哪天」問日期，不是列缺件
        return "get_deadlines", {}
    if _has(q, _REVIEW_WORDS):
        if _has(q, _COUNT_WORDS) and not _has(q, _LIST_WORDS):
            return "overview_counts", {}
        return "list_cases", {"blocker": "待專員放行"}
    if _has(q, _DEFICIENCY_WORDS):
        args = {}
        if "逾期" in q:
            args["status"] = "overdue"
        if "學生" in q:
            args["owner"] = "student"
        return "list_deficiencies", args
    if _has(q, _COMMITTEE_WORDS):
        return "list_committee_data", {"missing_only": not _has(q, ("全部", "所有", "已回填"))}
    if _has(q, _ASSIST_CONFIRM_WORDS):
        status = next((st for word, st in _CONFIRM_STATUS if word in q), "")
        return "list_confirmations", ({"status": status} if status else {})
    if _has(q, _DEADLINE_WORDS):
        return "get_deadlines", {}
    if _has(q, _MAIL_WORDS):
        days = re.search(r"(\d+)\s*天", q)
        return "communication_counts", ({"days": int(days.group(1))} if days else {})
    stage = next((s for s in _STAGE_NAMES if s in q), "")
    if stage:
        return "list_cases", {"blocker": stage}
    if _has(q, _CASES_WORDS):
        return "list_cases", {}
    if _has(q, _COUNT_WORDS) or "案件" in q:
        return "overview_counts", {}
    return None


def _rows(output: dict) -> list:
    rows = output.get("rows")
    return rows if isinstance(rows, list) else []


def _answer_list_cases(args: dict, output: dict) -> str:
    rows = _rows(output)
    want = args.get("blocker") or args.get("stage") or ""
    if not rows:
        return f"目前沒有{want}的案件。" if want else "目前沒有符合條件的案件。"
    head = f"卡在「{want}」的案件共 {output.get('count', len(rows))} 件：" if want else "各案件目前的關卡："
    lines = [head]
    for r in rows:
        stage = r.get("stage")
        if want and stage != want:
            lines.append(f"- {r.get('case')}：{want}（目前最前面的關卡是「{stage}」）")
        else:
            lines.append(f"- {r.get('case')}：目前在「{stage}」")
    return "\n".join(lines)


def _answer_get_case(output: dict) -> str:
    lines = [f"{output.get('case')} 目前在「{output.get('stage')}」。",
             f"- 口試日期：{output.get('exam_date') or '學生尚未提供'}"]
    confirmations = output.get("advisor_confirmations") or []
    for c in confirmations:
        lines.append(f"- 指導教授 {c.get('advisor') or '（未填）'} 的附件4：{c.get('status')}")
    if not confirmations:
        lines.append("- 附件4：尚未寄出")
    review = output.get("review")
    if isinstance(review, dict) and review.get("status"):
        lines.append(f"- 人工關卡：{review['status']}")
    deficiencies = output.get("deficiencies") or []
    if deficiencies:
        lines.append("- 缺件：" + "、".join(f"{d.get('field')}（{d.get('status')}）" for d in deficiencies))
    return "\n".join(lines)


def _answer_confirmations(args: dict, output: dict) -> str:
    """2026-10-12 起附件4 只寄給教授參考、不用回信：回答寄送狀況，不談回覆期限或逾期。"""
    rows = _rows(output)
    status = args.get("status") or ""
    if status == "overdue":
        sent = (output.get("counts_by_status") or {}).get(_CONFIRM_LABEL["pending"])
        text = "附件4 只寄給指導教授參考，教授不用回信，所以沒有逾期未回的問題。"
        return text + (f"目前已寄出 {sent} 件。" if sent else "")
    label = _CONFIRM_LABEL.get(status, "")
    title = f"{label}的附件4" if label else "附件4 寄送狀況"
    if not rows:
        return f"目前沒有{title}。"
    lines = [f"{title}共 {output.get('count', len(rows))} 件："]
    for r in rows:
        lines.append(f"- {r.get('advisor')}（{'、'.join(r.get('cases') or [])}）：{r.get('status')}")
    return "\n".join(lines)


def _answer_deficiencies(args: dict, output: dict) -> str:
    rows = _rows(output)
    what = "補件逾期的缺件" if args.get("status") == "overdue" else "未解決的缺件"
    if not rows:
        return f"目前沒有{what}。"
    lines = [f"{what}共 {output.get('count', len(rows))} 項，涉及 {output.get('cases_affected', 0)} 件案件："]
    for r in rows:
        deadline = f"，期限 {r['deadline']}" if r.get("deadline") else ""
        lines.append(f"- {r.get('case')}：{r.get('field')}（{r.get('status')}{deadline}）")
    return "\n".join(lines)


def _answer_committee(output: dict) -> str:
    rows = _rows(output)
    if not rows:
        return "目前沒有尚未回填撥帳／身分資料的委員。" if output.get("missing_only") else "目前沒有需要撥帳資料的委員。"
    lines = [f"共 {output.get('count', len(rows))} 位委員："]
    for r in rows:
        lines.append(f"- {r.get('member')}（{'、'.join(r.get('cases') or [])}）：{r.get('data')}，{r.get('request')}")
    return "\n".join(lines)


def _answer_counts(question: str, output: dict) -> str:
    text = f"目前共 {output.get('total_cases', 0)} 件案件，待專員放行 {output.get('review_pending', 0)} 件。"
    stages = output.get("cases_by_stage") or {}
    if stages and not _has(question, _REVIEW_WORDS):
        text += "\n各關卡：" + "、".join(f"{k} {v} 件" for k, v in stages.items())
    return text


def _answer_deadlines(output: dict) -> str:
    def v(key):
        return output.get(key) or "尚未設定"
    return (f"本期學期 {v('current_term')}，目前階段：{v('phase')}。\n"
            f"- 填寫截止日：{v('fill_deadline')}\n"
            f"- 補件截止日：{v('deficiency_deadline')}"
            f"（補件期限 {output.get('deficiency_days')} 個{output.get('day_counting') or '日'}）")


def _answer_mail(output: dict) -> str:
    window = f"最近 {output['window_days']} 天" if output.get("window_days") is not None else "全部通訊記錄"
    return (f"{window}共 {output.get('total', 0)} 封，已寄出 {output.get('sent', 0)} 封、"
            f"寄送失敗 {output.get('failed', 0)} 封。")


def _answer_source_issues(output: dict) -> str:
    rows = _rows(output)
    if not rows:
        return "目前沒有尚未處理的附件2 來源異常。"
    return "\n".join([f"尚未處理的附件2 來源異常共 {output.get('count', len(rows))} 筆："]
                     + [f"- {r.get('field')}：{r.get('description')}" for r in rows])


_ANSWERS = {
    "list_cases": lambda q, a, o: _answer_list_cases(a, o),
    "get_case": lambda q, a, o: _answer_get_case(o),
    "list_confirmations": lambda q, a, o: _answer_confirmations(a, o),
    "list_deficiencies": lambda q, a, o: _answer_deficiencies(a, o),
    "list_committee_data": lambda q, a, o: _answer_committee(o),
    "overview_counts": lambda q, a, o: _answer_counts(q, o),
    "get_deadlines": lambda q, a, o: _answer_deadlines(o),
    "communication_counts": lambda q, a, o: _answer_mail(o),
    "list_source_issues": lambda q, a, o: _answer_source_issues(o),
}


def assistant_step(system: str, user: str) -> dict:
    question = user.split("\n", 1)[0].removeprefix("問題：")
    steps = _STEP_RE.findall(user)
    if not steps:
        plan = _plan(question)
        if plan is None:
            return {"type": "answer", "answer": _OUT_OF_SCOPE, "sources": []}
        tool, args = plan
        return {"type": "tool_call", "tool": tool, "args": args}
    index, tool, raw_args, raw = steps[-1]
    try:
        output = json.loads(raw)
        args = json.loads(raw_args) if raw_args else {}
    except json.JSONDecodeError:
        output, args = {}, {}
    if not isinstance(output, dict) or "error" in output:
        if tool == "get_case" and len(steps) == 1:   # 學生代號對到多個案件等：改查案件清單
            return {"type": "tool_call", "tool": "list_cases", "args": {}}
        return {"type": "answer", "answer": "查詢工具回報錯誤，請直接查看工作總覽。", "sources": []}
    answer = _ANSWERS.get(tool, lambda q, a, o: "查詢結果如下表，請參考工具結果。")
    return {"type": "answer", "answer": answer(question, args if isinstance(args, dict) else {}, output),
            "sources": [int(index)]}


# ── 信箱 agent：提問回信（mailbox_answer）──────────────────
# 關鍵字對欄位、與過去回覆比字面相似度；只輸出 {F#} 代號，真值由程式代入。

_ANSWER_FIELDS = (
    ("口試日期", ("哪天", "哪一天", "日期", "幾號", "幾月")),
    ("口試時間", ("幾點", "時間", "時段")),
    ("口試地點", ("教室", "地點", "在哪", "哪裡", "會議室")),
    ("口試方式", ("方式", "視訊", "線上", "實體")),
    ("口試委員", ("委員",)),
    ("本期附件填寫截止日", ("截止", "期限", "什麼時候交")),
)


def _payload(user: str) -> dict:
    start = user.find("{")
    try:
        data, _ = json.JSONDecoder().raw_decode(user[start:]) if start >= 0 else ({}, 0)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def mailbox_answer(system: str, user: str) -> dict:
    data = _payload(user)
    by_label = {re.sub(r"^案件\d+的", "", f["label"]): f["id"] for f in data.get("facts") or []}
    memory = data.get("memory") or []
    sentences, used_facts, used_memory, unanswered = [], [], [], []
    for question in data.get("questions") or []:
        best = max(memory, default=None, key=lambda m: max(
            (_similar(question, q) for q in m.get("questions") or []), default=0))
        if best and max(_similar(question, q) for q in best["questions"]) >= 0.35:
            answer = best["answer"]
            if all(label in by_label for label in re.findall(r"【([^】]+)】", answer)):
                sentences.append(re.sub(r"【([^】]+)】", lambda m: "{" + by_label[m.group(1)] + "}", answer))
                used_memory.append(best["id"])
                continue
        hits = [label for label, words in _ANSWER_FIELDS if label in by_label and _has(question, words)]
        if hits:
            sentences.append("；".join(f"您的{label}是 {{{by_label[label]}}}" for label in hits) + "。")
            used_facts += [by_label[label] for label in hits]
        else:
            unanswered.append(question)
    if unanswered and not sentences:
        context = [f"{{{by_label[k]}}}" for k in ("口試日期", "口試時間") if k in by_label]
        sentences.append("感謝來信。" + (f"您的口試排在 {' '.join(context)}"
                                        + (f"，地點 {{{by_label['口試地點']}}}" if "口試地點" in by_label else "")
                                        + "。" if context and any("當天" in q for q in unanswered) else ""))
    return {"answer": "\n".join(sentences), "used_facts": used_facts, "used_memory": used_memory,
            "unanswered": unanswered}


def _similar(a: str, b: str) -> float:
    x, y = _bigrams(re.sub(r"[\s，。？?！!、]", "", a)), _bigrams(re.sub(r"[\s，。？?！!、]", "", b))
    return len(x & y) / len(x | y) if x and y else 0.0


DEFAULT_RESPONSES: dict[str, Callable[[str, str], dict]] = {
    "mailbox_analysis": mailbox_analysis,
    "mailbox_answer": mailbox_answer,
    "affiliation_match": affiliation_match,
    "title_level": title_level,
    "source_correction": source_correction,
    "assistant_step": assistant_step,
}


def make_offline_llm(extra: dict | None = None) -> ScriptedLLM:
    """預設的離線備援 LLM；extra 可覆寫或補充個別 schema 的回應。"""
    return ScriptedLLM({**DEFAULT_RESPONSES, **(extra or {})})


# ── 展示用故障模擬（只包在展示／試用的模型外面，正式流程不使用）─────────────

class FlakyLLM:
    """包住模型；信中含指定字串時模擬連線逾時，用來示範「模型失敗 → 轉人工」。"""

    def __init__(self, inner, fail_marker: str):
        self.inner, self.fail_marker = inner, fail_marker
        self.model = self.model_name = getattr(inner, "model_name", None) or getattr(inner, "model", None)
        self.calls, self.failed = 0, False

    @property
    def last_usage(self):  # 真模型的 token 用量照常記進軌跡與成效統計；模擬逾時那次沒有用量
        return None if self.failed else getattr(self.inner, "last_usage", None)

    def complete_json(self, system, user, *, schema_name):
        self.calls += 1
        self.failed = self.fail_marker in user
        if self.failed:
            raise LLMError("模擬：模型連線逾時（timeout）")
        return self.inner.complete_json(system, user, schema_name=schema_name)


class SloppyOnceLLM:
    """包住模型；信中含指定字串時，第一次判讀把引用「改寫」成不是原文逐字，用來示範自我修正。

    真實模型偶爾會改寫引用；程式檢核擋下後把原因回饋給模型，第二次照規則逐字引用。
    """

    def __init__(self, inner, marker: str):
        self.inner, self.marker, self.fired = inner, marker, False
        self.model = self.model_name = getattr(inner, "model_name", None) or getattr(inner, "model", None)

    @property
    def last_usage(self):
        return getattr(self.inner, "last_usage", None)

    def complete_json(self, system, user, *, schema_name):
        out = self.inner.complete_json(system, user, schema_name=schema_name)
        if (schema_name == "mailbox_analysis" and not self.fired and self.marker in user
                and "系統檢核回饋" not in user and isinstance(out, dict)):
            self.fired = True
            out = dict(out, evidence=[f"教授說{e}" for e in out.get("evidence") or ["要改時間"]])
        return out
