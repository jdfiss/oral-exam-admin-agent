# -*- coding: utf-8 -*-
"""Agent A：委員服務單位 → 距離表對應（決定交通費，高風險）。

流程（每個查不到距離的服務單位一次）：
1. 已確認對照表有這個寫法 → 建檔時直接查表，不會出現在這裡、不呼叫模型。
2. 確定性候選檢索（tool：distance_table.search）：臺／台、國立／私立、學系／系、
   台大／NTU 等縮寫種子表正規化後比對，算出候選與相似度。
3. 正規化後恰好一筆完全相同 → 規則提案，不呼叫模型。
4. 其餘才請模型在候選中排序，回 {match_id 或 null, confidence, rationale}；
   match_id 必須是候選之一，否則視為無效輸出。
5. 一律進佇列等專員確認／改判／不採用；確認後寫入版本化對照表，之後查表不再問模型。

未確認前交通費維持「查無距離」（NO_DISTANCE 例外），建議本身不會碰到金額。
送模型的只有服務單位字串與距離表單位名稱，沒有任何委員或學生個資。
"""

from __future__ import annotations

import re
import unicodedata

from . import ai_proposals as P, util as U
from .agent_trace import trace_step

AGENT = "affiliation"
PROMPT_VERSION = "affiliation-v1"
MAX_CANDIDATES = 8

# 縮寫種子表：先比對較長的寫法。英文縮寫只在單字邊界比對。
_SEED_ALIASES = (
    ("national taiwan university of science and technology", "台灣科技大學"),
    ("national taiwan normal university", "台灣師範大學"),
    ("national taiwan university", "台灣大學"),
    ("national tsing hua university", "清華大學"),
    ("national cheng kung university", "成功大學"),
    ("national yang ming chiao tung university", "交通大學"),
    ("national chiao tung university", "交通大學"),
    ("national chengchi university", "政治大學"),
    ("national sun yat-sen university", "中山大學"),
    ("national chung hsing university", "中興大學"),
    ("national chung cheng university", "中正大學"),
    ("academia sinica", "中央研究院"),
    ("computer science and information engineering", "資訊工程"),
    ("electrical engineering", "電機工程"),
)
_SEED_ABBR = {
    "ntust": "台灣科技大學", "ntnu": "台灣師範大學", "ntu": "台灣大學", "nthu": "清華大學",
    "ncku": "成功大學", "nycu": "交通大學", "nctu": "交通大學", "nccu": "政治大學",
    "nsysu": "中山大學", "nchu": "中興大學", "ccu": "中正大學", "csie": "資訊工程", "ee": "電機工程",
}
_SEED_ZH = (
    ("陽明交通大學", "交通大學"), ("台科大", "台灣科技大學"), ("台師大", "台灣師範大學"), ("台大", "台灣大學"), ("清大", "清華大學"),
    ("成大", "成功大學"), ("交大", "交通大學"), ("政大", "政治大學"), ("師大", "台灣師範大學"),
    ("中研院", "中央研究院"), ("資工", "資訊工程"), ("電機系", "電機工程系"),
)
_KNOWN_SCHOOLS = sorted({v for _, v in (*_SEED_ALIASES, *_SEED_ABBR.items(), *_SEED_ZH)
                         if v.endswith(("大學", "研究院"))}, key=len, reverse=True)
_SCHOOL_RE = re.compile(r"(.+?(?:大學|研究院|學院))")

SYSTEM_PROMPT = (
    "你是口試事務系統的單位對應助手。任務：判斷一個口試委員『服務單位』的寫法，"
    "對應到距離表候選清單中的哪一筆（同一個學校與系所）。只能從候選 id 中選，"
    "沒有同一單位就回 null；不同系所、不同校區不算同一單位。你的答案只是建議，"
    "會由專員確認後才影響交通費。只輸出 JSON："
    '{"match_id": "D1" 或 null, "confidence": 0 到 1 的數字, "rationale": "繁體中文一兩句理由"}'
)


def canonical(unit: str) -> str:
    s = unicodedata.normalize("NFKC", str(unit or "")).lower()
    s = re.sub(r"\b(department|dept\.?|school|college|institute)\s+of\b", " ", s)
    for long, short in _SEED_ALIASES:
        s = s.replace(long, short)
    s = re.sub(r"[a-z]+", lambda m: _SEED_ABBR.get(m.group(0), m.group(0)), s)
    s = s.replace("臺", "台")
    for long, short in _SEED_ZH:
        s = s.replace(long, short)
    s = re.sub(r"[\s　,，、.()（）\-_/／&]+", "", s)
    for word in ("國立", "私立", "財團法人"):
        s = s.replace(word, "")
    s = s.replace("學系", "系").replace("研究所", "所")
    for school in _KNOWN_SCHOOLS:  # 英文寫法常是「系, 校」順序，校名移到最前面
        if school in s and not s.startswith(school):
            s = school + s.replace(school, "", 1)
            break
    return s.rstrip("系所")


def _school(key: str) -> str:
    m = _SCHOOL_RE.match(key)
    return m.group(1) if m else ""


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def search(unit: str, distance_units) -> list[dict]:
    """確定性候選檢索；回傳依分數排序的候選（含理由），不呼叫模型。"""
    key = canonical(unit)
    school = _school(key)
    ranked = []
    for name in distance_units:
        other = canonical(name)
        if other == key:
            score, why = 1.0, "正規化後完全相同"
        else:
            a, b = _bigrams(key), _bigrams(other)
            sim = len(a & b) / len(a | b) if a and b else 0.0
            same_school = bool(school) and school == _school(other)
            score = round((0.5 + 0.5 * sim) if same_school else 0.6 * sim, 3)
            why = (f"同校（{school}）；" if same_school else "") + f"字元相似度 {sim:.2f}"
        ranked.append({"name": name, "canonical": other, "score": score, "why": why})
    ranked.sort(key=lambda c: (-c["score"], c["name"]))
    return ranked[:MAX_CANDIDATES]


def unresolved_units(master, distance_table) -> list[str]:
    """主檔中交通費查不到距離的校外服務單位（依正規化寫法去重）。"""
    seen: dict[str, str] = {}
    for exam in master.exams:
        if not exam.生效:
            continue
        for member in exam.委員:
            committee = master.committees.get(member.committee_id)
            unit = member.服務單位 or (committee.服務單位 if committee else "")
            if not unit or member.交通費 is not None or U.is_fee_exempt_unit(unit):
                continue
            if distance_table.resolve(unit) in distance_table.rows:
                continue  # 已有確認對照，下次建檔就會算出金額
            seen.setdefault(P.alias_key(unit), str(unit).strip())
    return list(seen.values())


def _check_match(data: dict, ids: dict) -> None:
    match = data.get("match_id")
    if match not in (None, "", "null") and match not in ids:
        raise ValueError(f"match_id「{match}」不在候選內；只能填候選清單的 id（{'、'.join(ids)}），都不符合就填 null")


def run(master, state_dir, *, distance_table, llm=None, run_id: str | None = None) -> list[dict]:
    """為查不到距離的單位產生建議（已建議過的不重問）；回傳本輪新增的建議。"""
    if not P.load_settings(state_dir)[AGENT]:
        return []
    run_id = run_id or P.new_run_id()
    aliases = P.alias_map(state_dir)
    units = list(distance_table.rows)
    created = []
    for unit in unresolved_units(master, distance_table):
        key = P.alias_key(unit)
        subject_key = f"affiliation:{key}"
        if key in aliases or P.existing(state_dir, AGENT, subject_key, model_available=llm is not None):
            continue
        candidates = search(unit, units)
        trace_step(state_dir, agent=AGENT, run_id=run_id, step="retrieve_candidates", tracking_key=subject_key,
                   tool="distance_table.search", input_summary=f"服務單位「{unit}」；距離表 {len(units)} 筆",
                   output=[{"name": c["name"], "score": c["score"]} for c in candidates])
        exact = [c for c in candidates if c["score"] == 1.0]
        proposal = {"agent": AGENT, "subject_key": subject_key, "subject": unit, "run_id": run_id,
                    "candidates": candidates, "suggestion": None, "confidence": None, "rationale": "",
                    "method": "none", "model": None, "prompt_version": None, "error": ""}
        if len(exact) == 1:
            proposal.update(suggestion=exact[0]["name"], method="rule",  # 規則比對不給信心分數
                            rationale=f"規則比對：{exact[0]['why']}（臺台、國立、學系等寫法差異）")
        elif not candidates:
            proposal.update(rationale="距離表沒有任何單位，請先上傳距離表")
        elif llm is None:
            proposal.update(rationale="未設定模型；請從候選中手動指定或補距離表")
        else:
            ids = {f"D{i}": c["name"] for i, c in enumerate(candidates, 1)}
            user = P.scrub(P.dumps({"服務單位": unit, "候選": [{"id": i, "單位": n} for i, n in ids.items()]}))
            data, error = P.call_llm(llm, state_dir, agent=AGENT, run_id=run_id, tracking_key=subject_key,
                                     system=SYSTEM_PROMPT, user=user, schema_name="affiliation_match", required=("match_id",),
                                     check=lambda d: _check_match(d, ids),
                                     prompt_version=PROMPT_VERSION, input_summary=f"服務單位＋{len(ids)} 個候選")
            proposal.update(method="llm", model=getattr(llm, "model", None) or type(llm).__name__,
                            prompt_version=PROMPT_VERSION)
            if data is not None:
                match = data.get("match_id")
                if match not in (None, "", "null") and match not in ids:
                    error = f"模型回傳不在候選內的 id：{match}"
                else:
                    proposal.update(suggestion=ids.get(match) if match else None,
                                    confidence=P.confidence(data.get("confidence")),
                                    rationale=str(data.get("rationale") or "")[:500])
            if error:
                proposal.update(error=error, rationale="模型呼叫失敗，維持未對應，請專員手動處理")
        saved = P.add(state_dir, proposal)
        trace_step(state_dir, agent=AGENT, run_id=run_id, step="propose", tracking_key=subject_key,
                   tool="ai_proposals.add", output={"id": saved["id"], "suggestion": saved["suggestion"],
                                                    "confidence": saved["confidence"], "method": saved["method"]},
                   decision="pending_human", human_required=True)
        created.append(saved)
    return created


def decide(state_dir, proposal_id: str, *, action: str, actor: str, target: str = "", note: str = "",
           distance_units=()) -> dict:
    """專員決定：approve＝採用建議；override＝改指定距離表單位；reject＝不對應（維持查無距離）。"""
    proposal = P.get(state_dir, proposal_id)
    if proposal is None or proposal["agent"] != AGENT:
        raise ValueError("找不到這筆服務單位建議")
    if not actor.strip():
        raise ValueError("請填寫處理人姓名")
    if action == "reject":
        return P.mark_decided(state_dir, proposal_id, status=P.REJECTED, actor=actor,
                              decision={"target": None}, note=note)
    chosen = proposal["suggestion"] if action == "approve" else target.strip()
    if not chosen:
        raise ValueError("這筆建議沒有可採用的對應，請改指定距離表單位或選不採用")
    units = {U.norm_unit(u): u for u in distance_units}
    if U.norm_unit(chosen) not in units:
        raise ValueError("指定的單位不在目前距離表中")
    status = P.APPROVED if action == "approve" or U.norm_unit(chosen) == U.norm_unit(proposal["suggestion"] or "") \
        else P.OVERRIDDEN
    with U.state_lock(state_dir):
        P.set_alias(state_dir, unit=proposal["subject"], target=chosen, actor=actor,
                    source="ai_approved" if status == P.APPROVED else "staff_override",
                    proposal_id=proposal_id, reason=note)
        return P.mark_decided(state_dir, proposal_id, status=status, actor=actor,
                              decision={"target": U.norm_unit(chosen)}, note=note)
