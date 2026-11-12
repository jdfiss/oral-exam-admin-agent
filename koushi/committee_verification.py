"""Expiring email challenges. Wall time deliberately ignores the demo clock."""
from __future__ import annotations
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from . import util as U

FILE = "committee_verification.json"
TTL = 600
GRANT_TTL = 900


def _load(state_dir):
    p = Path(state_dir) / FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def _digest(token, code, secret):
    return hmac.new(str(secret).encode(), (token + ":" + code).encode(), hashlib.sha256).hexdigest()


@U.locked_state()
def send_code(state_dir, *, token, email, secret, transport=None, now=None):
    from .mail.email_sender import send_email
    now = time.time() if now is None else now
    data = _load(state_dir)
    old = data.get(token, {})
    if now < old.get("sent_at", 0) + 60:
        raise ValueError("請等候 60 秒再寄驗證碼")
    code = f"{secrets.randbelow(1000000):06d}"
    ok = (transport or send_email)(to=email, subject="[口試委員驗證] 信箱驗證碼",
        body=f"您的驗證碼是：{code}\n10 分鐘內有效。請勿轉交他人。")
    if ok is False:
        raise ValueError("驗證碼未寄出，請稍後重試")
    data[token] = {"digest": _digest(token, code, secret), "sent_at": now,
                   "expires_at": now + TTL, "attempts": 0}
    U.atomic_write_json(data, Path(state_dir) / FILE)


@U.locked_state()
def verify_code(state_dir, *, token, code, secret, now=None):
    now = time.time() if now is None else now
    data = _load(state_dir)
    item = data.get(token)
    if not item:
        raise ValueError("尚未寄送驗證碼（或已驗證過），請先按上方的「寄送驗證碼」")
    if now >= item["expires_at"] or item["attempts"] >= 5:
        raise ValueError("驗證碼已失效，請重新寄送")
    item["attempts"] += 1
    matched = hmac.compare_digest(item["digest"], _digest(token, code.strip(), secret))
    if matched:
        del data[token]
    U.atomic_write_json(data, Path(state_dir) / FILE)
    if not matched:
        raise ValueError("驗證碼不正確")
    return now + GRANT_TTL
