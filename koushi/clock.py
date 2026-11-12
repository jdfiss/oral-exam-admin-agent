"""Shared business clock. Demo time is opt-in; verification expiry uses real time."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path


def now() -> datetime:
    root = os.environ.get("KOUSHI_DEMO_ROOT")
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
