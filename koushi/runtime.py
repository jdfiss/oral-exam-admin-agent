"""Local service configuration; secrets are never returned to the UI."""
import os
from .mail.email_sender import ENV_PATHS, _parse


def load_local_env():
    if os.environ.get("KOUSHI_NO_LOCAL_ENV"):  # 測試用：不讀本機 .env，避免呼叫真實 API
        return
    for path in reversed(ENV_PATHS):
        if path.is_file():
            for key, value in _parse(path).items():
                if not os.environ.get(key):
                    os.environ[key] = value


def make_classifier(state_dir=None):
    """教授回信判讀器；AI 總開關已關閉或沒有 API key 時回傳 None（全部交人工）。

    KOUSHI_LLM_PROVIDER=offline 時回傳離線備援（關鍵字規則，標示 offline-replay），不連網。
    """
    from .ai_switch import is_disabled
    if is_disabled(state_dir):
        return None
    from .llm import offline_selected, provider_config
    if offline_selected():  # 強制離線時即使有金鑰也不連外：改用關鍵字規則的離線備援判讀器
        from .offline_llm import make_offline_classifier
        return make_offline_classifier()
    conf = provider_config()
    if conf is None:
        return None
    from .adapters.openai_adapter import OpenAIAdapter
    adapter = OpenAIAdapter(api_key=conf["api_key"], model=conf["model"], base_url=conf["base_url"],
                            timeout=conf.get("timeout", 30.0))

    def classify(mail, request):
        # 與信箱 agent 共用個資閘門（審查 F01）：含疑似身分證或帳號就整封不送模型、轉人工
        from .classifier import ClassifierError
        from .mailbox_agent import redact_pii
        if redact_pii(mail.reply_text)[1]:
            raise ClassifierError("來信含疑似身分證或帳號，不送模型，請專員判讀")
        return adapter.classify(mail.reply_text)
    return classify


def make_mailbox_llm(state_dir=None):
    """信箱 agent 用的結構化 LLM（koushi.llm）；沒有 API key 或 AI 已停止時回傳 None（全部交人工）。"""
    from .llm import make_llm
    return make_llm(state_dir)


def readiness():
    load_local_env()
    status = {key: bool(os.environ.get(key)) for key in (
        "MAIL_SENDER", "MAIL_PASSWORD", "OPENAI_API_KEY", "KOUSHI_WEB_SECRET_KEY", "KOUSHI_OFFICE_PASSWORD")}
    # Gemini 金鑰也能驅動 LLM 功能（koushi.llm.provider_config）
    status["OPENAI_API_KEY"] = status["OPENAI_API_KEY"] or bool(os.environ.get("GEMINI_API_KEY"))
    return status
