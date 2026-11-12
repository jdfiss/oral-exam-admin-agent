# -*- coding: utf-8 -*-
"""事件時間線（AuditLog）—— 全流程操作與狀態變更的稽核日誌。

只能追加，不提供刪除或修改既有事件的方法——這是稽核用的時間線，任何
「修正」都應該是新增一筆事件，不是回頭改舊紀錄。

刻意跟 issue_store／send_log 沒有 import 關係：兩邊呼叫 append() 寫入，
這裡不反過來認得它們的資料結構，避免稽核日誌被綁死在特定模組的形狀上。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
import json
from dataclasses import dataclass
from pathlib import Path

from . import util as U

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str() -> str:
    return clock.now().strftime(_TIMESTAMP_FMT)


@dataclass(frozen=True)
class AuditEvent:
    """一筆稽核事件。sequence 在同一個 AuditLog 內嚴格遞增，可用來排序／偵測缺漏。"""

    sequence: int
    at: str
    source: str        # 事件來源，例如 "issue_store"、"send_log"、"confirmation"
    action: str         # 事件動作，例如 "status_changed"、"sent"、"resolved"
    tracking_key: tuple | None
    detail: dict


class AuditLog:
    """只能追加的稽核日誌。"""

    def __init__(self, events: list[AuditEvent] | None = None):
        self._events: list[AuditEvent] = list(events or [])

    def append(
        self,
        source: str,
        action: str,
        *,
        tracking_key: tuple | None = None,
        detail: dict | None = None,
        at: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            sequence=len(self._events) + 1,
            at=at or _now_str(),
            source=source,
            action=action,
            tracking_key=tuple(tracking_key) if tracking_key else None,
            detail=dict(detail or {}),
        )
        self._events.append(event)
        return event

    def all(self) -> list[AuditEvent]:
        return list(self._events)

    def by_source(self, source: str) -> list[AuditEvent]:
        return [e for e in self._events if e.source == source]

    def by_tracking_key(self, tracking_key: tuple) -> list[AuditEvent]:
        key = tuple(tracking_key)
        return [e for e in self._events if e.tracking_key == key]

    def since(self, sequence: int) -> list[AuditEvent]:
        """回傳 sequence 之後（不含）新增的事件，供輪詢／增量同步使用。"""
        return [e for e in self._events if e.sequence > sequence]

    def __len__(self) -> int:
        return len(self._events)


# ── 序列化 ──────────────────────────────────────────

def event_to_dict(e: AuditEvent) -> dict:
    return {
        "sequence": e.sequence, "at": e.at, "source": e.source, "action": e.action,
        "tracking_key": list(e.tracking_key) if e.tracking_key else None,
        "detail": e.detail,
    }


def event_from_dict(d: dict) -> AuditEvent:
    return AuditEvent(
        sequence=d["sequence"], at=d["at"], source=d["source"], action=d["action"],
        tracking_key=tuple(d["tracking_key"]) if d.get("tracking_key") else None,
        detail=d.get("detail", {}),
    )


def load(path: str | Path) -> AuditLog:
    """從 JSON 載入。檔案不存在時回傳空日誌。"""
    path = Path(path)
    if not path.is_file():
        return AuditLog()
    raw = json.loads(path.read_text(encoding="utf-8"))
    events = [event_from_dict(d) for d in raw.get("events", [])]
    events.sort(key=lambda e: e.sequence)
    return AuditLog(events=events)


def save(log: AuditLog, path: str | Path) -> Path:
    raw = {"events": [event_to_dict(e) for e in log.all()]}
    return U.atomic_write_json(raw, path)


def append_and_save(log: AuditLog, path: str | Path, source: str, action: str, **kwargs) -> AuditEvent:
    """append() 後立刻落盤，適合單次操作（如 CLI 指令）不想手動管生命週期的情境。"""
    event = log.append(source, action, **kwargs)
    save(log, path)
    return event


def record(path: str | Path, source: str, action: str, **kwargs) -> AuditEvent:
    """在狀態鎖內重新讀檔、追加一筆、寫回——多個請求同時寫稽核也不會掉事件。"""
    path = Path(path)
    with U.state_lock(path.parent):
        return append_and_save(load(path), path, source, action, **kwargs)
