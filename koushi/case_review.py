# -*- coding: utf-8 -*-
"""人工關卡：專員逐案審核口試委員資格與例外，按「已處理」放行（2026-09-23 定案）。

每案一次，在補件截止日之前進行。放行後才寄信向委員索取個資、才納入文件中心產檔。
（2026-10-12 起寄給指導教授的附件4 不等放行：學生資料齊全就寄，見
`advisor_confirmation.sendable_keys`。）
放行紀錄存在 `STATE_DIR/case_reviews.json`，留存處理人、時間、備註，並寫稽核日誌。

放行當下記下委員名單（姓名＋服務單位＋職稱）的簽章。之後名單一變（學生補件、
附件2 重傳或專員修正），放行就**失效**、回到待放行：專員重新放行之前，
不向新委員索取個資（2026-10-01 Q9，`cleared_keys`）。紀錄本身保留，畫面
可顯示上次放行的人與時間。放行當下也記下當時的例外清單；名單沒變、只多了放行時
沒有的例外（含附件2 重傳帶來的新來源例外），維持放行，只標示「放行後有新例外」
（review-2026-09-23 P2-23；2026-10-09 稽核：來源例外不再撤銷放行）。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import audit_log, util as U
from .model import Exam, Master

_FILENAME = "case_reviews.json"


@dataclass(frozen=True)
class CaseReview:
    case_key: str
    cleared_at: str
    cleared_by: str
    note: str
    committee_signature: str
    # 放行當下已存在的例外（問題代碼|欄位）；None＝舊紀錄沒存，無法比對。
    exception_keys: tuple[str, ...] | None = None
    exception_evidence: tuple[dict, ...] = ()


def exception_key(issue: dict) -> str:
    payload = {k: issue.get(k, "") for k in ("問題代碼", "欄位", "說明", "來源")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def committee_signature(exam: Exam, master: Master) -> str:
    """委員名單（姓名＋服務單位＋職稱）的簽章，用來偵測放行後名單是否變動。"""
    rows = []
    for member in exam.委員:
        committee = master.committees.get(member.committee_id)
        name = member.委員姓名 or (committee.委員姓名 if committee else member.committee_id)
        email = member.Email or (committee.Email if committee else "")
        rows.append(f"{U.canon_name(name)}|{U.norm_unit(member.服務單位)}|{member.職稱}|{email.lower()}|{member.視訊出席}")
    return hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()[:16]


def load(state_dir: str | Path) -> dict[str, CaseReview]:
    path = Path(state_dir) / _FILENAME
    if not path.is_file():
        return {}
    raw = U.read_json_state(path)
    reviews = {}
    for key, value in raw.get("cases", {}).items():
        if value.get("exception_keys") is not None:
            value = {**value, "exception_keys": tuple(value["exception_keys"])}
        reviews[key] = CaseReview(**value)
    return reviews


@U.locked_state()
def clear(
    state_dir: str | Path, *, exam: Exam, master: Master, actor: str = "office", note: str = "",
    now: _dt.datetime | None = None, exceptions: list[dict] | None = None,
) -> CaseReview:
    """放行一案；重複放行會覆蓋為最新一次（例如名單變動後專員重審）。

    `exceptions` 是專員放行時畫面上看到的例外，用來之後判斷有沒有新例外。
    """
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    state_dir = Path(state_dir)
    reviews = load(state_dir)
    if exceptions is None:
        exceptions = [i for i in master.source_issues if i.get("案件鍵") == exam.案件鍵]
    review = CaseReview(
        case_key=exam.案件鍵,
        cleared_at=(now or clock.now()).strftime("%Y-%m-%d %H:%M:%S"),
        cleared_by=actor,
        note=note.strip(),
        committee_signature=committee_signature(exam, master),
        exception_keys=tuple(sorted({exception_key(i) for i in exceptions or []})),
        exception_evidence=tuple(dict(i) for i in exceptions or []),
    )
    reviews[exam.案件鍵] = review
    U.atomic_write_json({"cases": {k: asdict(v) for k, v in reviews.items()}}, state_dir / _FILENAME)
    log_path = state_dir / "audit_log.json"
    audit_log.record(
        log_path, "case_review", "cleared",
        tracking_key=(exam.案件鍵,), detail={"actor": actor, "note": review.note},
    )
    return review


def changed_since_clear(review: CaseReview, exam: Exam, master: Master) -> bool:
    """放行是否已因委員名單變動而失效。

    只看委員名單簽章（已確認流程決策第四節第 5 條）：名單沒變、只多了其他例外
    （包括附件2 重傳帶來的新來源例外），維持放行，改由 `new_exceptions` 標
    「放行後有新例外」。學生責任的來源問題（例如尚未填寫）也不影響放行是否有效：
    那些問題本身會擋附件4／6，放行不會略過它們。
    """
    return review.committee_signature != committee_signature(exam, master)


def cleared_keys(master: Master, state_dir: str | Path) -> set[str]:
    """目前仍有效的放行：有放行紀錄，且委員名單從放行後沒變過。"""
    reviews = load(state_dir)
    return {
        exam.案件鍵 for exam in master.exams
        if exam.案件鍵 in reviews and not changed_since_clear(reviews[exam.案件鍵], exam, master)
    }


def new_exceptions(review: CaseReview, exceptions: list[dict]) -> list[dict]:
    """放行後才出現的例外；舊紀錄沒存放行當下的例外時無法判斷，回傳空清單。"""
    if review.exception_keys is None:
        return []
    known = set(review.exception_keys)
    return [i for i in exceptions if exception_key(i) not in known]
