# -*- coding: utf-8 -*-
"""Agent 層級指標：只讀 `audit_log.json` 的 agent_trace 事件（koushi.agent_trace.load_trace）。

同一套計算可以用在正式環境的 STATE_DIR（真實營運紀錄）與評測跑出來的暫存 state。
不需要 gold 標註，所以量的是「流程有沒有走完、有沒有出錯、花多少」；
「答得對不對」要看 run_agent_eval.py 的情境評測（有預期結果）。

任務單位＝(agent, run_id, tracking_key)：
- mailbox：一封信（run_id 每封一個，tracking_key＝message_id）
- affiliation／correction／title：一個建議主題（同一批 run_id 會處理多個主題）
- assistant：一個問題（tracking_key＝run_id）

各 agent 的「完成」與「有效產出」定義見 TASK_RULES 與 eval/README.md。

網頁「Agent 指標」頁（koushi/web/app.py）即時呼叫 compute()；命令列入口在
eval/agent_metrics.py（薄包裝）：
    .venv\\Scripts\\python.exe eval\\agent_metrics.py <STATE_DIR> [--prices prices.json] [--json]
"""

from __future__ import annotations

from collections import Counter, defaultdict

# ── 價目表（US$／每百萬 token）──
# gpt-4o-mini：OpenAI 官方價目頁，輸入 0.15／輸出 0.60（與 eval/README.md 回信判讀評測相同，2026-09 核對）。
# gemini-2.5-flash：Google AI 付費層價目（2025-06 正式版公告），文字輸入 0.30／輸出 2.50（輸出含 thinking）；
#   免費額度內實際不收費。2026-10-09 撰寫時未再上網核對，上線前請以官方價目頁為準，可用 --prices 覆寫。
PRICES_AS_OF = "gpt-4o-mini：2026-09 核對；gemini-2.5-flash：依 2025-06 官方公告（未再核對）"
DEFAULT_PRICES = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
}

PROPOSAL_AGENTS = ("affiliation", "correction", "title")
HUMAN_STEPS = {"staff_review", "staff_mark", "human_decision"}
HUMAN_DECISIONS = {"suggest", "draft", "pending_human", "reference_only"}

# 每個 agent 的完成（terminal）步驟與有效產出條件（文件化在 README）
TASK_RULES = {
    "mailbox": "完成＝有 done 步驟；有效產出＝analyze 不是錯誤且類別不是 unknown（模型或沿用附件4 判讀）",
    "affiliation": "完成＝有 propose 步驟；有效產出＝建議有對應單位（suggestion 非空，含規則比對）",
    "correction": "完成＝有 propose 步驟；有效產出＝建議有通過檢核的具體修正（suggestion 非空）",
    "title": "完成＝有 propose 步驟；有效產出＝建議有級別（suggestion 非空）",
    "assistant": "完成＝有 grounding_check、refused、out_of_scope 或 llm_error 步驟；有效產出＝grounding_check 通過、正確拒絕（refused）或說明規則問題不在範圍（out_of_scope）",
}


# 指標定義（網頁儀表板逐項顯示；與 eval/README.md「從軌跡算的指標」一致）
METRIC_DEFINITIONS = {
    "tasks": "任務數：一個 (agent, run_id, 追蹤鍵) 算一個任務——一封信、一個建議主題或一個問題；只有專員處理紀錄的不算。",
    "completion_rate": "完成率：走到該 agent 的結束步驟（見下方各 agent 定義）的任務比例。",
    "useful_output_rate": "有效產出率：產出可用結果（分類成功、有具體建議、回答通過程式檢查或正確拒絕）的任務比例。",
    "human_intervention_rate": "人工介入率：任一步驟標示需人工（human_required）或決策為草稿／建議／待專員的任務比例。",
    "llm_fallback_rate": "模型失敗→轉人工（中止率）：有呼叫模型的任務中，模型呼叫本身失敗（例外、逾時、格式或 schema 不符）而改由專員處理的比例。",
    "tool_success_rate": "工具成功率：非模型的工具步驟（讀信、比對、寄送、查詢等）沒有回報錯誤的比例。",
    "retries": "跨輪重試：同一主題前一輪模型出錯、之後的 run 又送一次模型的次數。",
    "repairs": "自我修正：同一輪內模型輸出未通過程式檢核、把原因回饋給模型重來的次數（信箱 agent 的逐字引用、"
               "提案型的候選／級別、問答助理的 grounding check；每次呼叫最多一次，docs/自我修正設計.md）。",
    "repair_success_rate": "修正成功率：有觸發自我修正的任務中，修正後通過檢核、不必轉人工的比例。",
    "latency": "延遲：模型呼叫步驟的平均／最大毫秒數（離線備援的延遲沒有參考價值）。",
    "tokens": "token：軌跡記錄的 prompt／completion token 合計；離線備援與假模型不回報用量。",
    "estimated_cost_usd": "估計成本：token × 價目表（美元／百萬 token）；只是估算，以供應商帳單為準，沒有價目的模型只列 token。",
}


def load_events(state_dir) -> list[dict]:
    from .agent_trace import load_trace
    return load_trace(state_dir)


def _d(event: dict) -> dict:
    return event.get("detail") or event


ATTEMPT_STEPS = ("validate", "replan", "draft_answer_check")   # 信箱 agent：每一次模型嘗試各一筆（自我修正、重新規劃、擬回信）


def _is_llm_step(d: dict, attempt_steps: bool = False) -> bool:
    agent, step = d.get("agent"), d.get("step")
    if agent == "mailbox":
        if attempt_steps:   # 有逐次嘗試的軌跡：以嘗試為準，analyze 只是彙整（避免重複計算 token 與錯誤）
            return step in ATTEMPT_STEPS
        return step == "analyze" and d.get("tool") == "llm.complete_json"
    if agent in PROPOSAL_AGENTS:
        return step == "llm_call"
    if agent == "assistant":
        return step in ("tool_call", "grounding_check", "llm_error") or (
            step == "refused" and d.get("decision") == "action_request:model")
    return step == "llm_call"


def _llm_error(d: dict) -> str:
    """模型呼叫本身失敗（例外、格式、schema）的錯誤訊息；成功回空字串。"""
    out = d.get("output")
    if d.get("agent") == "assistant":
        if d.get("step") == "llm_error":
            return str((out or {}).get("error") or "error")
        if d.get("step") == "grounding_check":
            problems = (out or {}).get("problems") or []
            fmt = [p for p in problems if "模型輸出格式不符" in str(p)]
            return str(fmt[0]) if fmt else ""
        return ""
    if isinstance(out, dict) and out.get("error"):
        return str(out["error"])
    if d.get("decision") == "fail" and isinstance(out, dict) and out.get("problems"):
        return "未通過檢核：" + "；".join(map(str, out["problems"]))[:200]   # 修正次數用完仍不合格
    return ""


def _tool_error(d: dict) -> str:
    out = d.get("output")
    if isinstance(out, dict):
        if out.get("error"):
            return str(out["error"])
        if out.get("status") == "failed":
            return str(out.get("result") or "failed")
    return ""


def categorize_error(message: str) -> str:
    m = str(message or "")
    low = m.lower()
    if "timeout" in low or "timed out" in low or "逾時" in m:
        return "逾時"
    if "json" in low:
        return "輸出不是 JSON"
    if "逐字" in m:
        return "引用不是逐字（驗證擋下）"
    if "不在候選" in m or "未知的類別" in m or "未知級別" in m or "格式不符" in m or "缺少" in m \
            or "必須" in m or "檢核" in m or "不允許" in m:
        return "輸出不符 schema／白名單（驗證擋下）"
    if "連線" in m or "api" in low or "呼叫失敗" in m or "connection" in low or "rate" in low:
        return "API／連線錯誤"
    if "寄送" in m or "failed" in low:
        return "工具執行失敗"
    return "其他"


def estimate_cost(model: str | None, usage: dict | None, prices: dict) -> float | None:
    if not usage or not model:
        return None
    price = prices.get(model)
    if price is None:
        return None
    pin = usage.get("prompt_tokens") or 0
    pout = usage.get("completion_tokens") or 0
    return (pin * price["input"] + pout * price["output"]) / 1_000_000


def _useful(agent: str, steps: list[dict]) -> bool:
    if agent == "mailbox":
        analyze = next((d for d in steps if d.get("step") == "analyze"), None)
        out = (analyze or {}).get("output") or {}
        return bool(analyze) and not out.get("error") and out.get("category") not in (None, "unknown") \
            and analyze.get("tool") not in ("skipped_no_llm", "skipped_empty")
    if agent in PROPOSAL_AGENTS:
        propose = next((d for d in steps if d.get("step") == "propose"), None)
        return bool(propose) and (propose.get("output") or {}).get("suggestion") not in (None, "", {})
    if agent == "assistant":
        return any((d.get("step") == "grounding_check" and d.get("decision") == "pass") or d.get("step") in ("refused", "out_of_scope")
                   for d in steps)
    return False


def _completed(agent: str, steps: list[dict]) -> bool:
    names = {d.get("step") for d in steps}
    if agent == "mailbox":
        return "done" in names
    if agent in PROPOSAL_AGENTS:
        return "propose" in names
    if agent == "assistant":
        return bool(names & {"grounding_check", "refused", "out_of_scope", "llm_error"})
    return bool(steps)


def compute(events: list[dict], prices: dict | None = None) -> dict:
    """events：load_trace() 的輸出（可合併多個 state）。回傳 {agent: 指標}。"""
    prices = DEFAULT_PRICES if prices is None else prices
    tasks: dict[tuple, list[dict]] = defaultdict(list)
    for event in events:
        d = _d(event)
        key = event.get("tracking_key") or []
        tasks[(d.get("agent"), d.get("run_id"), tuple(key) if isinstance(key, list) else (key,))].append(d)

    per_agent: dict[str, dict] = {}
    llm_attempts: dict[tuple, list[tuple[str, bool]]] = defaultdict(list)   # (agent, key) -> [(run_id, error?)]
    # 依第一筆事件的先後（load_trace 已依 sequence 排序），重試判定才會是時間順序
    for (agent, run_id, key), steps in tasks.items():
        m = per_agent.setdefault(agent, {
            "tasks": 0, "completed": 0, "useful": 0, "human_intervention": 0, "llm_tasks": 0,
            "llm_fallback": 0, "grounding_fallback": 0, "llm_calls": 0, "llm_errors": 0,
            "tool_calls": 0, "tool_errors": 0, "latencies": [], "task_latencies": [],
            "prompt_tokens": 0, "completion_tokens": 0, "calls_with_usage": 0, "cost_usd": 0.0,
            "uncosted_calls": 0, "models": Counter(), "prompt_versions": Counter(), "errors": Counter(),
            "decisions": Counter(), "human_decisions": 0, "repairs": 0, "repaired_tasks": 0,
            "repaired_ok": 0,
        })
        system_steps = [d for d in steps if d.get("step") not in HUMAN_STEPS]
        if not system_steps:
            continue   # 只有專員處理的紀錄（例如舊的 run）不算一個新任務
        m["tasks"] += 1
        m["completed"] += _completed(agent, system_steps)
        m["useful"] += _useful(agent, system_steps)
        m["human_decisions"] += sum(1 for d in steps if d.get("step") in HUMAN_STEPS)
        if any(d.get("human_required") or d.get("decision") in HUMAN_DECISIONS for d in system_steps):
            m["human_intervention"] += 1
        task_llm_error = False
        task_has_llm = False
        attempt_steps = agent == "mailbox" and any(d.get("step") in ATTEMPT_STEPS for d in system_steps)
        task_repairs = sum(1 for d in system_steps if d.get("decision") == "repair" and d.get("step") != "reflect")
        for d in system_steps:
            if d.get("decision"):
                m["decisions"][str(d["decision"])] += 1
            if agent == "mailbox" and attempt_steps and d.get("step") == "analyze":
                continue   # 彙整步驟：已由逐次嘗試計入
            if _is_llm_step(d, attempt_steps):
                task_has_llm = True
                m["llm_calls"] += 1
                if d.get("model"):
                    m["models"][str(d["model"])] += 1
                if d.get("prompt_version"):
                    m["prompt_versions"][str(d["prompt_version"])] += 1
                if d.get("latency_ms") is not None:
                    m["latencies"].append(int(d["latency_ms"]))
                usage = d.get("usage")
                if usage:
                    m["calls_with_usage"] += 1
                    m["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                    m["completion_tokens"] += int(usage.get("completion_tokens") or 0)
                    cost = estimate_cost(d.get("model"), usage, prices)
                    if cost is None:
                        m["uncosted_calls"] += 1
                    else:
                        m["cost_usd"] += cost
                err = _llm_error(d)
                if err:
                    m["llm_errors"] += 1
                    m["errors"][categorize_error(err)] += 1
                    # 重新規劃／擬回信沒成功只是「那部分仍交專員」，第一次分析已成功，不算整件轉人工
                    task_llm_error = task_llm_error or d.get("step") not in ("replan", "draft_answer_check")
                if agent == "assistant" and d.get("step") == "tool_call":
                    # 助理的 tool_call 步驟同時是一次模型決策與一次唯讀工具呼叫
                    m["tool_calls"] += 1
                    terr = _tool_error(d)
                    if terr:
                        m["tool_errors"] += 1
                        m["errors"]["工具參數／未知工具"] += 1
            elif d.get("tool"):
                m["tool_calls"] += 1
                terr = _tool_error(d)
                if terr:
                    m["tool_errors"] += 1
                    m["errors"][categorize_error(terr)] += 1
            if agent == "assistant" and d.get("step") == "grounding_check" and d.get("decision") == "fail" \
                    and not _llm_error(d):
                m["grounding_fallback"] += 1
                m["errors"]["回答未通過程式檢查（改顯示原始資料）"] += 1
        task_ms = sum(int(d["latency_ms"]) for d in system_steps if d.get("latency_ms") is not None)
        m["task_latencies"].append(task_ms)
        if task_repairs:
            m["repairs"] += task_repairs
            m["repaired_tasks"] += 1
            # 修正成功＝修正後通過檢核、沒有轉人工（正確答案可以是「沒有對應」）；助理則要回答通過 grounding check
            m["repaired_ok"] += (not task_llm_error) and (agent != "assistant" or _useful(agent, system_steps))
        if task_has_llm:
            m["llm_tasks"] += 1
            m["llm_fallback"] += task_llm_error
            llm_attempts[(agent, key)].append((run_id, task_llm_error))

    # 重試：同一主題前一輪模型出錯、之後的 run 又送了一次模型（系統沒有同輪自動重試）
    retries = Counter()
    for (agent, _key), attempts in llm_attempts.items():
        for i in range(1, len(attempts)):
            if attempts[i - 1][1]:
                retries[agent] += 1

    out = {}
    for agent, m in sorted(per_agent.items(), key=lambda kv: str(kv[0])):
        lat, tlat = m.pop("latencies"), m.pop("task_latencies")
        tasks_n = m["tasks"] or 0
        rate = lambda n, d: (n / d) if d else None  # noqa: E731
        out[agent] = {
            "tasks": tasks_n,
            "completion_rate": rate(m["completed"], tasks_n),
            "useful_output_rate": rate(m["useful"], tasks_n),
            "human_intervention_rate": rate(m["human_intervention"], tasks_n),
            "llm_tasks": m["llm_tasks"],
            "llm_fallback_rate": rate(m["llm_fallback"], m["llm_tasks"]),
            "grounding_fallback": m["grounding_fallback"],
            "llm_calls": m["llm_calls"],
            "llm_errors": m["llm_errors"],
            "tool_calls": m["tool_calls"],
            "tool_errors": m["tool_errors"],
            "tool_success_rate": rate(m["tool_calls"] - m["tool_errors"], m["tool_calls"]),
            "retries": retries.get(agent, 0),
            "repairs": m["repairs"],
            "repaired_tasks": m["repaired_tasks"],
            "repair_success_rate": rate(m["repaired_ok"], m["repaired_tasks"]),
            "llm_latency_ms_mean": (sum(lat) / len(lat)) if lat else None,
            "llm_latency_ms_max": max(lat) if lat else None,
            "task_latency_ms_mean": (sum(tlat) / len(tlat)) if tlat else None,
            "task_latency_ms_max": max(tlat) if tlat else None,
            "prompt_tokens": m["prompt_tokens"],
            "completion_tokens": m["completion_tokens"],
            "calls_with_usage": m["calls_with_usage"],
            "estimated_cost_usd": round(m["cost_usd"], 6) if m["calls_with_usage"] else None,
            "uncosted_calls": m["uncosted_calls"],
            "models": dict(m["models"]),
            "prompt_versions": dict(m["prompt_versions"]),
            "decisions": dict(m["decisions"]),
            "human_decisions": m["human_decisions"],
            "error_categories": dict(m["errors"]),
            "task_rule": TASK_RULES.get(agent, ""),
        }
    return out


def _pct(value) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _num(value, digits=0) -> str:
    if value is None:
        return "—"
    return f"{value:,.{digits}f}"


def markdown(metrics: dict, *, title: str = "Agent 軌跡指標") -> str:
    lines = [f"### {title}", "",
             "| agent | 任務數 | 完成率 | 有效產出率 | 人工介入率 | 模型失敗→轉人工 | 工具成功率 | 重試 | 自我修正（成功率） | 模型呼叫 | 平均／最大模型延遲 ms | token（入／出） | 估計成本 US$ |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for agent, m in metrics.items():
        cost = "—" if m["estimated_cost_usd"] is None else f"{m['estimated_cost_usd']:.6f}"
        if m["uncosted_calls"]:
            cost += f"（{m['uncosted_calls']} 次無價目）"
        tokens = "—" if not m["calls_with_usage"] else f"{m['prompt_tokens']:,}／{m['completion_tokens']:,}"
        fallback = "—" if m["llm_fallback_rate"] is None else f"{_pct(m['llm_fallback_rate'])}（{m['llm_errors']}/{m['llm_calls']}）"
        lines.append(
            f"| {agent} | {m['tasks']} | {_pct(m['completion_rate'])} | {_pct(m['useful_output_rate'])} | "
            f"{_pct(m['human_intervention_rate'])} | {fallback} | "
            f"{_pct(m['tool_success_rate'])}（{m['tool_calls'] - m['tool_errors']}/{m['tool_calls']}） | {m['retries']} | "
            f"{m['repairs']}（{_pct(m['repair_success_rate'])}） | "
            f"{m['llm_calls']} | {_num(m['llm_latency_ms_mean'], 1)}／{_num(m['llm_latency_ms_max'])} | {tokens} | {cost} |")
    errors = {a: m["error_categories"] for a, m in metrics.items() if m["error_categories"]}
    if errors:
        lines += ["", "錯誤分類：", ""]
        for agent, cats in errors.items():
            lines.append(f"- {agent}：" + "、".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])))
    versions = {a: (m["models"], m["prompt_versions"]) for a, m in metrics.items() if m["prompt_versions"] or m["models"]}
    if versions:
        lines += ["", "模型與 prompt 版本（依軌跡）：", ""]
        for agent, (models, pv) in versions.items():
            lines.append(f"- {agent}：模型 {', '.join(models) or '—'}；prompt {', '.join(pv) or '—'}")
    return "\n".join(lines)


# 軌跡查核頁（koushi/agent_runs.py）逐筆判斷用的公開名稱
is_llm_step = _is_llm_step
llm_error = _llm_error
tool_error = _tool_error
is_completed = _completed
is_useful = _useful
