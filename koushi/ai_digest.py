# -*- coding: utf-8 -*-
"""工作總覽與案件頁的 AI 摘要（唯讀）：把信箱 agent、提案型 agent 與軌跡整理成主畫面看得懂的數字。

不呼叫模型、不寫任何狀態。資料來源：
- 信箱 agent 紀錄（mailbox_agent.json）
- AI 建議佇列（ai_proposals.json）
- agent 軌跡（audit_log.json，source＝agent_trace）：只用來數「自我修正」

「今日」以系統時鐘（clock.now()，展示環境為模擬日期）為準；紀錄的時間戳記也都用同一個時鐘。
顯示前一律過 agent_runs.sanitize（遮 8 碼以上數字與電子郵件帳號）。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import ai_proposals as P, mailbox_agent as MA
from .agent_runs import AGENT_LABELS, sanitize, task_id
from .agent_trace import load_trace


def _today_str(today) -> str:
    return today.isoformat() if hasattr(today, "isoformat") else str(today)


def _repaired_tasks(state_dir, day: str) -> int:
    """今天有幾個任務做過自我修正（輸出未通過檢核→回饋重來、重新規劃、回答反思）。"""
    tasks = set()
    for event in load_trace(state_dir):
        if not str(event.get("at", "")).startswith(day):
            continue
        d = event.get("detail") or {}
        if d.get("decision") == "repair" or (d.get("step") == "replan" and d.get("decision") == "pass"):
            tasks.add((d.get("agent"), d.get("run_id"), tuple(event.get("tracking_key") or ())))
    return len(tasks)


def _resolved(p: dict, master, state_dir) -> bool:
    """來源異常修正建議對應的例外已在案件放行時由專員確認過：不再算待決定。"""
    if master is None or p.get("agent") != "correction" or not p.get("issue"):
        return False
    from .correction_agent import already_resolved
    return already_resolved(master, state_dir, {**p["issue"], "案件鍵": p.get("case_key")})


def activity(state_dir, *, today, master=None) -> dict:
    """工作總覽「AI 今日動態／待你決定」：今天 agent 做了什麼，以及目前等專員決定的事。"""
    sd = Path(state_dir)
    day = _today_str(today)
    mails = MA.load_mails(sd)
    proposals = P.load(sd)
    mails_today = [m for m in mails if str(m.get("processed_at", "")).startswith(day)]
    proposals_today = [p for p in proposals if str(p.get("created_at", "")).startswith(day)]
    categories = Counter((m.get("analysis") or {}).get("category", MA.UNKNOWN) for m in mails_today)
    by_agent_today = Counter(p["agent"] for p in proposals_today)
    pending = [p for p in proposals if p["status"] == P.PENDING and not _resolved(p, master, sd)]
    by_agent_pending = Counter(p["agent"] for p in pending)
    counts = MA.counts(sd)
    today_data = {
        "mails": len(mails_today),
        "categories": [(MA.CATEGORY_LABELS.get(c, c), n) for c, n in categories.most_common()],
        "auto_done": sum(1 for m in mails_today if m.get("status") == MA.MAIL_AUTO_DONE),
        "proposals": len(proposals_today),
        "proposals_by_agent": [(P.AGENTS[a], n) for a, n in by_agent_today.items() if a in P.AGENTS],
        "repairs": _repaired_tasks(sd, day),
        "failures": sum(1 for m in mails_today if m.get("llm_error"))
                    + sum(1 for p in proposals_today if p.get("error")),
    }
    pending_data = {
        "mails": counts.get(MA.MAIL_AWAITING, 0),
        "drafts": counts.get("drafts", 0),
        "proposals": len(pending),
        "proposals_by_agent": [(P.AGENTS[a], n) for a, n in by_agent_pending.items() if a in P.AGENTS],
        "proposal_errors": sum(1 for p in pending if p.get("error")),
    }
    return {
        "day": day, "today": today_data, "pending": pending_data,
        "has_activity": bool(today_data["mails"] or today_data["proposals"] or today_data["repairs"]),
        "has_pending": bool(pending_data["mails"] or pending_data["drafts"] or pending_data["proposals"]),
    }


def _member_keys(exam, committees) -> tuple[set[str], set[str]]:
    """本案委員的服務單位鍵與職稱鍵（對應服務單位、職稱建議的 subject_key）。"""
    units, titles = set(), set()
    for member in exam.委員:
        committee = committees.get(member.committee_id)
        unit = member.服務單位 or (committee.服務單位 if committee else "")
        if unit:
            units.add(P.alias_key(unit))
        if member.職稱:
            titles.add(P.title_key(member.職稱))
    return units, titles


def _proposal_view(p: dict) -> dict:
    suggestion = p.get("suggestion")
    if isinstance(suggestion, dict):   # 來源異常修正：{label, old, new, ...}
        suggestion = f"{suggestion.get('label', '')}：「{suggestion.get('old', '')}」→「{suggestion.get('new', '')}」"
    view = sanitize({
        "agent": p["agent"], "agent_label": P.AGENTS.get(p["agent"], p["agent"]),
        "subject": (f"{p['issue'].get('問題代碼', '')}：{p['issue'].get('說明', '')}"
                    if p["agent"] == "correction" and p.get("issue") else p.get("subject", "")),
        "suggestion": suggestion or "", "confidence": p.get("confidence"), "method": p.get("method"),
        "error": bool(p.get("error")), "created_at": p.get("created_at", ""),
    })
    # 代號是十六進位字串，可能含連續數字；不能過遮蔽，否則連結會壞
    view.update(id=p["id"], task_id=task_id(p["agent"], p.get("run_id", ""), [p["subject_key"]]))
    return view


def _mail_view(m: dict) -> dict:
    analysis = m.get("analysis") or {}
    view = sanitize({
        "processed_at": m.get("processed_at", ""),
        "role": MA.ROLE_LABELS.get((m.get("sender") or {}).get("role"), "不明"),
        "category": MA.CATEGORY_LABELS.get(analysis.get("category"), "無法判定"),
        "summary": analysis.get("summary", ""), "suspicious": bool(analysis.get("suspicious")),
        "status": m.get("status", MA.MAIL_AWAITING),
        "status_label": MA.MAIL_STATUS_LABELS.get(m.get("status"), "待專員處理"),
        "llm_error": bool(m.get("llm_error")),
        "actions": [{"label": a.get("label") or MA.ACTION_LABELS.get(a["type"], a["type"]),
                     "policy": a["policy"], "policy_label": MA.POLICY_LABELS.get(a["policy"], a["policy"]),
                     "status": a.get("status"), "status_label": MA.ACT_STATUS_LABELS.get(a.get("status"), "")}
                    for a in m.get("actions", []) if a["type"] != MA.A_NO_ACTION],
    })
    # 只用來組連結（信箱頁錨點與信箱頁相同、軌跡代號是雜湊），不顯示；不能過遮蔽，否則連結會壞
    view.update(message_id=m["message_id"], task_id=task_id(MA.AGENT, m.get("run_id", ""), [m["message_id"]]))
    return view


def case_panel(state_dir, case_key: str, master=None) -> dict:
    """案件頁「AI 協作」：與本案相關的信箱 agent 紀錄與待決 AI 建議。

    信件只取寄件者身分或附件4 代碼已驗證連到本案的（link.case_keys）；信中提到學號但寄件者
    不明的「未驗證線索」不列，避免把別人的信掛到這個案件上。
    """
    sd = Path(state_dir)
    mails = [_mail_view(m) for m in reversed(MA.load_mails(sd))
             if case_key in ((m.get("link") or {}).get("case_keys") or [])]
    exam = master.by_key().get(case_key) if master is not None else None
    units, titles = _member_keys(exam, master.committees) if exam is not None else (set(), set())
    related = []
    for p in P.pending(sd):
        if _resolved(p, master, sd):
            continue
        subject_key = p.get("subject_key", "")
        if p["agent"] == "correction":
            hit = p.get("case_key") == case_key
        elif p["agent"] == "affiliation":
            hit = subject_key.split(":", 1)[-1] in units
        elif p["agent"] == "title":
            hit = subject_key.split(":", 1)[-1] in titles
        else:
            hit = False
        if hit:
            related.append(_proposal_view(p))
    return {"mails": mails, "proposals": related, "agent_labels": AGENT_LABELS}
