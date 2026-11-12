# -*- coding: utf-8 -*-
"""Agent 執行軌跡（trace）——每個 agent 步驟寫成一筆稽核事件（所有 agent 共用）。

寫進 `STATE_DIR/audit_log.json`（source＝"agent_trace"，action＝步驟名稱），
detail 固定含下列鍵：

    agent, run_id, step, tool, input_summary, output, model, prompt_version,
    latency_ms, decision, human_required, usage

input_summary／output 只放簡短摘要，不放個資；呼叫端負責先去除或代號化，
這裡再把 input_summary 的長數字串（學號、身分證、帳號）遮掉一次。
稽核日誌只能追加，所以軌跡本身也不可改寫。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import audit_log

SOURCE = "agent_trace"
AUDIT_FILE = "audit_log.json"
_LONG_DIGITS = re.compile(r"\d{8,}")


def mask_digits(text) -> str:
    """連續 8 碼以上數字一律遮成 *。"""
    return _LONG_DIGITS.sub(lambda m: "*" * len(m.group(0)), str(text or ""))


def _plain(value):
    """轉成可存 JSON 的值（dataclass、tuple 等一律轉字串／串列）。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _key(tracking_key) -> tuple:
    if tracking_key is None:
        return ()
    if isinstance(tracking_key, (tuple, list)):
        return tuple(str(k) for k in tracking_key)
    return (str(tracking_key),)


def trace_step(state_dir, *, agent, run_id, step, tracking_key, tool=None, input_summary="",
               output=None, model=None, prompt_version=None, latency_ms=None, decision=None,
               human_required=False, usage=None):
    """寫一筆 agent 步驟軌跡；回傳 AuditEvent。usage 為 token 用量（供成本統計）。"""
    detail = {
        "agent": agent, "run_id": run_id, "step": step, "tool": tool,
        "input_summary": mask_digits(input_summary)[:300], "output": _plain(output),
        "model": model, "prompt_version": prompt_version,
        "latency_ms": int(latency_ms) if latency_ms is not None else None,
        "decision": decision, "human_required": bool(human_required),
    }
    if usage:
        detail["usage"] = _plain(usage)
    return audit_log.record(Path(state_dir) / AUDIT_FILE, SOURCE, step,
                            tracking_key=_key(tracking_key) or None, detail=detail)


def load_trace(state_dir, run_id=None, tracking_key=None) -> list[dict]:
    """讀出 agent 軌跡（依 sequence 排序）；可依 run_id 或 tracking_key 篩選。

    每筆同時提供攤平的 detail 欄位（event["step"]）與原始稽核格式（event["detail"]["step"]）。
    """
    log = audit_log.load(Path(state_dir) / AUDIT_FILE)
    key = _key(tracking_key) if tracking_key is not None else None
    events = []
    for event in log.by_source(SOURCE):
        detail = event.detail or {}
        if run_id is not None and detail.get("run_id") != run_id:
            continue
        if key is not None and event.tracking_key != key:
            continue
        events.append({**detail, **audit_log.event_to_dict(event)})
    return events
