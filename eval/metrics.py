# -*- coding: utf-8 -*-
"""評測指標（純函式，不呼叫任何 API；report.py 與 run_eval.py 共用）。

每筆「判讀紀錄」record 是 dict，至少有：
    set, id, predicted（intent 或 "error"／"skipped"）, has_reservation, modifications, questions,
    production_rejected（被正式流程的逐字核對擋下，原因字串；沒有則為空）
gold 由資料集提供：expected_intent, expected_has_reservation, expected_modifications。
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict

INTENTS = ("confirm", "modify", "question", "auto_reply", "unrelated", "unknown")


# ── 自動放行閘門（與正式流程一致）────────────────────────────

def would_auto_release(rec: dict) -> bool:
    """模型輸出是否會讓這封信通過 LLM 這一層閘門。

    對應 koushi/advisor_confirmation.py ingest_replies() 的條件：
        cr.intent == confirm and not cr.has_reservation and not cr.modifications
        and not cr.questions
    以及 _call_classifier() 的逐字依據核對（不通過→轉人工）。
    確定性核對（寄件者、版本、期限、附件、白名單、shadow mode）不在本評測範圍，
    這裡假設它們全部通過，也就是只量 LLM 這一層會不會放錯。
    """
    if rec.get("predicted") != "confirm" or rec.get("production_rejected"):
        return False
    return not rec.get("has_reservation") and not rec.get("modifications") and not rec.get("questions")


def gold_auto_release(gold: dict) -> bool:
    """人工標註：這封信「應該」可以自動記為已確認。"""
    return gold["expected_intent"] == "confirm" and not gold.get("expected_has_reservation", False)


# ── 分類指標 ──────────────────────────────────────────────────

def confusion(pairs: list[tuple[str, str]]) -> dict[str, Counter]:
    """pairs = [(expected, predicted)]；回傳 matrix[expected][predicted] = 次數。"""
    m: dict[str, Counter] = defaultdict(Counter)
    for exp, pred in pairs:
        m[exp][pred] += 1
    return m


def per_intent_prf(pairs: list[tuple[str, str]]) -> dict[str, dict]:
    out = {}
    for intent in INTENTS:
        tp = sum(1 for e, p in pairs if e == intent and p == intent)
        fp = sum(1 for e, p in pairs if e != intent and p == intent)
        fn = sum(1 for e, p in pairs if e == intent and p != intent)
        support = tp + fn
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / support if support else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        out[intent] = {"precision": prec, "recall": rec, "f1": f1, "support": support, "tp": tp, "fp": fp, "fn": fn}
    return out


def macro_f1(prf: dict[str, dict]) -> float:
    vals = [v["f1"] for v in prf.values() if v["support"]]
    return sum(vals) / len(vals) if vals else 0.0


# ── 修改內容抽取 ──────────────────────────────────────────────

_FIELD_ALIASES = {
    "日期": ("日期", "date", "口試日期", "day"),
    "時間": ("時間", "time", "時段", "開始時間"),
    "方式": ("方式", "口試方式", "mode", "format", "形式", "視訊", "出席方式"),
    "地點": ("地點", "location", "place", "venue", "教室", "場地"),
    "備註": ("備註", "note", "notes", "remark", "remarks"),
    "委員": ("委員", "committee", "服務單位", "口試委員", "委員名單", "委員資料"),
}


def canonical_field(value: str) -> str:
    v = (value or "").strip().lower()
    for canon, aliases in _FIELD_ALIASES.items():
        if any(a.lower() in v for a in aliases):
            return canon
    return v


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return re.sub(r"[\s「」『』\"'（）()，,。．.：:]", "", text)


def value_match(gold: str, pred: str) -> bool:
    """新值是否相符：正規化後互為子字串，或數字序列相同（12/25 ≈ 12 月 25 日）。"""
    g, p = _norm(gold), _norm(pred)
    if not g:          # gold 沒有具體新值（教授只說要改）→ 不比對新值
        return True
    if g in p or (p and p in g):
        return True
    gd, pd = re.findall(r"\d+", g), re.findall(r"\d+", p)
    return bool(gd) and [int(x) for x in gd] == [int(x) for x in pd]


def score_modifications(gold_mods: list[dict], pred_mods: list[dict]) -> dict:
    """gold 與模型輸出的修改項一對一貪婪配對。

    回傳：tp（欄位與新值都對）、field_tp（欄位對）、value_tp（新值對，不論欄位）、gold 數、pred 數。
    gold 沒有具體新值時（教授只說要改），新值不列入比對，只看欄位。
    """
    pool = list(pred_mods or [])
    tp = field_tp = value_tp = 0
    for g in gold_mods:
        gf = canonical_field(g.get("field", ""))
        gv = g.get("new_value", "")
        best = None  # (score, index, field_ok, value_ok)
        for i, p in enumerate(pool):
            field_ok = canonical_field(p.get("field", "")) == gf
            value_ok = value_match(gv, p.get("new_value", "")) if gv else field_ok
            score = 2 * field_ok + value_ok
            if score and (best is None or score > best[0]):
                best = (score, i, field_ok, value_ok)
        if best is not None:
            _, i, field_ok, value_ok = best
            field_tp += field_ok
            value_tp += value_ok
            tp += field_ok and value_ok
            pool.pop(i)
    return {"tp": tp, "field_tp": field_tp, "value_tp": value_tp,
            "gold": len(gold_mods), "pred": len(pred_mods or [])}


def _prf(tp: int, n_pred: int, n_gold: int) -> dict:
    p = tp / n_pred if n_pred else 0.0
    r = tp / n_gold if n_gold else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0}


# ── 彙總 ──────────────────────────────────────────────────────

def summarize(records: list[dict], gold_by_key: dict) -> dict:
    """records 與 gold 以 (set, id) 對應；回傳完整指標 dict。"""
    rows = []
    for rec in records:
        gold = gold_by_key[(rec.get("set", "dev"), rec["id"])]
        rows.append((rec, gold))

    evaluated = [(r, g) for r, g in rows if not r.get("skipped")]
    pairs = [(g["expected_intent"], r["predicted"]) for r, g in evaluated]
    correct = sum(1 for e, p in pairs if e == p)
    prf = per_intent_prf(pairs)

    # 自動放行（skipped＝空白不送模型＝轉人工）
    neg = [(r, g) for r, g in rows if not gold_auto_release(g)]
    pos = [(r, g) for r, g in rows if gold_auto_release(g)]
    false_release = [(r, g) for r, g in neg if not r.get("skipped") and would_auto_release(r)]
    needless_human = [(r, g) for r, g in pos if r.get("skipped") or not would_auto_release(r)]

    # 保留意見（只在 gold 為 confirm 的信上看；這是保留意見唯一有意義的情境）
    conf = [(r, g) for r, g in evaluated if g["expected_intent"] == "confirm"]
    res_tp = sum(1 for r, g in conf if g.get("expected_has_reservation") and r.get("has_reservation"))
    res_fp = sum(1 for r, g in conf if not g.get("expected_has_reservation") and r.get("has_reservation"))
    res_fn = sum(1 for r, g in conf if g.get("expected_has_reservation") and not r.get("has_reservation"))

    # 修改抽取（gold 為 modify 的信，不論模型判成什麼）
    mod = Counter()
    for r, g in evaluated:
        if g["expected_intent"] != "modify":
            continue
        mod.update(score_modifications(g.get("expected_modifications", []), r.get("modifications") or []))

    by_cat: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r, g in evaluated:
        by_cat[g.get("category", "")][1] += 1
        by_cat[g.get("category", "")][0] += g["expected_intent"] == r["predicted"]

    lat = sorted(r["latency_ms"] for r, _ in evaluated if r.get("latency_ms") is not None)
    usage = [r["usage"] for r, _ in evaluated if r.get("usage")]

    return {
        "n_total": len(rows),
        "n_evaluated": len(evaluated),
        "n_skipped": len(rows) - len(evaluated),
        "n_errors": sum(1 for r, _ in evaluated if r["predicted"] == "error"),
        "n_production_rejected": sum(1 for r, _ in evaluated if r.get("production_rejected")),
        "accuracy": correct / len(evaluated) if evaluated else 0.0,
        "correct": correct,
        "macro_f1": macro_f1(prf),
        "per_intent": prf,
        "confusion": {k: dict(v) for k, v in confusion(pairs).items()},
        "auto_release": {
            "should_not_release": len(neg),
            "false_release": len(false_release),
            "false_release_rate": len(false_release) / len(neg) if neg else 0.0,
            "false_release_ids": [r["id"] for r, _ in false_release],
            "should_release": len(pos),
            "needless_human": len(needless_human),
            "needless_human_rate": len(needless_human) / len(pos) if pos else 0.0,
            "needless_human_ids": [r["id"] for r, _ in needless_human],
        },
        "reservation": {"tp": res_tp, "fp": res_fp, "fn": res_fn, **_prf(res_tp, res_tp + res_fp, res_tp + res_fn)},
        "modifications": {
            "gold_items": mod["gold"], "pred_items": mod["pred"],
            "field_value": _prf(mod["tp"], mod["pred"], mod["gold"]),
            "field_only": _prf(mod["field_tp"], mod["pred"], mod["gold"]),
            "value_only": _prf(mod["value_tp"], mod["pred"], mod["gold"]),
            "tp": mod["tp"], "field_tp": mod["field_tp"], "value_tp": mod["value_tp"],
        },
        "by_category": {k: {"correct": v[0], "total": v[1]} for k, v in sorted(by_cat.items())},
        "latency_ms": {
            "n": len(lat),
            "p50": lat[len(lat) // 2] if lat else None,
            "p95": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None,
            "mean": sum(lat) / len(lat) if lat else None,
        },
        "tokens": {
            "prompt": sum(u.get("prompt_tokens", 0) for u in usage),
            "completion": sum(u.get("completion_tokens", 0) for u in usage),
            "calls_with_usage": len(usage),
        },
    }


def failures(records: list[dict], gold_by_key: dict, text_of) -> list[dict]:
    """所有判錯、放錯、漏偵測保留意見的案例；text_of(rec) 回傳實際送模型的文字。"""
    out = []
    for rec in records:
        g = gold_by_key[(rec.get("set", "dev"), rec["id"])]
        if rec.get("skipped"):
            if gold_auto_release(g):
                out.append({"rec": rec, "gold": g, "text": text_of(rec), "kinds": ["空白未送模型（轉人工）"]})
            continue
        kinds = []
        if rec["predicted"] != g["expected_intent"]:
            kinds.append("意圖錯誤")
        if not gold_auto_release(g) and would_auto_release(rec):
            kinds.append("誤自動放行")
        elif gold_auto_release(g) and not would_auto_release(rec):
            kinds.append("可放行卻轉人工")
        if g["expected_intent"] == "confirm" and bool(g.get("expected_has_reservation")) != bool(rec.get("has_reservation")):
            kinds.append("保留意見判錯")
        if rec.get("production_rejected"):
            kinds.append("正式流程核對擋下：" + rec["production_rejected"])
        if kinds:
            out.append({"rec": rec, "gold": g, "text": text_of(rec), "kinds": kinds})
    return out
