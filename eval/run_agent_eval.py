# -*- coding: utf-8 -*-
"""Agent 層級評測：情境 benchmark＋提示注入／個資／越權測試集，結果存 eval/results/（JSON＋Markdown）。

使用方式（在專案根目錄）：
    .venv\\Scripts\\python.exe eval\\run_agent_eval.py                      # 離線（假 LLM），兩個測試集全跑
    .venv\\Scripts\\python.exe eval\\run_agent_eval.py --suite injection    # 只跑注入／個資測試集
    .venv\\Scripts\\python.exe eval\\run_agent_eval.py --provider gemini --sleep 4   # 真實模型（GEMINI_API_KEY）
    .venv\\Scripts\\python.exe eval\\run_agent_eval.py --provider openai    # 備選供應商（OPENAI_API_KEY）

離線模式（預設 --provider fake）不讀任何金鑰、不連網，跑這些模式：
- benchmark：oracle（正確答案的假模型）、rule_only（llm=None，固定工作流對照組）、
  fault:exception／timeout／malformed_json／wrong_schema／hallucinated（模型故障）
- injection：obey（照注入文字做事的被攻陷模型，最壞情境）、oracle（守規矩的模型）
真實模型模式：benchmark 跑 real＋rule_only，injection 跑 real；金鑰與正式系統相同
（環境變數或專案根目錄 .env，koushi.llm.make_llm()／koushi.runtime.make_classifier()），不會印出或寫進結果。
任何模式都不會寄出真信（寄送一律走假 transport）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))
sys.path.insert(0, str(EVAL_DIR.parent))

import agent_harness as H  # noqa: E402
import agent_metrics as AM  # noqa: E402

RESULTS_DIR = EVAL_DIR / "results"
PROPOSAL_AGENTS = ("affiliation", "correction", "title")


# ── 模型 ──────────────────────────────────────────────────

def real_llm(provider: str, model: str | None):
    """依 provider 建立真實 LLM 與附件4 classifier（沿用 koushi.llm.make_llm／runtime.make_classifier）。"""
    from koushi.runtime import load_local_env
    load_local_env()
    os.environ["KOUSHI_NO_LOCAL_ENV"] = "1"   # 下面調整的環境變數不要被 .env 蓋回去
    if provider == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("缺少 OPENAI_API_KEY（環境變數或專案根目錄 .env）。")
        if "generativelanguage" in os.environ.get("OPENAI_BASE_URL", ""):
            raise SystemExit("OPENAI_BASE_URL 指向 Gemini；請改用 --provider gemini。")
        os.environ.pop("GEMINI_API_KEY", None)   # provider_config() 以 GEMINI_API_KEY 優先；本程序內改走 OpenAI
    elif provider == "gemini":
        if os.environ.get("GEMINI_API_KEY"):
            os.environ.pop("OPENAI_API_KEY", None)   # 只用 Gemini，不讓 OpenAI 當備援混進結果
            os.environ.pop("OPENAI_BASE_URL", None)
        elif not ("generativelanguage" in os.environ.get("OPENAI_BASE_URL", "") and os.environ.get("OPENAI_API_KEY")):
            raise SystemExit("缺少 GEMINI_API_KEY（或指向 Gemini 的 OPENAI_API_KEY＋OPENAI_BASE_URL）。")
    elif provider == "ollama":
        os.environ["KOUSHI_LLM_PROVIDER"] = "ollama"   # 本機 Ollama（OpenAI 相容端點），不需金鑰
    else:
        raise SystemExit(f"未知的 provider：{provider}")
    if model:
        os.environ["KOUSHI_LLM_MODEL"] = model
    from koushi.llm import make_llm, provider_config
    from koushi.runtime import make_classifier
    conf = provider_config()
    if conf is None or conf["provider"] != provider:
        raise SystemExit(f"環境設定解析出的供應商是 {conf and conf['provider']}，不是 {provider}。")
    return make_llm(), make_classifier(), conf["model"]


# ── 執行 ──────────────────────────────────────────────────

def _run_case(agent: str, scn: dict, mode: str, *, suite_data: dict, real=None, sleep: float = 0.0) -> dict:
    llm = classifier = None
    if mode == "real":
        inner, classifier, _ = real
        llm = H.RecordingLLM(inner)
    if agent == "mailbox":
        result = H.run_mailbox(scn, mode, llm=llm, classifier=classifier,
                               default_settings=suite_data.get("default_settings"))
    elif agent == "assistant":
        result = H.run_assistant(scn, mode, llm=llm)
    else:
        result = H.run_proposal(agent, scn, mode, llm=llm)
    if mode == "real" and sleep and result.get("llm_calls"):
        time.sleep(sleep)
    return result


def _cases(suite_data: dict, suite: str):
    for agent in ("mailbox", *PROPOSAL_AGENTS, "assistant"):
        for scn in suite_data.get(agent, []):
            yield agent, scn
    for scn in suite_data.get("proposals", []):   # 注入測試集的提案型情境（各自標 agent）
        yield scn["agent"], scn


def run_suite(suite: str, modes: list[str], *, real=None, sleep: float = 0.0, verbose: bool = True) -> dict:
    path = H.BENCHMARK_PATH if suite == "benchmark" else H.INJECTION_PATH
    data = H.load_scenarios(path)
    out = {"scenario_file": path.name, "modes": {}}
    for mode in modes:
        if mode == "real" and real is None:
            continue
        results = []
        for agent, scn in _cases(data, suite):
            if mode == "rule_only" and agent == "assistant":
                continue   # 沒有模型時問答助理不提供（頁面隱藏），沒有可比的固定流程
            r = _run_case(agent, scn, mode, suite_data=data, real=real, sleep=sleep)
            results.append(r)
            if verbose:
                mark = "PASS" if r["passed"] else ("FAIL" if r["safety_passed"] else "FAIL(安全)")
                print(f"  [{suite}/{mode}] {r['id']:<9} {mark}  {r['title']}")
        trace = [e for r in results for e in r.pop("_trace", [])]
        for r in results:   # 結果檔只留失敗檢查的細節，通過的只列名稱
            r["checks_ok"] = [c["name"] for c in r["checks"] if c["ok"]]
            r["checks"] = [c for c in r["checks"] if not c["ok"]]
        out["modes"][mode] = {"cases": results, "summary": summarize(results),
                              "trace_metrics": AM.compute(trace), "_trace_events": len(trace)}
        if suite == "benchmark" and mode == "oracle":
            out["sample_trace"] = _sample_trace(data)
    if suite == "injection":
        out["static_checks"] = H.static_assistant_checks()
    return out


def _sample_trace(data: dict) -> dict:
    """附件二「可查核的操作 trace」：MB03（教授要求修改）在 oracle 模式的完整軌跡。"""
    scn = next(s for s in data["mailbox"] if s["id"] == "MB03")
    r = H.run_mailbox(scn, "oracle")
    events = r.pop("_trace", [])
    return {"scenario": "MB03", "title": scn["title"], "events": [
        {k: e.get(k) for k in ("sequence", "step", "tool", "input_summary", "output", "model", "prompt_version",
                               "latency_ms", "decision", "human_required", "usage")} for e in events]}


def summarize(results: list[dict]) -> dict:
    by_agent: dict[str, dict] = defaultdict(lambda: Counter())
    for r in results:
        c = by_agent[r["agent"]]
        c["cases"] += 1
        c["passed"] += r["passed"]
        c["safety_passed"] += r["safety_passed"]
        c["crashed"] += bool(r.get("error"))
        o = r.get("outcome") or {}
        if r["agent"] == "mailbox" and o:
            c["classified"] += o["classified"]
            c["specific_proposal"] += o["specific_proposal"]
            c["bare_handoff"] += o["bare_handoff"]
            c["category_correct"] += o["category"] == o.get("expected_category")
            c["awaiting_staff"] += o["status"] == "awaiting_staff"
            c["no_staff_needed"] += o["status"] in ("auto_done", "ignored")
            c["mapped_changes"] += o["mapped_changes"]
            c["llm_error"] += bool(o["llm_error"])
        elif r["agent"] in PROPOSAL_AGENTS and o:
            c["useful"] += bool(o["useful"])
            c["correct"] += bool(o["correct"])
            c["error_flagged"] += bool(o["error"])
        elif r["agent"] == "assistant" and o:
            c[f"kind_{o['kind']}"] += 1
    total = Counter()
    for c in by_agent.values():
        total.update({k: c[k] for k in ("cases", "passed", "safety_passed", "crashed")})
    return {"total": dict(total), "by_agent": {a: dict(c) for a, c in by_agent.items()},
            "failures": [{"id": r["id"], "agent": r["agent"], "safety": not r["safety_passed"],
                          "failed_checks": [f"{c['name']}（{c['detail']}）" if c["detail"] else c["name"]
                                            for c in r["checks"] if not c["ok"]]}
                         for r in results if not r["passed"]]}


# ── 報告 ──────────────────────────────────────────────────

def _git_commit() -> str:
    try:
        return subprocess.run(["git", "describe", "--always", "--dirty"], cwd=EVAL_DIR.parent, capture_output=True,
                              text=True, timeout=10).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _prompt_versions() -> dict:
    from koushi import affiliation_agent, assistant, correction_agent, mailbox_agent, title_agent
    from koushi.classifier import PROMPT_VERSION as CLASSIFIER_VERSION
    return {"mailbox": mailbox_agent.PROMPT_VERSION, "affiliation": affiliation_agent.PROMPT_VERSION,
            "correction": correction_agent.PROMPT_VERSION, "title": title_agent.PROMPT_VERSION,
            "assistant": assistant.PROMPT_VERSION, "attachment4_classifier": CLASSIFIER_VERSION}


def _pct(n, d) -> str:
    return f"{n}/{d}（{n / d * 100:.0f}%）" if d else "—"


def to_markdown(report: dict) -> str:
    env = report["environment"]
    lines = [f"# Agent 層級評測結果（{report['created_at']}）", "",
             f"- 模型：{report['provider']}" + (f"／{report['model']}" if report.get("model") else "（腳本化假 LLM，未呼叫任何 API）"),
             f"- 程式版本：git {env['git_commit']}；Python {env['python']}；{env['platform']}",
             "- Prompt 版本：" + "、".join(f"{k} {v}" for k, v in report["prompt_versions"].items()),
             f"- 價目：{report['prices_as_of']}",
             "- 資料：全部合成（eval/agent_scenarios.json、eval/injection_scenarios.json），每個情境在獨立暫存 STATE_DIR 從零執行；寄信走假 transport。",
             "- 任務成功＝該情境的所有檢查都通過；安全不變量見 eval/README.md。", ""]
    bench = report["suites"].get("benchmark")
    if bench:
        lines += ["## 情境 benchmark", "",
                  "| 模式 | 情境數 | 任務成功 | 安全不變量全過 | 未處理例外 |", "|---|---|---|---|---|"]
        for mode, m in bench["modes"].items():
            t = m["summary"]["total"]
            lines.append(f"| {mode} | {t['cases']} | {_pct(t['passed'], t['cases'])} | {_pct(t['safety_passed'], t['cases'])} | {t['crashed']} |")
        lines += ["", "### 各 agent（任務成功）", "", "| agent | " + " | ".join(bench["modes"]) + " |",
                  "|---|" + "---|" * len(bench["modes"])]
        agents = sorted({a for m in bench["modes"].values() for a in m["summary"]["by_agent"]})
        for agent in agents:
            cells = []
            for m in bench["modes"].values():
                c = m["summary"]["by_agent"].get(agent)
                cells.append(_pct(c["passed"], c["cases"]) if c else "—")
            lines.append(f"| {agent} | " + " | ".join(cells) + " |")
        lines += ["", "### 對照：固定工作流（rule_only，llm=None）vs agent", "",
                  "信箱 agent（同一批信）：", "",
                  "| 模式 | 有分類＋摘要 | 有具體處理提案 | 只轉人工、沒有任何資訊 | 類別正確 | 可直接結案（不需專員） | 結構化修改項目 |",
                  "|---|---|---|---|---|---|---|"]
        for mode, m in bench["modes"].items():
            c = m["summary"]["by_agent"].get("mailbox")
            if not c or mode.startswith("fault:"):
                continue
            n = c["cases"]
            lines.append(f"| {mode} | {_pct(c['classified'], n)} | {_pct(c['specific_proposal'], n)} | "
                         f"{_pct(c['bare_handoff'], n)} | {_pct(c['category_correct'], n)} | "
                         f"{_pct(c['no_staff_needed'], n)} | {c['mapped_changes']} |")
        lines += ["", "提案型 agent（有可採用的建議／建議正確，正確含「應回 null」的情境）：", "",
                  "| 模式 | " + " | ".join(PROPOSAL_AGENTS) + " |", "|---|" + "---|" * len(PROPOSAL_AGENTS)]
        for mode, m in bench["modes"].items():
            if mode.startswith("fault:"):
                continue
            cells = []
            for agent in PROPOSAL_AGENTS:
                c = m["summary"]["by_agent"].get(agent)
                cells.append("—" if not c else f"有建議 {c['useful']}/{c['cases']}；正確 {c['correct']}/{c['cases']}")
            lines.append(f"| {mode} | " + " | ".join(cells) + " |")
        faults = {k: v for k, v in bench["modes"].items() if k.startswith("fault:")}
        if faults:
            lines += ["", "### 模型故障注入（逾時、例外、壞 JSON、錯 schema、幻覺）", "",
                      "| 故障 | 情境數 | 未處理例外 | 安全不變量全過 | 信箱：模型失敗→轉人工 | 提案型：失敗有註記錯誤 | 助理結果 |",
                      "|---|---|---|---|---|---|---|"]
            for mode, m in faults.items():
                s = m["summary"]
                mb = s["by_agent"].get("mailbox", {})
                prop_err = sum(s["by_agent"].get(a, {}).get("error_flagged", 0) for a in PROPOSAL_AGENTS)
                prop_llm = sum(len([r for r in m["cases"] if r["agent"] == a and r["llm_calls"]]) for a in PROPOSAL_AGENTS)
                asst = s["by_agent"].get("assistant", {})
                kinds = "、".join(f"{k[5:]} {v}" for k, v in sorted(asst.items()) if k.startswith("kind_"))
                lines.append(f"| {mode[6:]} | {s['total']['cases']} | {s['total']['crashed']} | "
                             f"{_pct(s['total']['safety_passed'], s['total']['cases'])} | "
                             f"{_pct(mb.get('llm_error', 0), len([r for r in m['cases'] if r['agent'] == 'mailbox' and r['llm_calls']]))} | "
                             f"{_pct(prop_err, prop_llm)} | {kinds} |")
        lines += ["", "### 軌跡指標（由 audit_log.json 的 agent_trace 算出，eval/agent_metrics.py）", ""]
        for mode in ("oracle", "repair", "rule_only", "real", "fault:timeout"):
            if mode in bench["modes"]:
                lines += [AM.markdown(bench["modes"][mode]["trace_metrics"], title=f"{mode}"), ""]
    inj = report["suites"].get("injection")
    if inj:
        modes = list(inj["modes"])
        lines += ["## 提示注入、個資與越權測試", "",
                  "預設設定為自動化全開（自動放行開啟、非 shadow、教授甲在白名單、自動收件通知開啟）。"
                  "obey＝被攻陷、照注入文字做事的假模型（最壞情境）；「安全」欄只看安全不變量。", "",
                  "| 模式 | 案例數 | 全部檢查通過 | 安全不變量全過 |", "|---|---|---|---|"]
        for mode, m in inj["modes"].items():
            t = m["summary"]["total"]
            lines.append(f"| {mode} | {t['cases']} | {_pct(t['passed'], t['cases'])} | {_pct(t['safety_passed'], t['cases'])} |")
        lines += ["", "| 案例 | 類型 | 說明 | " + " | ".join(modes) + " |", "|---|---|---|" + "---|" * len(modes)]
        by_id: dict[str, dict] = {}
        for mode, m in inj["modes"].items():
            for r in m["cases"]:
                by_id.setdefault(r["id"], {"r": r})[mode] = r
        for cid, row in by_id.items():
            cells = []
            for mode in modes:
                r = row.get(mode)
                if r is None:
                    cells.append("—")
                elif r["passed"]:
                    cells.append("通過")
                else:
                    failed = [c["name"] for c in r["checks"]]
                    cells.append(("**安全失敗**：" if not r["safety_passed"] else "偵測未中：") + "；".join(failed))
            lines.append(f"| {cid} | {row['r']['kind']} | {row['r']['title']} | " + " | ".join(cells) + " |")
        for c in inj.get("static_checks", []):
            lines.append(f"| STATIC | 權限 | {c['name']} | {'通過' if c['ok'] else '失敗'}（{c['detail']}） |" + " |" * (len(modes) - 1))
    sample = (bench or {}).get("sample_trace")
    if sample:
        lines += ["", f"## 操作軌跡範例（{sample['scenario']}：{sample['title']}）", "",
                  "| # | step | tool | decision | 需人工 | output 摘要 |", "|---|---|---|---|---|---|"]
        for e in sample["events"]:
            out = json.dumps(e["output"], ensure_ascii=False)
            lines.append(f"| {e['sequence']} | {e['step']} | {e['tool'] or ''} | {e['decision'] or ''} | "
                         f"{'是' if e['human_required'] else ''} | {out[:90].replace('|', '／')} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Agent 層級評測（情境 benchmark＋提示注入／個資測試）")
    parser.add_argument("--suite", choices=["benchmark", "injection", "all"], default="all")
    parser.add_argument("--provider", choices=["fake", "openai", "gemini", "ollama"], default="fake")
    parser.add_argument("--model", help="覆寫模型名稱（真實模型模式）")
    parser.add_argument("--sleep", type=float, default=0.0, help="真實模型每個情境之間暫停秒數（免費額度限速用）")
    parser.add_argument("--out", default=str(RESULTS_DIR))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    logging.getLogger("koushi").setLevel(logging.ERROR)

    real, model = None, None
    if args.provider == "fake":
        os.environ["KOUSHI_NO_LOCAL_ENV"] = "1"
        bench_modes = ["oracle", "repair", "rule_only", *[f"fault:{k}" for k in H.FAULT_KINDS]]
        inj_modes = ["obey", "oracle"]
    else:
        real = real_llm(args.provider, args.model)
        model = real[2]
        bench_modes = ["real", "rule_only"]
        inj_modes = ["real"]

    created = datetime.now()
    report = {
        "schema": 1, "created_at": created.strftime("%Y-%m-%d %H:%M:%S"), "provider": args.provider, "model": model,
        "prompt_versions": _prompt_versions(), "prices": AM.DEFAULT_PRICES, "prices_as_of": AM.PRICES_AS_OF,
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "git_commit": _git_commit()},
        "suites": {},
    }
    started = time.perf_counter()
    if args.suite in ("benchmark", "all"):
        report["suites"]["benchmark"] = run_suite("benchmark", bench_modes, real=real, sleep=args.sleep,
                                                  verbose=not args.quiet)
    if args.suite in ("injection", "all"):
        report["suites"]["injection"] = run_suite("injection", inj_modes, real=real, sleep=args.sleep,
                                                  verbose=not args.quiet)
    report["wall_seconds"] = round(time.perf_counter() - started, 1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_model = re.sub(r'[\/:*?"<>|]', "-", model) if model else ""   # Windows 檔名不能有冒號（qwen2.5:3b）
    stem = f"agent_eval_{args.provider}{'_' + safe_model if model else ''}_{created.strftime('%Y%m%d_%H%M%S')}"
    (out_dir / f"{stem}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    (out_dir / f"{stem}.md").write_text(to_markdown(report), encoding="utf-8")
    print(f"\n結果：{out_dir / (stem + '.json')}\n報告：{out_dir / (stem + '.md')}")
    for suite, data in report["suites"].items():
        for mode, m in data["modes"].items():
            t = m["summary"]["total"]
            print(f"  {suite}/{mode}: 任務成功 {t['passed']}/{t['cases']}，安全不變量 {t['safety_passed']}/{t['cases']}，例外 {t['crashed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
