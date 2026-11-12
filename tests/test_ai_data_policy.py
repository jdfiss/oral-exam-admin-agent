# -*- coding: utf-8 -*-
"""審查 2026-10-13 F01／F03：所有送模型的入口都要先過個資政策。"""
import pytest

from koushi import mail_answer, runtime
from koushi.advisor_confirmation import InboundMail
from koushi.classifier import ClassifierError


def test_advisor_reply_classifier_never_sends_pii(monkeypatch):
    """F01：教授回信判讀器在送模型前檢查個資，含身分證或帳號就整封轉人工。"""
    from koushi.adapters import openai_adapter
    monkeypatch.setenv("KOUSHI_NO_LOCAL_ENV", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    for key in ("KOUSHI_LLM_PROVIDER", "OPENAI_BASE_URL", "GEMINI_API_KEY", "KOUSHI_AI_DISABLED"):
        monkeypatch.delenv(key, raising=False)
    sent = []
    monkeypatch.setattr(openai_adapter.OpenAIAdapter, "classify", lambda self, text: sent.append(text) or "ok")
    classify = runtime.make_classifier()

    def mail(text):
        return InboundMail(message_id="m", from_addr="a@x.org", subject="A4", received_at="", body=text,
                           reply_text=text)
    with pytest.raises(ClassifierError):
        classify(mail("確認無誤。我的身分證 A123456789，帳號 12345678"), None)
    assert sent == []
    assert classify(mail("確認無誤"), None) == "ok" and sent == ["確認無誤"]


def test_answer_memory_is_scrubbed_on_save_and_before_prompt(tmp_path):
    """F03：記下的提問與回答都去識別；舊記憶送模型前再清一次。"""
    facts = [{"id": "F1", "label": "口試日期", "value": "2026-11-18"}]
    entry = mail_answer.remember(tmp_path, questions=["王小明想問可以寄到 wang@example.org 嗎"],
                                 body="同學您好：\n\n請寄到 wang@example.org，地址台北市大安區羅斯福路四段1號，"
                                      "口試日期 2026-11-18。\n\n— 系辦", facts=facts, names={"王小明"},
                                 actor="專員甲", message_id="<m>")
    text = str(entry)
    for secret in ("王小明", "wang@example.org", "羅斯福路", "2026-11-18"):
        assert secret not in text, secret

    class Capture:
        def __init__(self):
            self.prompts = []

        def complete_json(self, system, user, *, schema_name):
            self.prompts.append(user)
            return {"answer": "請參考系辦公告", "unanswered": [], "sources": []}

    old = [{"questions": ["李大華問 lee@example.org"], "answer": "寄到 lee@example.org，帳號 12345678"}]
    llm = Capture()
    try:
        mail_answer.compose(llm, questions=["怎麼申請"], mail_text="怎麼申請", facts=[], memory=old,
                            names={"李大華"})
    except Exception:  # noqa: BLE001 — 只看送出的 prompt
        pass
    assert llm.prompts
    for secret in ("李大華", "lee@example.org", "12345678"):
        assert secret not in llm.prompts[0], secret
