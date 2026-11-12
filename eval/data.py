# -*- coding: utf-8 -*-
"""載入評測資料集（全部為合成資料），並把引用測試集轉成正式流程看到的兩種文字。"""

from __future__ import annotations

import json
import sys
from email.message import EmailMessage
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR.parent))

DEV_PATH = EVAL_DIR / "dev_set.json"
QUOTED_PATH = EVAL_DIR / "quoted_set.json"


def load_dev(path: Path = DEV_PATH) -> list[dict]:
    return [dict(c, set="dev") for c in json.loads(Path(path).read_text(encoding="utf-8"))]


def _parse_quoted(case: dict):
    """把 raw_body 包成一封真的 MIME 信，交給 koushi.mail.inbox.parse_message()。

    回傳 InboundMail：.body 是原始全文（未去引用），.reply_text 是正式流程送模型的文字。
    """
    from koushi.mail.inbox import parse_message

    msg = EmailMessage()
    msg["Subject"] = "Re: [口試附件4確認 A4-0a1b2c3d] 口試一覽表請確認"
    msg["From"] = "示範教授 <professor@example.edu>"
    msg["Message-ID"] = f"<{case['id']}@example.edu>"
    subtype = "html" if case["content_type"] == "text/html" else "plain"
    msg.set_content(case["raw_body"], subtype=subtype)
    return parse_message(bytes(msg))


def load_quoted(path: Path = QUOTED_PATH) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for c in data["cases"]:
        mail = _parse_quoted(c)
        out.append(dict(c, set="quoted", raw_text=mail.body.strip(), cleaned_text=mail.reply_text))
    return out


def gold_index(*case_lists: list[dict]) -> dict:
    return {(c["set"], c["id"]): c for cases in case_lists for c in cases}


def inputs(sets: list[str]) -> list[dict]:
    """展開成要送判讀的輸入：dev 一份；quoted 兩份（raw＝不去引用、cleaned＝去引用後）。"""
    items = []
    if "dev" in sets:
        for c in load_dev():
            items.append({"set": "dev", "variant": "reply_text", "id": c["id"], "text": c["reply_text"]})
    if "quoted" in sets:
        for c in load_quoted():
            items.append({"set": "quoted", "variant": "raw", "id": c["id"], "text": c["raw_text"]})
            items.append({"set": "quoted", "variant": "cleaned", "id": c["id"], "text": c["cleaned_text"]})
    return items


def all_gold() -> dict:
    return gold_index(load_dev(), load_quoted())
