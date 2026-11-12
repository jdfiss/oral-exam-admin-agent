# -*- coding: utf-8 -*-
"""不用 LLM 的對照組：關鍵字確認偵測。

代表「不用模型，只看信裡有沒有確認字眼就放行」的最簡做法，用來對照 LLM 的價值。
它只會回 confirm 或 unknown（unknown＝轉人工），因此六類準確率沒有意義，
只看自動放行的兩個指標（誤放行率、可放行卻轉人工率）。
"""

from __future__ import annotations

from koushi.classifier import ClassificationResult, ClassifierAdapter, INTENT_CONFIRM, INTENT_UNKNOWN

CONFIRM_KEYWORDS = (
    "確認無誤", "沒問題", "沒有問題", "內容正確", "確認", "OK", "Confirmed", "Looks good",
    "No problem", "好的", "都對", "可以",
)


class KeywordBaseline(ClassifierAdapter):
    def classify(self, reply_text: str, *, context: dict | None = None) -> ClassificationResult:
        lowered = reply_text.lower()
        for kw in CONFIRM_KEYWORDS:
            if kw.lower() in lowered:
                start = lowered.index(kw.lower())
                return ClassificationResult(intent=INTENT_CONFIRM, evidence=reply_text[start:start + len(kw)],
                                            model=self.model_name, prompt_version=self.prompt_version)
        return ClassificationResult(intent=INTENT_UNKNOWN, evidence=reply_text.strip()[:20] or "(空)",
                                    model=self.model_name, prompt_version=self.prompt_version)

    @property
    def model_name(self) -> str:
        return "keyword-baseline"

    @property
    def prompt_version(self) -> str:
        return "none"
