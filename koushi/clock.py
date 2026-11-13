"""Shared business clock. Demo time is opt-in; verification expiry uses real time."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path


from contextvars import ContextVar

# 公開評審環境的每位評審有自己的示範資料夾（沙盒）；網頁伺服器多執行緒處理請求，
# 所以「這個請求用哪個資料夾」記在 context（每個執行緒各自一份），不改全域環境變數。
_request_root: ContextVar[str | None] = ContextVar("koushi_demo_root", default=None)


def use_root(root):
    """本執行緒接下來改用這個示範資料夾；回傳 token 給 reset_root() 還原。"""
    return _request_root.set(str(root))


def reset_root(token) -> None:
    _request_root.reset(token)


def now() -> datetime:
    root = _request_root.get() or os.environ.get("KOUSHI_DEMO_ROOT")
    if root:
        path = Path(root) / "clock.json"
        if path.is_file():
            return datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["now"])
    from datetime import timezone
    return datetime.now(timezone(timedelta(hours=8)))


def today():
    return now().date()


def advance(root: Path, days: int = 1) -> datetime:
    from .util import atomic_write_json
    if days not in (1, 7):
        raise ValueError("只能推進一天或七天")
    updated = now() + timedelta(days=days)
    atomic_write_json({"now": updated.isoformat()}, root / "clock.json")
    return updated
