# -*- coding: utf-8 -*-
"""Agent B：來源異常修正建議。

對附件2／學生填寫合併時產生的案件層異常（master.source_issues 中有案件鍵、責任為系辦者），
請模型提出一筆具體修正（欄位、原值、新值、理由），或說明「不需要／無法以修正層處理」。

安全規則：
- 送模型的只有：問題代碼、欄位、說明（學生姓名、學號、委員姓名換成代號，再過個資遮蔽）、
  案件的口試日期／時間／地點／方式／題目、委員代號＋服務單位＋職稱＋視訊出席。
  不送 Email、身分證、銀行、地址。
- 模型只能提可修正欄位（不含 Email、委員名單、指導教授信箱）；程式用專員修正層的
  同一套檢核驗證新值，原值由程式從主檔讀，不採信模型。
- **從不自動套用**：專員按「套用」才走既有專員修正層（記處理人、原因，寫稽核）。
"""

from __future__ import annotations

from . import ai_proposals as P, corrections, source_issues
from .agent_trace import trace_step
from .model import Committee

AGENT = "correction"
PROMPT_VERSION = "correction-v1"
EXAM_FIELDS = ("口試日期", "口試開始時間", "口試結束時間", "口試地點", "口試方式", "論文題目")
MEMBER_FIELDS = ("職稱", "服務單位", "視訊出席")

SYSTEM_PROMPT = (
    "你是口試事務系統的資料異常修正助手。系統比對 Portal 附件2 與學生填寫時發現一個異常。"
    "請根據異常說明與案件目前的值，提出最多一筆具體修正；只能改這些欄位：案件層 "
    + "、".join(EXAM_FIELDS) + "；委員層 " + "、".join(MEMBER_FIELDS) + "（委員以代號指定）。"
    "日期用 YYYY-MM-DD，口試方式只能是 實體／視訊／部分視訊，視訊出席用 true／false。"
    "如果異常不能靠修改這些欄位解決（例如需要系辦重新上傳檔案或確認身分），action 回 none 並說明建議的處理方式。"
    "你的建議會由專員確認後才套用。只輸出 JSON："
    '{"action": "correct" 或 "none", "field": 欄位名或 null, "member_ref": "委員1" 或 null, '
    '"new_value": 字串或 null, "confidence": 0 到 1 的數字, "rationale": "繁體中文一兩句理由"}'
)


def _member_name(member, committees) -> str:
    return member.委員姓名 or (committees.get(member.committee_id) or Committee()).委員姓名


def build_context(exam, issue, committees) -> tuple[dict, dict[str, str]]:
    """組出送模型的去識別內容；回傳 (內容, 委員代號 -> 案件委員關聯鍵)。"""
    refs: dict[str, str] = {}
    members = []
    message = str(issue.get("說明") or "")
    replacements = [(exam.姓名, "［學生］"), (exam.學號, "［學號］"), (exam.案件鍵, "［案件］")]
    for index, member in enumerate(exam.委員, 1):
        ref = f"委員{index}"
        refs[ref] = member.association_id or member.committee_id
        replacements.append((_member_name(member, committees), ref))
        members.append({"代號": ref, "服務單位": member.服務單位, "職稱": member.職稱,
                        "視訊出席": member.視訊出席})
    for raw, mask in sorted(replacements, key=lambda r: -len(str(r[0] or ""))):
        if raw:
            message = message.replace(str(raw), mask)
    context = {
        "異常": {"問題代碼": issue.get("問題代碼"), "欄位": issue.get("欄位"), "說明": message},
        "案件": {"學位別": exam.學位別, **{f: corrections.source_value(exam, committees, f) for f in EXAM_FIELDS}},
        "委員": members,
    }
    return context, refs


# 要人確認身分或處理行政事項、改欄位解決不了的問題：不送模型、不產生建議（否則只會出現「無建議」雜訊）
NOT_CORRECTABLE = frozenset({
    "COMMITTEE_MATCHED_BY_NAME", "UNVERIFIED_COMMITTEE_IDENTITY", "DUPLICATE_COMMITTEE_IDENTITY",
    "COMMITTEE_EMAIL_CONFLICT", "CASE_CANCELLED", "DUPLICATE_SUBMISSION", "SUBMISSION_PARSE_ERROR",
    "SUBMISSION_WITHOUT_PORTAL_CASE", "ADVISOR_SOURCE_UNAVAILABLE", "CASE_REVIEW_PENDING",
})


def candidate_issues(master) -> list[dict]:
    keys = master.by_key()
    return [i for i in master.source_issues
            if i.get("案件鍵") in keys and i.get("責任", "office") == "office"
            and i.get("問題代碼") not in NOT_CORRECTABLE]


def already_resolved(master, state_dir, issue) -> bool:
    """專員放行案件時已確認過這個例外（放行仍有效、例外當時就在），不必再請模型提修正建議。"""
    from . import case_review
    case_key = issue.get("案件鍵")
    if case_key not in case_review.cleared_keys(master, state_dir):
        return False
    review = case_review.load(state_dir).get(case_key)
    if review is None or review.exception_keys is None:
        return False
    same = ("問題代碼", "欄位", "說明")
    return any(all(str(e.get(k) or "") == str(issue.get(k) or "") for k in same)
               for e in review.exception_evidence)


def subject_key(issue) -> str:
    return f"correction:{issue.get('案件鍵')}:{source_issues.key(issue)[:16]}"


def run(master, state_dir, *, llm=None, run_id: str | None = None) -> list[dict]:
    if not P.load_settings(state_dir)[AGENT]:
        return []
    run_id = run_id or P.new_run_id()
    exams = master.by_key()
    created = []
    for issue in candidate_issues(master):
        key = subject_key(issue)
        if P.existing(state_dir, AGENT, key, model_available=llm is not None) or already_resolved(master, state_dir, issue):
            continue
        exam = exams[issue["案件鍵"]]
        context, refs = build_context(exam, issue, master.committees)
        proposal = {"agent": AGENT, "subject_key": key, "subject": f"{issue.get('問題代碼')}：{issue.get('欄位')}",
                    "case_key": exam.案件鍵, "issue": {k: issue.get(k) for k in ("問題代碼", "欄位", "說明")},
                    "run_id": run_id, "suggestion": None, "confidence": None, "rationale": "",
                    "method": "none", "model": None, "prompt_version": None, "error": ""}
        if llm is None:
            proposal.update(rationale="未設定模型；請專員在案件頁自行修正")
        else:
            user = P.scrub(P.dumps(context))
            data, error = P.call_llm(llm, state_dir, agent=AGENT, run_id=run_id, tracking_key=key,
                                     system=SYSTEM_PROMPT, user=user, schema_name="source_correction", required=("action",),
                                     check=lambda d: _validated(d, exam, master.committees, refs),
                                     prompt_version=PROMPT_VERSION,
                                     input_summary=f"{issue.get('問題代碼')}＋去識別案件欄位（{len(refs)} 位委員代號）")
            proposal.update(method="llm", model=getattr(llm, "model", None) or type(llm).__name__,
                            prompt_version=PROMPT_VERSION)
            suggestion = None
            if data is not None:
                try:
                    suggestion = _validated(data, exam, master.committees, refs)
                except (ValueError, TypeError) as exc:
                    error = f"模型建議未通過檢核：{exc}"
            if error:
                proposal.update(error=error, rationale="模型呼叫失敗或建議無效，請專員在案件頁自行處理")
            else:
                proposal.update(suggestion=suggestion, confidence=P.confidence(data.get("confidence")),
                                rationale=str(data.get("rationale") or "")[:500])
        saved = P.add(state_dir, proposal)
        trace_step(state_dir, agent=AGENT, run_id=run_id, step="propose", tracking_key=key,
                   tool="ai_proposals.add", output={"id": saved["id"], "suggestion": saved["suggestion"]},
                   decision="pending_human", human_required=True)
        created.append(saved)
    return created


def _validated(data: dict, exam, committees, refs: dict[str, str]) -> dict | None:
    if data.get("action") != "correct":
        return None
    field = str(data.get("field") or "")
    member_ref = str(data.get("member_ref") or "")
    if member_ref:
        if field not in MEMBER_FIELDS or member_ref not in refs:
            raise ValueError(f"委員欄位或代號不允許：{member_ref} {field}")
        member = refs[member_ref]
    else:
        if field not in EXAM_FIELDS:
            raise ValueError(f"欄位不允許：{field}")
        member = ""
    value = corrections._validate(field, str(data.get("new_value") or ""), member)
    old = corrections.source_value(exam, committees, field, member)
    if old is None:
        raise ValueError("找不到對應委員")
    if old == value:
        raise ValueError("新值與目前值相同")
    return {"field": field, "member": member, "member_ref": member_ref, "old": old, "new": value,
            "label": f"{member_ref}的{corrections.MEMBER_FIELDS.get(field, field)}" if member_ref
            else corrections.EXAM_FIELDS.get(field, field)}


def decide(state_dir, proposal_id: str, *, action: str, actor: str, master_path, value: str = "",
           note: str = "") -> dict:
    """approve／override 都走既有專員修正層（save_case_correction）；reject 只記錄。"""
    proposal = P.get(state_dir, proposal_id)
    if proposal is None or proposal["agent"] != AGENT:
        raise ValueError("找不到這筆修正建議")
    actor = actor.strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    if action == "reject":
        return P.mark_decided(state_dir, proposal_id, status=P.REJECTED, actor=actor, decision=None, note=note)
    suggestion = proposal.get("suggestion")
    if not suggestion:
        raise ValueError("這筆建議沒有可套用的修正，請到案件頁自行處理或選不採用")
    new = suggestion["new"] if action == "approve" else value.strip()
    status = P.APPROVED if new == suggestion["new"] else P.OVERRIDDEN
    reason = (note.strip() or proposal.get("rationale") or "")
    from .application import effective_case_value, save_case_correction
    from . import util as U
    with U.state_lock(state_dir):
        current = effective_case_value(master_path=master_path, state_dir=state_dir, case_key=proposal["case_key"],
                                       field=suggestion["field"], member=suggestion["member"])
        if current is not None and current != suggestion["old"]:   # 前提已變（審查 F04）
            raise ValueError(f"資料已變更：{suggestion.get('label') or suggestion['field']}目前是「{current}」，"
                             f"這筆建議是依「{suggestion['old']}」提出的，已過期；請選「不採用」後到案件頁處理")
        save_case_correction(master_path=master_path, state_dir=state_dir, case_key=proposal["case_key"],
                             field=suggestion["field"], member=suggestion["member"], value=new, actor=actor,
                             reason=f"採用 AI 修正建議 {proposal_id}：{reason}"[:500])
        return P.mark_decided(state_dir, proposal_id, status=status, actor=actor,
                              decision={"field": suggestion["field"], "member": suggestion["member"],
                                        "old": suggestion["old"], "new": new}, note=note)
