# -*- coding: utf-8 -*-
"""評測腳本：用合成資料集跑教授回信判讀，結果存到 eval/results/，再交 report.py 算指標。

使用方式（在專案根目錄）：
    .venv\\Scripts\\python.exe eval\\run_eval.py                       # Gemini gemini-3.5-flash-lite，dev＋quoted 兩個資料集
    .venv\\Scripts\\python.exe eval\\run_eval.py --provider openai      # 備選供應商 OpenAI gpt-4o-mini
    .venv\\Scripts\\python.exe eval\\run_eval.py --provider keyword     # 不用 LLM 的關鍵字對照組（免 API）
    .venv\\Scripts\\python.exe eval\\run_eval.py --sets dev --provider openai --model gpt-4o

金鑰來源與正式系統相同：環境變數，或專案根目錄 .env（koushi.runtime.load_local_env）。
  - gemini：GEMINI_API_KEY；未設時可用 OPENAI_API_KEY＋OPENAI_BASE_URL
            指向 generativelanguage.googleapis.com
  - openai：OPENAI_API_KEY（若同時設了 OPENAI_BASE_URL，就是打那個相容端點）
金鑰不會被印出或寫進結果檔。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(EVAL_DIR.parent))

import data as D  # noqa: E402
from koushi.classifier import ClassifierError  # noqa: E402

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-*]{6,}|AIza[0-9A-Za-z_\-*]{10,})")


def _redact(text: str) -> str:
    return _SECRET_RE.sub("<redacted>", text)


def build_adapter(provider: str, model: str | None):
    """回傳 (adapter, model_name)。缺金鑰時拋 SystemExit，訊息不含金鑰。"""
    if provider == "keyword":
        from baselines import KeywordBaseline
        a = KeywordBaseline()
        return a, a.model_name

    from koushi.runtime import load_local_env
    load_local_env()
    from koushi.adapters.openai_adapter import OpenAIAdapter

    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("缺少 OPENAI_API_KEY（環境變數或專案根目錄 .env），略過 openai。")
        if "generativelanguage" in os.environ.get("OPENAI_BASE_URL", ""):
            raise SystemExit("OPENAI_BASE_URL 指向 Gemini；請改用 --provider gemini。")
        model = model or "gpt-4o-mini"
        return OpenAIAdapter(model=model), model
    if provider == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if key:
            os.environ["OPENAI_BASE_URL"] = GEMINI_BASE_URL  # OpenAI SDK 讀這個環境變數
        elif "generativelanguage" in os.environ.get("OPENAI_BASE_URL", "") and os.environ.get("OPENAI_API_KEY"):
            key = os.environ["OPENAI_API_KEY"]
        else:
            raise SystemExit("缺少 GEMINI_API_KEY（或指向 Gemini 的 OPENAI_API_KEY＋OPENAI_BASE_URL），略過 gemini。")
        from koushi.llm import GEMINI_MODEL
        model = model or GEMINI_MODEL
        return OpenAIAdapter(api_key=key, model=model, base_url=os.environ["OPENAI_BASE_URL"]), model
    if provider == "ollama":
        os.environ["KOUSHI_LLM_PROVIDER"] = "ollama"
        if model:
            os.environ["KOUSHI_LLM_MODEL"] = model
        from koushi.llm import provider_config
        conf = provider_config()
        return (OpenAIAdapter(api_key=conf["api_key"], model=conf["model"], base_url=conf["base_url"],
                              timeout=conf["timeout"]), conf["model"])
    raise SystemExit(f"未知的 provider：{provider}")


def _instrument(adapter) -> dict:
    """包住 OpenAI client 的 create()，記下每次呼叫的 token 用量。走的仍是正式 adapter 的程式碼。"""
    box: dict = {}
    client = getattr(adapter, "_client", None)
    if client is None:
        return box
    original = client.chat.completions.create

    def create(*args, **kwargs):
        resp = original(*args, **kwargs)
        u = getattr(resp, "usage", None)
        box["usage"] = ({"prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                         "completion_tokens": getattr(u, "completion_tokens", 0) or 0} if u else None)
        return resp

    client.chat.completions.create = create
    return box


def production_reject_reason(cr, text: str) -> str:
    """與 advisor_confirmation._call_classifier() 相同的逐字核對；不通過時正式流程轉人工。"""
    if not cr.evidence.strip() or cr.evidence not in text:
        return "判讀依據不是逐字引用"
    if any(not m.quote or m.quote not in text for m in cr.modifications):
        return "修改依據不是逐字引用"
    return ""


def run(adapter, items: list[dict], *, sleep: float, verbose: bool) -> list[dict]:
    usage_box = _instrument(adapter)
    records = []
    for n, item in enumerate(items, 1):
        rec = {k: item[k] for k in ("set", "variant", "id")}
        text = item["text"]
        if not text.strip():
            # 正式流程：reply_text 空白不送模型，直接轉人工
            rec.update(predicted="skipped", skipped=True)
            records.append(rec)
            if verbose:
                print(f"  [{n:3d}/{len(items)}] {item['set']}/{item['variant']} #{item['id']} → 空白，不送模型")
            continue
        usage_box.clear()
        t0 = time.perf_counter()
        try:
            cr = adapter.classify(text)
            rec.update(
                predicted=cr.intent, evidence=cr.evidence, has_reservation=cr.has_reservation,
                modifications=[{"row": m.row, "field": m.field, "new_value": m.new_value, "quote": m.quote}
                               for m in cr.modifications],
                questions=list(cr.questions),
                production_rejected=production_reject_reason(cr, text),
            )
        except ClassifierError as e:
            rec.update(predicted="error", error=_redact(str(e))[:500])
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        if usage_box.get("usage"):
            rec["usage"] = usage_box["usage"]
        records.append(rec)
        if verbose:
            extra = f"  [有保留]" if rec.get("has_reservation") else ""
            extra += f"  [核對擋下：{rec['production_rejected']}]" if rec.get("production_rejected") else ""
            extra += f"  {rec.get('error', '')[:80]}" if rec["predicted"] == "error" else ""
            print(f"  [{n:3d}/{len(items)}] {item['set']}/{item['variant']} #{item['id']} → {rec['predicted']}{extra}")
        if sleep:
            time.sleep(sleep)
    return records


def main():
    ap = argparse.ArgumentParser(description="評估教授回信意圖判讀")
    ap.add_argument("--provider", default="gemini", choices=["openai", "gemini", "ollama", "keyword"])
    ap.add_argument("--model", default=None, help="模型名稱（gemini 預設 gemini-3.5-flash-lite；openai 預設 gpt-4o-mini；ollama 預設 qwen2.5:3b）")
    ap.add_argument("--sets", default="dev,quoted", help="逗號分隔：dev、quoted")
    ap.add_argument("--sleep", type=float, default=None, help="每次呼叫間隔秒數（Gemini 預設 4，配合免費額度；其他 LLM 預設 0.5）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    adapter, model = build_adapter(args.provider, args.model)
    sets = [s.strip() for s in args.sets.split(",") if s.strip()]
    items = D.inputs(sets)
    sleep = args.sleep if args.sleep is not None else {"keyword": 0.0, "gemini": 4.0}.get(args.provider, 0.5)
    print(f"provider={args.provider} model={model} prompt={adapter.prompt_version} 輸入 {len(items)} 筆（{', '.join(sets)}）")

    t0 = time.time()
    records = run(adapter, items, sleep=sleep, verbose=not args.quiet)
    elapsed = round(time.time() - t0, 1)

    results_dir = EVAL_DIR / "results"
    results_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_model = re.sub(r'[\/:*?"<>|]', "-", model)   # Windows 檔名不能有冒號（qwen2.5:3b）
    path = results_dir / f"eval_{safe_model}_{adapter.prompt_version}_{stamp}.json"
    path.write_text(json.dumps({
        "schema": 2,
        "provider": args.provider,
        "model": model,
        "prompt_version": adapter.prompt_version,
        "timestamp": stamp,
        "sets": sets,
        "elapsed_seconds": elapsed,
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果已存到：{path}")

    import report
    report.print_report(path)


if __name__ == "__main__":
    main()
