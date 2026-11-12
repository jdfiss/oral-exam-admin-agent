# -*- coding: utf-8 -*-
"""Agent 層級指標的命令列入口（薄包裝）。

計算邏輯在 koushi/agent_metrics.py（網頁「Agent 指標」頁也用同一份），這裡只負責
讀一或多個 STATE_DIR、套自訂價目、輸出 Markdown 或 JSON。

用法：
    .venv\\Scripts\\python.exe eval\\agent_metrics.py <STATE_DIR> [--prices prices.json] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR.parent))

from koushi.agent_metrics import (  # noqa: E402,F401 — 對外沿用 eval.agent_metrics 的名稱
    DEFAULT_PRICES, HUMAN_DECISIONS, HUMAN_STEPS, METRIC_DEFINITIONS, PRICES_AS_OF, PROPOSAL_AGENTS, TASK_RULES,
    categorize_error, compute, estimate_cost, load_events, markdown,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="從 audit_log.json 的 agent_trace 算 agent 層級指標")
    parser.add_argument("state_dir", nargs="+", help="一或多個 STATE_DIR（內含 audit_log.json）")
    parser.add_argument("--prices", help="自訂價目 JSON：{model: {input, output}}（US$／百萬 token）")
    parser.add_argument("--json", action="store_true", help="輸出 JSON 而非 Markdown")
    args = parser.parse_args(argv)
    prices = dict(DEFAULT_PRICES)
    if args.prices:
        prices.update(json.loads(Path(args.prices).read_text(encoding="utf-8")))
    events = [e for sd in args.state_dir for e in load_events(sd)]
    metrics = compute(events, prices)
    if args.json:
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    else:
        print(markdown(metrics))
        print(f"\n價目：{PRICES_AS_OF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
