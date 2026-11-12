# -*- coding: utf-8 -*-
"""Agent 軌跡查核（唯讀）：把 audit_log.json 的 agent_trace 事件整理成「一次執行」清單與明細。

一次執行＝一個任務 (agent, run_id, 追蹤鍵)，與 koushi/agent_metrics.py 的任務單位相同：
信箱 agent 一封信、提案型 agent 一個建議主題、問答助理一個問題。專員對它做的決定
（staff_review／staff_mark／human_decision 步驟）用同一組鍵記錄，所以一併列在明細裡。

個資：軌跡寫入時（koushi/agent_trace.py）只遮了 input_summary 的長數字；output 與追蹤鍵
可能含案件鍵（學期-學號）、信件 Message-ID 或專員姓名。本模組顯示與匯出前一律再過
`sanitize()`：所有字串的 8 碼以上數字遮成 *、電子郵件只留第一個字與網域。頁面與 JSON
匯出都只用遮蔽後的內容。
"""

from __future__ import annotations

import hashlib
import json
import re

from . import agent_metrics as AM
from .agent_trace import load_trace, mask_digits

EXPORT_FORMAT = "koushi-agent-trace-v1"
AGENT_LABELS = {
    "mailbox": "信箱 agent",
    "affiliation": "服務單位對應",
    "correction": "來源異常修正",
    "title": "職稱判讀",
    "assistant": "問答助理",
}
DECISION_LABELS = {
    "pending_human": "待專員決定", "reference_only": "僅供參考", "draft": "草稿待核准", "suggest": "建議",
    "auto": "自動完成", "ignore": "略過", "awaiting_staff": "待專員處理", "auto_done": "已自動完成",
    "handled": "已處理", "ignored": "已略過", "pass": "檢查通過", "fail": "檢查未通過", "tool_call": "查詢工具",
    "approved": "已採用", "overridden": "已改指定", "rejected": "不採用", "approve": "核准", "reject": "退回",
    "done": "已處理", "action_request:rule": "拒絕動作請求（規則）", "action_request:model": "拒絕動作請求（模型）",
}
STEP_ORDER_NOTE = "步驟依稽核日誌 sequence 排序（只能追加，不可改寫）"
_EMAIL = re.compile(r"([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


def sanitize(value):
    """顯示／匯出前的遮蔽：長數字（學號、身分證、帳號）與電子郵件帳號；遞迴處理 dict／list。"""
    if isinstance(value, str):
        return _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", mask_digits(value))
    if isinstance(value, dict):
        return {sanitize(str(k)): sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v) for v in value]
    return value


def _key_tuple(key) -> tuple:
    if key is None:
        return ()
    if isinstance(key, (list, tuple)):
        return tuple(str(k) for k in key)
    return (str(key),)


def task_id(agent, run_id, tracking_key) -> str:
    """一次執行的穩定代號（網址用）；由 agent、run_id、追蹤鍵雜湊而來，不含原始值。"""
    raw = json.dumps([str(agent or ""), str(run_id or ""), list(_key_tuple(tracking_key))], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _step_view(event: dict) -> dict:
    d = event.get("detail") or {}
    error = AM.llm_error(d) if AM.is_llm_step(d) else ""
    if not error:
        error = AM.tool_error(d) if d.get("tool") and d.get("step") not in AM.HUMAN_STEPS else ""
    return sanitize({
        "sequence": event.get("sequence"), "at": event.get("at"), "step": d.get("step"), "tool": d.get("tool"),
        "input_summary": d.get("input_summary") or "", "output": d.get("output"), "decision": d.get("decision"),
        "human_required": bool(d.get("human_required")), "latency_ms": d.get("latency_ms"),
        "usage": d.get("usage"), "model": d.get("model"), "prompt_version": d.get("prompt_version"),
        "llm_step": AM.is_llm_step(d), "staff": d.get("step") in AM.HUMAN_STEPS, "error": error,
    })


def _final_decision(agent: str, system: list[dict]) -> str:
    if agent == "mailbox":
        done = next((d for d in reversed(system) if d.get("step") == "done"), None)
        status = ((done or {}).get("output") or {}).get("status") if done else None
        if status:
            return str(status)
    for d in reversed(system):
        if d.get("decision"):
            return str(d["decision"])
    return ""


def _staff_outcome(d: dict) -> str:
    """專員決定的結果：AI 建議記在 decision；信箱 agent 記在輸入摘要最後一個字（approve／reject／handled…）。"""
    if d.get("step") == "human_decision":
        return str(d.get("decision") or "")
    words = str(d.get("input_summary") or "").split()
    return words[-1] if words else ""


def _summarize(agent, run_id, key, events: list[dict]) -> dict:
    details = [e.get("detail") or {} for e in events]
    system = [d for d in details if d.get("step") not in AM.HUMAN_STEPS]
    staff = [d for d in details if d.get("step") in AM.HUMAN_STEPS]
    usage = [d.get("usage") or {} for d in system]
    models = [str(d["model"]) for d in system if d.get("model")]
    versions = [str(d["prompt_version"]) for d in system if d.get("prompt_version")]
    errors = [e for e in (AM.llm_error(d) if AM.is_llm_step(d) else AM.tool_error(d) if d.get("tool") else ""
                          for d in system) if e]
    latencies = [int(d["latency_ms"]) for d in system if d.get("latency_ms") is not None]
    # 問答助理的追蹤鍵只是 run_id；清單改顯示（已遮蔽的）問題本身
    question = next((str(d.get("input_summary") or "") for d in system if d.get("step") == "question"), "")
    return {
        "question": sanitize(question[:120]) if question else "",
        "task_id": task_id(agent, run_id, key), "agent": agent, "agent_label": AGENT_LABELS.get(agent, agent),
        "run_id": run_id, "subject": sanitize(" ／ ".join(key)) if key else "",
        "started_at": events[0].get("at", ""), "ended_at": events[-1].get("at", ""),
        "steps": len(system), "staff_steps": len(staff),
        "final_decision": _final_decision(agent, system),
        "staff_decision": _staff_outcome(staff[-1]) if staff else "",
        "human_required": any(d.get("human_required") or d.get("decision") in AM.HUMAN_DECISIONS for d in system),
        "has_error": bool(errors), "errors": sanitize(errors[:5]),
        "model": models[0] if models else "", "prompt_version": versions[0] if versions else "",
        "latency_ms": sum(latencies) if latencies else None,
        "prompt_tokens": sum(int(u.get("prompt_tokens") or 0) for u in usage),
        "completion_tokens": sum(int(u.get("completion_tokens") or 0) for u in usage),
        "completed": AM.is_completed(agent, system) if system else False,
        "useful": AM.is_useful(agent, system) if system else False,
    }


def _group(state_dir) -> dict[tuple, list[dict]]:
    groups: dict[tuple, list[dict]] = {}
    for event in load_trace(state_dir):
        d = event.get("detail") or {}
        groups.setdefault((d.get("agent"), d.get("run_id"), _key_tuple(event.get("tracking_key"))), []).append(event)
    return groups


def list_runs(state_dir, *, agent: str = "", date_from: str = "", date_to: str = "",
              needs_human: bool = False, has_error: bool = False) -> list[dict]:
    """全部執行的摘要（新到舊）；可依 agent、日期（YYYY-MM-DD，含首尾）、需人工、有錯誤篩選。"""
    runs = [_summarize(a, r, k, events) for (a, r, k), events in _group(state_dir).items()]
    out = []
    for run in runs:
        day = run["started_at"][:10]
        if agent and run["agent"] != agent:
            continue
        if (date_from and day < date_from) or (date_to and day > date_to):
            continue
        if needs_human and not run["human_required"]:
            continue
        if has_error and not run["has_error"]:
            continue
        out.append(run)
    return sorted(out, key=lambda r: (r["started_at"], r["task_id"]), reverse=True)


def get_run(state_dir, tid: str) -> dict | None:
    """單次執行的明細：摘要＋依序的每一步＋專員決定；找不到回 None。"""
    for (agent, run_id, key), events in _group(state_dir).items():
        if task_id(agent, run_id, key) == tid:
            steps = [_step_view(e) for e in events]
            return {"summary": _summarize(agent, run_id, key, events),
                    "steps": [s for s in steps if not s["staff"]],
                    "staff_decisions": [s for s in steps if s["staff"]]}
    return None


def export_run(run: dict, *, exported_at: str = "") -> dict:
    """可查核的單次執行 JSON（已遮蔽）；評審模式也可下載。"""
    return {
        "format": EXPORT_FORMAT, "exported_at": exported_at,
        "source": "STATE_DIR/audit_log.json（source＝agent_trace）", "order": STEP_ORDER_NOTE,
        "masking": "字串中 8 碼以上數字遮成 *、電子郵件只留首字與網域；寫入時已不含送模型前去除的個資",
        "summary": run["summary"], "steps": run["steps"], "staff_decisions": run["staff_decisions"],
    }
