# -*- coding: utf-8 -*-
"""畫面用的中文標籤（只影響顯示，不改資料）。

系統內部用英文代碼（問題代碼、責任、agent 步驟、決策、角色、狀態）與學期代碼（1151）
記錄資料；系辦與評審看的頁面一律換成中文。原始代碼保留在網址、表單值與 title 提示，
查核時仍對得回稽核日誌與 JSON 匯出。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime

from .. import mailbox_agent as MA

# ── 學期 ─────────────────────────────────────────────
_TERM_RE = re.compile(r"^(\d{2,3})([123])$")
_SEMESTER = {"1": "第1學期", "2": "第2學期", "3": "暑期"}


def term_label(term) -> str:
    """學期代碼轉中文：1151 → 115學年度第1學期；不認得的格式原樣顯示。"""
    text = str(term or "").strip()
    m = _TERM_RE.match(text)
    if not m:
        return text
    return f"{m.group(1)}學年度{_SEMESTER[m.group(2)]}"


def case_label(case_key) -> str:
    """案件鍵（學期-學號）轉成「115學年度第1學期・學號 999900001」；格式不符原樣顯示。"""
    text = str(case_key or "")
    term, sep, sid = text.partition("-")
    if not sep or not _TERM_RE.match(term):
        return text
    return f"{term_label(term)}・學號 {sid}"


# ── 檢核問題代碼與責任 ───────────────────────────────
ISSUE_LABELS = {
    "MISSING_SUBMISSION": "學生尚未在系統填寫",
    "NO_EXAM_DATE": "未填口試日期",
    "SUBMISSION_PARSE_ERROR": "填寫資料無法讀取",
    "ADVISOR_SOURCE_UNAVAILABLE": "未填指導教授",
    "MISSING_COMMITTEE_EMAIL": "委員 E-mail 未填",
    "MISSING_COMMITTEE_LIST": "未填委員名單",
    "NO_COMMITTEE": "沒有委員資料",
    "SUBMISSION_WITHOUT_PORTAL_CASE": "附件2 查無此案",
    "COMMITTEE_MISMATCH": "委員名單與附件2 不一致",
    "COMMITTEE_RANK_EXCEPTION": "委員職級需人工判斷",
    "COMMITTEE_MATCHED_BY_NAME": "委員以姓名對到舊資料，請確認同一人",
    "EXAM_DATE_TOO_EARLY": "口試日期太近，流程可能來不及",
    "COMMITTEE_COUNT_BELOW_MIN": "委員人數不足",
    "CASE_REVIEW_PENDING": "尚未經專員放行",
    "COMMITTEE_DATA_MISSING_BEFORE_EXAM": "口試將至，委員仍未提供撥帳資料",
    "DUPLICATE_COMMITTEE_IDENTITY": "委員身分重複",
    "NO_DISTANCE": "交通費查無距離",
    "DUPLICATE_SUBMISSION": "重複填寫",
    "UNVERIFIED_COMMITTEE_IDENTITY": "委員身分待確認（等委員提供）",
    "MISSING_BANK_INFO": "委員撥帳資料待委員提供",
    "CONFIRMATION_PENDING": "指導教授尚未回覆附件4（已停用，不再阻擋）",
    "CASE_CANCELLED": "案件已撤銷",
    "COMMITTEE_CHANGED": "委員異動申請",
    "STUDENT_OVERDUE_INCOMPLETE": "補件逾期仍未完成",
    "STUDENT_NAME_CONFLICT": "學生姓名與附件2 不一致",
    "COMMITTEE_EMAIL_CONFLICT": "委員 E-mail 衝突",
    "COMMITTEE_IDENTITY_MISMATCH": "委員身分不一致",
    "CORRECTION_SOURCE_CHANGED": "來源已變更，專員修正需重新確認",
    "CORRECTION_TARGET_MISSING": "專員修正的對象已不存在",
    "MISSING_ATTACHMENT_3": "缺附件3",
    "ATTACHMENT_3_PARSE_ERROR": "附件3 無法讀取",
    "ATTACHMENT_2_MISSING_CASE_KEY": "附件2 缺學期或學號",
    "ATTACHMENT_2_MISSING_COMMITTEE_IDENTITY": "附件2 缺委員身分",
    "DUPLICATE_ATTACHMENT_2_COMMITTEE": "附件2 委員重複",
    "ATTACHMENT_2_MULTIPLE_RECORDS": "附件2 同一案有多筆",
    "ATTACHMENT_3_WITHOUT_APPLICATION": "附件3 查無申請",
    "ATTACHMENT_2_CASE_FIELD_CONFLICT": "附件2 欄位互相矛盾",
    "DUPLICATE_ATTACHMENT_3": "附件3 重複",
}

OWNER_LABELS = {
    "student": "學生補件",
    "office": "系辦處理",
    "post_exam": "向委員索取（口試前後補齊，不找學生）",
    "none": "僅供參考",
}


_ISSUE_TEXT_REPLACEMENTS = (
    ("委員身分為暫定（P...），待委員本人提供身分資料後才能跨案件合併",
     "委員身分尚待確認：系統向委員本人索取身分資料後，才能和其他案件的同一位委員合併"),
    ("尚未收到本系統的補充資料填寫", "學生尚未在本系統送出補充資料"),
    ("尚未收到介面填寫", "學生尚未在本系統送出補充資料"),
    ("尚未在介面填寫", "尚未在學生填寫頁填寫"),
)
_LEGACY_ID_RE = re.compile(r"舊口委資料庫 (C\d+) 的資料")


def friendly_issue_text(text) -> str:
    """檢核說明裡的內部用語換成一般說法（不改資料）。"""
    out = str(text or "")
    for old, new in _ISSUE_TEXT_REPLACEMENTS:
        out = out.replace(old, new)
    return _LEGACY_ID_RE.sub(r"舊口委資料庫（編號 \1）的資料", out)


def friendly_value(value, field: str = "") -> str:
    """專員修正下拉選單的「目前值」：JSON 名單、true/false 改成可讀文字。"""
    text = str(value or "")
    if field == "視訊出席":
        return {"true": "是", "false": "否"}.get(text.lower(), text)
    if field in ("committee_list", "advisor_emails") and text.startswith(("[", "{")):
        try:
            data = json.loads(text)
        except ValueError:
            return text
        if field == "advisor_emails" and isinstance(data, dict):
            return "、".join(f"{k} {v}" for k, v in data.items())
        if isinstance(data, list):
            parts = []
            for m in data:
                if isinstance(m, dict):
                    extra = "，".join(x for x in (m.get("unit"), m.get("title"), "視訊" if m.get("remote") else "") if x)
                    parts.append(f"{m.get('name', '')}（{extra}）" if extra else str(m.get("name", "")))
            return f"{len(parts)} 位：" + "、".join(parts)
    return text


def field_label(label) -> str:
    """修正欄位名稱去掉格式說明（JSON、true／false），格式說明改放在輸入框提示。"""
    return (str(label or "").replace("（姓名與Email的JSON物件）", "")
            .replace("（JSON；姓名、單位、職稱、Email與視訊出席）", "（整份名單）")
            .replace("（true／false）", ""))


def issue_label(code) -> str:
    return ISSUE_LABELS.get(str(code or ""), str(code or ""))


_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


def codes_to_labels(text) -> str:
    """字串中出現的問題代碼（例如軌跡輸入摘要、建議主題）換成中文；不認得的代碼保留。"""
    return _CODE_RE.sub(lambda m: ISSUE_LABELS.get(m.group(0), m.group(0)), str(text or ""))


def owner_label(owner) -> str:
    return OWNER_LABELS.get(str(owner or ""), str(owner or ""))


# ── Agent 軌跡 ───────────────────────────────────────
STEP_LABELS = {
    # 信箱 agent
    "parse": "解析來信", "identify_sender": "辨識寄件者", "link_case": "連結案件", "pii_guard": "個資防護",
    "validate": "模型判讀", "analyze": "判讀結果彙整", "replan": "重新規劃修改項目", "plan_actions": "規劃動作",
    "policy": "政策判斷", "execute": "自動執行", "done": "完成",
    "staff_review": "專員審核", "staff_mark": "專員標記整封信",
    "draft_answer_check": "AI 擬回信", "draft_answer": "回信草稿", "remember_answer": "記下專員回覆",
    # 提案型 agent
    "retrieve_candidates": "檢索候選單位", "rule_check": "規則判斷", "llm_call": "模型判讀",
    "propose": "提出建議", "human_decision": "專員決定",
    # 問答助理
    "question": "收到問題", "refused": "拒絕動作請求", "llm_error": "模型失敗", "tool_call": "查詢資料",
    "grounding_check": "根據檢查", "reflect": "反思補查",
}
_ATTEMPT_RE = re.compile(r"第\s*(\d+)\s*次")

DECISION_LABELS = {
    "pending_human": "待專員決定", "reference_only": "僅供參考", "draft": "草稿待核准", "suggest": "建議",
    "auto": "自動完成", "ignore": "略過", "awaiting_staff": "待專員處理", "auto_done": "已自動完成",
    "handled": "已處理", "ignored": "已略過", "pass": "檢核通過", "fail": "檢核未通過",
    "partial": "部分通過", "repair": "回饋原因請模型重來", "error": "模型失敗", "tool_call": "查詢工具",
    "approved": "已採用", "overridden": "已改指定", "rejected": "不採用", "approve": "核准", "reject": "退回",
    "done": "已處理", "action_request:rule": "拒絕動作請求（規則）", "action_request:model": "拒絕動作請求（模型）",
}

ROLE_LABELS = dict(MA.ROLE_LABELS)
REQUEST_STATE_LABELS = {"pending": "等待回覆", "confirmed": "已確認", "modified": "需修改", "stale": "舊版本代碼",
                        "not_sent": "尚未寄出", "none": "無"}
INBOX_STATUS_LABELS = {"pending_review": "教授回信待判讀", "applied": "已套用", "dismissed": "已略過"}
RISK_LABELS = {"high": "高", "medium": "中", "low": "低"}

# 軌跡輸出常見欄位的中文名稱
_KEY_LABELS = {
    "reply_chars": "本文字數", "quote_stripped": "已去除引用", "attachments": "附件數", "injection_flag": "疑似夾帶指令",
    "case_keys": "案件", "other_roles": "其他身分", "role": "身分", "verified": "身分已驗證",
    "unverified_case_keys": "信中提到但未驗證的案件", "ref": "附件4 代碼", "request_state": "附件4 請求狀態",
    "confirmation_inbox": "教授回信判讀", "redactions": "遮蔽個資處數", "send_to_model": "送模型",
    "ok": "通過檢核", "problems": "檢核問題", "error": "錯誤", "partial": "部分通過",
    "category": "分類", "summary": "摘要", "modifications": "修改項目數", "suspicious": "疑似夾帶指令",
    "dropped": "被丟棄的動作", "attempts": "模型嘗試次數", "repaired": "曾自我修正",
    "id": "代號", "type": "動作", "target_case": "案件", "source": "來源", "reason": "理由", "risk": "風險",
    "status": "狀態", "result": "結果", "replaced": "重新整理項目數", "mapped": "對上欄位數",
    "name": "單位", "score": "分數", "suggestion": "建議", "confidence": "信心", "method": "方法",
    "target": "對應單位", "level": "級別", "field": "欄位", "member": "委員", "old": "原值", "new": "新值",
}
_SOURCE_LABELS = {"rule": "規則", "llm": "AI 提出", "model": "模型"}
_METHOD_LABELS = {"rule": "規則比對", "llm": "模型排序", "none": "無"}


def step_label(step, input_summary: str = "") -> str:
    """步驟名稱；模型判讀每一次嘗試標成「模型判讀（第 n 次）」，放在「判讀結果彙整」之前才合理。"""
    name = str(step or "")
    label = STEP_LABELS.get(name, name)
    if name in ("validate", "llm_call", "replan", "draft_answer_check"):
        m = _ATTEMPT_RE.search(str(input_summary or ""))
        if m:
            label = f"{label}（第 {m.group(1)} 次）"
    return label


def decision_label(decision) -> str:
    if decision is None or decision == "":
        return ""
    if isinstance(decision, dict):
        return readable(decision)
    text = str(decision)
    if text in MA.POLICY_LABELS:
        return MA.POLICY_LABELS[text]
    return DECISION_LABELS.get(text, text)


def _value_label(key: str, value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (list, tuple)):
        if not value:
            return "無"
        return "、".join(_value_label(key, v) for v in value)
    if isinstance(value, dict):
        return readable(value)
    text = str(value)
    if key in ("case_keys", "target_case", "unverified_case_keys"):
        return case_label(text)
    if key == "role":
        return ROLE_LABELS.get(text, text)
    if key == "category":
        return MA.CATEGORY_LABELS.get(text, text)
    if key == "type":
        return MA.ACTION_LABELS.get(text, text)
    if key == "request_state":
        return REQUEST_STATE_LABELS.get(text, text)
    if key == "confirmation_inbox":
        return INBOX_STATUS_LABELS.get(text, text)
    if key == "risk":
        return RISK_LABELS.get(text, text)
    if key == "status":
        return (MA.MAIL_STATUS_LABELS.get(text) or MA.ACT_STATUS_LABELS.get(text) or DECISION_LABELS.get(text, text))
    if key == "source":
        return _SOURCE_LABELS.get(text, text)
    if key == "method":
        return _METHOD_LABELS.get(text, text)
    if key == "confidence" and isinstance(value, (int, float)):
        return f"{round(value * 100)}%"
    return text


def readable(output) -> str:
    """把軌跡輸出（dict／list）換成一行中文摘要；無法辨識的欄位保留原名。"""
    if output is None:
        return ""
    if isinstance(output, dict):
        if not output:
            return "（無）"
        if set(output) == {"ok"}:
            return "通過檢核" if output["ok"] else "未通過檢核"
        parts = []
        for key, value in output.items():
            if key == "ok" and value is True:
                parts.append("通過檢核")
                continue
            parts.append(f"{_KEY_LABELS.get(key, key)}：{_value_label(key, value)}")
        return "；".join(parts)
    if isinstance(output, (list, tuple)):
        if not output:
            return "（無）"
        return " ／ ".join(readable(v) if isinstance(v, (dict, list, tuple)) else str(v) for v in output)
    if isinstance(output, bool):
        return "是" if output else "否"
    return str(output)


def pretty_json(obj) -> str:
    """原始資料（中文不轉成 \\u 跳脫）。"""
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


# ── 時間 ─────────────────────────────────────────────

def fmt_time(value) -> str:
    """ISO 時間（2026-10-10T10:00:00+08:00）轉成 2026-10-10 10:00；日期原樣、無法解析原樣。"""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if len(text) <= 10:
        return parsed.date().isoformat()
    return parsed.strftime("%Y-%m-%d %H:%M")


def register(app) -> None:
    env = app.jinja_env
    env.filters.update(term_label=term_label, case_label=case_label, issue_label=issue_label,
                       owner_label=owner_label, step_label=step_label, decision_label=decision_label,
                       readable=readable, pretty_json=pretty_json, fmt_time=fmt_time,
                       friendly_issue_text=friendly_issue_text, friendly_value=friendly_value,
                       field_label=field_label, codes_to_labels=codes_to_labels)
    env.globals.update(term_label=term_label, issue_label=issue_label, step_label=step_label,
                       decision_label=decision_label)
