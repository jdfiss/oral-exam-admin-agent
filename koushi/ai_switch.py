# -*- coding: utf-8 -*-
"""AI 總開關（緊急停止）：一個開關停掉所有 LLM 呼叫與 agent 自動動作。

比賽辦法第十四條要求發生資安事件時立即停止；第十一條（三）把人工覆核與中止機制
視為良好設計。關閉後系統退回純規則／人工流程：

- `koushi.llm.make_llm()` 與 `koushi.runtime.make_classifier()` 回傳 None，
  所有 agent 既有的「沒有模型就轉人工」路徑自然生效；
- 信箱 agent 不呼叫模型（即使呼叫端注入了模型），所有自動動作（收件通知、
  自動記錄異動申請）改為「建議」交專員；
- 教授回信不做 AI 判讀，自動放行不會發生；
- 問答助理與 AI 建議頁停用。

規則流程（建檔、檢核、補件通知、催件、產檔）照常，因為它們本來就不依賴模型。

狀態存在各狀態目錄的 `ai_switch.json`（與 settings.json 分開：緊急停止不該被
設定頁其他欄位的格式錯誤擋住，也不會被設定表單順手改回去）。另有環境變數
`KOUSHI_AI_DISABLED=1` 給維運人員直接停用（介面無法解除）。檔案損毀時視為
已停止（fail-closed）。

`make_llm()` 沒有狀態目錄參數的呼叫端（網頁）由 `use_state_dir()` 註冊的函式
取得目前狀態目錄；CLI 與每日自動化則直接傳 state_dir。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from . import audit_log, clock, util as U

ENV = "KOUSHI_AI_DISABLED"
FILENAME = "ai_switch.json"
_TRUTHY = {"1", "true", "yes", "on"}

_state_dir_provider: Callable[[], Path | str | None] | None = None


def use_state_dir(provider: Callable[[], Path | str | None] | None) -> None:
    """登記「目前狀態目錄」的取得方式（網頁啟動時登記 cfg().state_dir）。"""
    global _state_dir_provider
    _state_dir_provider = provider


def _current_state_dir() -> Path | None:
    if _state_dir_provider is None:
        return None
    try:
        value = _state_dir_provider()
    except Exception:  # noqa: BLE001 — 還沒設定好網頁 config 時視為沒有狀態目錄
        return None
    return Path(value) if value else None


def env_disabled() -> bool:
    return (os.environ.get(ENV) or "").strip().lower() in _TRUTHY


def load(state_dir: str | Path) -> dict:
    """回傳 {disabled, by, reason, at}；檔案不存在＝未停止，損毀＝已停止（fail-closed）。"""
    path = Path(state_dir) / FILENAME
    if not path.is_file():
        return {"disabled": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("格式錯誤")
    except (OSError, ValueError):
        return {"disabled": True, "by": "", "reason": f"{FILENAME} 無法讀取，為安全起見視為已停止", "at": ""}
    return {**data, "disabled": bool(data.get("disabled"))}


def disabled_reason(state_dir: str | Path | None = None) -> str | None:
    """AI 已停止時回傳原因（可直接顯示）；未停止回 None。

    state_dir 為 None 時改用 use_state_dir() 登記的目前狀態目錄。
    """
    if env_disabled():
        return f"維運人員以環境變數 {ENV} 停用 AI"
    directory = Path(state_dir) if state_dir else _current_state_dir()
    if directory is None:
        return None
    status = load(directory)
    if not status["disabled"]:
        return None
    who = f"{status.get('by')} 於 {status.get('at')} " if status.get("by") else ""
    return f"{who}啟用 AI 緊急停止：{status.get('reason') or '未填原因'}"


def is_disabled(state_dir: str | Path | None = None) -> bool:
    return disabled_reason(state_dir) is not None


@U.locked_state()
def set_disabled(state_dir: str | Path, *, disabled: bool, actor: str, reason: str) -> dict:
    """系辦切換 AI 總開關；處理人與原因必填，寫入稽核紀錄。"""
    actor, reason = (actor or "").strip(), (reason or "").strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    if not reason:
        raise ValueError("請填寫停止或恢復 AI 的原因")
    state_dir = Path(state_dir)
    before = load(state_dir)["disabled"]
    status = {"disabled": bool(disabled), "by": actor, "reason": reason[:500],
              "at": clock.now().strftime("%Y-%m-%d %H:%M:%S")}
    U.atomic_write_json(status, state_dir / FILENAME)
    audit_log.record(state_dir / "audit_log.json", "ai_switch", "disabled" if disabled else "enabled",
                     detail={"actor": actor, "reason": status["reason"], "was_disabled": before})
    return status
