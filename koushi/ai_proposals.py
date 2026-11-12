# -*- coding: utf-8 -*-
"""提案型 agent 的共用狀態：建議佇列、開關、已確認對照表與個資遮蔽。

原則：AI 提案、專員決定。這裡的資料只有三種會被系統「使用」：
- 服務單位對照表（affiliation_aliases.json）：專員確認後才寫入，建檔時查距離表用。
- 職稱級別對照（title_levels.json）：專員確認後才寫入，只作下次判讀的快取與參考，
  不改變任何資格檢核結果。
- 來源異常修正：不存在這裡；專員按「套用」時走既有專員修正層（corrections）。

佇列中的建議（ai_proposals.json）本身不影響主檔、金額或資格。
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from pathlib import Path

from . import audit_log, clock, util as U
from .agent_trace import trace_step

PROPOSALS_FILE = "ai_proposals.json"
SETTINGS_FILE = "agent_settings.json"
ALIASES_FILE = "affiliation_aliases.json"
TITLE_LEVELS_FILE = "title_levels.json"

AGENTS = {
    "affiliation": "委員服務單位 → 距離表對應",
    "correction": "來源異常修正建議",
    "title": "委員職稱輔助判讀",
}

PENDING, APPROVED, OVERRIDDEN, REJECTED = "pending", "approved", "overridden", "rejected"
STATUS_LABELS = {PENDING: "待專員決定", APPROVED: "已採用", OVERRIDDEN: "專員改判", REJECTED: "不採用"}


def _now() -> str:
    return clock.now().strftime("%Y-%m-%d %H:%M:%S")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _read(path: Path, default: dict) -> dict:
    return U.read_json_state(path) if path.is_file() else default


# ── 個資遮蔽：送模型前的最後一道防線（呼叫端本來就不該放個資進去）──
_ID_RE = re.compile(r"[A-Za-z][12489]\d{8}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_LONG_DIGITS_RE = re.compile(r"\d[\d\-]{7,}\d")


def scrub(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = _ID_RE.sub("[已遮蔽]", text)
    text = _EMAIL_RE.sub("[已遮蔽]", text)
    return _LONG_DIGITS_RE.sub("[已遮蔽]", text)


# ── 開關（預設開；沒有任何自動套用選項）──

def load_settings(state_dir) -> dict[str, bool]:
    raw = _read(Path(state_dir) / SETTINGS_FILE, {})
    return {name: bool(raw.get(name, True)) for name in AGENTS}


@U.locked_state()
def save_settings(state_dir, values: dict[str, bool], *, actor: str) -> dict[str, bool]:
    if not actor.strip():
        raise ValueError("請填寫處理人姓名")
    current = load_settings(state_dir)
    updated = {name: bool(values.get(name, current[name])) for name in AGENTS}
    U.atomic_write_json(updated, Path(state_dir) / SETTINGS_FILE)
    audit_log.record(Path(state_dir) / "audit_log.json", "ai_proposals", "settings_changed",
                     detail={"actor": actor.strip(), "from": current, "to": updated})
    return updated


# ── 建議佇列 ──

def load(state_dir) -> list[dict]:
    return list(_read(Path(state_dir) / PROPOSALS_FILE, {"proposals": []}).get("proposals", []))


def get(state_dir, proposal_id: str) -> dict | None:
    return next((p for p in load(state_dir) if p["id"] == proposal_id), None)


def unanalyzed(p: dict) -> bool:
    """當時沒有模型（未設定或 AI 停止）而沒分析的待決建議。"""
    return p["status"] == PENDING and p.get("method") == "none" and not p.get("suggestion") and not p.get("error")


def existing(state_dir, agent: str, subject_key: str, *, model_available: bool = False) -> dict | None:
    """同一主題已有建議（含已決定）就不再問模型；出錯的待決建議可重試，
    沒有模型時產生的空建議在模型恢復後也可重試（審查 F08）。"""
    for p in load(state_dir):
        if p["agent"] == agent and p["subject_key"] == subject_key:
            if p["status"] == PENDING and (p.get("error") or (model_available and unanalyzed(p))):
                continue
            return p
    return None


@U.locked_state()
def add(state_dir, proposal: dict) -> dict:
    """新增建議；同一 agent、同一對象已有待處理的有效建議時直接回傳那筆（兩輪同時跑也不重複）。"""
    analyzed = not unanalyzed({"status": PENDING, **proposal})

    def replaceable(p):   # 出錯的、或（這次有分析時）當時沒模型的空建議，由新建議取代
        return p["status"] == PENDING and (p.get("error") or (analyzed and unanalyzed(p)))

    existing = next((p for p in load(state_dir)
                     if p["agent"] == proposal["agent"] and p["subject_key"] == proposal["subject_key"]
                     and p["status"] == PENDING and not replaceable(p)), None)
    if existing is not None:
        return existing
    items = [p for p in load(state_dir)
             if not (p["agent"] == proposal["agent"] and p["subject_key"] == proposal["subject_key"]
                     and replaceable(p))]
    proposal = {"id": uuid.uuid4().hex[:10], "status": PENDING, "created_at": _now(),
                "decided_by": "", "decided_at": "", "decision": None, **proposal}
    items.append(proposal)
    U.atomic_write_json({"proposals": items}, Path(state_dir) / PROPOSALS_FILE)
    return proposal


@U.locked_state()
def mark_decided(state_dir, proposal_id: str, *, status: str, actor: str, decision: dict, note: str = "") -> dict:
    actor = actor.strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    if status not in (APPROVED, OVERRIDDEN, REJECTED):
        raise ValueError("不支援的決定")
    items = load(state_dir)
    target = next((p for p in items if p["id"] == proposal_id), None)
    if target is None:
        raise ValueError("找不到這筆 AI 建議")
    if target["status"] != PENDING:
        raise ValueError("這筆 AI 建議已處理過")
    target.update(status=status, decided_by=actor, decided_at=_now(), decision=decision, note=note.strip())
    U.atomic_write_json({"proposals": items}, Path(state_dir) / PROPOSALS_FILE)
    trace_step(state_dir, agent=target["agent"], run_id=target.get("run_id", ""), step="human_decision",
               tracking_key=target["subject_key"], tool="office_ui",
               input_summary=f"處理人 {actor}", output=decision, decision=status, human_required=False)
    return target


def pending(state_dir, agent: str | None = None) -> list[dict]:
    return [p for p in load(state_dir) if p["status"] == PENDING and (agent is None or p["agent"] == agent)]


# ── 服務單位對照表（版本化；交通費只認這裡已確認的對應）──

def alias_key(unit: str) -> str:
    return U.norm_unit(unit)


def load_aliases(state_dir) -> dict:
    return _read(Path(state_dir) / ALIASES_FILE, {"version": 0, "aliases": {}, "history": []})


def alias_map(state_dir) -> dict[str, str]:
    return {key: entry["target"] for key, entry in load_aliases(state_dir).get("aliases", {}).items()}


@U.locked_state()
def set_alias(state_dir, *, unit: str, target: str, actor: str, source: str, proposal_id: str = "",
              reason: str = "") -> dict:
    actor = actor.strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    key, target = alias_key(unit), U.norm_unit(target)
    if not key or not target:
        raise ValueError("對照的服務單位不可空白")
    data = load_aliases(state_dir)
    data["version"] = int(data.get("version", 0)) + 1
    entry = {"unit": str(unit).strip(), "target": target, "actor": actor, "at": _now(), "source": source,
             "proposal_id": proposal_id, "reason": reason.strip(), "version": data["version"]}
    data.setdefault("aliases", {})[key] = entry
    data.setdefault("history", []).append({"action": "set", "key": key, **entry})
    U.atomic_write_json(data, Path(state_dir) / ALIASES_FILE)
    audit_log.record(Path(state_dir) / "audit_log.json", "affiliation_alias", "set", tracking_key=(key,),
                     detail=entry)
    return entry


@U.locked_state()
def remove_alias(state_dir, *, unit_key: str, actor: str, reason: str = "") -> None:
    actor = actor.strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    data = load_aliases(state_dir)
    if unit_key not in data.get("aliases", {}):
        raise ValueError("找不到這筆對照")
    old = data["aliases"].pop(unit_key)
    data["version"] = int(data.get("version", 0)) + 1
    data.setdefault("history", []).append({"action": "removed", "key": unit_key, "target": old["target"],
                                           "actor": actor, "at": _now(), "reason": reason.strip(),
                                           "version": data["version"]})
    U.atomic_write_json(data, Path(state_dir) / ALIASES_FILE)
    audit_log.record(Path(state_dir) / "audit_log.json", "affiliation_alias", "removed",
                     tracking_key=(unit_key,), detail={"actor": actor, "target": old["target"], "reason": reason})


# ── 職稱級別對照（專員確認後的快取；只作參考）──

def title_key(title: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(title or ""))).strip().lower()


def load_title_levels(state_dir) -> dict:
    return _read(Path(state_dir) / TITLE_LEVELS_FILE, {"version": 0, "titles": {}})


@U.locked_state()
def set_title_level(state_dir, *, title: str, level: str, actor: str, proposal_id: str = "") -> dict:
    data = load_title_levels(state_dir)
    data["version"] = int(data.get("version", 0)) + 1
    entry = {"title": str(title).strip(), "level": level, "actor": actor.strip(), "at": _now(),
             "proposal_id": proposal_id, "version": data["version"]}
    data.setdefault("titles", {})[title_key(title)] = entry
    U.atomic_write_json(data, Path(state_dir) / TITLE_LEVELS_FILE)
    audit_log.record(Path(state_dir) / "audit_log.json", "title_level", "set",
                     tracking_key=(title_key(title),), detail=entry)
    return entry


def call_llm(llm, state_dir, *, agent: str, run_id: str, tracking_key: str, system: str, user: str,
             schema_name: str, prompt_version: str, input_summary: str,
             required: tuple[str, ...] = (), check=None) -> tuple[dict | None, str]:
    """呼叫模型並記軌跡；任何例外都吞下回傳錯誤訊息，該項留給專員處理。

    required 列出輸出必須有的鍵：缺鍵視為模型故障（而不是「沒有建議」），專員才分得出兩者。
    check(data) 是 agent 自己的內容檢核（候選代號、級別白名單、可修正欄位……），不合格時拋
    ValueError。缺鍵或檢核不合格會把原因回饋給模型修正一次（koushi.agent_loop），每次嘗試各記一筆軌跡。
    """
    from .agent_loop import ValidationFailed, complete_validated
    from .llm import model_name

    def validate(data):
        missing = [key for key in required if key not in data]
        if missing:
            raise ValidationFailed("模型輸出缺少欄位：" + "、".join(missing))
        if check is not None:
            try:
                check(data)
            except (ValueError, TypeError) as exc:
                raise ValidationFailed(str(exc)) from exc
        return data

    def on_attempt(attempt):
        trace_step(state_dir, agent=agent, run_id=run_id, step="llm_call", tracking_key=tracking_key,
                   tool=f"llm.complete_json:{schema_name}",
                   input_summary=f"{input_summary}（第 {attempt['attempt']} 次）" if attempt["attempt"] > 1 else input_summary,
                   output={k: attempt[k] for k in ("ok", "problems", "error") if attempt.get(k)},
                   model=model_name(llm) or type(llm).__name__, prompt_version=prompt_version,
                   latency_ms=attempt["latency_ms"], human_required=True, usage=attempt["usage"],
                   decision="pass" if attempt["ok"] else "repair" if attempt.get("will_repair") else "fail")

    try:
        data, _ = complete_validated(llm, system, user, schema_name=schema_name, validate=validate,
                                     on_attempt=on_attempt)
    except Exception as exc:  # noqa: BLE001 - 逐項隔離模型錯誤
        return None, f"{type(exc).__name__}: {exc}"[:300]
    return data, ""


def confidence(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, number))


def dumps(data) -> str:
    return json.dumps(data, ensure_ascii=False)
