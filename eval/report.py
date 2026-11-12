# -*- coding: utf-8 -*-
"""讀 eval/results/*.json，計算並印出評測指標（不呼叫 API）。

使用方式：
    .venv\\Scripts\\python.exe eval\\report.py eval\\results\\某次結果.json
    .venv\\Scripts\\python.exe eval\\report.py eval\\results\\*.json --compare   # 多個結果並列比較
    .venv\\Scripts\\python.exe eval\\report.py ... --failures                    # 附上每筆失敗的原文

舊格式（schema 1，v1.0／v1.1 時期）的結果也能讀；會以目前資料集的 gold 重新計分。
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))

import data as D  # noqa: E402
import metrics as M  # noqa: E402

# 美元／百萬 token（輸入, 輸出）。只列已核對過的；其他模型不估成本。
PRICES = {"gpt-4o-mini": (0.15, 0.60)}
GROUP_LABEL = {
    ("dev", "reply_text"): "dev（60 封）",
    ("quoted", "cleaned"): "quoted：去引用後",
    ("quoted", "raw"): "quoted：原始全文",
}


def _legacy_records(results: list[dict]) -> list[dict]:
    gold = {c["id"]: c for c in D.load_dev()}
    out = []
    for r in results:
        rec = {"set": "dev", "variant": "reply_text", "id": r["id"], "predicted": r["predicted"]}
        if r.get("skipped"):
            rec.update(predicted="skipped", skipped=True)
        else:
            text = gold[r["id"]]["reply_text"]
            mods = r.get("modifications") or []
            rec.update(evidence=r.get("evidence", ""), has_reservation=r.get("has_reservation", False),
                       modifications=mods, questions=r.get("questions") or [])
            if r["predicted"] != "error":
                ev = rec["evidence"]
                if not ev.strip() or ev not in text:
                    rec["production_rejected"] = "判讀依據不是逐字引用"
                elif any(not m.get("quote") or m["quote"] not in text for m in mods):
                    rec["production_rejected"] = "修改依據不是逐字引用"
        out.append(rec)
    return out


def load_result(path) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("schema") == 2:
        return raw
    return {"schema": 1, "provider": "openai", "model": raw["model"], "prompt_version": raw.get("prompt_version", "?"),
            "timestamp": raw.get("timestamp", ""), "elapsed_seconds": raw.get("summary", {}).get("elapsed_seconds"),
            "records": _legacy_records(raw["results"])}


def analyse(result: dict) -> dict:
    gold = D.all_gold()
    groups = defaultdict(list)
    for rec in result["records"]:
        groups[(rec["set"], rec["variant"])].append(rec)
    return {key: M.summarize(recs, gold) for key, recs in groups.items()}, groups, gold


def _pct(x):
    return f"{x * 100:.1f}%"


def _cost(model: str, tokens: dict):
    price = PRICES.get(model)
    if not price or not tokens["calls_with_usage"]:
        return None
    return tokens["prompt"] / 1e6 * price[0] + tokens["completion"] / 1e6 * price[1]


def _text_of_factory(gold, groups_variant):
    def text_of(rec):
        g = gold[(rec["set"], rec["id"])]
        if rec["set"] == "dev":
            return g["reply_text"]
        return g["cleaned_text"] if rec["variant"] == "cleaned" else g["raw_text"]
    return text_of


def print_report(path, *, show_failures: bool = True) -> dict:
    result = load_result(path)
    summaries, groups, gold = analyse(result)
    print("\n" + "=" * 70)
    print(f"  {result['model']}（prompt {result['prompt_version']}，{result['timestamp']}）  {Path(path).name}")
    print("=" * 70)
    for key in sorted(summaries, key=lambda k: list(GROUP_LABEL).index(k) if k in GROUP_LABEL else 99):
        s = summaries[key]
        ar = s["auto_release"]
        print(f"\n── {GROUP_LABEL.get(key, key)} ──")
        print(f"  評估 {s['n_evaluated']} 封（空白不送模型 {s['n_skipped']}；API／格式錯誤 {s['n_errors']}；"
              f"逐字核對擋下 {s['n_production_rejected']}）")
        print(f"  意圖準確率 {s['correct']}/{s['n_evaluated']} = {_pct(s['accuracy'])}　macro-F1 {s['macro_f1']:.3f}")
        print(f"  ★ 誤自動放行率 {ar['false_release']}/{ar['should_not_release']} = {_pct(ar['false_release_rate'])}"
              + (f"　案例 {ar['false_release_ids']}" if ar["false_release_ids"] else ""))
        print(f"  可放行卻轉人工 {ar['needless_human']}/{ar['should_release']} = {_pct(ar['needless_human_rate'])}"
              + (f"　案例 {ar['needless_human_ids']}" if ar["needless_human_ids"] else ""))
        r = s["reservation"]
        print(f"  保留意見（gold=confirm 的信）P {r['precision']:.2f} R {r['recall']:.2f}（TP {r['tp']} FP {r['fp']} FN {r['fn']}）")
        m = s["modifications"]
        if m["gold_items"]:
            print(f"  修改抽取（{m['gold_items']} 項 gold／{m['pred_items']} 項輸出）"
                  f"欄位+新值 F1 {m['field_value']['f1']:.2f}（R {m['field_value']['recall']:.2f}）；"
                  f"只看欄位 F1 {m['field_only']['f1']:.2f}；只看新值 F1 {m['value_only']['f1']:.2f}")
        print("  各意圖 P / R / F1（support）：")
        for intent, v in s["per_intent"].items():
            if v["support"] or v["fp"]:
                print(f"    {intent:11s} {v['precision']:.2f} / {v['recall']:.2f} / {v['f1']:.2f}（{v['support']}）")
        print("  混淆矩陣（列＝gold，欄＝模型）：")
        cols = list(M.INTENTS) + ["error"]
        print("    " + " " * 11 + "".join(f"{c[:8]:>9s}" for c in cols))
        for intent in M.INTENTS:
            row = s["confusion"].get(intent, {})
            if row:
                print(f"    {intent:11s}" + "".join(f"{row.get(c, 0):9d}" for c in cols))
        if key[0] == "dev":
            weak = {k: v for k, v in s["by_category"].items() if v["correct"] < v["total"]}
            print(f"  類別正確率：{len(s['by_category']) - len(weak)}/{len(s['by_category'])} 類全對"
                  + ("；未全對：" + "、".join(f"{k} {v['correct']}/{v['total']}" for k, v in weak.items()) if weak else ""))
        if key[0] == "quoted":
            text_of = _text_of_factory(gold, key)
            trig = "確認無誤"
            leaks = [r["id"] for r in groups[key] if trig in text_of(r)
                     and trig not in gold[(r["set"], r["id"])]["professor_reply"]]
            print(f"  送模型文字仍含原信「{trig}」：{len(leaks)}/{len(groups[key])}" + (f"　{leaks}" if leaks else ""))
        lat, tok = s["latency_ms"], s["tokens"]
        if lat["n"]:
            print(f"  延遲 p50 {lat['p50']} ms／p95 {lat['p95']} ms")
        if tok["calls_with_usage"]:
            cost = _cost(result["model"], tok)
            print(f"  token：輸入 {tok['prompt']}、輸出 {tok['completion']}"
                  + (f"，約 US${cost:.4f}" if cost is not None else "（未列價目，不估成本）"))
        if show_failures:
            fails = M.failures(groups[key], gold, _text_of_factory(gold, key))
            if fails:
                print(f"  失敗明細（{len(fails)}）：")
                for f in fails:
                    rec, g = f["rec"], f["gold"]
                    print(f"    #{rec['id']} [{g.get('category', '')}] {'、'.join(f['kinds'])}")
                    print(f"       原文：{f['text'][:120]!r}")
                    print(f"       應為 {g['expected_intent']}{'（有保留）' if g.get('expected_has_reservation') else ''}"
                          f"，判為 {rec['predicted']}{'（有保留）' if rec.get('has_reservation') else ''}"
                          + (f"；依據：{rec.get('evidence', '')[:60]!r}" if rec.get("evidence") else "")
                          + (f"；錯誤：{rec.get('error', '')[:80]}" if rec.get("error") else ""))
    return summaries


def compare(paths: list[str]) -> None:
    print("\n| 模型 | prompt | 資料 | 準確率 | macro-F1 | 誤自動放行率 | 可放行卻轉人工 | 保留意見 R | 修改抽取 F1（欄位+新值） | 延遲 p50 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for p in paths:
        result = load_result(p)
        summaries, _, _ = analyse(result)
        for key in GROUP_LABEL:
            if key not in summaries:
                continue
            s = summaries[key]
            ar, m = s["auto_release"], s["modifications"]
            print(f"| {result['model']} | {result['prompt_version']} | {GROUP_LABEL[key]} | "
                  f"{s['correct']}/{s['n_evaluated']}（{_pct(s['accuracy'])}） | {s['macro_f1']:.3f} | "
                  f"{ar['false_release']}/{ar['should_not_release']}（{_pct(ar['false_release_rate'])}） | "
                  f"{ar['needless_human']}/{ar['should_release']} | "
                  f"{s['reservation']['recall']:.2f} | "
                  f"{m['field_value']['f1']:.2f} | {'—' if s['latency_ms']['p50'] is None else s['latency_ms']['p50']} |")


def main():
    ap = argparse.ArgumentParser(description="讀評測結果並計算指標")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--compare", action="store_true", help="最後印一張 Markdown 比較表")
    ap.add_argument("--no-failures", action="store_true")
    args = ap.parse_args()
    paths = [p for pat in args.paths for p in (sorted(glob.glob(pat)) or [pat])]
    for p in paths:
        print_report(p, show_failures=not args.no_failures)
    if args.compare:
        compare(paths)


if __name__ == "__main__":
    main()
