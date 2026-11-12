# -*- coding: utf-8 -*-
"""Agent C：委員職稱輔助判讀（只給專員參考）。

確定性規則（validation._title_rank）判斷不了的職稱——兼任、客座、英文、研究機構、
業界職稱——才送模型，回 {level, confidence, rationale}。建議只顯示在 AI 建議頁，
**不改變**任何檢核結果（COMMITTEE_RANK_EXCEPTION 照舊列出）、不放行案件；
委員資格的最終判定依決策第四節第 4、5 條由專員在人工關卡決定。

專員確認的「職稱 → 級別」存進 title_levels.json 作快取：同一職稱之後不再問模型。
送模型的只有職稱字串，不含姓名、服務單位或任何個資。
"""

from __future__ import annotations

from . import ai_proposals as P, util as U, validation
from .agent_trace import trace_step

AGENT = "title"
PROMPT_VERSION = "title-v1"
LEVELS = ("教授級", "副教授級", "助理教授級", "未達助理教授級", "無法判斷")

SYSTEM_PROMPT = (
    "你是大學口試事務的職稱判讀助手。依台灣學術職級，判斷一個口試委員職稱相當於哪一級："
    "教授級、副教授級、助理教授級、未達助理教授級、無法判斷。研究員／副研究員／助研究員分別比照"
    "教授／副教授／助理教授；兼任、客座、名譽等修飾詞請說明是否影響級別；業界職稱通常無法對應，"
    "除非職稱本身說明了學術職級。你的答案只供專員參考，不決定資格。只輸出 JSON："
    '{"level": "上述五個值之一", "confidence": 0 到 1 的數字, "rationale": "繁體中文一兩句理由"}'
)


def unclassified_titles(master) -> dict[str, dict]:
    """規則判斷不了、且附件2 委員資格沒有註明符合的職稱 → {職稱鍵: {title, count}}。"""
    out: dict[str, dict] = {}
    for exam in master.exams:
        if not exam.生效:
            continue
        for member in exam.委員:
            title = str(member.職稱 or "").strip()
            if not title or str(member.委員資格 or "").strip().startswith("符合"):
                continue
            rank, _reason = validation._title_rank(title)
            if rank is not None:
                continue
            entry = out.setdefault(P.title_key(title), {"title": title, "count": 0})
            entry["count"] += 1
    return out


def _check_level(data: dict) -> None:
    if data.get("level") not in LEVELS:
        raise ValueError(f"level「{data.get('level')}」不是允許的級別；只能是：{'、'.join(LEVELS)}")


def run(master, state_dir, *, llm=None, run_id: str | None = None) -> list[dict]:
    if not P.load_settings(state_dir)[AGENT]:
        return []
    run_id = run_id or P.new_run_id()
    cached = P.load_title_levels(state_dir).get("titles", {})
    created = []
    for key, entry in unclassified_titles(master).items():
        subject_key = f"title:{key}"
        if key in cached or P.existing(state_dir, AGENT, subject_key, model_available=llm is not None):
            continue
        title = entry["title"]
        _rank, rule_reason = validation._title_rank(title)
        trace_step(state_dir, agent=AGENT, run_id=run_id, step="rule_check", tracking_key=subject_key,
                   tool="validation._title_rank", input_summary=f"職稱「{title}」",
                   output={"rank": None, "reason": rule_reason})
        proposal = {"agent": AGENT, "subject_key": subject_key, "subject": title, "run_id": run_id,
                    "count": entry["count"], "rule_reason": rule_reason, "suggestion": None,
                    "confidence": None, "rationale": "", "method": "none", "model": None,
                    "prompt_version": None, "error": ""}
        if llm is None:
            proposal.update(rationale="未設定模型；請專員自行判斷")
        else:
            data, error = P.call_llm(llm, state_dir, agent=AGENT, run_id=run_id, tracking_key=subject_key,
                                     system=SYSTEM_PROMPT, user=P.scrub(P.dumps({"職稱": title})),
                                     schema_name="title_level", required=("level",), prompt_version=PROMPT_VERSION,
                                     check=_check_level,
                                     input_summary="職稱字串")
            proposal.update(method="llm", model=getattr(llm, "model", None) or type(llm).__name__,
                            prompt_version=PROMPT_VERSION)
            if data is not None and data.get("level") not in LEVELS:
                error = f"模型回傳未知級別：{data.get('level')}"
            if error:
                proposal.update(error=error, rationale="模型呼叫失敗，請專員自行判斷")
            else:
                proposal.update(suggestion=data["level"], confidence=P.confidence(data.get("confidence")),
                                rationale=str(data.get("rationale") or "")[:500])
        saved = P.add(state_dir, proposal)
        trace_step(state_dir, agent=AGENT, run_id=run_id, step="propose", tracking_key=subject_key,
                   tool="ai_proposals.add", output={"id": saved["id"], "suggestion": saved["suggestion"]},
                   decision="reference_only", human_required=True)
        created.append(saved)
    return created


def decide(state_dir, proposal_id: str, *, action: str, actor: str, level: str = "", note: str = "") -> dict:
    """確認＝把職稱級別存成對照快取（僅供參考）；不碰任何案件的資格狀態。"""
    proposal = P.get(state_dir, proposal_id)
    if proposal is None or proposal["agent"] != AGENT:
        raise ValueError("找不到這筆職稱建議")
    if not actor.strip():
        raise ValueError("請填寫處理人姓名")
    if action == "reject":
        return P.mark_decided(state_dir, proposal_id, status=P.REJECTED, actor=actor,
                              decision={"level": None}, note=note)
    chosen = proposal["suggestion"] if action == "approve" else level.strip()
    if chosen not in LEVELS:
        raise ValueError("請選擇職稱級別")
    status = P.APPROVED if chosen == proposal["suggestion"] else P.OVERRIDDEN
    with U.state_lock(state_dir):
        P.set_title_level(state_dir, title=proposal["subject"], level=chosen, actor=actor, proposal_id=proposal_id)
        return P.mark_decided(state_dir, proposal_id, status=status, actor=actor,
                              decision={"level": chosen}, note=note)
