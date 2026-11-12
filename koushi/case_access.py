# -*- coding: utf-8 -*-
"""學生修改權限的個案例外：申請異動與專員開放修改（2026-10-01 Q8／Q13）。

補件期間只有收到補件信的案件能改；其他學生遇到真實異動（口試改期、委員換 E-mail）
時，在學生頁按「申請異動」並寫原因，申請出現在專員介面。專員對單一案件按
「開放修改」（須填處理人和原因），開放到指定日期（預設補件截止日）。補件截止後
仍有問題的案件，專員也用同一個入口對單一案件延長修改期限。

學生直接寄信到業務信箱的申請，之後由 LLM 分類、摘要後以 source="email" 存進來；
寄件者對不上案件登記的學生信箱時 verified=False，畫面標「身分未驗證」。
LLM 只做分類與摘要，不能開放修改——`grant_unlock` 一定要專員（處理人）呼叫。

存在 `STATE_DIR/case_access.json`。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import audit_log, util as U

FILENAME = "case_access.json"
PENDING = "pending"
HANDLED = "handled"


@dataclass(frozen=True)
class ChangeRequest:
    case_key: str
    reason: str
    at: str
    source: str = "web"        # web：學生頁；email：業務信箱（LLM 摘要）
    verified: bool = True      # email 寄件者對不上學生信箱時為 False
    status: str = PENDING
    handled_by: str = ""
    handled_at: str = ""


@dataclass(frozen=True)
class Unlock:
    case_key: str
    until: str                 # YYYY-MM-DD（含）
    actor: str
    reason: str
    at: str

    @property
    def until_date(self) -> _dt.date:
        return _dt.date.fromisoformat(self.until)


def _now(now: _dt.datetime | None) -> str:
    return (now or clock.now()).strftime("%Y-%m-%d %H:%M:%S")


def _load_raw(state_dir: Path) -> dict:
    path = state_dir / FILENAME
    if not path.is_file():
        return {"requests": [], "unlocks": {}}
    data = U.read_json_state(path)
    data.setdefault("requests", [])
    data.setdefault("unlocks", {})
    return data


def requests(state_dir: str | Path, *, case_key: str | None = None, pending_only: bool = False) -> list[ChangeRequest]:
    items = [ChangeRequest(**r) for r in _load_raw(Path(state_dir))["requests"]]
    if case_key is not None:
        items = [r for r in items if r.case_key == case_key]
    if pending_only:
        items = [r for r in items if r.status == PENDING]
    return items


def unlock_for(state_dir: str | Path, case_key: str) -> Unlock | None:
    raw = _load_raw(Path(state_dir))["unlocks"].get(case_key)
    return Unlock(**raw) if raw else None


def is_unlocked(state_dir: str | Path, case_key: str, today: _dt.date) -> bool:
    unlock = unlock_for(state_dir, case_key)
    return unlock is not None and today <= unlock.until_date


@U.locked_state()
def submit_request(
    state_dir: str | Path, *, case_key: str, reason: str, source: str = "web", verified: bool = True,
    now: _dt.datetime | None = None,
) -> ChangeRequest:
    reason = reason.strip()
    if not reason:
        raise ValueError("請寫下需要異動的原因")
    if len(reason) > 1000:
        raise ValueError("原因請精簡在 1000 字以內")
    state_dir = Path(state_dir)
    data = _load_raw(state_dir)
    request = ChangeRequest(case_key=case_key, reason=reason, at=_now(now), source=source, verified=verified)
    data["requests"].append(asdict(request))
    U.atomic_write_json(data, state_dir / FILENAME)
    audit_log.record(state_dir / "audit_log.json", "case_access", "change_requested",
                     tracking_key=(case_key,), detail={"source": source, "verified": verified})
    return request


@U.locked_state()
def grant_unlock(
    state_dir: str | Path, *, case_key: str, until: _dt.date, actor: str, reason: str,
    now: _dt.datetime | None = None,
) -> Unlock:
    """專員對單一案件開放修改到 until（含）；同案未處理的異動申請一併標為已處理。"""
    actor, reason = actor.strip(), reason.strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    if not reason:
        raise ValueError("請填寫開放修改的原因")
    stamp = _now(now)
    if until < _dt.date.fromisoformat(stamp[:10]):
        raise ValueError("開放期限不能早於今天")
    state_dir = Path(state_dir)
    data = _load_raw(state_dir)
    unlock = Unlock(case_key=case_key, until=until.isoformat(), actor=actor, reason=reason, at=stamp)
    data["unlocks"][case_key] = asdict(unlock)
    for item in data["requests"]:
        if item["case_key"] == case_key and item.get("status", PENDING) == PENDING:
            item.update(status=HANDLED, handled_by=actor, handled_at=stamp)
    U.atomic_write_json(data, state_dir / FILENAME)
    audit_log.record(state_dir / "audit_log.json", "case_access", "unlocked",
                     tracking_key=(case_key,), detail={"actor": actor, "reason": reason, "until": unlock.until})
    return unlock


@U.locked_state()
def dismiss_requests(state_dir: str | Path, *, case_key: str, actor: str, note: str = "",
                     now: _dt.datetime | None = None) -> None:
    """專員看過申請、決定不開放（例如已代為修正）：標為已處理。"""
    actor = actor.strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    state_dir = Path(state_dir)
    data = _load_raw(state_dir)
    stamp = _now(now)
    for item in data["requests"]:
        if item["case_key"] == case_key and item.get("status", PENDING) == PENDING:
            item.update(status=HANDLED, handled_by=actor, handled_at=stamp)
    U.atomic_write_json(data, state_dir / FILENAME)
    audit_log.record(state_dir / "audit_log.json", "case_access", "requests_dismissed",
                     tracking_key=(case_key,), detail={"actor": actor, "note": note})
