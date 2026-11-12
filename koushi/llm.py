# -*- coding: utf-8 -*-
"""供應商中立的結構化 JSON LLM 呼叫（所有 agent 共用）。

業務流程只依賴 `StructuredLLM` Protocol；測試注入假實作，正式環境由 `make_llm()`
依 .env 建立實作（沒有任何 API key 時回傳 None，呼叫端一律改走人工）。

金鑰來源（`provider_config()`，依序；2026-11-09 起 Gemini 為主要供應商）：
- GEMINI_API_KEY → Gemini 的 OpenAI 相容端點，預設 gemini-3.5-flash-lite
  （gemini-2.5-flash 已不開放給新帳號，2026-11 實測回 404）
- OPENAI_API_KEY（可搭配 OPENAI_BASE_URL 指向相容端點）→ 預設 gpt-4o-mini；兩把都有時當備援
KOUSHI_LLM_MODEL 可覆寫模型名稱。KOUSHI_LLM_PROVIDER=openai 改為 OpenAI 為主、Gemini 備援；
KOUSHI_LLM_PROVIDER=offline 改用離線備援
（koushi/offline_llm.py，不連網、不需金鑰）。

模型輸出只是「提案」：呼叫端必須自行驗證 schema、逐字引用與動作白名單，
不能把模型回傳直接當成決定。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Protocol, runtime_checkable

DEFAULT_MODEL = "gpt-4o-mini"
# 2026-11-08 以免費方案金鑰實測：3.5-flash-lite 約 1 秒、一天跑 140 次以上沒被限額，評測與 gpt-4o-mini 相當；
# 3.5-flash 判讀較穩但免費額度每天只有 20 次（展示一輪就用完）；3.8／3.7-flash 逾時或 503；2.5-flash 新帳號 404
GEMINI_MODEL = "gemini-3.5-flash-lite"
# 免費額度有每分鐘次數上限：連續呼叫 Gemini 時至少間隔這麼多秒（KOUSHI_LLM_MIN_INTERVAL 可覆寫，0＝不限速）
GEMINI_MIN_INTERVAL = 4.0
# 沒有備援時，429 依供應商建議的秒數等待後重試一次；超過這個秒數就不等（直接轉人工）。
# 展示時畫面在等，所以上限要短（2026-11-11：原本 60 秒，台上會乾等）。
MAX_RETRY_WAIT = 5.0
# 雲端模型單次呼叫逾時秒數（KOUSHI_LLM_TIMEOUT 可覆寫）。實測 gemini-3.5-flash-lite 平均 1–7 秒；
# 原本 30 秒，加上重試一次，台上最壞要等 1 分鐘以上才換備援。
CLOUD_TIMEOUT = 15.0
# 主要供應商暫時性失敗（逾時、429、5xx）後，這麼多秒內直接用備援，不再每封信重試主要供應商
PRIMARY_COOLDOWN = 300.0
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
# 本機模型（Ollama 的 OpenAI 相容端點）：不連外網、不需金鑰、沒有額度限制，資料不離開這台電腦
OLLAMA_BASE_URL = "http://localhost:11434/v1"
OLLAMA_MODEL = "qwen2.5:3b"
OLLAMA_TIMEOUT = 180.0   # 本機 CPU 推論較慢，第一次呼叫還要載入模型


class LLMError(Exception):
    """LLM 呼叫或輸出格式錯誤；呼叫端逐項捕捉，該項轉人工。

    kind：「api」＝呼叫失敗（transient＝逾時、429、5xx 等值得重試的錯誤）；
    「output」＝模型有回應但內容不是 JSON 物件（可請模型修正，見 koushi.agent_loop）。
    """

    def __init__(self, message: str = "", *, kind: str = "", transient: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.kind, self.transient, self.retry_after = kind, transient, retry_after


@runtime_checkable
class StructuredLLM(Protocol):
    def complete_json(self, system: str, user: str, *, schema_name: str) -> dict: ...


def model_name(llm) -> str | None:
    """取模型名稱供軌跡記錄；假實作沒有就回 None。"""
    return getattr(llm, "model_name", None) or getattr(llm, "model", None)


def last_usage(llm) -> dict | None:
    """最近一次呼叫的 token 用量（prompt_tokens／completion_tokens）；假實作沒有就回 None。"""
    usage = getattr(llm, "last_usage", None)
    return dict(usage) if isinstance(usage, dict) else None


def parse_json_object(raw: str) -> dict:
    cleaned = (raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(ln for ln in cleaned.split("\n") if not ln.strip().startswith("```")).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型輸出不是有效的 JSON：{exc}", kind="output") from exc
    if not isinstance(data, dict):
        raise LLMError("模型輸出必須是 JSON 物件", kind="output")
    return data


def provider_config() -> dict | None:
    """依環境變數決定供應商；回傳 {api_key, base_url, model, provider}，沒有金鑰回 None。"""
    from .runtime import load_local_env
    load_local_env()
    override = (os.environ.get("KOUSHI_LLM_MODEL") or "").strip()
    if (os.environ.get("KOUSHI_LLM_PROVIDER") or "").strip().lower() == "ollama":
        base_url = (os.environ.get("KOUSHI_OLLAMA_URL") or "").strip() or OLLAMA_BASE_URL
        return {"api_key": "ollama", "base_url": base_url, "provider": "ollama",
                "model": override or OLLAMA_MODEL, "timeout": OLLAMA_TIMEOUT}
    openai_first = (os.environ.get("KOUSHI_LLM_PROVIDER") or "").strip().lower() == "openai"
    if openai_first and (os.environ.get("OPENAI_API_KEY") or "").strip():
        # KOUSHI_LLM_PROVIDER=openai：OpenAI 為主、Gemini 當備援（決賽展示用：延遲短、不需免費額度限速）
        base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip() or None
        if not (base_url and "generativelanguage" in base_url):
            return {"api_key": os.environ["OPENAI_API_KEY"].strip(), "base_url": base_url, "provider": "openai",
                    "model": override or DEFAULT_MODEL}
    gemini_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if gemini_key:
        return {"api_key": gemini_key, "base_url": GEMINI_BASE_URL, "provider": "gemini",
                "model": override or GEMINI_MODEL}
    openai_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if openai_key:
        base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip() or None
        gemini = bool(base_url and "generativelanguage" in base_url)
        return {"api_key": openai_key, "base_url": base_url, "provider": "gemini" if gemini else "openai",
                "model": override or (GEMINI_MODEL if gemini else DEFAULT_MODEL)}
    return None


class OpenAIStructuredLLM:
    """OpenAI chat completions（JSON mode）；也用於 Gemini 等 OpenAI 相容端點。"""

    def __init__(self, *, api_key: str | None = None, model: str = DEFAULT_MODEL,
                 base_url: str | None = None, timeout: float = 30.0):
        from openai import OpenAI
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise LLMError("未提供 LLM API key")
        self.model = self.model_name = model
        self.last_usage: dict | None = None
        self._base_url = base_url
        # max_retries=0：重試由 ResilientLLM 負責（有記錄）；SDK 內建會再悄悄重試 2 次，30 秒逾時變成 90 秒以上
        self._client = OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=0)

    def complete_json(self, system: str, user: str, *, schema_name: str) -> dict:
        self.last_usage = None
        pace(self._base_url)
        try:
            response = self._client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.0, response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or ""
        except Exception as exc:  # noqa: BLE001 — 任何 API 錯誤都轉成 LLMError
            note = "（今日免費額度已用完，明天重置）" if is_daily_quota(exc) else ""
            raise LLMError(f"LLM 呼叫失敗（{schema_name}）{note}：{exc}", kind="api",
                           transient=_is_transient(exc), retry_after=_retry_after(exc)) from exc
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {"prompt_tokens": getattr(usage, "prompt_tokens", None),
                               "completion_tokens": getattr(usage, "completion_tokens", None)}
        return parse_json_object(raw)


_RETRY_IN = re.compile(r"retry in ([\d.]+)s", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"]([\d.]+)s")
_PER_DAY = re.compile(r"per ?day", re.IGNORECASE)


def _is_transient(exc: Exception) -> bool:
    """逾時、連線中斷、429、5xx 值得重試；金鑰錯誤、參數錯誤（4xx）不重試。"""
    if is_daily_quota(exc):
        return False   # 每日額度用完：重試同一家沒有用
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    name = type(exc).__name__
    return any(k in name for k in ("Timeout", "Connection", "RateLimit", "InternalServer"))


def _retry_after(exc: Exception) -> float | None:
    match = _RETRY_IN.search(str(exc)) or _RETRY_DELAY.search(str(exc))
    return float(match.group(1)) if match else None


def is_daily_quota(exc: Exception) -> bool:
    """免費額度「每日」上限用完（429 且配額名稱含 PerDay）：等幾秒也沒用，要等隔天重置。"""
    text = str(exc)
    return "429" in text and bool(_PER_DAY.search(text))


def min_interval(base_url: str | None) -> float:
    """同一個端點兩次呼叫之間的最小間隔秒數；只有 Gemini（免費額度限速）預設會限速。"""
    raw = (os.environ.get("KOUSHI_LLM_MIN_INTERVAL") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return GEMINI_MIN_INTERVAL if base_url and "generativelanguage" in base_url else 0.0


_pace_lock = threading.Lock()
_last_call: dict[str, float] = {}


def pace(base_url: str | None, *, sleep=time.sleep, clock=time.monotonic) -> None:
    """呼叫前限速：距離上一次呼叫同一端點未滿最小間隔就先等（整個程序共用，多個 agent 一起算）。"""
    gap = min_interval(base_url)
    if gap <= 0:
        return
    with _pace_lock:
        wait = _last_call.get(base_url or "", float("-inf")) + gap - clock()
        if wait > 0:
            sleep(wait)
        _last_call[base_url or ""] = clock()


# 主要供應商冷卻到期時間（monotonic 秒），以模型名稱為鍵；整個程序共用，因為網頁每個請求都會新建 ResilientLLM
_cooldown_lock = threading.Lock()
_cooldown_until: dict[str, float] = {}


def reset_cooldown() -> None:
    with _cooldown_lock:
        _cooldown_until.clear()


class ResilientLLM:
    """連線層的動態調整：主要供應商失敗就改用備援，不讓畫面乾等。

    - 有備援：主要供應商暫時性失敗（逾時、429、5xx）不重試，立刻改用備援，並在 PRIMARY_COOLDOWN 秒內
      直接用備援（後面的信不必再等一次逾時）。最壞情況只等一次逾時。
    - 沒有備援：暫時性錯誤重試一次（等待不超過 max_wait 秒），仍失敗就拋出，呼叫端轉人工。
    只處理「API 呼叫失敗」；模型輸出不合格由 koushi.agent_loop 回饋修正。
    last_attempts 記下本次呼叫經過哪些供應商、各自結果，供軌跡記錄。
    """

    def __init__(self, primary, fallback=None, *, retries: int = 1, max_wait: float = MAX_RETRY_WAIT,
                 cooldown: float = PRIMARY_COOLDOWN, sleep=time.sleep, clock=time.monotonic):
        self.primary, self.fallback = primary, fallback
        self.retries, self.max_wait, self._sleep = retries, max_wait, sleep
        self.cooldown, self._clock = cooldown, clock
        self.model = self.model_name = model_name(primary)
        self.last_usage: dict | None = None
        self.last_attempts: list[dict] = []

    def _cooling(self) -> bool:
        with _cooldown_lock:
            return _cooldown_until.get(model_name(self.primary), float("-inf")) > self._clock()

    def _start_cooldown(self) -> None:
        if self.fallback is not None and self.cooldown > 0:
            with _cooldown_lock:
                _cooldown_until[model_name(self.primary)] = self._clock() + self.cooldown

    def complete_json(self, system: str, user: str, *, schema_name: str) -> dict:
        self.last_attempts, self.last_usage = [], None
        if self.fallback is None:
            chain = [(self.primary, self.retries)]
        elif self._cooling():
            self.last_attempts.append({"model": model_name(self.primary), "ok": False, "skipped": True,
                                       "error": "主要供應商剛失敗，冷卻中，直接用備援", "transient": True})
            chain = [(self.fallback, 0)]
        else:
            chain = [(self.primary, 0), (self.fallback, 0)]
        error: LLMError | None = None
        for llm, retries in chain:
            for n in range(retries + 1):
                try:
                    data = llm.complete_json(system, user, schema_name=schema_name)
                except LLMError as exc:
                    error = exc
                    self.last_attempts.append({"model": model_name(llm), "ok": False, "error": str(exc)[:160],
                                               "transient": exc.transient})
                    if exc.kind == "output":
                        raise   # 模型有回應但內容壞掉：交給 agent_loop 請模型修正，不換供應商
                    if llm is self.primary:
                        self._start_cooldown()
                    if not exc.transient:
                        break   # 金鑰、參數錯誤：重試同一家沒有用，直接換備援
                    if n < retries:
                        wait = exc.retry_after or 2.0
                        if wait > self.max_wait:
                            break   # 要等太久：不卡住畫面，直接改用備援或轉人工
                        self._sleep(wait)
                    continue
                self.last_usage = getattr(llm, "last_usage", None)
                self.model = self.model_name = model_name(llm)
                self.last_attempts.append({"model": self.model_name, "ok": True})
                return data
        raise error


def cloud_timeout() -> float:
    """雲端模型單次呼叫逾時秒數；KOUSHI_LLM_TIMEOUT 可覆寫（例如每日排程不在乎等待時可調長）。"""
    raw = (os.environ.get("KOUSHI_LLM_TIMEOUT") or "").strip()
    if raw:
        try:
            return max(1.0, float(raw))
        except ValueError:
            pass
    return CLOUD_TIMEOUT


def _secondary_config(primary: dict) -> dict | None:
    """主要供應商以外的另一家（兩家金鑰都有設定時才有備援）。本機模型不改用雲端（資料不離開這台電腦）。"""
    if primary["provider"] == "ollama":
        return None
    if primary["provider"] == "openai":
        key = (os.environ.get("GEMINI_API_KEY") or "").strip()
        return {"api_key": key, "base_url": GEMINI_BASE_URL, "model": GEMINI_MODEL, "provider": "gemini"} if key else None
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if key and "generativelanguage" not in (os.environ.get("OPENAI_BASE_URL") or ""):
        return {"api_key": key, "base_url": None, "model": DEFAULT_MODEL, "provider": "openai"}
    return None


OFFLINE_LABEL = "離線備援（規則替身）"


def offline_selected() -> bool:
    """KOUSHI_LLM_PROVIDER=offline：所有模型呼叫改用離線備援，不連網。"""
    from .runtime import load_local_env
    load_local_env()
    return (os.environ.get("KOUSHI_LLM_PROVIDER") or "").strip().lower() == "offline"


def describe_active_model(state_dir=None) -> str:
    """畫面標示用：目前 agent 會用哪個模型（不建立連線、不呼叫 API）。"""
    from .ai_switch import is_disabled
    if is_disabled(state_dir):
        return "AI 已停止（全部轉人工）"
    if offline_selected():
        return OFFLINE_LABEL
    conf = provider_config()
    if conf is None:
        return "未設定（全部轉人工）"
    return f"{conf['model']}（{conf['provider']}）"


def make_llm(state_dir=None) -> StructuredLLM | None:
    """依環境變數建立 LLM；沒有任何 API key 或 AI 總開關已關閉時回傳 None。"""
    from .ai_switch import is_disabled
    if is_disabled(state_dir):  # 緊急停止優先於一切：所有 agent 走既有「沒有模型→轉人工」路徑
        return None
    # KOUSHI_LLM_PROVIDER=offline：離線備援（預錄／規則替身，不連網），供展示時無網路使用
    if offline_selected():
        from .offline_llm import make_offline_llm
        return make_offline_llm()
    conf = provider_config()
    if conf is None:
        return None
    primary = OpenAIStructuredLLM(api_key=conf["api_key"], model=conf["model"], base_url=conf["base_url"],
                                  timeout=conf.get("timeout", cloud_timeout()))
    second = _secondary_config(conf)
    fallback = (OpenAIStructuredLLM(api_key=second["api_key"], model=second["model"], base_url=second["base_url"],
                                    timeout=cloud_timeout())
                if second else None)
    return ResilientLLM(primary, fallback)
