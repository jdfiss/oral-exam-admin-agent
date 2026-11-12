# -*- coding: utf-8 -*-
"""寄送記錄（SendEventLog）—— 草稿 → 審閱 → 送出的稽核軌跡。

以 drafts.NotificationDraft.idempotency_key 為主鍵：同一追蹤狀態重複產生
草稿會拿到同一把 key，record_draft 對已存在的 key 是 no-op，避免同一件事
被記錄成兩筆。狀態只能沿著 draft → approved/rejected → sent/failed 前進，
不允許跳步或回頭，也不提供刪除——是稽核軌跡，不是工作區。

只記錄事件本身，不寄信、不碰 SMTP；實際寄送仍由通訊層執行，這裡只負責
「這件事發生過」的軌跡。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import util as U
from .drafts import NotificationDraft

DRAFT = "draft"
APPROVED = "approved"
REJECTED = "rejected"
SENT = "sent"
FAILED = "failed"

_ALLOWED_TRANSITIONS = {
    DRAFT: {APPROVED, REJECTED},
    APPROVED: {SENT, FAILED},
    REJECTED: set(),
    SENT: set(),
    FAILED: {APPROVED},  # 寄送失敗可重新核准再試一次
}

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str() -> str:
    return clock.now().strftime(_TIMESTAMP_FMT)


@dataclass(frozen=True)
class SendEvent:
    """一張草稿的寄送稽核紀錄。"""

    idempotency_key: str
    type: str
    to: str | None
    subject: str
    status: str
    tracking: dict
    created_at: str
    reviewed_at: str | None = None
    reviewed_by: str | None = None
    sent_at: str | None = None
    note: str = ""


class SendEventLog:
    """draft → reviewed → sent 狀態鏈的記錄器。只能新增或推進既有事件，不可刪除。"""

    def __init__(self, events: dict[str, SendEvent] | None = None):
        self._events: dict[str, SendEvent] = dict(events or {})

    def record(
        self, *, idempotency_key: str, type: str, to: str | None, subject: str, tracking: dict,
    ) -> SendEvent:
        """記錄一筆新草稿事件，不要求呼叫端一定要有 NotificationDraft 實例。

        `koushi/mail/notify.py` 目前組信的方式跟 drafts.py 的模板不一樣
        （字串拼接 vs 模板引擎），但只要雙方用同一套 idempotency_key 算法
        （見 drafts.compute_idempotency_key），送進來的 key 就能互通，
        去重不需要統一草稿的組成方式。key 已存在時回傳既有事件，不覆寫。
        """
        existing = self._events.get(idempotency_key)
        if existing is not None:
            return existing
        event = SendEvent(
            idempotency_key=idempotency_key, type=type, to=to, subject=subject,
            status=DRAFT, tracking=dict(tracking), created_at=_now_str(),
        )
        self._events[idempotency_key] = event
        return event

    def record_draft(self, draft: NotificationDraft) -> SendEvent:
        """`record()` 的便利包裝，直接吃 drafts.py 產生的 NotificationDraft。"""
        return self.record(
            idempotency_key=draft.idempotency_key, type=draft.type, to=draft.to,
            subject=draft.subject, tracking=draft.tracking,
        )

    def record_review(
        self, idempotency_key: str, *, approved: bool, reviewer: str = "", note: str = "",
    ) -> SendEvent:
        """系辦審閱草稿：approved=True/False 對應核准／退回。"""
        event = self._require(idempotency_key)
        self._require_transition(event.status, APPROVED if approved else REJECTED)
        updated = replace(
            event, status=APPROVED if approved else REJECTED,
            reviewed_at=_now_str(), reviewed_by=reviewer, note=note,
        )
        self._events[idempotency_key] = updated
        return updated

    def record_sent(self, idempotency_key: str, *, success: bool, note: str = "") -> SendEvent:
        """通訊層回報實際寄送結果。"""
        event = self._require(idempotency_key)
        self._require_transition(event.status, SENT if success else FAILED)
        updated = replace(
            event, status=SENT if success else FAILED,
            sent_at=_now_str(), note=note or event.note,
        )
        self._events[idempotency_key] = updated
        return updated

    def dispatch(
        self, idempotency_key: str, send_fn: Callable[[], bool | None], *, note: str = "",
    ) -> SendEvent:
        """呼叫 send_fn() 實際寄送，並把結果寫回 log——寄送前一定先經過
        record_draft／record_review 核准，不能繞過去直接寄。

        send_fn 回傳 False 或拋例外都記為失敗；例外會在記成 failed 後
        重新拋出，不吞掉呼叫端需要知道的錯誤。send_fn 沒有回傳值
        （None）視為成功。
        """
        try:
            result = send_fn()
        except Exception as exc:
            self.record_sent(idempotency_key, success=False, note=f"{note}：{exc}" if note else str(exc))
            raise
        success = result is not False
        return self.record_sent(idempotency_key, success=success, note=note)

    def retry_after_failure(self, idempotency_key: str) -> SendEvent:
        """寄送失敗後重新核准，準備再試一次。"""
        event = self._require(idempotency_key)
        self._require_transition(event.status, APPROVED)
        updated = replace(event, status=APPROVED, sent_at=None)
        self._events[idempotency_key] = updated
        return updated

    def is_duplicate(self, idempotency_key: str) -> bool:
        """已經成功寄出的草稿視為重複，呼叫端不該再排入寄送。"""
        event = self._events.get(idempotency_key)
        return event is not None and event.status == SENT

    def get(self, idempotency_key: str) -> SendEvent | None:
        return self._events.get(idempotency_key)

    def all(self) -> list[SendEvent]:
        return list(self._events.values())

    def by_status(self, status: str) -> list[SendEvent]:
        return [e for e in self._events.values() if e.status == status]

    def _require(self, idempotency_key: str) -> SendEvent:
        if idempotency_key not in self._events:
            raise KeyError(f"找不到寄送記錄：{idempotency_key}")
        return self._events[idempotency_key]

    def _require_transition(self, from_status: str, to_status: str) -> None:
        if to_status not in _ALLOWED_TRANSITIONS.get(from_status, set()):
            raise ValueError(f"不允許的狀態轉移：{from_status} → {to_status}")


# ── 序列化 ──────────────────────────────────────────

def event_to_dict(e: SendEvent) -> dict:
    return {
        "idempotency_key": e.idempotency_key, "type": e.type, "to": e.to,
        "subject": e.subject, "status": e.status, "tracking": e.tracking,
        "created_at": e.created_at, "reviewed_at": e.reviewed_at,
        "reviewed_by": e.reviewed_by, "sent_at": e.sent_at, "note": e.note,
    }


def event_from_dict(d: dict) -> SendEvent:
    return SendEvent(
        idempotency_key=d["idempotency_key"], type=d["type"], to=d.get("to"),
        subject=d["subject"], status=d.get("status", DRAFT), tracking=d.get("tracking", {}),
        created_at=d["created_at"], reviewed_at=d.get("reviewed_at"),
        reviewed_by=d.get("reviewed_by"), sent_at=d.get("sent_at"), note=d.get("note", ""),
    )


def load(path: str | Path) -> SendEventLog:
    """從 JSON 載入。檔案不存在時回傳空記錄檔。"""
    path = Path(path)
    if not path.is_file():
        return SendEventLog()
    raw = U.read_json_state(path)
    events = {d["idempotency_key"]: event_from_dict(d) for d in raw.get("events", [])}
    return SendEventLog(events=events)


def save(log: SendEventLog, path: str | Path) -> Path:
    raw = {"events": [event_to_dict(e) for e in log.all()]}
    return U.atomic_write_json(raw, path)
