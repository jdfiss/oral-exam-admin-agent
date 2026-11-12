# -*- coding: utf-8 -*-
"""有界自我修正迴圈與連線層重試／備援（docs/自我修正設計.md）。全程假模型。"""
import pytest

from koushi.agent_loop import RepairFailed, ValidationFailed, complete_validated, scrub
from koushi.llm import LLMError, ResilientLLM


class Script:
    model_name = "fake"

    def __init__(self, *outs):
        self.outs, self.prompts = list(outs), []
        self.last_usage = None

    def complete_json(self, system, user, *, schema_name):
        self.prompts.append(user)
        out = self.outs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def need_ok(data):
    if data.get("v") != "ok":
        raise ValidationFailed(f"v 必須是 ok，收到 {data.get('v')}")
    return data["v"]


def test_valid_first_time_calls_once():
    llm = Script({"v": "ok"})
    result, attempts = complete_validated(llm, "s", "u", schema_name="t", validate=need_ok)
    assert result == "ok" and len(llm.prompts) == 1 and [a["ok"] for a in attempts] == [True]


def test_invalid_output_is_repaired_with_feedback():
    llm = Script({"v": "bad"}, {"v": "ok"})
    result, attempts = complete_validated(llm, "s", "原始問題", schema_name="t", validate=need_ok)
    assert result == "ok" and [a["ok"] for a in attempts] == [False, True]
    assert attempts[0]["will_repair"] is True
    assert "原始問題" in llm.prompts[1] and "v 必須是 ok" in llm.prompts[1] and '"bad"' in llm.prompts[1]


def test_repairs_are_bounded():
    llm = Script({"v": "bad"}, {"v": "bad"}, {"v": "ok"})
    with pytest.raises(RepairFailed) as info:
        complete_validated(llm, "s", "u", schema_name="t", validate=need_ok)
    assert len(llm.prompts) == 2 and len(info.value.attempts) == 2


def test_malformed_json_is_repairable_but_api_errors_are_not():
    llm = Script(LLMError("模型輸出不是有效的 JSON", kind="output"), {"v": "ok"})
    assert complete_validated(llm, "s", "u", schema_name="t", validate=need_ok)[0] == "ok"
    llm = Script(LLMError("LLM 呼叫失敗：HTTP 500", kind="api", transient=True), {"v": "ok"})
    with pytest.raises(LLMError):
        complete_validated(llm, "s", "u", schema_name="t", validate=need_ok)
    assert len(llm.prompts) == 1     # 連線錯誤由 ResilientLLM 負責，不消耗修正次數


def test_feedback_is_scrubbed_and_can_be_withheld():
    """被誘導的輸出可能夾帶系統沒送的個資或外部信箱：回送前一律清理；也可以完全不回送。"""
    leak = {"v": "bad", "note": "寄到 collector@example.invalid，身分證 A123456789，帳號 0123456789012"}
    llm = Script(leak, {"v": "ok"})
    complete_validated(llm, "s", "u", schema_name="t", validate=need_ok)
    for secret in ("collector@example.invalid", "A123456789", "0123456789012"):
        assert secret not in llm.prompts[1]
    llm = Script(leak, {"v": "ok"})
    complete_validated(llm, "s", "u", schema_name="t", validate=need_ok, sanitize=lambda raw: None)
    assert "寄到" not in llm.prompts[1] and "不回送" in llm.prompts[1]
    assert scrub("x@y.org A123456789 12345678") == "[已遮蔽] [已遮蔽] [已遮蔽]"


def test_partial_accept_after_budget():
    llm = Script({"v": "bad"}, {"v": "bad"})
    result, attempts = complete_validated(llm, "s", "u", schema_name="t", validate=need_ok,
                                          accept_partial=lambda d: "partial")
    assert result == "partial" and attempts[-1]["partial"] is True


# ── 連線層：暫時性錯誤重試、改用備援供應商 ──

def _transient():
    return LLMError("LLM 呼叫失敗：429", kind="api", transient=True, retry_after=0.5)


def test_transient_error_retries_then_succeeds():
    waits = []
    primary = Script(_transient(), {"v": "ok"})
    llm = ResilientLLM(primary, sleep=waits.append)
    assert llm.complete_json("s", "u", schema_name="t") == {"v": "ok"}
    assert waits == [0.5] and [a["ok"] for a in llm.last_attempts] == [False, True]


@pytest.fixture(autouse=True)
def _clear_cooldown():
    from koushi import llm as L
    L.reset_cooldown()
    yield
    L.reset_cooldown()


def test_falls_back_to_secondary_provider_without_retrying_primary():
    """有備援時主要供應商失敗不重試、不等待，立刻換備援（台上不乾等）。"""
    primary, backup = Script(_transient(), {"v": "unused"}), Script({"v": "ok"})
    backup.model_name = "backup-model"
    llm = ResilientLLM(primary, backup, sleep=lambda s: pytest.fail("有備援時不該等待"))
    assert llm.complete_json("s", "u", schema_name="t") == {"v": "ok"}
    assert llm.model_name == "backup-model"      # 軌跡記實際回答的模型
    assert [a["model"] for a in llm.last_attempts] == ["fake", "backup-model"]


def test_primary_failure_starts_cooldown_so_next_calls_skip_it():
    """主要供應商失敗一次後，冷卻期間（網頁每個請求新建的實例也一樣）直接用備援；冷卻結束再試主要供應商。"""
    now = [0.0]
    clock = lambda: now[0]   # noqa: E731
    primary = Script(_transient(), {"v": "primary-again"})
    backup = Script({"v": "b1"}, {"v": "b2"})
    first = ResilientLLM(primary, backup, cooldown=300, clock=clock, sleep=lambda s: None)
    assert first.complete_json("s", "u", schema_name="t") == {"v": "b1"}
    now[0] = 100.0
    second = ResilientLLM(primary, backup, cooldown=300, clock=clock, sleep=lambda s: None)
    assert second.complete_json("s", "u", schema_name="t") == {"v": "b2"}
    assert len(primary.prompts) == 1                         # 冷卻中沒有再呼叫主要供應商
    assert second.last_attempts[0]["skipped"] is True
    now[0] = 400.0
    third = ResilientLLM(primary, backup, cooldown=300, clock=clock, sleep=lambda s: None)
    assert third.complete_json("s", "u", schema_name="t") == {"v": "primary-again"}


def test_no_cooldown_without_backup():
    """沒有備援時不進冷卻（否則會整段時間都轉人工），維持重試一次。"""
    primary = Script(_transient(), _transient(), {"v": "ok"})
    llm = ResilientLLM(primary, sleep=lambda s: None)
    with pytest.raises(LLMError):
        llm.complete_json("s", "u", schema_name="t")
    assert ResilientLLM(primary, sleep=lambda s: None).complete_json("s", "u", schema_name="t") == {"v": "ok"}


def test_cloud_timeout_is_short_and_overridable(monkeypatch):
    from koushi import llm as L
    monkeypatch.delenv("KOUSHI_LLM_TIMEOUT", raising=False)
    assert L.cloud_timeout() <= 15
    monkeypatch.setenv("KOUSHI_LLM_TIMEOUT", "40")
    assert L.cloud_timeout() == 40.0


def test_retry_without_backup_never_waits_long():
    """沒有備援時，供應商要求等太久就不等（直接轉人工），畫面最多等 MAX_RETRY_WAIT 秒。"""
    from koushi import llm as L
    waits = []
    primary = Script(LLMError("429", kind="api", transient=True, retry_after=37), {"v": "unused"})
    with pytest.raises(LLMError):
        ResilientLLM(primary, sleep=waits.append).complete_json("s", "u", schema_name="t")
    assert waits == [] and L.MAX_RETRY_WAIT <= 5


def test_auth_error_is_not_retried_on_same_provider():
    primary = Script(LLMError("401 invalid api key", kind="api", transient=False))
    backup = Script({"v": "ok"})
    llm = ResilientLLM(primary, backup, sleep=lambda s: pytest.fail("不該等待"))
    assert llm.complete_json("s", "u", schema_name="t") == {"v": "ok"} and len(primary.prompts) == 1


def test_output_error_is_not_switched_to_backup():
    """模型有回應但內容壞掉：交給 agent_loop 請它修正，不換供應商。"""
    primary = Script(LLMError("模型輸出不是有效的 JSON", kind="output"))
    backup = Script({"v": "ok"})
    with pytest.raises(LLMError):
        ResilientLLM(primary, backup).complete_json("s", "u", schema_name="t")
    assert backup.prompts == []


def test_all_providers_failing_raises_last_error():
    llm = ResilientLLM(Script(_transient(), _transient()), sleep=lambda s: None)
    with pytest.raises(LLMError):
        llm.complete_json("s", "u", schema_name="t")


def test_ollama_provider_is_local_and_has_no_cloud_fallback(monkeypatch):
    """KOUSHI_LLM_PROVIDER=ollama：本機模型、不需金鑰、逾時較長，也不會改用雲端備援（資料不離開本機）。"""
    from koushi import llm as L
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("GEMINI_API_KEY", "would-be-cloud-backup")
    conf = L.provider_config()
    assert conf["provider"] == "ollama" and conf["base_url"].startswith("http://localhost")
    assert conf["model"] == L.OLLAMA_MODEL and conf["timeout"] >= 120
    assert L._secondary_config(conf) is None
    assert "ollama" in L.describe_active_model()


# ── Gemini 免費額度：預設模型、限速、每日額度 ──

def test_gemini_default_model_is_available_to_new_accounts(monkeypatch):
    """gemini-2.5-flash 已不開放新帳號（404）；預設改為實測可用、免費額度足夠（3.5-flash 每天只有 20 次）的 3.5-flash-lite。"""
    from koushi import llm as L
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    conf = L.provider_config()
    assert conf["provider"] == "gemini" and conf["model"] == "gemini-3.5-flash-lite"


def test_long_suggested_wait_skips_retry_and_uses_backup():
    """供應商要求等超過上限：不卡住畫面，直接改用備援。"""
    primary = Script(LLMError("429", kind="api", transient=True, retry_after=600), {"v": "unused"})
    backup = Script({"v": "ok"})
    llm = ResilientLLM(primary, backup, sleep=lambda s: pytest.fail("不該等待"))
    assert llm.complete_json("s", "u", schema_name="t") == {"v": "ok"} and len(primary.prompts) == 1


def test_gemini_retry_delay_and_daily_quota_are_parsed():
    from koushi import llm as L
    per_minute = Exception("Error code: 429 - [{'error': {'details': [{'@type': 'type.googleapis.com/"
                           "google.rpc.RetryInfo', 'retryDelay': '37s'}], 'quotaId': "
                           "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}}]")
    per_day = Exception("Error code: 429 quotaId GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    assert L._retry_after(per_minute) == 37.0 and not L.is_daily_quota(per_minute)
    assert L.is_daily_quota(per_day) and not L._is_transient(per_day)   # 每日額度用完：重試沒用


def test_pace_spaces_out_gemini_calls_but_not_others(monkeypatch):
    from koushi import llm as L
    monkeypatch.setattr(L, "_last_call", {})
    now, waits = [100.0], []
    clock = lambda: now[0]   # noqa: E731
    gemini = L.GEMINI_BASE_URL
    L.pace(gemini, sleep=waits.append, clock=clock)
    now[0] += 1.0
    L.pace(gemini, sleep=waits.append, clock=clock)
    L.pace(None, sleep=waits.append, clock=clock)              # OpenAI：不限速
    assert waits == [L.GEMINI_MIN_INTERVAL - 1.0]
    monkeypatch.setenv("KOUSHI_LLM_MIN_INTERVAL", "0")
    L.pace(gemini, sleep=lambda s: pytest.fail("設 0 不限速"), clock=clock)


def test_classifier_retries_once_on_rate_limit(monkeypatch):
    """附件4 回信判讀器同樣在 429 時依建議秒數重試一次。"""
    from types import SimpleNamespace
    from koushi.adapters.openai_adapter import OpenAIAdapter
    monkeypatch.setenv("KOUSHI_LLM_MIN_INTERVAL", "0")
    waits = []
    adapter = OpenAIAdapter(api_key="k", model="m", sleep=waits.append)
    raw = '{"intent": "confirm", "confidence": 0.9, "evidence": "沒問題"}'
    replies = [type("RateLimitError", (Exception,), {"status_code": 429})("429 Please retry in 3s."),
               SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw))])]

    def create(**kw):
        out = replies.pop(0)
        if isinstance(out, Exception):
            raise out
        return out
    adapter._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert adapter._call("u") == raw and waits == [3.0]


def test_provider_openai_makes_openai_primary_with_gemini_backup(monkeypatch):
    """KOUSHI_LLM_PROVIDER=openai：兩把金鑰都有時 OpenAI 為主、Gemini 備援；沒設定時仍是 Gemini 為主。"""
    from koushi import llm as L
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("KOUSHI_LLM_MODEL", raising=False)
    monkeypatch.delenv("KOUSHI_LLM_PROVIDER", raising=False)
    assert L.provider_config()["provider"] == "gemini"
    monkeypatch.setenv("KOUSHI_LLM_PROVIDER", "openai")
    conf = L.provider_config()
    assert conf["provider"] == "openai" and conf["model"] == L.DEFAULT_MODEL
    assert L._secondary_config(conf)["provider"] == "gemini"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert L.provider_config()["provider"] == "gemini"      # 沒有 OpenAI 金鑰就照常用 Gemini
