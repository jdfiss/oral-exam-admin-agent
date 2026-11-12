# -*- coding: utf-8 -*-
"""專員修正層（2026-10-01 已確認流程決策第四節第 6 條）。

專員在案件頁修改欄位時，修正另外存一層（`STATE_DIR/corrections.json`），不直接改
附件2 或學生填寫。每次建主檔都在來源讀完後套用，所以重傳附件2、學生重新送出都
不會蓋掉；之後的檢核與文件也都照修正後的值。

每筆修正記下「修正當時的來源值」。之後來源改成跟原值、修正值都不同的第三個值時，
修正照樣套用，但列一筆例外（CORRECTION_SOURCE_CHANGED）交專員確認——專員重新存一次
修正（以新來源值為原值）或移除修正即可解除。

可修正的欄位刻意限縮在案件層（口試日期、時間、地點、方式、指導教授、論文題目）與
委員配對層（職稱、服務單位）；委員以「姓名」辨識，姓名本身不在這裡改。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import audit_log, util as U, validation
from .model import Committee, Exam, Member

FILENAME = "corrections.json"
CONFLICTS_FILENAME = "correction_conflicts.json"  # 最近一次建檔時需要專員確認的修正

EXAM_FIELDS = {
    "口試日期": "口試日期",
    "口試開始時間": "口試開始時間",
    "口試結束時間": "口試結束時間",
    "口試地點": "口試地點",
    "口試方式": "口試方式",
    "指導教授": "指導教授（多位以、分隔）",
    "論文題目": "論文題目",
    "advisor_emails": "教授信箱（姓名與Email的JSON物件）",
    "student_email": "學生Email",
    "committee_list": "委員名單（JSON；姓名、單位、職稱、Email與視訊出席）",
}
MEMBER_FIELDS = {
    "職稱": "委員職稱",
    "委員姓名": "委員姓名",
    "Email": "委員Email",
    "視訊出席": "視訊出席（true／false）",
    "服務單位": "委員服務單位",
}

ISSUE_SOURCE_CHANGED = "CORRECTION_SOURCE_CHANGED"
ISSUE_TARGET_MISSING = "CORRECTION_TARGET_MISSING"


@dataclass(frozen=True)
class Correction:
    case_key: str
    field: str
    value: str
    original: str        # 修正當時的來源值
    actor: str
    reason: str
    at: str
    member: str = ""     # 委員層修正：委員姓名（canon_name 比對）；案件層為空

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.case_key, self.member, self.field)

    @property
    def label(self) -> str:
        if self.member:
            return f"委員「{self.member}」的{MEMBER_FIELDS.get(self.field, self.field)}"
        return EXAM_FIELDS.get(self.field, self.field)


def load(state_dir: str | Path) -> list[Correction]:
    path = Path(state_dir) / FILENAME
    if not path.is_file():
        return []
    raw = U.read_json_state(path)
    return [Correction(**item) for item in raw.get("corrections", [])]


def by_case(state_dir: str | Path, case_key: str) -> list[Correction]:
    return [c for c in load(state_dir) if c.case_key == case_key]


def _save(state_dir: Path, corrections: list[Correction]) -> None:
    U.atomic_write_json({"corrections": [asdict(c) for c in corrections]}, state_dir / FILENAME)


def _format(field: str, value) -> str:
    if value is None:
        return ""
    if isinstance(value, _dt.date):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _find_member(exam: Exam, committees: dict[str, Committee], name: str):
    exact = [m for m in exam.委員 if name in (m.association_id, m.committee_id)]
    if len(exact) == 1:
        return exact[0]
    target = U.canon_name(name)
    found = [m for m in exam.委員 if U.canon_name(m.委員姓名 or
             (committees.get(m.committee_id) or Committee()).委員姓名) == target]
    if len(found) > 1:
        raise ValueError("同名委員有多位，請使用案件委員關聯鍵")
    return found[0] if found else None


def source_value(exam: Exam, committees: dict[str, Committee], field: str, member: str = "") -> str | None:
    """來源目前的值；委員找不到時回傳 None。"""
    if member:
        found = _find_member(exam, committees, member)
        if found is None:
            return None
        value = getattr(found, field)
        if field in ("委員姓名", "Email") and not value:
            value = getattr(committees.get(found.committee_id) or Committee(), field)
        return _format(field, value)
    if field == "committee_list":
        return _format(field, [{"association_id": m.association_id or m.committee_id,
            "name": m.委員姓名 or (committees.get(m.committee_id) or Committee()).委員姓名,
            "unit": m.服務單位, "title": m.職稱, "email": m.Email or
                (committees.get(m.committee_id) or Committee()).Email, "remote": m.視訊出席}
            for m in exam.委員])
    return _format(field, getattr(exam, field))


def _validate(field: str, value: str, member: str) -> str:
    allowed = MEMBER_FIELDS if member else EXAM_FIELDS
    if field not in allowed:
        raise ValueError(f"這個欄位不能在案件頁修正：{field}")
    value = value.strip()
    if field == "口試日期" and value:
        try:
            _dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("口試日期格式錯誤（請用 YYYY-MM-DD）") from exc
    if field == "口試方式" and value not in ("", "實體", "視訊", "部分視訊"):
        raise ValueError("口試方式只能是空白（實體）、視訊或部分視訊")
    if field in ("Email", "student_email") and value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise ValueError("Email格式錯誤")
    if field == "視訊出席" and value not in ("true", "false"):
        raise ValueError("視訊出席需為true或false")
    if field == "committee_list":
        data = json.loads(value)
        if not isinstance(data, list) or len(data) < 3:
            raise ValueError("委員名單至少三位")
        identities = set()
        for row in data:
            if (not isinstance(row, dict) or set(row) - {"association_id", "name", "unit", "title", "email", "remote"}
                    or any(not isinstance(row.get(k), str) or not row[k].strip() for k in ("name", "unit", "email"))
                    or not isinstance(row.get("title", ""), str) or type(row.get("remote", False)) is not bool
                    or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", row["email"])):
                raise ValueError("委員名單格式錯誤")
            identity = (U.canon_name(row["name"]), U.norm_unit(row["unit"]))
            if identity in identities:
                raise ValueError("委員名單重複")
            identities.add(identity)
    if field == "advisor_emails":
        data = json.loads(value)
        if not isinstance(data, dict) or any(not isinstance(v, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v) for v in data.values()):
            raise ValueError("教授信箱格式錯誤")
    return value


@U.locked_state()
def set_correction(
    state_dir: str | Path, *, case_key: str, field: str, value: str, original: str,
    actor: str, reason: str, member: str = "", now: _dt.datetime | None = None,
) -> Correction:
    """新增或覆蓋一筆修正；處理人與原因必填。"""
    actor, reason = actor.strip(), reason.strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    if not reason:
        raise ValueError("請填寫修正原因")
    value = _validate(field, value, member.strip())
    state_dir = Path(state_dir)
    correction = Correction(
        case_key=case_key, field=field, value=value, original=original, actor=actor, reason=reason,
        at=(now or _dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S"), member=member.strip(),
    )
    corrections = [c for c in load(state_dir) if c.key != correction.key] + [correction]
    _save(state_dir, corrections)
    audit_log.record(state_dir / "audit_log.json", "correction", "set", tracking_key=(case_key,),
                     detail={"actor": actor, "reason": reason, "field": correction.label,
                             "from": original, "to": value})
    return correction


@U.locked_state()
def remove_correction(
    state_dir: str | Path, *, case_key: str, field: str, actor: str, member: str = "",
) -> None:
    actor = actor.strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    state_dir = Path(state_dir)
    key = (case_key, member.strip(), field)
    corrections = load(state_dir)
    if not any(c.key == key for c in corrections):
        raise ValueError("找不到這筆修正")
    _save(state_dir, [c for c in corrections if c.key != key])
    audit_log.record(state_dir / "audit_log.json", "correction", "removed", tracking_key=(case_key,),
                     detail={"actor": actor, "field": f"{member}{field}"})


def apply(exams: dict[str, Exam], committees: dict[str, Committee], corrections: list[Correction], *, registry=None) -> list[dict]:
    """把修正套進剛讀完來源的案件；回傳需要專員確認的例外。"""
    issues: list[dict] = []
    for correction in corrections:
        exam = exams.get(correction.case_key)
        if exam is None:
            continue  # 案件已不在附件2：由附件2 重傳流程處理，不另外報
        current = source_value(exam, committees, correction.field, correction.member)
        if current is None:
            issues.append(_issue(exam, correction, ISSUE_TARGET_MISSING,
                                 f"{correction.label}的修正找不到對應委員（名單可能已變動），請確認或移除修正"))
            continue
        if current not in (correction.original, correction.value):
            issue = _issue(exam, correction, ISSUE_SOURCE_CHANGED,
                           f"{correction.label}：來源已改為「{current}」，與修正前（{correction.original}）"
                           f"和修正值（{correction.value}）都不同；目前仍套用修正，請重新確認修正或移除")
            issues.append({**issue, "修正鍵": list(correction.key), "來源值": current})
        target = _find_member(exam, committees, correction.member) if correction.member else exam
        value = correction.value
        if correction.field == "committee_list":
            rows = json.loads(_validate("committee_list", value, ""))
            if len(rows) < (5 if exam.學位別 == "博" else 3):
                raise ValueError("博士口試委員至少五位")
            old = {m.association_id or m.committee_id: m for m in exam.委員}
            members = []
            for index, row in enumerate(rows, 1):
                prior = old.get(row.get("association_id", ""))
                same = prior and U.canon_name(prior.委員姓名 or
                    (committees.get(prior.committee_id) or Committee()).委員姓名) == U.canon_name(row["name"])
                if same:
                    cid, association = prior.committee_id, prior.association_id
                else:
                    if registry is None:
                        raise ValueError("名單更換需要委員身分資料庫")
                    cid = registry.new_provisional_id(exam.案件鍵, row["name"], row["unit"], source="專員修正")
                    association = cid
                    committees[cid] = Committee(committee_id=cid, 委員姓名=row["name"], 服務單位=row["unit"])
                members.append(Member(committee_id=cid, association_id=association, 委員順序=index,
                    委員姓名=row["name"], 服務單位=row["unit"], 職稱=row.get("title", ""),
                    Email=row["email"], 視訊出席=row.get("remote", False)))
            exam.委員 = members
        elif correction.field == "口試日期":
            exam.口試日期 = _dt.date.fromisoformat(value) if value else None
            exam.口試日期原文 = ""
        elif correction.field == "advisor_emails":
            exam.advisor_emails = json.loads(value)
        elif correction.field == "視訊出席":
            target.視訊出席 = value == "true"
            target.交通費, target.交通費說明 = None, ""
        else:
            setattr(target, correction.field, value)
            if correction.field in ("口試方式", "服務單位"):
                for m in exam.委員:
                    m.交通費, m.交通費說明 = None, ""
    return issues


def mark_staff_filled(exams: dict[str, Exam], committees: dict[str, Committee],
                      corrections: list[Correction]) -> set[str]:
    """從未送出的案件，專員以修正層補齊介面填寫的必要資料後，視同已填寫。

    已確認流程決策第三節第 5 條：逾期未完成的案件，專員可以自己在案件頁補資料
    （專員修正層）後放行。必要資料同學生送出時的檢查：口試日期、指導教授（每位都有
    E-mail）、委員名單（每位都有 E-mail）。補齊後把案件標為「有附件3／介面委員已填」，
    「尚未填寫」（MISSING_SUBMISSION）這個學生責任問題就此解決，不再擋截止後的附件4。
    只要有一項沒補齊，就維持未填寫。回傳被視同已填寫的案件鍵。
    """
    filled: set[str] = set()
    for case_key in sorted({c.case_key for c in corrections}):
        exam = exams.get(case_key)
        if exam is None or exam.有附件3:
            continue
        advisors = [a.strip() for a in re.split(r"[、,，;；]", exam.指導教授 or "") if a.strip()]
        emails = {str(k).strip(): str(v or "").strip() for k, v in (exam.advisor_emails or {}).items()}
        if not exam.口試日期 or not advisors or not all(emails.get(a) for a in advisors):
            continue
        if not exam.委員 or any(
            U.blank(m.Email or (committees.get(m.committee_id) or Committee()).Email) for m in exam.委員
        ):
            continue
        exam.有附件3 = True
        exam.介面委員已填 = True
        filled.add(case_key)
    return filled


def _issue(exam: Exam, correction: Correction, code: str, message: str) -> dict:
    return {
        "案件鍵": exam.案件鍵, "學期": exam.學期, "學號": exam.學號, "姓名": exam.姓名,
        "嚴重度": validation.WARN, "問題代碼": code, "欄位": correction.label, "說明": message,
        "阻擋產出": "否", "阻擋附件4": "否", "阻擋附件6": "否",
        "來源": "專員修正", "責任": validation.OWNER_OFFICE,
    }


def original_for(state_dir: str | Path, *, case_key: str, field: str, member: str, shown_value: str) -> str:
    """新存一筆修正時要記的「來源值」。

    主檔顯示的是套用修正後的值，看不到來源；沒有舊修正時兩者相同。已有修正時，
    若最近一次建檔發現來源變了，用那個新來源值（專員重新確認後就不再報例外），
    否則沿用舊修正記下的來源值。
    """
    key = [case_key, member.strip(), field]
    for issue in load_conflicts(state_dir):
        if issue.get("修正鍵") == key and "來源值" in issue:
            return str(issue["來源值"])
    for correction in load(state_dir):
        if list(correction.key) == key:
            return correction.original
    return shown_value


def save_conflicts(state_dir: str | Path, issues: list[dict]) -> None:
    """建檔時呼叫：記下這次需要專員確認的修正，讓人工關卡清單顯示。"""
    U.atomic_write_json({"issues": issues}, Path(state_dir) / CONFLICTS_FILENAME)


def load_conflicts(state_dir: str | Path) -> list[dict]:
    path = Path(state_dir) / CONFLICTS_FILENAME
    if not path.is_file():
        return []
    return U.read_json_state(path).get("issues", [])
