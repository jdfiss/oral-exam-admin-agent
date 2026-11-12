# -*- coding: utf-8 -*-
"""有界自我修正迴圈：呼叫模型 → 程式檢核 → 不合格就把具體問題回饋給模型修正。

設計見 docs/自我修正設計.md。所有 agent 共用：

- `complete_validated()`：模型輸出未通過檢核（格式、逐字引用、候選代號、白名單……）時，
  把「上一次的輸出＋不合格原因」交回模型重寫，最多 `max_repairs` 次（預設 1）。
  修正後的輸出走**同一個** validate，不會因為重試而放寬；仍不合格就拋 `RepairFailed`，
  呼叫端照舊轉人工。連線類錯誤（逾時、429、5xx）不在這裡處理，見 `koushi.llm.ResilientLLM`。
- 每一次嘗試回傳一筆 attempt（ok、problems、latency_ms、usage），呼叫端寫進 agent 軌跡，
  軌跡上看得到「檢核失敗 → 修正 → 檢核通過」。

回饋不加入任何新的原始資料；模型上一次的輸出也要先經 `sanitize` 清理才送回（預設遮掉信箱、
身分證、長串數字），因為被誘導的模型可能在輸出裡夾帶系統刻意沒送的內容（例如引用串裡的指令、
外部信箱）。信箱 agent 更保守，完全不回送上一次的輸出，只回饋檢核原因。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from .llm import LLMError, last_usage

MAX_REPAIRS = 1
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_ID_RE = re.compile(r"[A-Za-z][12]\d{8}")
_DIGITS_RE = re.compile(r"\d{8,}")

REPAIR_TEMPLATE = """\
{user}

──── 系統檢核回饋（第 {attempt} 次修正）────
你上一次的輸出：
{previous}

未通過系統檢核：
{problems}

請修正上述問題後，重新輸出完整的 JSON 物件（同樣的欄位）。
- 引用一律逐字複製原文，不可改寫、不可補字。
- 不確定就留空或選「無法判定」，不要為了通過檢核而捏造內容。
"""


class ValidationFailed(LLMError):
    """模型輸出未通過檢核；problems 是給模型看的具體原因。"""

    def __init__(self, problems: list[str] | str):
        self.problems = [problems] if isinstance(problems, str) else [str(p) for p in problems if str(p)]
        super().__init__("；".join(self.problems) or "模型輸出未通過檢核")


class RepairFailed(LLMError):
    """修正次數用完仍未通過檢核。attempts 是每一次嘗試的紀錄。"""

    def __init__(self, message: str, attempts: list[dict]):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class Attempt:
    attempt: int
    ok: bool
    problems: list[str] = field(default_factory=list)
    latency_ms: int = 0
    usage: dict | None = None
    error: str = ""

    def as_dict(self) -> dict:
        return {"attempt": self.attempt, "ok": self.ok, "problems": self.problems,
                "latency_ms": self.latency_ms, "usage": self.usage, "error": self.error}


def scrub(text: str) -> str:
    """回饋給模型前的預設清理：信箱、身分證、8 碼以上數字一律遮掉。"""
    text = _EMAIL_RE.sub("[已遮蔽]", str(text or ""))
    text = _ID_RE.sub("[已遮蔽]", text)
    return _DIGITS_RE.sub("[已遮蔽]", text)


def _previous(raw, sanitize) -> str:
    if raw is None:
        return "（無法解析成 JSON）"
    if sanitize is not None:
        raw = sanitize(raw)
        if raw is None:
            return "（不回送，請依下列原因重新輸出）"
    text = scrub(json.dumps(raw, ensure_ascii=False, default=str))
    return text if len(text) <= 2000 else text[:2000] + "…"


def repair_prompt(user: str, raw, problems: list[str], attempt: int, sanitize=None) -> str:
    return REPAIR_TEMPLATE.format(user=user, attempt=attempt, previous=_previous(raw, sanitize),
                                  problems="\n".join(f"- {scrub(p)}" for p in problems))


def complete_validated(llm, system: str, user: str, *, schema_name: str, validate: Callable,
                       max_repairs: int = MAX_REPAIRS, on_attempt: Callable[[dict], None] | None = None,
                       accept_partial: Callable | None = None, sanitize: Callable | None = None):
    """呼叫模型並檢核；不合格時回饋修正。回傳 (validate 的結果, attempts)。

    validate(data) 回傳任何結果；不合格時拋 ValidationFailed（或 LLMError）。
    accept_partial(data)：修正次數用完時的寬鬆檢核（例如丟掉不合格的項目、保留其餘）；
    能接受就回傳它的結果（最後一筆 attempt 標 partial），否則照樣拋 RepairFailed。
    sanitize(raw)：回送上一次輸出前的清理；回傳 None 表示完全不回送（只回饋原因）。
    模型輸出不是 JSON 也算可修正（parse_json_object 拋 LLMError）；
    連線錯誤（ResilientLLM 重試後仍失敗）直接往外拋，不消耗修正次數。
    """
    attempts: list[dict] = []
    prompt, raw = user, None
    for n in range(max_repairs + 1):
        started = time.perf_counter()
        try:
            raw = llm.complete_json(system, prompt, schema_name=schema_name)
        except LLMError as exc:
            if getattr(exc, "transient", False) or not _is_output_error(exc):
                attempts.append(Attempt(n + 1, False, latency_ms=_ms(started), usage=last_usage(llm),
                                        error=str(exc)[:200]).as_dict())
                _notify(on_attempt, attempts[-1])
                exc.attempts = attempts
                raise
            raw, problems = None, [f"輸出不是有效的 JSON 物件（{exc}）"]
        else:
            try:
                result = validate(raw)
            except LLMError as exc:
                problems = getattr(exc, "problems", None) or [str(exc)]
            else:
                attempts.append(Attempt(n + 1, True, latency_ms=_ms(started), usage=last_usage(llm)).as_dict())
                _notify(on_attempt, attempts[-1])
                return result, attempts
        attempts.append(Attempt(n + 1, False, problems, _ms(started), last_usage(llm)).as_dict())
        attempts[-1]["will_repair"] = n < max_repairs
        if n < max_repairs:
            _notify(on_attempt, attempts[-1])
            prompt = repair_prompt(user, raw, problems, n + 1, sanitize)
            continue
        if accept_partial is not None and raw is not None:
            try:
                result = accept_partial(raw)
            except LLMError:
                pass
            else:
                attempts[-1]["partial"] = True
                _notify(on_attempt, attempts[-1])
                return result, attempts
        _notify(on_attempt, attempts[-1])
    raise RepairFailed("修正後仍未通過檢核：" + "；".join(attempts[-1]["problems"]), attempts)


def _is_output_error(exc: LLMError) -> bool:
    """模型有回應但內容壞掉（JSON 解析失敗）才值得請它修正；API 呼叫失敗不是。"""
    return getattr(exc, "kind", "") == "output"


def _notify(callback, attempt: dict) -> None:
    if callback is not None:
        callback(attempt)


def _ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
