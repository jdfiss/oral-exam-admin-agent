# -*- coding: utf-8 -*-
"""系辦問答助理的唯讀工具與個資代號化。

設計重點（詳見 docs/問答助理設計.md）：

1. **資料跟畫面同源**：`Snapshot` 由網頁層用工作總覽、人工關卡、文件中心、
   委員個資、通訊記錄頁「同一批函式」算出的結果組成；工具只在這份快照上篩選，
   不自己重算狀態，所以助理說的和畫面顯示的是同一份資料。
2. **唯讀**：工具都是純函式，沒有任何寫入、寄信、放行的能力。
3. **不給模型個資**：身分證、銀行、帳號、戶籍地址、E-mail 從不放進工具輸出；
   委員個資只回「已回填／未回填」。案件鍵、學號、學生姓名、教授與委員姓名
   一律換成代號（CASE_01、STU_01、PER_01）再交給模型，回答顯示時才換回真實值
   （依 LLM前置研究第七節：學校確認前不把學生資料送雲端）。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import advisor_confirmation, case_access, committee_requests, send_log, source_issues
from . import settings as office_settings
from .responsibility import STUDENT

# ── 狀態詞彙（工具輸出與回答檢查共用）────────────────────────

STAGE_REVOKED = "已撤銷"
STAGE_UNMATCHED = "附件2查無"
STAGE_NOT_FILLED = "未填寫"
STAGE_AWAITING_ATT2 = "等待附件2上傳"
STAGE_DEFICIENCY = "學生缺件"
STAGE_REVIEW = "待專員放行"
STAGE_CONFIRMATION = "教授要求修改附件4"   # 2026-10-12 起教授不用回信；只有要求修改才算待辦
STAGE_COMMITTEE = "委員個資未齊"
STAGE_ATT6_UNSENT = "附件6尚未交付"
STAGE_ATT6_OUT = "附件6待回收"
STAGE_ATT6_FIX = "附件6需補正"
STAGE_DONE = "流程完成"

# 流程順序：案件「卡在哪關」＝第一個尚未通過的關卡
STAGES = (STAGE_REVOKED, STAGE_UNMATCHED, STAGE_NOT_FILLED, STAGE_AWAITING_ATT2, STAGE_DEFICIENCY,
          STAGE_REVIEW, STAGE_CONFIRMATION, STAGE_COMMITTEE, STAGE_ATT6_UNSENT, STAGE_ATT6_OUT,
          STAGE_ATT6_FIX, STAGE_DONE)

DEFICIENCY_STATUS_LABELS = {"new": "尚未通知", "notified": "已通知待補", "overdue": "補件逾期"}
OWNER_LABELS = {"student": "學生", "office": "系辦", "post_exam": "口試後", "none": "不需通知"}
REVIEW_LABELS = ("已放行", "放行後名單變動")
COMMITTEE_LABELS = ("已回填", "未回填", "已寄索取信", "尚未寄索取信")

# 文件中心的教授確認／附件6 狀態標籤（與 web/app.py 的 _TRACK_STATUS_LABELS 一致；
# 網頁層建快照時會傳入自己那份，這裡只是預設值）。
TRACK_STATUS_LABELS = {
    "draft": "尚未寄出", "pending": "已寄出", "confirmed": "已確認", "modified": "需修改",
    "overdue": "逾期未回", "outstanding": "已交付待收回", "collected": "已收回（合格）",
    "needs_correction": "收回但需補正",
}

MAX_ROWS = 50


def status_vocabulary(track_labels: dict | None = None) -> tuple[str, ...]:
    """回答檢查用的封閉狀態詞彙（長的在前，避免「逾期」先吃掉「逾期未回」）。"""
    words = set(STAGES) | set(DEFICIENCY_STATUS_LABELS.values()) | set(REVIEW_LABELS) \
        | set(COMMITTEE_LABELS) | set((track_labels or TRACK_STATUS_LABELS).values())
    return tuple(sorted(words, key=len, reverse=True))


# ── 快照 ─────────────────────────────────────────────────

@dataclass
class Snapshot:
    today: _dt.date
    settings: office_settings.OfficeSettings
    overview_rows: list[dict]
    reviews: list
    tracking: object | None
    master: object | None
    committee_plan: list = field(default_factory=list)
    send_events: list = field(default_factory=list)
    source_issues: list = field(default_factory=list)
    inbox_pending: int = 0
    change_request_cases: set = field(default_factory=set)
    track_labels: dict = field(default_factory=lambda: dict(TRACK_STATUS_LABELS))


def build_snapshot(*, state_dir, today, overview_rows, reviews, tracking, master,
                   track_labels=None) -> Snapshot:
    """overview_rows／reviews／tracking 由呼叫端傳入工作總覽頁實際使用的結果；
    其餘資料用各頁面相同的讀取函式載入。"""
    state_dir = Path(state_dir)
    return Snapshot(
        today=today,
        settings=office_settings.load(state_dir),
        overview_rows=list(overview_rows),
        reviews=list(reviews),
        tracking=tracking,
        master=master,
        committee_plan=committee_requests.plan(master, state_dir) if master else [],
        send_events=send_log.load(state_dir / "send_log.json").all(),
        source_issues=source_issues.unresolved(master, state_dir) if master else [],
        inbox_pending=sum(1 for i in advisor_confirmation.load_inbox(state_dir)
                          if i.status == advisor_confirmation.INBOX_PENDING),
        change_request_cases={r.case_key for r in case_access.requests(state_dir, pending_only=True)},
        track_labels=dict(track_labels or TRACK_STATUS_LABELS),
    )


# ── 代號化 ───────────────────────────────────────────────

TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(CASE|STU|PER)_\d+(?![0-9])")
_EMAIL_RE = re.compile(r"[^\s@<>，、；;]+@[^\s@<>，、；;]+\.[A-Za-z]{2,}")
_LONG_DIGITS = re.compile(r"\d{8,}")


class Aliaser:
    """真實識別資料 ↔ 代號。代號依排序決定，同一份快照每次都一樣。"""

    def __init__(self, snapshot: Snapshot):
        self.forward: dict[str, str] = {}
        self.reverse: dict[str, str] = {}
        self.case_names: dict[str, str] = {}
        self.student_cases: dict[str, list[str]] = {}
        exams = list(snapshot.master.exams) if snapshot.master else []
        cases = {r["id"]: r for r in snapshot.overview_rows}
        for e in exams:
            cases.setdefault(e.案件鍵, {"id": e.案件鍵, "student_id": e.學號, "student_name": e.姓名})
        for key in sorted(cases):
            row = cases[key]
            self._add(key, "CASE")
            self.case_names[key] = row.get("student_name") or ""
        for key in sorted(cases):
            row = cases[key]
            for value in (row.get("student_id"), row.get("student_name")):
                value = str(value or "").strip()
                if len(value) >= 2:
                    token = self._add(value, "STU")
                    self.student_cases.setdefault(token, [])
                    if key not in self.student_cases[token]:
                        self.student_cases[token].append(key)
        people = set()
        for e in exams:
            people.update(e.指導教授們)
            people.update(m.委員姓名 for m in e.委員)
        if snapshot.master:
            people.update(c.委員姓名 for c in snapshot.master.committees.values())
        for name in sorted(p.strip() for p in people if p and len(p.strip()) >= 2):
            self._add(name, "PER")
        self._pattern = None

    def _add(self, real: str, kind: str) -> str:
        if real in self.forward:
            return self.forward[real]
        count = sum(1 for t in self.reverse if t.startswith(kind + "_")) + 1
        token = f"{kind}_{count:02d}"
        self.forward[real] = token
        self.reverse[token] = real
        return token

    def token(self, real: str) -> str:
        return self.forward.get(real, real)

    def scrub(self, text) -> str:
        """真實值換代號，再遮 E-mail 與 8 碼以上數字（學號、身分證、帳號）。"""
        text = str(text if text is not None else "")
        if self.forward:
            if self._pattern is None:
                reals = sorted(self.forward, key=len, reverse=True)
                self._pattern = re.compile("|".join(re.escape(r) for r in reals))
            text = self._pattern.sub(lambda m: self.forward[m.group(0)], text)
        text = _EMAIL_RE.sub("[E-mail 已隱藏]", text)
        return _LONG_DIGITS.sub(lambda m: "*" * len(m.group(0)), text)

    def scrub_obj(self, obj):
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v) for v in obj]
        if isinstance(obj, str):
            return self.scrub(obj)
        return obj

    def resolve_case(self, value) -> str | None:
        """工具參數的案件：可給 CASE 代號、只有一個案件的 STU 代號，或真實案件鍵。"""
        value = str(value or "").strip()
        real = self.reverse.get(value)
        if value.startswith("CASE_") and real:
            return real
        if value.startswith("STU_"):
            keys = self.student_cases.get(value, [])
            return keys[0] if len(keys) == 1 else None
        if value in self.forward and self.forward[value].startswith("CASE_"):
            return value
        return None

    def display(self, token: str) -> tuple[str, str]:
        """代號 → (種類, 真實值)。查無代號時原樣回傳。"""
        real = self.reverse.get(token)
        if real is None:
            return "text", token
        return token.split("_")[0].lower(), real

    def legend(self, tokens) -> list[tuple[str, str]]:
        return [(t, self.reverse[t]) for t in sorted(set(tokens)) if t in self.reverse]


# ── 階段判定（只用畫面同源資料）──────────────────────────

def _d(value) -> str | None:
    return value.isoformat() if isinstance(value, _dt.date) else (value or None)


def _deadline(item) -> str | None:
    return _d(item.deadline.computed_date) if getattr(item, "deadline", None) else None


class _Index:
    """把快照依案件鍵整理一次，供各工具共用。"""

    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.rows = {r["id"]: r for r in snap.overview_rows}
        self.reviews = {r.case_key: r for r in snap.reviews}
        tr = snap.tracking
        self.deficiency = tr.deficiency.by_case if tr else {}
        self.confirmations: dict[str, list] = {}
        self.collections: dict[str, list] = {}
        if tr:
            for req in tr.confirmation.requests:
                for key in req.case_keys:
                    self.confirmations.setdefault(key, []).append(req)
            for item in tr.collection.items:
                for key in item.case_keys:
                    self.collections.setdefault(key, []).append(item)
        self.committee: dict[str, list] = {}
        for req in snap.committee_plan:
            for key in req.case_keys:
                self.committee.setdefault(key, []).append(req)

    def label(self, status: str) -> str:
        return self.snap.track_labels.get(status, status)

    def blockers(self, key: str) -> list[dict]:
        """依流程順序列出此案所有尚未通過的關卡（第一個就是「卡在哪關」）。"""
        row = self.rows.get(key, {})
        status = row.get("status", "")
        out = []
        if status == "已撤銷":
            return [{"stage": STAGE_REVOKED, "detail": "附件2 為撤銷案"}]
        if status == "附件2查無":
            out.append({"stage": STAGE_UNMATCHED, "detail": "學生有填寫但附件2 查無此案，交專員確認"})
        elif status == "未填寫":
            out.append({"stage": STAGE_NOT_FILLED, "detail": "附件2 有此案，學生尚未送出填寫"})
        if self.snap.tracking is None:
            out.append({"stage": STAGE_AWAITING_ATT2, "detail": "尚未建立主檔（附件2 未上傳或建檔資料未齊）"})
            return out
        student_items = [i for i in self.deficiency.get(key, []) if i.owner == STUDENT]
        if student_items:
            out.append({"stage": STAGE_DEFICIENCY, "detail": f"學生責任待補 {len(student_items)} 項"})
        review = self.reviews.get(key)
        if review is not None and review.pending:
            detail = ("放行後委員名單變動，需重新放行" if review.changed_since_clear
                      else f"尚未放行（例外 {len(review.exceptions)} 項）")
            out.append({"stage": STAGE_REVIEW, "detail": detail})
        # 2026-10-12 起取消教授確認：沒回信不算卡住；教授回信要求修改、系辦還沒處理完才是待辦
        modified = [r for r in self.confirmations.get(key, []) if r.status == "modified"]
        if modified:
            out.append({"stage": STAGE_CONFIRMATION,
                        "detail": "、".join(f"{r.advisor or '（未填指導教授）'}：{self.label(r.status)}"
                                           for r in modified)})
        missing = [r for r in self.committee.get(key, []) if not r.received]
        if missing:
            out.append({"stage": STAGE_COMMITTEE, "detail": f"{len(missing)} 位委員尚未回填撥帳／身分資料"})
        for item in self.collections.get(key, []):
            st = item.status
            if st == "draft":
                out.append({"stage": STAGE_ATT6_UNSENT, "detail": self.label(st)})
            elif st in ("outstanding", "overdue"):
                out.append({"stage": STAGE_ATT6_OUT, "detail": self.label(st)})
            elif st == "needs_correction":
                out.append({"stage": STAGE_ATT6_FIX, "detail": self.label(st)})
        return out

    def stage(self, key: str) -> str:
        b = self.blockers(key)
        return b[0]["stage"] if b else STAGE_DONE


# ── 工具 ─────────────────────────────────────────────────

def _limit(rows: list) -> dict:
    out = {"count": len(rows), "rows": rows[:MAX_ROWS]}
    if len(rows) > MAX_ROWS:
        out["note"] = f"只列前 {MAX_ROWS} 筆"
    return out


def tool_overview_counts(snap: Snapshot, idx: _Index) -> dict:
    """工作總覽頁頂端的數字。"""
    rows = snap.overview_rows
    student_overdue = len([i for i in snap.tracking.deficiency.overdue if i.owner == STUDENT]) \
        if snap.tracking else 0
    stages: dict[str, int] = {}
    for key in idx.rows:
        s = idx.stage(key)
        stages[s] = stages.get(s, 0) + 1
    return {
        "total_cases": len(rows),
        "not_filled": sum(1 for r in rows if r["status"] == "未填寫"),
        "unmatched": sum(1 for r in rows if r["status"] == "附件2查無"),
        "review_pending": sum(1 for r in snap.reviews if r.pending),
        "student_deficiency_overdue": student_overdue,
        "change_requests_pending": len(snap.change_request_cases),
        "cases_by_stage": stages,
    }


def tool_list_cases(snap: Snapshot, idx: _Index, stage: str = "", blocker: str = "") -> dict:
    stage, blocker = str(stage or "").strip(), str(blocker or "").strip()
    for value in (stage, blocker):
        if value and value not in STAGES:
            return {"error": f"不認得的關卡：{value}", "allowed": list(STAGES)}
    rows = []
    for key, row in sorted(idx.rows.items()):
        blockers = idx.blockers(key)
        current = blockers[0]["stage"] if blockers else STAGE_DONE
        if stage and current != stage:
            continue
        if blocker and blocker not in {b["stage"] for b in blockers}:
            continue
        rows.append({
            "case": key, "stage": current, "blockers": [b["stage"] for b in blockers],
            "exam_date": row.get("exam_date") or "", "fill_status": row.get("status", ""),
            "deficiency_count": row.get("deficiency_count", 0),
        })
    return {"filter": {"stage": stage, "blocker": blocker}, **_limit(rows)}


def tool_get_case(snap: Snapshot, idx: _Index, case: str = "", _aliaser: Aliaser | None = None) -> dict:
    key = _aliaser.resolve_case(case) if _aliaser else case
    if not key or key not in idx.rows and key not in idx.reviews:
        return {"error": f"找不到案件：{case}"}
    row = idx.rows.get(key, {})
    blockers = idx.blockers(key)
    review = idx.reviews.get(key)
    out = {
        "case": key,
        "stage": blockers[0]["stage"] if blockers else STAGE_DONE,
        "blockers": blockers,
        "fill_status": row.get("status", ""), "submission_version": row.get("version"),
        "exam_date": row.get("exam_date") or "", "late_added": bool(row.get("late_added")),
        "has_student_email": not row.get("no_email", False),
        "deficiencies": [{"field": i.field, "description": i.description,
                          "owner": OWNER_LABELS.get(i.owner, i.owner),
                          "status": DEFICIENCY_STATUS_LABELS.get(i.status, i.status),
                          "deadline": _deadline(i)} for i in idx.deficiency.get(key, [])],
        "review": None if review is None else {
            "status": STAGE_REVIEW if review.pending else "已放行",
            "changed_since_clear": review.changed_since_clear,
            "cleared_at": review.review.cleared_at if review.review else None,
            "exceptions": [str(e.get("說明", "")) for e in review.exceptions],
            "new_exceptions_after_clear": len(review.new_exceptions),
        },
        "advisor_confirmations": [{"advisor": r.advisor, "status": idx.label(r.status),
                                   "sent_date": _d(r.sent_date), "deadline": _deadline(r),
                                   "reminder_count": r.reminder_count}
                                  for r in idx.confirmations.get(key, [])],
        "committee_data": [{"member": r.name, "data": "已回填" if r.received else "未回填",
                            "request": "已寄索取信" if r.sent_date else "尚未寄索取信"}
                           for r in idx.committee.get(key, [])],
        "attachment6": [{"status": idx.label(i.status), "deadline": _deadline(i)}
                        for i in idx.collections.get(key, [])],
        "change_request_pending": key in snap.change_request_cases,
        "page": "工作總覽 → 案件詳情",
    }
    return out


def tool_list_confirmations(snap: Snapshot, idx: _Index, status: str = "") -> dict:
    """附件4 教授確認（文件中心同一份追蹤資料）。status：draft/pending/overdue/modified/confirmed，空白＝未確認全部。"""
    status = str(status or "").strip()
    if status and status not in TRACK_STATUS_LABELS:
        return {"error": f"不認得的狀態：{status}", "allowed": ["draft", "pending", "overdue", "modified", "confirmed"]}
    requests = list(snap.tracking.confirmation.requests) if snap.tracking else []
    counts: dict[str, int] = {}
    for r in requests:
        counts[idx.label(r.status)] = counts.get(idx.label(r.status), 0) + 1
    chosen = [r for r in requests if (r.status == status if status else r.status != "confirmed")]
    # 2026-10-12 起教授不用回信：沒有回覆期限、也不催，所以不給模型期限與催辦次數
    rows = [{"advisor": r.advisor or "（未填指導教授）", "status": idx.label(r.status),
             "cases": list(r.case_keys), "sent_date": _d(r.sent_date)} for r in chosen]
    return {"filter": status or "全部（不含已確認）", "counts_by_status": counts,
            "advisor_replies_awaiting_review": snap.inbox_pending, **_limit(rows)}


def tool_list_deficiencies(snap: Snapshot, idx: _Index, owner: str = "", status: str = "") -> dict:
    """缺件與例外（文件中心／案件詳情同一份缺件追蹤）。owner：student/office/post_exam；status：new/notified/overdue。"""
    owner, status = str(owner or "").strip(), str(status or "").strip()
    if owner and owner not in OWNER_LABELS:
        return {"error": f"不認得的責任別：{owner}", "allowed": list(OWNER_LABELS)}
    if status and status not in DEFICIENCY_STATUS_LABELS:
        return {"error": f"不認得的狀態：{status}", "allowed": list(DEFICIENCY_STATUS_LABELS)}
    items = list(snap.tracking.deficiency.items) if snap.tracking else []
    items = [i for i in items if (not owner or i.owner == owner) and (not status or i.status == status)]
    rows = [{"case": i.case_key, "field": i.field, "description": i.description,
             "owner": OWNER_LABELS.get(i.owner, i.owner),
             "status": DEFICIENCY_STATUS_LABELS.get(i.status, i.status), "deadline": _deadline(i)}
            for i in sorted(items, key=lambda i: (i.case_key, i.field))]
    return {"filter": {"owner": owner, "status": status}, "cases_affected": len({i.case_key for i in items}),
            **_limit(rows)}


def tool_list_source_issues(snap: Snapshot, idx: _Index) -> dict:
    """沒有案件鍵、尚未處理的附件2 來源異常（來源異常頁同一份）。"""
    rows = [{"code": str(i.get("問題代碼", "")), "field": str(i.get("欄位", "")),
             "description": str(i.get("說明", ""))} for i in snap.source_issues]
    return _limit(rows)


def tool_list_committee_data(snap: Snapshot, idx: _Index, missing_only: bool = True) -> dict:
    """委員個資狀態（委員個資頁同一份）：只回「已回填／未回填」，不回任何個資值。"""
    lead = snap.settings.committee_exception_lead_days
    reqs = [r for r in snap.committee_plan if not (missing_only and r.received)]
    rows = [{"member": r.name, "cases": list(r.case_keys), "data": "已回填" if r.received else "未回填",
             "request": "已寄索取信" if r.sent_date else "尚未寄索取信",
             "earliest_exam": _d(r.earliest_exam),
             "exam_soon_still_missing": r.missing_before_exam(snap.today, lead)} for r in reqs]
    return {"missing_only": bool(missing_only),
            "missing_total": sum(1 for r in snap.committee_plan if not r.received), **_limit(rows)}


def tool_get_deadlines(snap: Snapshot, idx: _Index) -> dict:
    s = snap.settings
    return {
        "today": snap.today.isoformat(), "current_term": s.current_term or "",
        "phase": office_settings.PHASE_LABELS[s.phase_on(snap.today)],
        "fill_deadline": _d(s.fill_deadline), "att2_first_uploaded": _d(s.att2_first_uploaded),
        "check_start": _d(s.check_start), "deficiency_deadline": _d(s.deficiency_deadline),
        "deficiency_deadline_overridden": s.deficiency_deadline_override is not None,
        "deficiency_days": s.deficiency_days, "deficiency_reminder_lead_days": s.deficiency_reminder_lead_days,
        "confirmation_days": s.confirmation_days, "collection_days": s.collection_days,
        "committee_exception_lead_days": s.committee_exception_lead_days,
        "day_counting": "工作日" if s.business_days else "曆日",
    }


_SEND_TYPE_LABELS = {
    "deficiency": "缺件補件通知", "confirmation": "教授確認提醒", "collection": "附件6催繳通知",
    "attachment4": "附件4 寄指導教授確認", "committee_request": "委員個資索取",
    "committee_link_update": "委員填寫連結更新",
}
_SEND_STATUS_LABELS = {send_log.DRAFT: "草稿中", send_log.APPROVED: "已核准待寄",
                       send_log.REJECTED: "已退回不寄", send_log.SENT: "已寄出", send_log.FAILED: "寄送失敗"}


def tool_communication_counts(snap: Snapshot, idx: _Index, days: int | None = None) -> dict:
    """通訊記錄（send_log）的件數統計；不回收件人與主旨。"""
    try:
        days = int(days) if days not in (None, "") else None
    except (TypeError, ValueError):
        return {"error": "days 必須是整數"}
    events = snap.send_events
    if days is not None:
        since = snap.today - _dt.timedelta(days=days)
        events = [e for e in events if (e.created_at or "")[:10] >= since.isoformat()]
    table: dict[str, dict[str, int]] = {}
    for e in events:
        t = _SEND_TYPE_LABELS.get(e.type, e.type)
        s = _SEND_STATUS_LABELS.get(e.status, e.status)
        table.setdefault(t, {}).setdefault(s, 0)
        table[t][s] += 1
    return {"window_days": days, "total": len(events),
            "sent": sum(1 for e in events if e.status == send_log.SENT),
            "failed": sum(1 for e in events if e.status == send_log.FAILED),
            "by_type_and_status": table}


@dataclass(frozen=True)
class Tool:
    name: str
    fn: object
    signature: str
    description: str


TOOLS = {t.name: t for t in (
    Tool("overview_counts", tool_overview_counts, "overview_counts()",
         "工作總覽的總數：案件數、未填寫、附件2查無、待專員放行、學生補件逾期、各關卡案件數"),
    Tool("list_cases", tool_list_cases, "list_cases(stage?, blocker?)",
         "列出案件與目前卡住的關卡；stage＝目前關卡、blocker＝任何未通過的關卡，值須為："
         + "／".join(STAGES)),
    Tool("get_case", tool_get_case, "get_case(case)",
         "單一案件的完整狀態：卡在哪關、缺件、人工關卡、教授確認、委員個資、附件6；case 用 CASE_ 或 STU_ 代號"),
    Tool("list_confirmations", tool_list_confirmations, "list_confirmations(status?)",
         "附件4 寄給指導教授的紀錄（寄給哪位教授、哪天寄出、教授有沒有回信要求修改）。2026-10-12 起教授不用回信確認，已寄出就是完成、沒回信不算卡住；status 可為 draft（尚未寄出）/pending（已寄出）/modified（教授要求修改）/overdue/confirmed，省略＝全部未確認"),
    Tool("list_deficiencies", tool_list_deficiencies, "list_deficiencies(owner?, status?)",
         "缺件與例外；owner＝student/office/post_exam，status＝new/notified/overdue"),
    Tool("list_source_issues", tool_list_source_issues, "list_source_issues()",
         "尚未處理、沒有案件鍵的附件2 來源異常"),
    Tool("list_committee_data", tool_list_committee_data, "list_committee_data(missing_only?)",
         "已放行案件中需要撥帳／身分資料的委員，只回已回填／未回填，missing_only 預設 true"),
    Tool("get_deadlines", tool_get_deadlines, "get_deadlines()",
         "本期學期、目前階段、填寫截止日、補件截止日與各項期限天數"),
    Tool("communication_counts", tool_communication_counts, "communication_counts(days?)",
         "通訊記錄件數（依類型與狀態）；days＝只看最近幾天"),
)}


def run_tool(name: str, args: dict, snap: Snapshot, aliaser: Aliaser, idx: _Index | None = None) -> dict:
    """執行工具並把輸出代號化；未知工具或參數錯誤回 {"error": ...}，不拋例外。"""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"沒有這個工具：{name}", "allowed": sorted(TOOLS)}
    idx = idx or _Index(snap)
    args = dict(args or {}) if isinstance(args, dict) else {}
    import inspect
    params = inspect.signature(tool.fn).parameters
    clean = {k: v for k, v in args.items() if k in params and not k.startswith("_")}
    if "_aliaser" in params:
        clean["_aliaser"] = aliaser
    try:
        result = tool.fn(snap, idx, **clean)
    except Exception as exc:  # 工具錯誤只回報給模型，不中斷整個問答
        result = {"error": f"工具執行失敗：{type(exc).__name__}"}
    return aliaser.scrub_obj(json.loads(json.dumps(result, ensure_ascii=False, default=str)))


def make_index(snap: Snapshot) -> _Index:
    return _Index(snap)
