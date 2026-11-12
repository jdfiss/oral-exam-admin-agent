# -*- coding: utf-8 -*-
"""OpenAI adapter — 用 GPT 判讀教授回信意圖。

使用方式：
    from koushi.adapters.openai_adapter import OpenAIAdapter

    adapter = OpenAIAdapter(api_key="sk-...")
    result = adapter.classify(reply_text)

環境變數 OPENAI_API_KEY 可替代建構參數。
"""

from __future__ import annotations

import json
import logging
import os
import time

from openai import OpenAI

from ..classifier import (
    ClassificationResult,
    ClassifierAdapter,
    ClassifierError,
    SYSTEM_PROMPT,
    CLASSIFY_USER_TEMPLATE,
    PROMPT_VERSION,
    parse_model_output,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIAdapter(ClassifierAdapter):
    """透過 OpenAI API 判讀教授回信意圖。"""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = 30.0,
        sleep=time.sleep,
    ):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not self._api_key:
            raise ClassifierError("未提供 OpenAI API key（參數或環境變數 OPENAI_API_KEY）")
        self._model = model
        self._timeout = timeout
        self._base_url = base_url
        self._sleep = sleep
        # max_retries=0：重試由 _call 負責；SDK 內建會再悄悄重試 2 次
        self._client = OpenAI(api_key=self._api_key, base_url=base_url, timeout=timeout, max_retries=0)

    def classify(self, reply_text: str, *, context: dict | None = None) -> ClassificationResult:
        """呼叫 OpenAI API 判讀回信意圖。"""
        user_message = CLASSIFY_USER_TEMPLATE.format(reply_text=reply_text)
        raw = self._call(user_message)

        result = parse_model_output(raw, model=self._model, prompt_ver=self.prompt_version)
        if result.evidence not in reply_text:
            raise ClassifierError("模型判讀依據不是回覆本文的逐字引用")
        return result

    def _call(self, user_message: str) -> str:
        """呼叫模型；免費額度限速（koushi.llm.pace），429／逾時依建議秒數等待後重試一次。"""
        from ..llm import MAX_RETRY_WAIT, _is_transient, _retry_after, pace
        for attempt in range(2):
            pace(self._base_url)
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                return response.choices[0].message.content or ""
            except Exception as e:
                wait = _retry_after(e) or 2.0
                if attempt == 0 and _is_transient(e) and wait <= MAX_RETRY_WAIT:
                    logger.info("模型呼叫暫時失敗，%.1f 秒後重試：%s", wait, type(e).__name__)
                    self._sleep(wait)
                    continue
                raise ClassifierError(f"OpenAI API 呼叫失敗：{e}") from e
        raise ClassifierError("OpenAI API 呼叫失敗")  # 不會到這裡

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def prompt_version(self) -> str:
        return PROMPT_VERSION
