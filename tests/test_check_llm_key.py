# -*- coding: utf-8 -*-
"""deploy/check_llm_key.py：.env 常見錯誤寫法都要被指出來（不連網）。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_llm_key", ROOT / "deploy" / "check_llm_key.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def _scan(tmp_path, content: bytes):
    path = tmp_path / ".env"
    path.write_bytes(content)
    return check.scan_env_file(path)


def test_clean_file_has_no_problems(tmp_path):
    values, problems = _scan(tmp_path, b"OPENAI_API_KEY=sk-abcdefghijklmnop\n")
    assert values == {"OPENAI_API_KEY": "sk-abcdefghijklmnop"}
    assert problems == []


def test_bom_quotes_and_inline_comment_are_reported(tmp_path):
    values, problems = _scan(tmp_path, "﻿OPENAI_API_KEY='sk-abcdefghijklmnop' # 我的金鑰\n".encode("utf-8"))
    text = "\n".join(problems)
    assert "BOM" in text and "單引號" in text and "註解" in text
    assert values["OPENAI_API_KEY"] == "sk-abcdefghijklmnop"


def test_unrelated_lines_are_ignored(tmp_path):
    values, problems = _scan(tmp_path, b"# comment\nMAIL_PASSWORD='x y'\n\nGEMINI_API_KEY=AIzaabcdefghijk\n")
    assert values == {"GEMINI_API_KEY": "AIzaabcdefghijk"}
    assert problems == []


def test_key_in_wrong_slot_and_forced_offline():
    problems = check.key_slot_problems({"OPENAI_API_KEY": "AIzaabcdefghijk", "KOUSHI_LLM_PROVIDER": "offline"})
    assert any("Gemini 金鑰" in p for p in problems)
    assert any("offline" in p for p in problems)
    gemini_via_openai = {"OPENAI_API_KEY": "AIzaabcdefghijk",
                         "OPENAI_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai/"}
    assert check.key_slot_problems(gemini_via_openai) == []
    assert any("OpenAI 金鑰" in p for p in check.key_slot_problems({"GEMINI_API_KEY": "sk-abcdefghijklmnop"}))


def test_mask_never_shows_full_key():
    shown = check.mask("sk-proj-abcdefghijklmnopqrstuvwxyz")
    assert shown.startswith("sk-p") and "wxyz" in shown and "abcdefghij" not in shown
