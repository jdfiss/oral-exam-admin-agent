# -*- coding: utf-8 -*-
"""評審模式：決賽評審專用、權限受控的測試環境（建在展示模式之上）。

- 只跑示範資料：`KOUSHI_ENV=production`、或資料夾與正式資料目錄重疊、或不是
  展示資料夾（沒有 demo-root.json 標記）時拒絕啟用。
- 不寄任何真實信件：所有寄信改走 `capture_transport()`，信件只寫進本輪狀態目錄
  的模擬寄件匣（judge_outbox.json），評審在「模擬寄件匣」頁看到系統「本來會寄出」
  的信；行程內另把 SMTP 寄信函式換成直接拒絕（`block_real_smtp()`）作為最後防線。
- 評審可在寄件匣對任一封信「模擬回信」，下一次執行本日流程時由信箱 agent 與
  附件4 回信流程讀取——不連線真實信箱。
- 評審帳號的權限白名單與稽核在 `koushi.web.app` 的 `_judge_guard()`。

- 沒有設定 LLM 金鑰時自動改用離線備援（`configure_llm()`），各頁橫幅標示目前模型。

啟動：`python -m koushi.demo judge [--offline]`（見 docs/評審模式.md）。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import uuid
from pathlib import Path

from . import clock, config as C, util as U

MODE_ENV = "KOUSHI_JUDGE_MODE"
PASSWORD_ENV = "KOUSHI_JUDGE_PASSWORD"
ACTOR = "評審"
BANNER = "評審模式：示範資料、不會寄出任何信件"
LABEL_ENV = "KOUSHI_JUDGE_LABEL"   # 系辦假資料試用（koushi.trial）設為「試用」，畫面不出現「評審」


def label() -> str:
    """畫面與稽核上對這個受控帳號的稱呼：預設「評審」，系辦試用為「試用」。"""
    return os.environ.get(LABEL_ENV, "").strip() or ACTOR


def login_enabled() -> bool:
    """系辦試用（koushi.trial）設 KOUSHI_JUDGE_LOGIN=0：保留假資料與模擬寄件匣，但不開評審帳號，承辦人用系辦登入。"""
    return os.environ.get("KOUSHI_JUDGE_LOGIN", "1").strip() != "0"


def banner() -> str:
    return BANNER.replace(ACTOR, label(), 1)
OUTBOX_FILE = "judge_outbox.json"
INBOX_FILE = "judge_inbox.json"
STRANGER = "unknown-sender@example.invalid"


def enabled() -> bool:
    return os.environ.get(MODE_ENV) == "1"


def ensure_allowed(root: str | Path) -> None:
    """評審模式只能用在示範資料夾；任何疑似正式環境的跡象都拒絕啟用。"""
    if os.environ.get("KOUSHI_ENV") == "production":
        raise RuntimeError("正式環境（KOUSHI_ENV=production）不可啟用評審模式")
    root = Path(root).resolve()
    real = [C.STATE_DIR, C.CURRENT_DIR, C.SRC_SUBMISSIONS_DIR]
    if os.environ.get("KOUSHI_DATA_ROOT"):
        real.append(Path(os.environ["KOUSHI_DATA_ROOT"]))
    for path in real:
        path = Path(path).resolve()
        if root == path or root.is_relative_to(path) or path.is_relative_to(root):
            raise RuntimeError(f"評審模式資料夾不可與正式資料目錄重疊：{path}")
    marker = root / "demo-root.json"
    if root.is_dir() and any(root.iterdir()):
        try:
            purpose = json.loads(marker.read_text(encoding="utf-8")).get("purpose")
        except (OSError, ValueError):
            purpose = None
        if purpose != "koushi-demo-v1":
            raise RuntimeError("評審模式只能使用示範資料夾（缺少展示標記），拒絕啟用")


def configure_llm(*, force_offline: bool = False) -> str:
    """評審模式的模型預設：沒有任何金鑰時改用離線備援，讓 agent 實際跑出提案與軌跡。

    有金鑰時沿用真實模型（`--offline` 可強制離線）；已明確設定 KOUSHI_LLM_PROVIDER 時尊重設定。
    回傳畫面與終端機標示用的模型名稱。只改本行程的環境變數。
    """
    from .llm import describe_active_model, provider_config
    if force_offline or (not (os.environ.get("KOUSHI_LLM_PROVIDER") or "").strip() and provider_config() is None):
        os.environ["KOUSHI_LLM_PROVIDER"] = "offline"
    return describe_active_model()


# ── 模擬寄件匣 ──────────────────────────────────────────

def _read(path: Path) -> list[dict]:
    return list(U.read_json_state(path).get("items", [])) if path.is_file() else []


def _write(items: list[dict], path: Path) -> None:
    U.atomic_write_json({"items": items}, path)


def outbox(state_dir: str | Path) -> list[dict]:
    return _read(Path(state_dir) / OUTBOX_FILE)


def capture_transport(state_dir: str | Path):
    """與 email_sender.send_email 同介面的假寄信：只記進模擬寄件匣，永遠不連 SMTP。"""
    state_dir = Path(state_dir)

    def send(to, subject, body, cc=None, attachments=None, message_id=None, in_reply_to=None, references=None):
        with U.state_lock(state_dir):
            items = outbox(state_dir)
            items.append({
                "id": uuid.uuid4().hex[:12], "to": to, "cc": cc or "", "subject": subject, "body": body,
                "attachments": [Path(a).name for a in attachments or []],
                "message_id": message_id or f"<{uuid.uuid4().hex}@judge.invalid>",
                "in_reply_to": in_reply_to or "", "references": list(references or []),
                "at": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            _write(items, state_dir / OUTBOX_FILE)
        return True
    return send


def block_real_smtp() -> None:
    """評審伺服器行程內的最後防線：任何漏網的預設寄信路徑都直接失敗，不連 SMTP。"""
    from .mail import email_sender

    def refuse(*args, **kwargs):
        raise RuntimeError("評審模式不寄出真實信件")
    email_sender.send_email = refuse


# ── 模擬收件（評審代為回信）────────────────────────────

def simulate_reply(state_dir: str | Path, *, outbox_id: str, text: str, stranger: bool = False) -> dict:
    """評審對模擬寄件匣中的一封信回信；下一次執行本日流程時才會被讀取。"""
    state_dir = Path(state_dir)
    text = (text or "").strip()
    if not text:
        raise ValueError("請輸入回信內容")
    if len(text) > 4000:
        raise ValueError("回信內容過長（上限 4000 字）")
    with U.state_lock(state_dir):
        original = next((m for m in outbox(state_dir) if m["id"] == outbox_id), None)
        if original is None:
            raise ValueError("找不到這封模擬寄出的信")
        quoted = "\n".join("> " + line for line in original["body"].splitlines())
        reply = {
            "message_id": f"<judge-reply-{uuid.uuid4().hex}@example.invalid>",
            "from_addr": STRANGER if stranger else original["to"],
            "subject": "Re: " + original["subject"],
            "received_at": clock.now().replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).isoformat(),
            "reply_text": text, "body": f"{text}\n\n{quoted}",
            "in_reply_to": original["message_id"], "delivered": False, "reply_to_outbox": outbox_id,
        }
        items = _read(state_dir / INBOX_FILE)
        items.append(reply)
        _write(items, state_dir / INBOX_FILE)
    return reply


def inbox(state_dir: str | Path) -> list[dict]:
    return _read(Path(state_dir) / INBOX_FILE)


def take_replies(state_dir: str | Path) -> list:
    """取出尚未讀取的模擬回信（InboundMail），並標為已讀取。"""
    from .advisor_confirmation import InboundMail
    state_dir = Path(state_dir)
    with U.state_lock(state_dir):
        items = inbox(state_dir)
        pending = [m for m in items if not m.get("delivered")]
        for m in pending:
            m["delivered"] = True
        if pending:
            _write(items, state_dir / INBOX_FILE)
    return [InboundMail(message_id=m["message_id"], from_addr=m["from_addr"], subject=m["subject"],
                        received_at=m["received_at"], body=m["body"], reply_text=m["reply_text"],
                        in_reply_to=m["in_reply_to"], received_at_trusted=True) for m in pending]
