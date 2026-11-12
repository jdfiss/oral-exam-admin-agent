# -*- coding: utf-8 -*-
"""Web 與 CLI 共用的口試操作入口。

學生端資料來源只有 Portal 與本系統的學生介面兩個（2026-09-23 定案）：學生必經
Portal 正式申請，系辦於填寫截止日上傳附件2 作為案件來源；本系統介面只補齊
Portal 沒有的附件 3 資料。學生送出即為有效版本，不經系辦審核。
本模組處理提交版本及跨次追蹤；確定性的資料轉換仍由 workflow 負責。
"""

from __future__ import annotations

import json
import re
import datetime as _dt
from . import ai_switch, clock, source_state
import os
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Mapping

from . import advisor_confirmation, audit_log, case_access, case_review, committee_requests, config as C, corrections, ingest, issue_store, send_log, settings as office_settings, util as U
from .mail import notify, recipients
from .model import Master
from . import master as master_workbook, validation
from .registry import CommitteeRegistry, DistanceTable
from .workflow import (
    BuildResult, RegistryPaths, TemplatePaths, PreExamResult,
    PostExamResult, build_master, generate_pre_exam, generate_post_exam,
)
from .engines.deficiency import (
    DeficiencyReport, scan as deficiency_scan, mark_notified,
    mark_reminded as deficiency_mark_reminded,
)
from .responsibility import STUDENT
from .deadline import Deadline
from .drafts import STATUS_NO_RECIPIENT
from .engines.confirmation import (
    CONFIRMED, MODIFIED, ConfirmationReport, create_requests as confirmation_requests,
    reconcile as confirmation_reconcile, report as confirmation_report,
    mark_sent as confirmation_mark_sent, record_response,
)
from .engines.collection import (
    CollectionReport, create_items as collection_items,
    reconcile as collection_reconcile, report as collection_report,
    mark_reminded as collection_mark_reminded, mark_sent as collection_mark_sent,
    mark_collected as collection_mark_collected,
)
from .engines.state import load_from_dir, save_to_dir
from .scheduler import ReminderScheduler, ScheduledReminder
from .engines.deficiency import DeficiencyItem

_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
_TERM_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")
_STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
_CASE_KEY_RE = re.compile(r"^[A-Za-z0-9]{1,10}-[A-Za-z0-9]{1,20}$")


def _send_window(settings, *, kind, today=None, now=None, state_dir=None, case_key=None):
    now = now or clock.now()
    today = today or now.date()
    if now.hour < settings.send_hour:
        return False
    phase = settings.phase_on(today)
    if kind == "student":
        return phase == office_settings.PHASE_DEFICIENCY or bool(
            phase == office_settings.PHASE_CLOSED and state_dir and case_key
            and case_access.is_unlocked(state_dir, case_key, today))
    return phase in (office_settings.PHASE_DEFICIENCY, office_settings.PHASE_CLOSED)


@U.locked_state()
def send_reminder_plan(*, state_dir, reminders, transport=None, date=None, settings=None, now=None, master=None):
    """所有正式催辦入口共用政策；逐封保存，已寄未對帳可重跑恢復。"""
    sd = Path(state_dir)
    settings = settings or office_settings.load(sd)
    if master is not None:
        source_state.ensure_generation(master)
    sent = []
    for reminder in reminders:
        term = str(reminder.tracking_key[0]).split("-")[0]
        if settings.current_term and term != settings.current_term:
            continue
        key = reminder.tracking_key[0] if reminder.kind == "deficiency" else None
        if not _send_window(settings, kind="student" if key else "office", today=date,
                            now=now, state_dir=sd, case_key=key):
            continue
        log_path = sd / "send_log.json"
        log = send_log.load(log_path)
        event = log.get(reminder.draft.idempotency_key)
        if event and event.status == send_log.SENT:
            mark_reminders_sent(sd, [reminder], date=_sent_event_date(event))
            _record_advisor_reminder(sd, reminder, sent_at=event.sent_at)
            continue
        try:
            delivered = notify.send_scheduled([reminder], log=log, transport=transport)
        finally:
            send_log.save(log, log_path)
        if delivered:
            mark_reminders_sent(sd, delivered, date=date)
            for item in delivered:
                _record_advisor_reminder(sd, item, sent_at=log.get(item.draft.idempotency_key).sent_at)
            sent.extend(delivered)
    return sent


def _active_master(master: Master, settings: office_settings.OfficeSettings) -> Master:
    if not settings.current_term:
        return master
    exams = [e for e in master.exams if e.學期 == settings.current_term]
    keys = {e.案件鍵 for e in exams}
    active = Master(exams=exams, committees=master.committees,
        source_issues=[i for i in master.source_issues if not i.get("案件鍵") or i["案件鍵"] in keys])
    for attr in ("source_generation", "source_master_path"):
        if hasattr(master, attr):
            setattr(active, attr, getattr(master, attr))
    return active


def _effective_deadline(settings, state_dir, case_key):
    unlock = case_access.unlock_for(state_dir, case_key)
    return max(settings.deficiency_deadline, unlock.until_date) if unlock and settings.deficiency_deadline else (
        unlock.until_date if unlock else settings.deficiency_deadline)


def _document_eligibility_issues(master, state_dir):
    settings = office_settings.load(state_dir)
    if settings.phase_on(clock.today()) != office_settings.PHASE_CLOSED:
        return []
    unresolved = [i for i in validation.check(master) if i.get("責任") == validation.OWNER_STUDENT]
    keys = {i.get("案件鍵") for i in unresolved}
    return [{"案件鍵": e.案件鍵, "學期": e.學期, "學號": e.學號, "姓名": e.姓名,
        "問題代碼": "STUDENT_OVERDUE_INCOMPLETE", "欄位": "補件", "責任": validation.OWNER_OFFICE,
        "嚴重度": "阻擋", "來源": "截止政策", "阻擋產出": "是", "阻擋附件4": "是", "阻擋附件6": "是",
        "說明": "截止後仍有學生問題，請完成更正及重新檢核；人工放行不能略過未完成資料"}
        for e in master.exams if e.案件鍵 in keys]


def freeze_cutoff(*, state_dir, master_path, submissions_dir=None, registries=None, settings=None):
    settings = settings or office_settings.load(state_dir)
    if settings.phase_on(clock.today()) != office_settings.PHASE_CLOSED or not settings.deficiency_deadline:
        return None
    regs = registries or _default_registries()
    paths = {"state": state_dir, "master": Path(master_path).parent,
        "attachment2.xlsx": regs.attachment2, "members.xlsx": regs.members, "distances.csv": regs.distances}
    if submissions_dir:
        paths["submissions"] = submissions_dir
    return source_state.freeze(Path(state_dir) / "freezes" / (settings.current_term or "unset") /
        settings.deficiency_deadline.isoformat(), paths, detail={"deadline": settings.deficiency_deadline.isoformat(),
        "term": settings.current_term, "captured_at": clock.now().isoformat(), "kind": "first_observation_after_cutoff"})


def _assert_active_case(case_key, state_dir):
    settings = office_settings.load(state_dir)
    if settings.current_term and not case_key.startswith(settings.current_term + "-"):
        raise ValueError("舊學期已封存，不能修改或放行")


def _audit(state_dir: Path, source: str, action: str, **kwargs) -> None:
    """追加一筆稽核事件並落盤。"""
    audit_log.record(state_dir / "audit_log.json", source, action, **kwargs)


@dataclass(frozen=True)
class ProcessResult:
    build: BuildResult
    drafts: list[dict]
    sent: list[dict]
    log_path: Path


@dataclass(frozen=True)
class TrackingResult:
    deficiency: DeficiencyReport
    confirmation: ConfirmationReport
    collection: CollectionReport


@dataclass(frozen=True)
class ReminderPlan:
    scheduled: list[ScheduledReminder]
    internal_todos: list[DeficiencyItem]


@dataclass(frozen=True)
class AutomationResult:
    phase: str
    process: ProcessResult
    tracking: TrackingResult | None
    reminders: ReminderPlan
    reminders_sent: list[ScheduledReminder]
    notes: tuple[str, ...] = ()
    committee_drafts: tuple[dict, ...] = ()
    committee_sent: tuple[dict, ...] = ()
    advisor: "AdvisorMailResult | None" = None
    replies: tuple = ()
    mailbox: tuple = ()   # 信箱 agent 本次新處理的信（mailbox_agent 紀錄）
    proposals: tuple = ()  # 提案型 agent 本次新增的 AI 建議（ai_proposals 紀錄，皆待專員決定）


def build_current_master(
    *, submission_paths: list[str | Path], master_path: str | Path,
    registries: RegistryPaths | None = None,
    attachment2_path: str | Path | None = None,
    submissions_dir: str | Path | None = None,
) -> BuildResult:
    """以附件2 與學生填寫建檔，並另存一份快照（outputs/<日期>/，檔名帶時分秒）供追溯。

    同一天多次建檔各留一份，不互相覆寫，才能回溯當天較早的版本。
    """
    result = build_master(
        attachment2_path=attachment2_path,
        submission_paths=submission_paths,
        output_path=master_path,
        registries=registries, submissions_dir=submissions_dir,
    )
    if Path(master_path).resolve() == C.CURRENT_MASTER.resolve():
        daily = C.master_path()
        daily.parent.mkdir(parents=True, exist_ok=True)
        stamp = clock.now().strftime("%H%M%S")
        snapshot = daily.with_name(f"{daily.stem}-{stamp}{daily.suffix}")
        n = 1
        while snapshot.exists():
            n += 1
            snapshot = daily.with_name(f"{daily.stem}-{stamp}-{n}{daily.suffix}")
        shutil.copy2(master_path, snapshot)
    return result


def _default_registries() -> RegistryPaths:
    return RegistryPaths(
        members=C.REG_MEMBER, distances=C.DISTANCE_CSV, attachment2=C.PORTAL_ATT2,
        committee_replies=C.STATE_DIR / committee_requests.REPLIES_FILE,
        corrections=C.STATE_DIR / corrections.FILENAME,
    )


@dataclass(frozen=True)
class OfficeFilesStatus:
    """系辦必要上傳檔目前是否存在；只看檔案在不在，不檢查內容涵蓋哪些案件。

    附件2（Portal 匯出）是案件來源，另兩份是系辦參照檔（2026-09-23：指導教授
    改由學生填寫，不再上傳師生對照表）。
    """
    attachment2: bool
    members: bool
    distances: bool

    @property
    def ready(self) -> bool:
        return self.attachment2 and self.members and self.distances


def office_files_status(registries: RegistryPaths | None = None) -> OfficeFilesStatus:
    registries = registries or _default_registries()
    return OfficeFilesStatus(
        attachment2=registries.attachment2 is not None and Path(registries.attachment2).is_file(),
        members=Path(registries.members).is_file(),
        distances=Path(registries.distances).is_file(),
    )


class _Attachment2Loader:
    """讓附件2 上傳走與參照檔相同的「能解析出資料列」檢查。"""

    def __init__(self, rows):
        self.rows = rows

    @classmethod
    def load(cls, path):
        exams, _issues = ingest.read_att2(CommitteeRegistry(), path)
        return cls(list(exams))


_OFFICE_FILE_LOADERS = {
    "attachment2": (".xlsx", _Attachment2Loader, "「學年度 / 學年學期」「學號」「姓名」「委員姓名」「委員服務單位」「委員職稱」"),
    "distances": (".csv", DistanceTable, "「服務單位」「單程公里」"),
    "members": (".xlsx", CommitteeRegistry, "「委員編號」「委員姓名」「身分證字號」"),
}


def save_office_registry_file(
    kind: str, source_path: str | Path, *, registries: RegistryPaths | None = None,
) -> Path:
    """驗證系辦上傳的建檔參照檔可被對應載入器解析出至少一列資料，再原子覆蓋正式檔。

    只做「格式對、非空」的檢查；不逐列核對欄位正確性，也不保留舊版本——
    這三份是外部權威資料的快照，上傳新版就是取代舊版。
    """
    registries = registries or _default_registries()
    targets = {"distances": registries.distances, "attachment2": registries.attachment2,
               "members": registries.members}
    if kind not in targets or targets[kind] is None:
        raise ValueError(f"未知的建檔資料種類：{kind}")
    suffix, loader_cls, required_cols = _OFFICE_FILE_LOADERS[kind]
    source_path = Path(source_path)
    if source_path.suffix.lower() != suffix:
        raise ValueError(f"檔案格式錯誤，需要 {suffix}")
    try:
        loaded = loader_cls.load(source_path)
    except Exception as exc:
        raise ValueError(f"檔案無法讀取：{exc}") from exc
    if not loaded.rows:
        raise ValueError(f"沒有解析出任何資料列，請確認欄位包含 {required_cols}")

    target = Path(targets[kind])
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.stem}-", suffix=target.suffix, dir=target.parent)
    os.close(fd)
    try:
        shutil.copy2(source_path, tmp_name)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


ATT2_UPLOADS_FILE = "att2_uploads.json"


def _load_att2_uploads(state_dir: str | Path) -> dict:
    path = Path(state_dir) / ATT2_UPLOADS_FILE
    if not path.is_file():
        return {"uploads": [], "acknowledged_removed": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("uploads", [])
    data.setdefault("acknowledged_removed", [])
    return data


@U.locked_state()
def record_att2_upload(
    state_dir: str | Path, attachment2_path: str | Path, *,
    day: _dt.date | None = None, actor: str = "office",
) -> office_settings.OfficeSettings:
    """系辦上傳附件2 後呼叫（2026-10-01）。

    記下本期第一次上傳日（正式檢核開始日與補件截止日依此起算，重傳不重算），
    並保留每次上傳的案件清單，用來找出「正式檢核開始後才出現」與「重傳後消失」的案件。
    """
    day = day or clock.today()
    settings = office_settings.record_att2_upload(state_dir, day=day, actor=actor)
    exams, _issues = ingest.read_att2(CommitteeRegistry(), attachment2_path)
    keys = sorted({exam.案件鍵 for exam in exams.values()})
    data = _load_att2_uploads(state_dir)
    data["uploads"].append({"term": settings.current_term, "date": day.isoformat(), "case_keys": keys})
    _write_json(Path(state_dir) / ATT2_UPLOADS_FILE, data)
    _audit(Path(state_dir), "attachment2", "uploaded", detail={
        "actor": actor, "date": day.isoformat(), "case_count": len(keys),
        "first_upload": settings.att2_first_uploaded.isoformat() if settings.att2_first_uploaded else None,
    })
    return settings


@dataclass(frozen=True)
class Att2Changes:
    """附件2 重傳造成的案件異動（2026-10-01 Q4）。"""
    late_added: frozenset[str]       # 正式檢核開始後重傳才出現：補件時間較短
    removed_notified: tuple[str, ...]  # 重傳後消失、但已寄過補件信：交專員確認（未確認者）


def att2_changes(
    state_dir: str | Path, settings: office_settings.OfficeSettings | None = None,
) -> Att2Changes:
    settings = settings or office_settings.load(state_dir)
    data = _load_att2_uploads(state_dir)
    uploads = [u for u in data["uploads"] if u.get("term", "") == settings.current_term]
    if not uploads:
        return Att2Changes(late_added=frozenset(), removed_notified=())
    latest = set(uploads[-1]["case_keys"])
    check_start = settings.check_start
    late_added: set[str] = set()
    if check_start is not None:
        before = [u for u in uploads if _dt.date.fromisoformat(u["date"]) < check_start]
        baseline = set(before[-1]["case_keys"]) if before else set()
        late_added = latest - baseline if before else set()
    ever = set().union(*(set(u["case_keys"]) for u in uploads))
    acknowledged = set(data["acknowledged_removed"])
    removed = (ever - latest) & notified_student_cases(state_dir)
    return Att2Changes(
        late_added=frozenset(late_added),
        removed_notified=tuple(sorted(removed - acknowledged)),
    )


@U.locked_state()
def acknowledge_att2_removed(state_dir: str | Path, case_key: str, *, actor: str, note: str = "") -> None:
    """專員確認「附件2 重傳後消失、已寄過補件信」的案件已處理。"""
    actor = actor.strip()
    if not actor:
        raise ValueError("請填寫處理人")
    data = _load_att2_uploads(state_dir)
    if case_key not in data["acknowledged_removed"]:
        data["acknowledged_removed"].append(case_key)
    _write_json(Path(state_dir) / ATT2_UPLOADS_FILE, data)
    _audit(Path(state_dir), "attachment2", "removed_case_acknowledged", tracking_key=(case_key,),
           detail={"actor": actor, "note": note})


def first_deficiency_mail_delay(
    state_dir: str | Path, settings: office_settings.OfficeSettings | None = None,
) -> int | None:
    """首次補件信比正式檢核開始日晚了幾天寄出；準時或還沒寄過時回傳 None（2026-10-01 Q6）。

    補件截止日不會因此自動順延，只在總覽警示，由系辦決定要不要手動延後。
    """
    settings = settings or office_settings.load(state_dir)
    if settings.check_start is None:
        return None
    log = send_log.load(Path(state_dir) / "send_log.json")
    sent_days = [
        _dt.datetime.strptime(event.sent_at, "%Y-%m-%d %H:%M:%S").date()
        for event in log.all()
        if event.status == send_log.SENT and event.type == notify.TYPE_STUDENT_DEFICIENCY and event.sent_at
    ]
    if not sent_days:
        return None
    delay = (min(sent_days) - settings.check_start).days
    return delay if delay > 0 else None


def open_cases_for_term(master: Master, state_dir: str | Path, term: str) -> list[str]:
    """該學期還沒結案的案件鍵：有未解決缺件或附件6 未收回（切換學期前提醒用）。

    2026-10-12 起教授不用回覆附件4，結案不看教授是否回信。
    """
    tracking = tracking_snapshot(master=master, state_dir=state_dir, term=term)
    keys: set[str] = {item.case_key for item in tracking.deficiency.items}
    for item in tracking.collection.items:
        if item.status != "collected":
            keys.update(item.case_keys)
    return sorted(keys)


@dataclass(frozen=True)
class AutoBuildResult:
    """一次「檢查是否可建檔」的結果；skipped 代表三檔尚未備齊，不是錯誤。

    busy 代表另一個建檔正在進行、等不到鎖：觸發建檔的動作（學生送出、系辦上傳）
    已經存檔，主檔會在下一次建檔時更新（review-2026-09-23 P2-19）。
    """
    status: str  # "skipped" | "built" | "failed" | "busy"
    build: BuildResult | None = None
    error: str = ""


@U.locked_state("submissions_dir")
def auto_build_if_ready(
    *,
    submissions_dir: str | Path,
    master_path: str | Path,
    registries: RegistryPaths | None = None,
    attachment2_path: str | Path | None = None,
    status_path: str | Path | None = None,
) -> AutoBuildResult:
    """核准或系辦建檔資料有變動後呼叫；三檔未齊或建檔失敗都不影響已完成的動作。

    建檔失敗時保留先前主檔不動——`build_current_master` 只有在全部解析完成
    後才會寫檔，失敗會在寫檔前拋出例外，不會留下半份主檔。建檔區段持有
    build_lock，多 worker 部署時同時到的 request 會排隊而非各自讀寫。
    """
    registries = registries or _default_registries()
    if not office_files_status(registries).ready:
        result = AutoBuildResult(status="skipped")
    else:
        try:
            with U.build_lock(master_path):
                paths = ingest.student_submission_paths(submissions_dir)
                try:
                    build = build_current_master(
                        submission_paths=paths, master_path=master_path,
                        registries=registries, attachment2_path=attachment2_path,
                        submissions_dir=submissions_dir,
                    )
                except Exception as exc:
                    result = AutoBuildResult(status="failed", error=str(exc))
                else:
                    current = source_state.require_current(master_path)
                    source_state.publish(master_path, source_state.fingerprint(current["sources"], [submissions_dir]), [submissions_dir])
                    source_state.bind(build.master, master_path)
                    result = AutoBuildResult(status="built", build=build)
        except U.BuildLockTimeout as exc:
            result = AutoBuildResult(status="busy", error=str(exc))

    # busy 沒有新結果，保留上一次的建檔狀態。
    if status_path is not None and result.status != "busy":
        with U.build_lock(master_path):
            if result.build is not None:
                try:
                    source_state.ensure_generation(result.build.master)
                except ValueError:
                    return AutoBuildResult(status="busy", error="已有較新的來源世代，未回寫舊結果")
            _write_json(Path(status_path), {
                "status": result.status,
                "at": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
                "case_count": len(result.build.master.exams) if result.build else 0,
                "issue_count": len(result.build.issues) if result.build else 0,
                "error": result.error,
            })
    return result


def load_auto_build_status(status_path: str | Path) -> dict | None:
    path = Path(status_path)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_current_master(master_path: str | Path) -> Master:
    master = master_workbook.load(master_path)
    if master is None:
        raise FileNotFoundError(f"找不到主檔：{master_path}")
    return source_state.bind(master, master_path)


def check_current_master(master_path: str | Path) -> list[dict[str, object]]:
    return validation.check(load_current_master(master_path))


def _unreviewed_case_issues(master: Master, state_dir: str | Path) -> list[dict[str, object]]:
    """專員尚未放行（或放行後委員名單變動而失效）的案件：不產附件4／6
    （已確認流程決策第四節第 5 條；2026-10-01 Q9）。"""
    reviews = case_review.load(state_dir)
    cleared = case_review.cleared_keys(master, state_dir)
    return [
        {
            "案件鍵": exam.案件鍵, "學期": exam.學期, "學號": exam.學號, "姓名": exam.姓名,
            "嚴重度": "阻擋", "問題代碼": "CASE_REVIEW_PENDING", "欄位": "人工關卡",
            "說明": ("放行後委員名單有變動，需專員重新審核並放行" if exam.案件鍵 in reviews
                     else "專員尚未審核委員資格並按「已處理」放行"),
            "阻擋產出": "是", "阻擋附件4": "是", "阻擋附件6": "是",
            "來源": "人工關卡", "責任": validation.OWNER_OFFICE,
        }
        for exam in master.exams
        if exam.生效 and exam.案件鍵 not in cleared
    ]


@dataclass(frozen=True)
class ReviewRow:
    """人工關卡清單的一列：一個案件、它的例外，以及放行狀態。"""
    case_key: str
    student_id: str
    student_name: str
    exceptions: list[dict[str, object]]
    review: case_review.CaseReview | None
    changed_since_clear: bool
    new_exceptions: list[dict[str, object]] = field(default_factory=list)

    @property
    def pending(self) -> bool:
        """待放行：從沒放行過，或放行後委員名單變動而失效（2026-10-01 Q9）。"""
        return self.review is None or self.changed_since_clear


def review_queue(
    master: Master, state_dir: str | Path, *, today: _dt.date | None = None,
) -> list[ReviewRow]:
    """每個生效案件一列；例外＝系辦責任的檢核問題（含職級不符、名單不一致），
    以及已放行後、口試將至仍未回填個資的委員。"""
    today = today or clock.today()
    reviews = case_review.load(state_dir)
    issues = validation.check(master) + corrections.load_conflicts(state_dir)
    earliest = earliest_feasible_exam_date(office_settings.load(state_dir))
    for exam in master.exams:
        if earliest and exam.口試日期 and exam.口試日期 < earliest:
            issues.append({
                "案件鍵": exam.案件鍵, "問題代碼": "EXAM_DATE_TOO_EARLY", "欄位": "口試日期",
                "責任": validation.OWNER_OFFICE,
                "說明": f"口試日期 {exam.口試日期} 早於補件截止日加預留教授核對天數（{earliest}），附件4 可能來不及在口試前讓教授核對",
            })
    settings = office_settings.load(state_dir)
    if settings.phase_on(today) == office_settings.PHASE_CLOSED:
        # 補件截止後仍有學生責任問題：逾期未完成，由專員個案處理（2026-10-01 Q13）
        overdue_cases = {i.case_key for i in load_from_dir(Path(state_dir)).deficiency if i.owner == STUDENT}
        for case_key in sorted(overdue_cases):
            issues.append({
                "案件鍵": case_key, "問題代碼": "STUDENT_OVERDUE_INCOMPLETE", "欄位": "補件",
                "責任": validation.OWNER_OFFICE,
                "說明": f"補件截止日（{settings.deficiency_deadline}）已過仍有學生責任問題未補（逾期未完成）："
                        "可在下方修正欄位後放行，或對此案開放修改、延長期限",
            })
    for request in committee_requests.plan(master, state_dir):
        if not request.missing_before_exam(today, settings.committee_exception_lead_days):
            continue
        for case_key in request.case_keys:
            issues.append({
                "案件鍵": case_key, "問題代碼": "COMMITTEE_DATA_MISSING_BEFORE_EXAM",
                "欄位": f"委員：{request.name}", "責任": validation.OWNER_OFFICE,
                "說明": f"口試（{request.earliest_exam}）將至，委員仍未提供撥帳資料；可於口試現場以附件6 補齊",
            })
    rows = []
    for exam in master.exams:
        if not exam.生效:
            continue
        review = reviews.get(exam.案件鍵)
        exceptions = [i for i in issues
                      if i.get("案件鍵") == exam.案件鍵 and i.get("責任") == validation.OWNER_OFFICE]
        rows.append(ReviewRow(
            case_key=exam.案件鍵, student_id=exam.學號, student_name=exam.姓名,
            exceptions=exceptions,
            review=review,
            changed_since_clear=bool(review) and case_review.changed_since_clear(review, exam, master),
            new_exceptions=case_review.new_exceptions(review, exceptions) if review else [],
        ))
    return rows


# 對外網址（例如 https://koushi.example.edu.tw）；委員索取信的專屬連結用它組成。
PUBLIC_URL_ENV = "KOUSHI_PUBLIC_URL"


@U.locked_state()
def process_committee_requests(
    *, master: Master, state_dir: str | Path, send: bool = False,
    transport: Callable[..., bool | None] | None = None, date: _dt.date | None = None,
) -> tuple[list[dict], list[dict]]:
    """已放行案件中撥帳資料不齊的委員：產生索取個資信，send=True 時寄出。

    已回填或已寄過的委員不再產生索取信；寄送走 send_log 去重，同一委員、同一組
    案件不會重寄。已寄過索取信、還沒回填的委員，若綁定的案件組變了（例如加入第二案，
    舊連結因此失效），寄一封「填寫連結更新」信附新連結（決策五-5：連結要能用到委員
    填寫為止）；這不是催件，同一組綁定只寄一次。回傳 (草稿, 已寄出)。
    """
    sd = Path(state_dir)
    source_state.ensure_generation(master)
    master = _active_master(master, office_settings.load(sd))
    names = {exam.案件鍵: exam.姓名 for exam in master.exams}
    planned = committee_requests.plan(master, sd)
    pending = [r for r in planned if not r.received and r.sent_date is None]
    if send:
        log = send_log.load(sd / "send_log.json")
        remaining = []
        for req in pending:
            candidate = committee_requests.draft(req, student_names=names)
            event = log.get(candidate["idempotency_key"])
            if event and event.status == send_log.SENT:
                committee_requests.mark_sent(sd, [req], date=_sent_event_date(event))
                _audit(sd, "committee_request", "sent_recovered", tracking_key=(req.key,))
            else:
                remaining.append(req)
        pending = remaining
    base_url = os.environ.get(PUBLIC_URL_ENV, "").strip()
    drafts = [
        committee_requests.draft(r, student_names=names, link=committee_requests.reply_url(
            base_url, committee_requests.link_token(sd, r.email, request=r)) if base_url else None)
        for r in pending
    ]
    sent: list[dict] = []
    log_path = sd / "send_log.json"
    if send and drafts:
        log = send_log.load(log_path)
        for d in drafts:
            try:
                sent.extend(notify.send_drafts([d], log=log, transport=transport))
            finally:
                send_log.save(log, log_path)
            event = log.get(d["idempotency_key"])
            if event and event.status == send_log.SENT:
                mailed = [r for r in pending if r.key == d["tracking"]["email"]]
                committee_requests.mark_sent(sd, mailed, date=date or clock.today())
                if base_url:
                    for r in mailed:
                        committee_requests.record_link_mailed(sd, r)
                _audit(sd, "committee_request", "sent", tracking_key=(d["tracking"]["email"],))
    # 案件組變動 → 舊連結失效：寄「連結更新」（沒有對外網址就沒有連結，委員本來就是回信）
    updates = committee_requests.link_updates_needed(planned, sd) if base_url else []
    for req in updates:
        d = committee_requests.draft_link_update(req, student_names=names, link=committee_requests.reply_url(
            base_url, committee_requests.link_token(sd, req.email, request=req)))
        drafts.append(d)
        if not send:
            continue
        log = send_log.load(log_path)
        event = log.get(d["idempotency_key"])
        if not (event and event.status == send_log.SENT):
            try:
                sent.extend(notify.send_drafts([d], log=log, transport=transport))
            finally:
                send_log.save(log, log_path)
            event = log.get(d["idempotency_key"])
        if event and event.status == send_log.SENT:
            committee_requests.record_link_mailed(sd, req)
            _audit(sd, "committee_request", "link_updated", tracking_key=(req.key,),
                   detail={"case_keys": list(req.case_keys)})
    return drafts, sent


CONFIRMATION_ACTIONS = ("sent", "confirmed", "modified")
COLLECTION_ACTIONS = ("sent", "collected", "needs_correction")


def request_token(request_key: tuple) -> str:
    """確認請求／附件6 項目的網頁用識別字串（學期|學位|指導教授|日期）。"""
    return "|".join(str(part) for part in request_key)


def _current_engine_lists(master: Master, state_dir: Path, *, reset_versions=True):
    state = load_from_dir(state_dir)
    confirmation = (confirmation_reconcile(master, state.confirmation)
                    if state.confirmation else confirmation_requests(master))
    if reset_versions:
        confirmation, _ = advisor_confirmation.reset_changed(master, confirmation, state_dir)
    collection = (collection_reconcile(master, state.collection)
                  if state.collection else collection_items(master))
    return state, confirmation, collection


@U.locked_state()
def record_confirmation(
    *, master_path: str | Path, state_dir: str | Path, token: str, action: str,
    actor: str = "office", note: str = "", date: _dt.date | None = None,
    expected_version: str | None = None,
) -> None:
    """系辦登錄附件4 教授確認（review-2026-09-23 P0-1）。

    自動寄送與 LLM 判讀回信完成前，這是唯一能讓案件通過教授確認關卡的方式；
    之後保留作為人工覆核入口（已確認流程決策第六節）。
    """
    master = load_current_master(master_path)
    _, requests, _ = _current_engine_lists(master, Path(state_dir))
    target = next((r for r in requests if request_token(r.request_key) == token), None)
    if target is None:
        raise ValueError("找不到目前的教授確認請求")
    _assert_active_case(target.case_keys[0], state_dir)
    if expected_version is not None and expected_version != advisor_confirmation.content_version(master, target):
        raise ValueError("附件4已改版，請重新核對新版後登錄確認")
    _apply_confirmation(master, Path(state_dir), token=token, action=action,
                        actor=actor, note=note, date=date)


@U.locked_state("sd")
def _apply_confirmation(
    master: Master, sd: Path, *, token: str, action: str, actor: str, note: str = "",
    date: _dt.date | None = None,
) -> None:
    if action not in CONFIRMATION_ACTIONS:
        raise ValueError(f"未知的確認動作：{action}")
    date = date or clock.today()
    state, confirmation, collection = _current_engine_lists(master, sd)
    settings = office_settings.load(sd)
    updated, found = [], False
    for req in confirmation:
        if request_token(req.request_key) != token:
            updated.append(req)
            continue
        found = True
        if action == "sent":
            deadline = Deadline(base_date=date, business_days=settings.confirmation_days,
                                computed_date=settings.add_days(date, settings.confirmation_days))
            updated.extend(confirmation_mark_sent([req], date=date, deadline=deadline))
        else:
            updated.append(record_response(req, CONFIRMED if action == "confirmed" else MODIFIED,
                                           date=date, notes=note.strip()))
    if not found:
        raise ValueError("找不到這筆教授確認（主檔可能已更新，請重新整理）")
    state.confirmation, state.collection = updated, collection
    save_to_dir(state, sd)
    target = next(r for r in updated if request_token(r.request_key) == token)
    if action in ("sent", "confirmed"):
        records = advisor_confirmation.load_mail_state(sd)
        ref = advisor_confirmation.ref_code(target)
        previous = records.get(ref, {})
        records[ref] = {**previous, "request_key": list(target.request_key),
            "version": advisor_confirmation.content_version(master, target),
            "to": previous.get("to", ""), "sent_date": previous.get("sent_date", date.isoformat()),
            "rounds": previous.get("rounds", 0)}
        advisor_confirmation._save_mail_state(sd, records)
    _audit(sd, "confirmation", action, tracking_key=tuple(token.split("|")),
           detail={"actor": actor, "note": note.strip()})


def _confirmation_deadline(settings: office_settings.OfficeSettings, date: _dt.date) -> Deadline:
    return Deadline(base_date=date, business_days=settings.confirmation_days,
                    computed_date=settings.add_days(date, settings.confirmation_days))


def _sent_event_date(event: send_log.SendEvent) -> _dt.date:
    """Recovery must retain the successful delivery date, not the rerun date."""
    return _dt.date.fromisoformat(event.sent_at[:10])


def _record_advisor_delivery(master: Master, sd: Path, event: send_log.SendEvent,
                             settings: office_settings.OfficeSettings, *, date: _dt.date | None = None) -> None:
    """Idempotently reconcile one persisted delivery with its current request."""
    state, confirmation, collection = _current_engine_lists(master, sd)
    records = advisor_confirmation.load_mail_state(sd)
    updated, changed = [], False
    for req in confirmation:
        tracking = event.tracking
        ref = advisor_confirmation.ref_code(req)
        previous = records.get(ref, {})
        round_no = int(tracking.get("round", 0))
        if (event.type != advisor_confirmation.TYPE_ATTACHMENT4 or event.status != send_log.SENT
                or tracking.get("ref") != ref
                or tracking.get("request_key") != list(req.request_key)
                or tracking.get("version") != advisor_confirmation.content_version(master, req)
                or round_no + 1 < int(previous.get("rounds", 0))
                or req.response in (CONFIRMED, MODIFIED)):
            updated.append(req)
            continue
        if req.sent_date is not None and previous.get("delivery_key") == event.idempotency_key:
            updated.append(req)
            continue
        sent_date = req.sent_date or date or _sent_event_date(event)
        advisor_confirmation.record_sent(
            sd, req, version=tracking["version"], to=event.to, date=sent_date,
            round_no=round_no, delivery_key=event.idempotency_key,
        message_id=notify.message_id_for(event.idempotency_key), sent_at=event.sent_at,
        )
        updated.extend(confirmation_mark_sent(
            [req], date=sent_date, deadline=_confirmation_deadline(settings, sent_date),
        ))
        changed = True
    if changed:
        state.confirmation, state.collection = updated, collection
        save_to_dir(state, sd)
        _audit(sd, "confirmation", "sent" if date else "sent_recovered",
               tracking_key=tuple(event.tracking["request_key"]),
               detail={"actor": "system", "ref": event.tracking["ref"],
                       "version": event.tracking["version"], "delivery_key": event.idempotency_key})


@dataclass(frozen=True)
class AdvisorMailResult:
    drafts: list[dict]
    sent: list[dict]
    reset: list[str]  # 因附件4 內容變動而重設、重寄的請求代碼
    notes: tuple[str, ...] = ()


@U.locked_state()
def process_advisor_confirmations(
    *, master: Master, state_dir: str | Path, submissions_dir: str | Path | None = None,
    send: bool = False, transport: Callable[..., bool | None] | None = None,
    date: _dt.date | None = None, settings: office_settings.OfficeSettings | None = None,
    template: str | Path | None = None, attachment_dir: str | Path | None = None,
) -> AdvisorMailResult:
    """附件4 寄給指導教授參考（已確認流程決策第六節；2026-10-10 起全學期一份）。

    2026-10-12 起教授不用回信、不催、不擋後續步驟；寄送關卡見
    `advisor_confirmation.sendable_keys`（學生資料齊全即寄，不等專員放行）。
    先把「自己那幾列」已變動的教授重設為草稿，再對「名下案件全部通過寄送關卡、尚未寄出」的
    教授產生附件4 信；send=True 時產生（或沿用目前最新的）全學期附件4，每位教授附同一份寄出，
    寄出成功才標為已寄出。別位教授的案件變動不會重寄給已寄出的教授；但全學期內容已變時，
    曾產過檔的學期會重產檔案，讓文件中心記錄的附件4 保持最新。
    附件4 範本轉檔失敗時本步驟不寄（記在 notes），不影響其他每日工作。
    """
    sd = Path(state_dir)
    today = date or clock.today()
    settings = settings or office_settings.load(sd)
    source_state.ensure_generation(master)
    master = _active_master(master, settings)
    state, confirmation, collection = _current_engine_lists(master, sd, reset_versions=False)
    confirmation, reset = advisor_confirmation.reset_changed(master, confirmation, sd)
    if reset:
        state.confirmation, state.collection = confirmation, collection
        save_to_dir(state, sd)
        for req in reset:
            _audit(sd, "confirmation", "reset_for_new_version",
                   tracking_key=req.request_key, detail={"ref": advisor_confirmation.ref_code(req)})

    if send:
        for event in sorted(send_log.load(sd / "send_log.json").by_status(send_log.SENT),
                            key=lambda e: (int(e.tracking.get("round", 0)), e.sent_at or "")):
            if event.type == advisor_confirmation.TYPE_ATTACHMENT4:
                _record_advisor_delivery(master, sd, event, settings)
        state, confirmation, collection = _current_engine_lists(master, sd, reset_versions=False)

    blocked = {i["案件鍵"] for i in _document_eligibility_issues(master, sd)}
    ready = [r for r in advisor_confirmation.ready_to_send(master, confirmation, sd)
             if not blocked.intersection(r.case_keys)]
    case_advisors: dict[str, dict[str, str]] = {}
    if submissions_dir is not None:
        bundle, _ = _submission_contacts(list(ingest.student_submission_paths(submissions_dir)),
                                         {e.案件鍵 for e in master.exams})
        case_advisors = bundle["case_advisors"]
    contacts = _effective_contacts(master, {"case_advisors": case_advisors, "advisors": recipients.load_contacts()["advisors"]})
    notes: list[str] = []

    def build_drafts(attachments: dict[str, Path]) -> list[dict]:
        return [
            advisor_confirmation.draft(
                req, master,
                to=recipients.resolve_advisor(req.advisor, contacts, case_keys=req.case_keys),
                attachment=attachments.get(req.term),
                round_no=advisor_confirmation.sent_rounds(sd, req),
            )
            for req in ready
        ]

    reset_refs = [advisor_confirmation.ref_code(r) for r in reset]
    if not send:
        return AdvisorMailResult(drafts=build_drafts({}), sent=[], reset=reset_refs)

    # 全學期附件4：收錄本學期所有通過寄送關卡（與寄送同一個條件 sendable_keys）的案件。
    included = advisor_confirmation.sendable_keys(master, sd) - blocked
    documents = advisor_confirmation.load_documents(sd)
    awaiting = {r.term for r in confirmation if r.sent_date and r.response not in (CONFIRMED, MODIFIED)}
    attachments: dict[str, Path] = {}
    to_write: dict[str, tuple[Master, str]] = {}
    for term in sorted({r.term for r in ready} | awaiting):
        doc_master = advisor_confirmation.document_master(master, term, included)
        version = advisor_confirmation.document_version(doc_master)
        record = documents.get(term) or {}
        path = Path(record.get("path", ""))
        current = (record.get("version") == version and path.is_file()
                   and set(record.get("case_keys", ())) == {e.案件鍵 for e in doc_master.exams})
        if current:
            attachments[term] = path
        elif doc_master.exams and (term in {r.term for r in ready} or record):
            # 要寄的學期一定要有檔；已寄出的學期，曾產過檔才重產（保持記錄的附件4 為最新版）
            to_write[term] = (doc_master, version)
    if not ready and not to_write:
        return AdvisorMailResult(drafts=[], sent=[], reset=reset_refs)

    if to_write:
        from .document_conversion import docx_template
        out_dir = Path(attachment_dir or sd / "attachment4_outbox")
        try:
            with docx_template(template or C.TPL_ATT4) as docx:
                for term, (doc_master, version) in to_write.items():
                    path = advisor_confirmation.write_term_document(
                        doc_master, term, template=docx, output_dir=out_dir)
                    advisor_confirmation.record_document(
                        sd, term, path=path, version=version,
                        case_keys=[e.案件鍵 for e in doc_master.exams])
                    attachments[term] = path
        except (RuntimeError, OSError, ValueError) as exc:
            notes.append(f"附件4 產檔失敗，本次未寄給指導教授：{exc}" if ready
                         else f"全學期附件4 更新失敗：{exc}")
            return AdvisorMailResult(drafts=build_drafts({}), sent=[], reset=reset_refs, notes=tuple(notes))
    if not ready:
        return AdvisorMailResult(drafts=[], sent=[], reset=reset_refs, notes=tuple(notes))

    drafts = build_drafts(attachments)
    log_path = sd / "send_log.json"
    log = send_log.load(log_path)
    sent = []
    for d in drafts:
        try:
            sent.extend(notify.send_drafts([d], log=log, transport=transport))
        finally:
            send_log.save(log, log_path)
        event = log.get(d["idempotency_key"])
        if event is None or event.status != send_log.SENT:
            continue
        _record_advisor_delivery(master, sd, event, settings, date=today)
    no_recipient = [d for d in drafts if d["status"] == STATUS_NO_RECIPIENT]
    if no_recipient:
        notes.append(f"{len(no_recipient)} 位指導教授查無 E-mail，未寄附件4")
    return AdvisorMailResult(drafts=drafts, sent=sent, reset=reset_refs, notes=tuple(notes))


def _preclassified(mails, classifier, requests, state_dir):
    """在鎖外先呼叫教授回信判讀器（審查 F09），回傳只重播結果、不再呼叫模型的 classifier。"""
    seen = {i.message_id for i in advisor_confirmation.load_inbox(state_dir)}
    results = {}
    for mail in mails:
        if not mail.message_id or mail.message_id in seen or not (mail.reply_text or "").strip():
            continue
        request, _ref, _notes = advisor_confirmation.match_request(mail, requests, state_dir)
        if request is None:
            continue
        try:
            results[mail.message_id] = classifier(mail, request)
        except Exception as exc:  # noqa: BLE001 — 錯誤原樣交給既有流程轉人工
            results[mail.message_id] = exc

    def replay(mail, request):
        outcome = results.get(mail.message_id)
        if outcome is None:
            raise advisor_confirmation.ClassifierError("這封信在判讀後才出現（並行），請專員判讀")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    return replay


def ingest_advisor_replies(
    *, master: Master, state_dir: str | Path, mails: list,
    classifier: advisor_confirmation.Classifier | None = None, date: _dt.date | None = None,
) -> list[advisor_confirmation.InboxItem]:
    """教授回信存入收件匣並做確定性核對；classifier 明確判為確認且核對通過者自動記為已確認。

    AI 總開關關閉時不做 AI 判讀（classifier 視同未設定），每封回信都交系辦判讀。
    """
    sd = Path(state_dir)
    reason = ""
    if ai_switch.is_disabled(sd):
        classifier = None
        reason = "AI 總開關已關閉：回信判讀交專員，因此未自動確認"
    _, confirmation, _ = _current_engine_lists(master, sd)
    if classifier is not None:
        classifier = _preclassified(mails, classifier, confirmation, sd)
    with U.state_lock(sd):
        return _ingest_advisor_replies_locked(master=master, sd=sd, mails=mails, classifier=classifier,
                                              reason=reason, date=date)


def _ingest_advisor_replies_locked(*, master, sd, mails, classifier, reason, date):
    _, confirmation, _ = _current_engine_lists(master, sd)
    by_ref = {advisor_confirmation.ref_code(r): r for r in confirmation}
    for item in advisor_confirmation.load_inbox(sd):
        req = by_ref.get(item.ref)
        if (item.status == advisor_confirmation.INBOX_PENDING and req
                and req.response == CONFIRMED and req.notes == "LLM 自動放行：" + item.message_id
                and item.content_version == advisor_confirmation.content_version(master, req)):
            advisor_confirmation.mark_handled(sd, item.message_id,
                status=advisor_confirmation.INBOX_APPLIED, actor="auto_confirm_recovered")
    added, auto = advisor_confirmation.ingest_replies(
        mails, master=master, requests=confirmation, state_dir=sd,
        token_of=lambda req: request_token(req.request_key), classifier=classifier,
        no_classifier_reason=reason,
    )
    decisions = advisor_confirmation.evaluate_auto_confirms(
        auto, master=master, requests=confirmation, state_dir=sd,
        settings=office_settings.load(sd),
    )
    updated, applied = advisor_confirmation.execute_auto_confirms(
        decisions, requests=confirmation, state_dir=sd, date=date, master=master,
    )
    notes = {d.item.message_id: advisor_confirmation.REJECT_LABELS.get(d.reason, d.reason)
             for d in decisions if not d.approved and d.reason}
    if notes:
        inbox = advisor_confirmation.load_inbox(sd)
        for item in inbox:
            if item.message_id in notes and item.status == advisor_confirmation.INBOX_PENDING:
                item.auto_note = notes[item.message_id]
        advisor_confirmation.save_inbox(sd, inbox)
    if applied:
        state = load_from_dir(sd)
        state.confirmation = updated
        save_to_dir(state, sd)
    return added


def run_mailbox_agent(
    *, master: Master, state_dir: str | Path, mails: list, llm=None, send: bool = False,
    transport: Callable[..., bool | None] | None = None, date: _dt.date | None = None,
    settings: office_settings.OfficeSettings | None = None,
) -> list[dict]:
    """系辦信箱 agent：每一封來信都分析、連結案件、提出建議（docs/信箱agent設計.md）。

    須在 ingest_advisor_replies() 之後呼叫：附件4 確認與自動放行仍只由既有流程決定，
    agent 讀取其結果，不會自己記為確認。AI 總開關關閉時由 agent 自行停用模型與自動動作。
    """
    from . import mailbox_agent
    sd = Path(state_dir)
    _, confirmation, _ = _current_engine_lists(master, sd)
    return mailbox_agent.process_mails(
        mails, master=master, state_dir=sd, requests=confirmation,
        token_of=lambda req: request_token(req.request_key), llm=llm,
        settings=settings, send=send, transport=transport, today=date,
    )


INBOX_ACTIONS = ("confirmed", "modified", "ignore")


@U.locked_state()
def handle_advisor_reply(
    *, master_path: str | Path, state_dir: str | Path, message_id: str, action: str, actor: str,
    note: str = "", date: _dt.date | None = None,
) -> str:
    """系辦判讀一封教授回信：確認無誤／需修改（寫入教授確認）或忽略。

    回傳給畫面的結果說明。處理後，信箱 agent 裡同一封信的「到文件中心判讀」建議一併結案。
    疑似夾帶操縱 AI 指令的回信不能直接記為確認或需修改（只能忽略，改以其他管道確認後手動登錄）。
    """
    from . import mailbox_agent
    if action not in INBOX_ACTIONS:
        raise ValueError(f"未知的處理方式：{action}")
    sd = Path(state_dir)
    item = next((i for i in advisor_confirmation.load_inbox(sd) if i.message_id == message_id), None)
    if item is None or item.status != advisor_confirmation.INBOX_PENDING:
        raise ValueError("找不到這封待判讀的回信（可能已被處理）")
    if action == "ignore":
        advisor_confirmation.mark_handled(sd, message_id, status=advisor_confirmation.INBOX_IGNORED, actor=actor)
        _audit(sd, "confirmation_inbox", "ignored", tracking_key=(item.ref or "",), detail={"actor": actor})
        synced = mailbox_agent.close_from_documents(sd, message_id=message_id, actor=actor, outcome="忽略這封回信")
        return "；".join(x for x in ("已忽略這封回信", synced) if x)
    if not item.token:
        raise ValueError("這封回信對不到目前的確認請求，只能忽略；如內容有效請請教授回覆最新版本")
    if mailbox_agent.looks_like_injection(item.reply_text) or mailbox_agent.looks_like_injection(item.body):
        raise ValueError("這封回信疑似夾帶操縱 AI 的指令，不能直接記為確認或需修改；"
                         "請以電話等其他管道向教授確認後，在「教授確認登錄」手動登錄，並忽略這封回信")
    master = load_current_master(master_path)
    _, requests, _ = _current_engine_lists(master, sd)
    target = next((r for r in requests if request_token(r.request_key) == item.token), None)
    record = advisor_confirmation.load_mail_state(sd).get(item.ref, {})
    fresh_checks = advisor_confirmation.check(advisor_confirmation.mail_from_item(item), target, master, sd) if target else {}
    if (target is None or not fresh_checks or not all(fresh_checks.values()) or not item.content_version
            or item.content_version != advisor_confirmation.content_version(master, target)
            or item.sent_round != record.get("rounds")):
        raise ValueError("這封回信無法確認目前版本；請取得新版回覆，或另行登錄線下確認證據")
    _apply_confirmation(master, sd, token=item.token, action=action,
                        actor=actor, note=note or item.intent_detail, date=date)
    advisor_confirmation.mark_handled(sd, message_id, status=advisor_confirmation.INBOX_APPLIED, actor=actor)
    outcome = ("記為教授已確認（附件4 確認無誤）" if action == "confirmed"
               else "記為教授要求修改附件4")
    synced = mailbox_agent.close_from_documents(sd, message_id=message_id, actor=actor, outcome=outcome)
    who = f"{target.advisor} 老師（{U.term_year_label(target.term)}附件4）"
    return "；".join(x for x in (f"已將 {who}{outcome}", synced) if x)


_CHECK_FIXES = {
    "寄件者為收件教授": "寄件者不是當初的收件教授（身分未驗證）",
    "附件4 為目前版本": "無法核對教授回覆的是目前版本的附件4",
    "回覆本輪寄送信件": "不是回覆本輪寄出的附件4 信件",
    "可信收件時間在寄出之後": "收件時間無法驗證，或早於本輪附件4 寄出時間",
    "已寄出且尚未登錄回覆": "這位教授的附件4 目前不是「已寄出、尚未登錄回覆」（已登錄確認／需修改，或尚未寄出）",
}


def advisor_reply_cards(*, master_path: str | Path, state_dir: str | Path) -> dict:
    """文件中心「教授回信待判讀」的畫面資料：以**目前**狀態重算核對，並帶入信箱 agent 的警示。

    - 回覆的是已改版／已重寄的舊版附件4 → 列為「已失效」，只能忽略（不再顯示當初通過的核對）。
    - 疑似夾帶指令、寄件者身分未驗證、核對未通過 → 不提供「確認無誤」（伺服器端也會拒絕）。
    回傳 {"actionable": [...], "stale": [...]}，每張卡片是 {"item", "checks", "badges", "blockers", ...}。
    """
    from . import mailbox_agent
    sd = Path(state_dir)
    pending = [i for i in advisor_confirmation.load_inbox(sd) if i.status == advisor_confirmation.INBOX_PENDING]
    if not pending:
        return {"actionable": [], "stale": []}
    mails = {m["message_id"]: m for m in mailbox_agent.load_mails(sd)}
    master, by_ref = None, {}
    try:
        master = load_current_master(master_path)
        _, requests, _ = _current_engine_lists(master, sd)
        by_ref = {advisor_confirmation.ref_code(r): r for r in requests}
    except (FileNotFoundError, ValueError):
        pass
    records = advisor_confirmation.load_mail_state(sd)
    actionable, stale = [], []
    for item in pending:
        checks, stale_reason = dict(item.checks), ""
        req = by_ref.get(item.ref) if item.token else None
        if item.token and master is not None:
            if req is None or request_token(req.request_key) != item.token:
                stale_reason = "舊版附件4 的回覆，已失效：這位教授的附件4 內容已改版或已重寄新版"
                checks = {}
            else:
                checks = advisor_confirmation.check(advisor_confirmation.mail_from_item(item), req, master, sd)
                record = records.get(item.ref, {})
                if (item.content_version != advisor_confirmation.content_version(master, req)
                        or item.sent_round != record.get("rounds")):
                    stale_reason = "舊版附件4 的回覆，已失效：附件4 已改版重寄，請等教授回覆新版"
                    checks = {}
                elif req.response in (CONFIRMED, MODIFIED):
                    stale_reason = "這筆教授確認已登錄（已確認或需修改），這封回信不需再判讀"
        mail = mails.get(item.message_id)
        analysis = (mail or {}).get("analysis") or {}
        suspicious = bool(mailbox_agent.looks_like_injection(item.reply_text)
                          or mailbox_agent.looks_like_injection(item.body)
                          or (mail and (mail.get("injection_flag") or analysis.get("suspicious"))))
        sender_verified = (mail is None or bool(mail["sender"].get("verified"))) and             checks.get("寄件者為收件教授", bool(item.checks.get("寄件者為收件教授")))
        badges = []
        if mail:
            badges.append(("info", "信箱 agent：" + mailbox_agent.CATEGORY_LABELS.get(
                analysis.get("category"), analysis.get("category") or "未分類")))
        if suspicious:
            badges.append(("missing", "疑似夾帶指令"))
        if not sender_verified:
            badges.append(("missing", "身分未驗證"))
        blockers = []
        if stale_reason:
            blockers.append(stale_reason)
        if suspicious:
            blockers.append("疑似夾帶操縱 AI 的指令：請以電話等其他管道向教授確認後，在下方「教授確認登錄」手動登錄，再忽略這封回信")
        if not item.token:
            blockers.append("對不到目前的確認請求")
        elif not stale_reason:
            blockers.extend(_CHECK_FIXES.get(name, name) for name, ok in checks.items() if not ok)
        card = {"item": item, "checks": checks, "badges": badges, "blockers": blockers,
                "suspicious": suspicious, "sender_verified": sender_verified,
                "can_decide": bool(item.token) and not blockers, "stale": bool(stale_reason),
                "mailbox_status": mailbox_agent.MAIL_STATUS_LABELS.get((mail or {}).get("status"), "")}
        (stale if stale_reason else actionable).append(card)
    return {"actionable": actionable, "stale": stale}


def advisor_request_states(*, master_path: str | Path, state_dir: str | Path) -> dict[str, str]:
    """各附件4 請求代碼的**目前**狀態（信箱 agent 頁顯示用；紀錄裡的是收信當下的快照）。

    pending／confirmed／modified／not_sent，另外：
    - resend_pending：寄過舊版，附件4 改版後新版尚未寄出（教授回的是舊版）
    """
    sd = Path(state_dir)
    try:
        master = load_current_master(master_path)
        _, requests, _ = _current_engine_lists(master, sd)
    except (FileNotFoundError, ValueError):
        return {}
    records = advisor_confirmation.load_mail_state(sd)
    states = {}
    for req in requests:
        ref = advisor_confirmation.ref_code(req)
        if req.response == CONFIRMED:
            states[ref] = "confirmed"
        elif req.response == MODIFIED:
            states[ref] = "modified"
        elif req.sent_date:
            states[ref] = "pending"
        elif records.get(ref, {}).get("sent_date"):
            states[ref] = "resend_pending"
        else:
            states[ref] = "not_sent"
    return states


def _committees_missing_banking(master: Master, case_keys) -> list[str]:
    by_key = master.by_key()
    names: list[str] = []
    for case_key in case_keys:
        exam = by_key.get(case_key)
        if exam is None:
            continue
        for member in exam.委員:
            committee = master.committees.get(member.committee_id)
            if committee is not None and not committee.banking_complete(member.服務單位 or committee.服務單位) and committee.委員姓名 not in names:
                names.append(committee.委員姓名)
    return names


@U.locked_state()
def record_collection(
    *, master_path: str | Path, state_dir: str | Path, token: str, action: str,
    actor: str = "office", note: str = "", date: _dt.date | None = None,
    expected_version: str | None = None,
) -> None:
    """系辦登錄附件6 交付與回收（合格／需補正）。

    標「已收回（合格）」前，這份清冊上所有委員的撥帳個資都要已登錄；委員在口試現場
    手寫在附件6 上的個資，由專員先到「委員個資」頁登錄（2026-10-01 Q15）。
    """
    if action not in COLLECTION_ACTIONS:
        raise ValueError(f"未知的回收動作：{action}")
    sd = Path(state_dir)
    date = date or clock.today()
    master = load_current_master(master_path)
    state, confirmation, collection = _current_engine_lists(master, sd)
    updated, found = [], False
    for item in collection:
        if request_token(item.request_key) != token:
            updated.append(item)
            continue
        found = True
        _assert_active_case(item.case_keys[0], state_dir)
        if expected_version is not None and expected_version != item.content_version:
            raise ValueError("附件6已改版，請重新核對交付與回收證據")
        if action == "collected":
            missing = _committees_missing_banking(master, item.case_keys)
            if missing:
                raise ValueError(
                    "以下委員的撥帳個資尚未登錄，不能標為已收回（合格）："
                    + "、".join(missing) + "。請先到「委員個資」頁登錄委員在附件6 上手寫的資料"
                )
        if action == "sent":
            settings = office_settings.load(sd)
            dates = [master.by_key()[key].口試日期 for key in item.case_keys if master.by_key()[key].口試日期]
            updated.extend(collection_mark_sent([item], date=date, deadline_days=settings.collection_days,
                holidays=settings.holiday_set, business_days=settings.business_days, exam_date=max(dates) if dates else None))
        else:
            updated.append(collection_mark_collected(item, date=date, qualified=action == "collected",
                                                     note=note.strip()))
    if not found:
        raise ValueError("找不到這份附件6（主檔可能已更新，請重新整理）")
    state.confirmation, state.collection = confirmation, updated
    save_to_dir(state, sd)
    _audit(sd, "collection", action, tracking_key=tuple(token.split("|")),
           detail={"actor": actor, "note": note.strip()})


@U.locked_state()
def clear_case_review(
    *, master_path: str | Path, state_dir: str | Path, case_key: str,
    actor: str = "office", note: str = "",
) -> case_review.CaseReview:
    """人工關卡放行。處理人必填（已確認流程決策第四節第 5 條），API／CLI 與網頁一致。

    案件仍有學生責任問題（例如尚未填寫、逾期未完成）時放行照樣生效：放行只代表專員
    審過委員資格與例外；學生資料缺漏本身會擋附件4／6，等補齊（學生補件或專員修正層）
    才會產出，放行不能略過。
    """
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("請填寫處理人姓名")
    master = load_current_master(master_path)
    exam = master.by_key().get(case_key)
    if exam is None:
        raise ValueError(f"主檔中找不到案件：{case_key}")
    _assert_active_case(case_key, state_dir)
    row = next((r for r in review_queue(master, state_dir) if r.case_key == case_key), None)
    return case_review.clear(state_dir, exam=exam, master=master, actor=actor, note=note,
                             exceptions=row.exceptions if row else [])


@dataclass(frozen=True)
class CorrectionRow:
    """案件頁修正表的一列：欄位、目前主檔值（已套用修正）與既有修正。"""
    field: str
    member: str
    label: str
    shown: str
    correction: corrections.Correction | None


def correction_rows(master: Master, state_dir: str | Path, case_key: str) -> list[CorrectionRow]:
    exam = master.by_key().get(case_key)
    if exam is None:
        return []
    existing = {c.key: c for c in corrections.by_case(state_dir, case_key)}
    rows = [
        CorrectionRow(field=f, member="", label=label,
                      shown=corrections.source_value(exam, master.committees, f) or "",
                      correction=existing.get((case_key, "", f)))
        for f, label in corrections.EXAM_FIELDS.items()
    ]
    for member in exam.委員:
        committee = master.committees.get(member.committee_id)
        if committee is None or not committee.委員姓名:
            continue
        for f, label in corrections.MEMBER_FIELDS.items():
            rows.append(CorrectionRow(
                field=f, member=member.association_id or member.committee_id,
                label=f"{member.委員姓名 or committee.委員姓名}（{member.服務單位}）：{label}",
                shown=corrections.source_value(exam, master.committees, f, member.association_id or member.committee_id) or "",
                correction=existing.get((case_key, member.association_id or member.committee_id, f))
                    or existing.get((case_key, committee.委員姓名, f)),
            ))
    return rows


def effective_case_value(
    *, master_path: str | Path, state_dir: str | Path, case_key: str, field: str, member: str = "",
) -> str | None:
    """目前有效的欄位值：專員修正層有就用修正值（可能尚未重建主檔），否則用主檔來源值。

    AI 建議／教授改期草稿核准前用它確認前提沒變（審查 F04），避免舊建議蓋掉較新的專員修正。
    """
    master = load_current_master(master_path)
    exam = master.by_key().get(case_key)
    if exam is None:
        return None
    for c in corrections.load(state_dir):
        if c.key == (case_key, member.strip(), field):
            return c.value
    return corrections.source_value(exam, master.committees, field, member)


def save_case_correction(
    *, master_path: str | Path, state_dir: str | Path, case_key: str, field: str, value: str,
    actor: str, reason: str, member: str = "",
) -> corrections.Correction:
    """專員在案件頁修正欄位（2026-10-01 Q10／Q11／Q13）；下一次建主檔起套用。"""
    master = load_current_master(master_path)
    exam = master.by_key().get(case_key)
    if exam is None:
        raise ValueError(f"主檔中找不到案件：{case_key}")
    _assert_active_case(case_key, state_dir)
    shown = corrections.source_value(exam, master.committees, field, member)
    if shown is None:
        raise ValueError(f"案件中找不到委員：{member}")
    if field == "committee_list" and not member:
        checked = corrections._validate(field, value, "")
        if len(json.loads(checked)) < (5 if exam.學位別 == "博" else 3):
            raise ValueError("博士口試委員至少五位")
    original = corrections.original_for(state_dir, case_key=case_key, field=field, member=member,
                                        shown_value=shown)
    return corrections.set_correction(state_dir, case_key=case_key, field=field, value=value,
                                      original=original, actor=actor, reason=reason, member=member)


def generate_artifacts(
    *, master_path: str | Path, output_dir: str | Path, templates: TemplatePaths,
    phase: str | None = None, term: str | None = None,
    state_dir: str | Path | None = None,
) -> PreExamResult | PostExamResult:
    """整批完成與校驗後發布文件清單；失敗清除本次產物，保留前次結果。"""
    if phase not in ("pre-exam", "post-exam"):
        raise ValueError("正式產檔必須指定階段 phase：pre-exam 或 post-exam")
    if state_dir is None:
        raise ValueError("正式產檔需要 state_dir")
    import uuid
    with U.state_lock(state_dir):
        generation = source_state.require_current(master_path, allow_legacy=False)
        master_hash = source_state.digest(master_path)
        destination = Path(output_dir).resolve() / "generations" / uuid.uuid4().hex
        destination.mkdir(parents=True)
        try:
            result = _generate_artifacts(master_path=master_path, output_dir=destination,
                templates=templates, phase=phase, term=term, state_dir=state_dir)
            source_state.require_current(master_path,
                expected_generation=generation["generation"] if generation else None)
            if source_state.digest(master_path) != master_hash:
                raise ValueError("產檔期間主檔已變動，請重新產檔")
            files = list(result.attachment6_files)
            if isinstance(result, PreExamResult):
                files.insert(0, result.attachment4)
            hashes = {}
            for path in files:
                path = Path(path).resolve()
                if not path.is_relative_to(destination) or not path.is_file():
                    raise ValueError("產檔結果不完整或檔案不在本次產出目錄")
                hashes[path.relative_to(destination).as_posix()] = source_state.digest(path)
            active = _active_master(load_current_master(master_path), office_settings.load(state_dir))
            cases = {e.案件鍵 for e in active.active(term)}
            included4 = sorted(cases - validation.attachment4_blocked_keys(result.issues)) if phase == "pre-exam" else []
            blocked6 = (validation.attachment6_prefill_blocked_keys if phase == "pre-exam"
                        else validation.attachment6_blocked_keys)
            included6 = sorted(cases - blocked6(result.issues))
            manifest = destination / "manifest.json"
            U.atomic_write_json({"phase": phase, "at": clock.now().isoformat(),
                "generation": generation["generation"] if generation else None,
                "master_hash": master_hash, "files": hashes, "issues": result.issues,
                "included_attachment4": included4, "included_attachment6": included6,
                "excluded_cases": sorted(cases - set(included6))}, manifest)
            return replace(result, manifest=manifest)
        except BaseException:
            if destination.parent == Path(output_dir).resolve() / "generations":
                shutil.rmtree(destination)
            raise


def _generate_artifacts(
    *, master_path: str | Path, output_dir: str | Path, templates: TemplatePaths,
    phase: str | None = None, term: str | None = None,
    state_dir: str | Path | None = None,
) -> PreExamResult | PostExamResult:
    """依指定行政階段產附件；範本檢查與案件資格沿用 workflow 的單一規則。

    正式產檔必須明確指定 phase 與 state_dir：只納入專員已放行的案件；2026-10-12 起
    教授是否回覆附件4 不影響產檔。被排除的案件列在回傳的 issues 裡，不是靜默消失。
    """
    if phase is None:
        raise ValueError("正式產檔必須指定階段：pre-exam（口試前）或 post-exam（口試後）")
    if phase not in ("pre-exam", "post-exam"):
        raise ValueError(f"未知產檔階段：{phase}")
    if state_dir is None:
        raise ValueError(f"{phase} 產檔需要 state_dir 以核對人工關卡")
    source_state.require_current(master_path)
    from . import source_issues
    if source_issues.unresolved(load_current_master(master_path), state_dir):
        raise ValueError("尚有無案件鍵的來源異常未處理，請先在來源異常頁核對並留下證據")
    master = _active_master(load_current_master(master_path), office_settings.load(state_dir))
    if phase == "pre-exam":
        pending_issues = [*_unreviewed_case_issues(master, state_dir), *_document_eligibility_issues(master, state_dir)]
        pending_keys = {i["案件鍵"] for i in pending_issues}
        allowed = Master(
            exams=[e for e in master.exams if e.案件鍵 not in pending_keys],
            committees=master.committees,
            source_issues=master.source_issues,
        )
        result = generate_pre_exam(allowed, templates=templates, output_dir=output_dir, term=term)
        return replace(result, issues=[*result.issues, *pending_issues])
    if phase == "post-exam":
        pending_issues = [*_unreviewed_case_issues(master, state_dir),
                          *_document_eligibility_issues(master, state_dir)]
        pending_keys = {i["案件鍵"] for i in pending_issues}
        allowed = Master(
            exams=[e for e in master.exams if e.案件鍵 not in pending_keys],
            committees=master.committees,
            source_issues=master.source_issues,
        )
        result = generate_post_exam(allowed, templates=templates, output_dir=output_dir, term=term)
        return replace(result, issues=[*result.issues, *pending_issues])
    raise ValueError(f"未知產檔階段：{phase}")


def _submission_path(directory: str | Path, case_key: str) -> Path:
    if not _CASE_KEY_RE.fullmatch(case_key):
        raise ValueError("案件鍵格式錯誤")
    return Path(directory) / f"{case_key}.json"


def _write_json(path: Path, data: dict) -> None:
    U.atomic_write_json(data, path)


MIN_COMMITTEES = C.MIN_COMMITTEES["碩"]  # 未知學位別時的下限；博士 5 位見 config.MIN_COMMITTEES


def _parse_committees(fields: Mapping[str, str]) -> list[dict]:
    committees: list[dict] = []
    idx = 0
    while f"committee_{idx}_name" in fields:
        name = str(fields.get(f"committee_{idx}_name", "")).strip()
        if name:
            committee = {
                key: str(fields.get(f"committee_{idx}_{source}", "")).strip()
                for key, source in (
                    ("name", "name"), ("unit", "unit"), ("title", "title"), ("email", "email"),
                )
            }
            committee["remote"] = bool(fields.get(f"committee_{idx}_remote"))
            committees.append(committee)
        idx += 1
    return committees


def _parse_advisors(fields: Mapping[str, str]) -> list[str]:
    return list(_parse_advisor_emails(fields))


def _parse_advisor_emails(fields: Mapping[str, str]) -> dict[str, str]:
    """指導教授姓名 → E-mail（學生填寫，2026-09-23 定案）；同名只取第一筆。"""
    advisors: dict[str, str] = {}
    idx = 0
    while f"advisor_{idx}" in fields:
        name = str(fields.get(f"advisor_{idx}", "")).strip()
        if name and name not in advisors:
            advisors[name] = str(fields.get(f"advisor_{idx}_email", "")).strip()
        idx += 1
    return advisors


def _validate_submission(fields: Mapping[str, str], advisors: list[str], committees: list[dict], *, degree=None) -> None:
    """送出時的伺服器端檢查（review-2026-09-23 P1-15）；不通過就不儲存、不歸檔舊版。"""
    problems = []
    email = str(fields.get("student_email", "")).strip()
    if not _EMAIL_RE.fullmatch(email):
        problems.append("請填寫正確的學生 Email")
    if not advisors:
        problems.append("請填寫指導教授")
    advisor_emails = _parse_advisor_emails(fields)
    for name in advisors:
        if not _EMAIL_RE.fullmatch(advisor_emails.get(name, "")):
            problems.append(f"指導教授「{name}」的 E-mail 格式不正確")
    identities = [(U.canon_name(c["name"]), U.norm_unit(c["unit"])) for c in committees]
    if len(set(identities)) != len(identities):
        problems.append("口試委員重複，請填寫不同委員")
    minimum = C.MIN_COMMITTEES.get(degree, MIN_COMMITTEES)
    if len(set(identities)) < minimum:
        problems.append(f"口試委員至少 {minimum} 位")
    for c in committees:
        if not c["unit"]:
            problems.append(f"委員「{c['name']}」缺服務單位")
        if not _EMAIL_RE.fullmatch(c["email"]):
            problems.append(f"委員「{c['name']}」的 E-mail 格式不正確")
    exam_date = str(fields.get("exam_date", "")).strip()
    if exam_date:
        try:
            _dt.date.fromisoformat(exam_date)
        except ValueError:
            problems.append("口試日期格式錯誤")
    else:
        problems.append("請填寫口試日期")
    start, end = str(fields.get("exam_start_time", "")).strip(), str(fields.get("exam_end_time", "")).strip()
    if start or end:
        try:
            if not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", start) or not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]", end):
                raise ValueError()
            if _dt.time.fromisoformat(end) <= _dt.time.fromisoformat(start):
                raise ValueError()
        except ValueError:
            problems.append("口試時間須成對填寫，結束時間須晚於開始時間")
    mode = str(fields.get("exam_mode") or "實體").strip()
    if mode not in ("實體", "視訊", "部分視訊"):
        problems.append("口試方式不正確")
    if mode == "實體" and any(c.get("remote") for c in committees):
        problems.append("實體口試不能勾選委員視訊出席，請更正口試方式或取消勾選")
    limits = {"thesis_title": 1000, "exam_location": 300, "student_email": 254}
    for name, value in fields.items():
        limit = limits.get(name, 1000)
        if len(str(value)) > limit:
            problems.append(f"欄位「{name}」超過{limit}字，請縮短內容")
    if problems:
        raise ValueError("未儲存：" + "；".join(problems))


def submission_self_check(
    data: Mapping, *, today: _dt.date | None = None,
    settings: office_settings.OfficeSettings | None = None, degree: str | None = None,
) -> list[str]:
    """學生送出後的自我檢查提示（review-2026-09-23 P1-14）。只是畫面提示，不是催件。

    degree＝附件2 的學位別（碩／博）；已知是碩士時不提示博士班人數。
    """
    today = today or clock.today()
    hints = []
    try:
        exam_date = _dt.date.fromisoformat(str(data.get("exam_date") or ""))
    except ValueError:
        exam_date = None
    if exam_date and exam_date < today:
        hints.append("口試日期已經過去，請確認是否填錯")
    earliest = earliest_feasible_exam_date(settings) if settings else None
    if exam_date and earliest and exam_date < earliest:
        hints.append(f"口試日期早於 {earliest}，補件與附件4 寄教授核對可能來不及在口試前完成，請與系辦確認")
    if len(data.get("committees") or []) < 5 and degree != "碩":
        hints.append("博士班口試委員至少 5 位" if degree == "博" else "博士班口試委員至少 5 位（碩士班 3 位即可）")
    return hints


def earliest_feasible_exam_date(settings: office_settings.OfficeSettings) -> _dt.date | None:
    """補件截止日再加上預留給教授核對附件4 的天數（設定 confirmation_days）；口試早於這天，流程可能來不及在口試前完成。

    附件2 還沒上傳時，以「準時上傳」推估補件截止日。
    """
    deadline = settings.projected_deficiency_deadline
    if deadline is None:
        return None
    return settings.add_days(deadline, settings.confirmation_days)


@U.locked_state("submissions_dir")
def submit_application(
    *, student_id: str, fields: Mapping[str, str], submissions_dir: str | Path,
    versions_dir: str | Path | None = None, now: _dt.datetime | None = None, degree: str | None = None,
) -> dict:
    """建立或重填自己的附件 3 補充資料；送出即有效，重填歸檔舊版。

    學期、學號來自登入身分；不收 Portal 已有的欄位（姓名、學院、系所）。
    委員只收姓名、服務單位、職稱與 E-mail，撥帳個資不經學生。
    """
    term = str(fields.get("term", "")).strip()
    if not _TERM_RE.fullmatch(term):
        raise ValueError("學期格式錯誤，未儲存")
    if not _STUDENT_ID_RE.fullmatch(student_id):
        raise ValueError("學號格式錯誤，未儲存")
    committees = _parse_committees(fields)
    advisors = _parse_advisors(fields)
    _validate_submission(fields, advisors, committees, degree=degree)

    case_key = U.case_key(term, student_id)
    path = _submission_path(submissions_dir, case_key)
    now = now or clock.now()
    previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    current_version = int(previous.get("version", 1)) if previous else 0
    if "base_version" in fields:
        try:
            expected = int(fields["base_version"])
        except (ValueError, TypeError) as exc:
            raise ValueError("編輯版本格式錯誤，請重新載入") from exc
        if expected != current_version:
            raise ValueError("資料已被更新，請重新載入後核對；本次輸入未覆蓋最新版")
    version = 1
    if previous is not None:
        version = int(previous.get("version", 1)) + 1
        archive_dir = Path(versions_dir) if versions_dir is not None else Path(submissions_dir) / "versions"
        stamp = now.strftime("%Y%m%d%H%M%S%f")
        _write_json(archive_dir / f"{case_key}_v{version - 1}_{stamp}.json", previous)

    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    data = {
        "case_key": case_key, "version": version, "submitted_at": timestamp,
        "term": term, "student_id": student_id,
        **{key: str(fields.get(key, "")).strip() for key in (
            "student_email", "thesis_title",
            "exam_date", "exam_start_time", "exam_end_time", "exam_location", "exam_mode",
        )},
        "advisors": advisors,
        "advisor_emails": _parse_advisor_emails(fields),
        "committees": committees,
    }
    _write_json(path, data)
    discard_draft(submissions_dir, case_key)
    return data


DRAFTS_DIRNAME = "drafts"


def _draft_path(submissions_dir: str | Path, case_key: str) -> Path:
    if not _CASE_KEY_RE.fullmatch(case_key):
        raise ValueError(f"案件鍵格式錯誤：{case_key}")
    return Path(submissions_dir) / DRAFTS_DIRNAME / f"{case_key}.json"


def save_draft(
    *, student_id: str, fields: Mapping[str, str], submissions_dir: str | Path,
    now: _dt.datetime | None = None,
) -> dict:
    """暫存填到一半的資料（2026-10-01 Q2）：不檢查、不算送出、不建版本。

    暫存放在 submissions/drafts/，建主檔只讀 submissions/*.json，所以暫存不會進主檔；
    填寫截止時只有暫存、從未送出的學生視為「未填寫」（Q2-1）。
    """
    term = str(fields.get("term", "")).strip()
    if not _TERM_RE.fullmatch(term):
        raise ValueError("學期格式錯誤，未暫存")
    if not _STUDENT_ID_RE.fullmatch(student_id):
        raise ValueError("學號格式錯誤，未暫存")
    case_key = U.case_key(term, student_id)
    data = {
        "base_version": str(fields.get("base_version", "0")),
        "case_key": case_key, "term": term, "student_id": student_id,
        "saved_at": (now or clock.now()).strftime("%Y-%m-%d %H:%M:%S"),
        **{key: str(fields.get(key, "")).strip() for key in (
            "student_email", "thesis_title",
            "exam_date", "exam_start_time", "exam_end_time", "exam_location", "exam_mode",
        )},
        "advisors": _parse_advisors(fields),
        "advisor_emails": _parse_advisor_emails(fields),
        "committees": _parse_committees(fields),
    }
    _write_json(_draft_path(submissions_dir, case_key), data)
    return data


def load_draft(submissions_dir: str | Path, case_key: str) -> dict | None:
    path = _draft_path(submissions_dir, case_key)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def discard_draft(submissions_dir: str | Path, case_key: str) -> None:
    path = _draft_path(submissions_dir, case_key)
    if path.is_file():
        path.unlink()


def draft_case_keys(submissions_dir: str | Path | None) -> set[str]:
    if submissions_dir is None:
        return set()
    directory = Path(submissions_dir) / DRAFTS_DIRNAME
    return {p.stem for p in directory.glob("*.json")} if directory.is_dir() else set()


def notified_issue_keys(state_dir: str | Path) -> dict[str, set[str]]:
    """每個案件已經寄過補件信的問題（「代碼|欄位」；2026-10-01 Q7：同一問題不重複寄）。

    舊紀錄只存代碼，記成「代碼|*」，視為該代碼的所有欄位都寄過。
    """
    log = send_log.load(Path(state_dir) / "send_log.json")
    out: dict[str, set[str]] = {}
    for event in log.all():
        if event.status != send_log.SENT or event.type != notify.TYPE_STUDENT_DEFICIENCY:
            continue
        case_key = event.tracking.get("case_key")
        if not case_key:
            continue
        keys = event.tracking.get("issue_keys")
        if keys is None:
            keys = [f"{code}|*" for code in event.tracking.get("issue_codes") or ()]
        out.setdefault(str(case_key), set()).update(keys)
    return out


ISSUE_FIRST_SEEN_FILE = "issue_first_seen.json"


@U.locked_state()
def record_issue_first_seen(
    state_dir: str | Path, issues: list[dict], *, date: _dt.date | None = None, prune: bool = False,
) -> dict[str, dict[str, str]]:
    """記下每個案件的學生責任問題第一次被看到的日期（案件鍵 → 「代碼|欄位」→ 日期）。

    已確認流程決策第三節第 3 條：補件期間冒出的新問題，學生頁立即顯示，「隔天」每日檢核
    時問題仍在才寄信。學生送出後網頁建檔與每日自動化都會記錄；`prune=True`（每日自動化）
    時順便清掉已消失的問題，之後復發就重新起算。
    """
    sd = Path(state_dir)
    path = sd / ISSUE_FIRST_SEEN_FILE
    data = U.read_json_state(path).get("cases", {}) if path.is_file() else {}
    stamp = (date or clock.today()).isoformat()
    current: dict[str, set[str]] = {}
    for issue in issues:
        if issue.get("責任") != validation.OWNER_STUDENT or not issue.get("案件鍵"):
            continue
        current.setdefault(str(issue["案件鍵"]), set()).add(notify.issue_key(issue))
    changed = False
    for case_key, keys in current.items():
        seen = data.setdefault(case_key, {})
        for key in keys:
            if key not in seen:
                seen[key], changed = stamp, True
    if prune:
        for case_key in list(data):
            kept = {k: v for k, v in data[case_key].items() if k in current.get(case_key, set())}
            if kept != data[case_key]:
                changed = True
                if kept:
                    data[case_key] = kept
                else:
                    del data[case_key]
    if changed:
        U.atomic_write_json({"cases": data}, path)
    return data


def _new_issue_too_fresh(
    issue: dict, notified: dict[str, set[str]], first_seen: dict[str, dict[str, str]], today: _dt.date,
) -> bool:
    """已收過補件信的案件，補件期間冒出的新問題：今天才第一次看到就先不寄，隔天仍在才寄。

    第一次的補件信（案件還沒收過任何補件信）照常當天寄出（決策三-2：開始日起直接寄）。
    """
    case_key = str(issue.get("案件鍵"))
    if issue.get("責任") != validation.OWNER_STUDENT or not notified.get(case_key):
        return False
    seen = first_seen.get(case_key, {}).get(notify.issue_key(issue))
    return seen is None or _dt.date.fromisoformat(seen) >= today


def _already_notified(issue: dict, notified: dict[str, set[str]]) -> bool:
    keys = notified.get(str(issue.get("案件鍵")), set())
    return notify.issue_key(issue) in keys or f"{issue.get('問題代碼') or ''}|*" in keys


@U.locked_state()
def mark_case_resubmitted(
    case_key: str, state_dir: str | Path, *, date: _dt.date | None = None,
) -> None:
    """學生重新送出表單後，把該案件所有 request_sent 的缺件標為 resubmitted。"""
    path = Path(state_dir) / "issue_records.json"
    store = issue_store.load(path)
    changed_keys: list[tuple[str, str, str]] = []
    for record in store.by_case(case_key):
        if record.status == issue_store.REQUEST_SENT:
            try:
                store.put(issue_store.transition(record, issue_store.RESUBMITTED, date=date))
                changed_keys.append(record.tracking_key)
            except ValueError:
                continue
    if changed_keys:
        issue_store.save(store, path)
        for key in changed_keys:
            _audit(
                Path(state_dir), "issue_store", "resubmitted",
                tracking_key=key,
                detail={"case_key": case_key},
            )


@U.locked_state()
def sync_issue_records(
    state_dir: str | Path, items: list[DeficiencyItem], *, date: _dt.date | None = None,
    reviewer: issue_store.Reviewer | None = None,
) -> issue_store.IssueRecordStore:
    """把本次缺件掃描結果對帳進 issue_store 的細顆粒生命週期記錄。

    跟 engines_state.json 平行存放（issue_records.json），不是同一份檔案
    （見 memory 設計筆記）。先對 resubmitted 記錄執行複驗（預設自動檢核，
    未來可傳入 LLM reviewer），再做 detected／resolved 的自動對帳；
    draft_ready／request_sent 由呼叫端在草稿產生、寄出成功時另外呼叫
    `_advance_issue_records()` 推進（見 `process_round()`）。
    """
    state_dir = Path(state_dir)
    path = state_dir / "issue_records.json"
    store = issue_store.load(path)
    current_keys = {item.tracking_key for item in items}
    store = issue_store.revalidate(
        store, current_keys,
        reviewer=reviewer or issue_store.automated_reviewer, date=date,
    )
    store = issue_store.reconcile(store, items, date=date)
    issue_store.save(store, path)
    return store


@U.locked_state()
def _advance_issue_records(
    state_dir: str | Path,
    tracking_keys: set[tuple[str, str, str]],
    to_status: str,
    *,
    date: _dt.date | None = None,
) -> None:
    """把指定追蹤鍵的 issue_store 記錄推進到 to_status。

    找不到記錄（還沒被 `sync_issue_records()` 對帳過）或轉移不合法
    （例如已經在更後面的狀態，重跑同一輪不該退回）都靜默跳過，不拋例外——
    這層生命週期記錄是疊加的觀察用途，不能因為它跳過而影響既有的建檔／
    寄信流程。
    """
    path = Path(state_dir) / "issue_records.json"
    store = issue_store.load(path)
    changed = False
    for key in tracking_keys:
        record = store.get(key)
        if record is None:
            continue
        try:
            store.put(issue_store.transition(record, to_status, date=date))
            changed = True
        except ValueError:
            continue
    if changed:
        issue_store.save(store, path)
        for key in tracking_keys:
            _audit(
                Path(state_dir), "issue_store", "status_changed",
                tracking_key=key,
                detail={"to_status": to_status},
            )


def tracking_snapshot(
    *, master: Master, state_dir: str | Path, term: str | None = None, persist: bool = False,
) -> TrackingResult:
    """按完整主檔對帳後再篩選顯示；指定學期不會刪掉其他學期狀態。"""
    state_dir = Path(state_dir)
    if persist:
        with U.state_lock(state_dir):
            return _tracking_snapshot(master=master, state_dir=state_dir, term=term, persist=True)
    return _tracking_snapshot(master=master, state_dir=state_dir, term=term, persist=False)


def _tracking_snapshot(
    *, master: Master, state_dir: Path, term: str | None, persist: bool,
) -> TrackingResult:
    if persist:
        source_state.ensure_generation(master)
    state = load_from_dir(state_dir)
    deficiency = deficiency_scan(master, previous=state.deficiency, holidays=None)
    confirmation = confirmation_reconcile(master, state.confirmation) if state.confirmation else confirmation_requests(master)
    collection = collection_reconcile(master, state.collection) if state.collection else collection_items(master)
    confirmation, _ = advisor_confirmation.reset_changed(master, confirmation, state_dir)
    if persist:
        state.deficiency = list(deficiency.items)
        state.confirmation = confirmation
        state.collection = collection
        save_to_dir(state, state_dir)
        sync_issue_records(state_dir, list(deficiency.items))
    if term is not None:
        deficiency = DeficiencyReport(
            items=tuple(item for item in deficiency.items if item.term == term),
            resolved_keys=frozenset(key for key in deficiency.resolved_keys if key[0].startswith(f"{term}-")),
        )
        confirmation = [item for item in confirmation if item.term == term]
        collection = [item for item in collection if item.term == term]
    return TrackingResult(
        deficiency=deficiency,
        confirmation=confirmation_report(confirmation),
        collection=collection_report(collection),
    )


def notified_student_cases(state_dir: str | Path) -> set[str]:
    """寄送記錄中確實寄出過學生補件信（首次通知或催件）的案件鍵。"""
    log = send_log.load(Path(state_dir) / "send_log.json")
    return {
        str(event.tracking.get("case_key"))
        for event in log.all()
        if event.status == send_log.SENT and event.type == notify.TYPE_STUDENT_DEFICIENCY
        and event.tracking.get("case_key")
    }


@U.locked_state()
def sync_deficiency_deadlines(
    *, master: Master, state_dir: str | Path, date: _dt.date | None = None,
    settings: office_settings.OfficeSettings | None = None,
    notified_case_keys: set[str] | None = None,
) -> DeficiencyReport:
    """新偵測到的缺件標為已通知並附上期限。

    系辦已設定填寫截止日時，學生責任缺件一律以全系統共用的**補件截止日**
    為期限（已確認流程決策第三節）；未設定時沿用「通知後 N 個工作日」的
    暫定預設。只更新缺件追蹤狀態，不寄信、不動主檔或其他引擎狀態。

    notified_case_keys：給定時，學生責任缺件只有案件真的寄出過補件信才標為
    已通知——預覽、尚未到發送時刻或查無收件人都不算通知（系辦責任缺件不寄信，
    照舊直接起算）。未給定時全部標記（直接呼叫的舊行為）。
    """
    state_dir = Path(state_dir)
    source_state.ensure_generation(master)
    settings = settings or office_settings.load(state_dir)
    # 學生送出後網頁建檔也走這裡：記下新問題第一次出現的日期，隔天才寄（決策三-3）
    record_issue_first_seen(state_dir, [*master.source_issues, *validation.check(master)], date=date)
    state = load_from_dir(state_dir)
    scanned = deficiency_scan(master, previous=state.deficiency, holidays=None)
    items = list(scanned.items)
    if notified_case_keys is None:
        marked = mark_notified(items, date=date)
    else:
        notifiable = [i.owner != STUDENT or i.case_key in notified_case_keys for i in items]
        newly = iter(mark_notified([i for i, ok in zip(items, notifiable) if ok], date=date))
        marked = [next(newly) if ok else i for i, ok in zip(items, notifiable)]
    notified = _apply_student_deadline(marked, settings, state_dir)
    state.deficiency = notified
    save_to_dir(state, state_dir)
    sync_issue_records(state_dir, notified, date=date)
    return DeficiencyReport(items=tuple(notified), resolved_keys=scanned.resolved_keys)


def _apply_student_deadline(
    items: list[DeficiencyItem], settings: office_settings.OfficeSettings, state_dir=None,
) -> list[DeficiencyItem]:
    deadline = settings.student_deadline()
    if deadline is None:
        return items
    return [replace(item, deadline=replace(deadline, computed_date=_effective_deadline(settings, state_dir, item.case_key))
        if state_dir else deadline) if item.owner == STUDENT else item for item in items]


STUDENT_NOT_OPEN_MESSAGE = "本學期尚未開放填寫"


def submission_block_reason(
    case_key: str, state_dir: str | Path, *, today: _dt.date | None = None,
    settings: office_settings.OfficeSettings | None = None,
) -> str | None:
    """學生此刻能否送出；可以時回傳 None，不行時回傳給學生看的原因。

    系辦設好本期學期與填寫截止日之前不開放（2026-10-01 Q1）。填寫截止日（含）前
    自由修改；補件期間只有本輪曾被標出學生責任問題的案件可改——修好一項後仍可在
    補件截止日前更正其他錯誤（review-2026-09-23 P1-10）；填寫截止後、正式檢核開始前
    一律鎖定；補件截止日後一律鎖定（硬截止）。
    """
    today = today or clock.today()
    settings = settings or office_settings.load(state_dir)
    if not settings.student_open:
        return STUDENT_NOT_OPEN_MESSAGE
    if case_access.is_unlocked(state_dir, case_key, today):
        return None  # 專員對此案開放修改（2026-10-01 Q8／Q13）
    phase = settings.phase_on(today)
    state = load_from_dir(Path(state_dir))
    student_items = [i for i in state.deficiency if i.case_key == case_key and i.owner == STUDENT]
    if phase == office_settings.PHASE_FILLING:
        return None
    if phase == office_settings.PHASE_AWAITING_ATT2:
        return "填寫截止日已過，系統尚未開始檢核；資料已鎖定，如需異動請按「申請異動」"
    if phase == office_settings.PHASE_DEFICIENCY:
        notified_this_round = any(
            r.owner == STUDENT for r in issue_store.load(Path(state_dir) / "issue_records.json").by_case(case_key)
        )
        if student_items or notified_this_round:
            return None
        return "填寫截止日已過；只有收到補件通知的案件可以在補件截止日前修改。資料已鎖定，如需異動請按「申請異動」"
    return "補件截止日已過，本輪不再接受自行送出；如需異動請按「申請異動」"


@dataclass(frozen=True)
class StudentCaseStatus:
    """學生頁顯示用：本案目前的待補項目、期限與能否修改（review-2026-09-23 P2-24）。"""
    case_key: str
    phase_label: str
    fill_deadline: _dt.date | None
    deficiency_deadline: _dt.date | None
    pending: list[DeficiencyItem]
    block_reason: str | None


def student_case_status(
    case_key: str, state_dir: str | Path, *, today: _dt.date | None = None,
) -> StudentCaseStatus:
    """只讀：學生責任、尚未解決的缺件（最近一次正式檢核結果）與補件截止日。

    系辦責任的例外與委員資格審查不對學生顯示；問題代碼也不顯示，只給欄位與說明。
    """
    today = today or clock.today()
    settings = office_settings.load(state_dir)
    state = load_from_dir(Path(state_dir))
    pending = [i for i in state.deficiency if i.case_key == case_key and i.owner == STUDENT]
    return StudentCaseStatus(
        case_key=case_key,
        phase_label=office_settings.PHASE_LABELS[settings.phase_on(today)],
        fill_deadline=settings.fill_deadline,
        deficiency_deadline=_effective_deadline(settings, state_dir, case_key),
        pending=pending,
        block_reason=submission_block_reason(case_key, state_dir, today=today, settings=settings),
    )


def record_student_submission(state_dir: str | Path, data: dict) -> None:
    """學生送出寫稽核：只記案件與版本，不記填寫內容（review-2026-09-23 P2-24）。"""
    _audit(Path(state_dir), "submission", "submitted", tracking_key=(data["case_key"],),
           detail={"version": data["version"], "actor": data["student_id"]})


def _resubmitted_case_keys(state_dir: str | Path) -> set[str]:
    """學生已重送、等待下一輪自動重檢的案件鍵（issue_store 的 resubmitted）。"""
    store = issue_store.load(Path(state_dir) / "issue_records.json")
    return {r.case_key for r in store.all() if r.status == issue_store.RESUBMITTED}


def _reminder_recipient_resolver(contacts: dict) -> Callable[[str, object], "str | None"]:
    """把 ReminderScheduler 的 (kind, obj) 轉接到 recipients.py 的解析函式。

    deficiency 傳進來的 obj 是同一案件的 DeficiencyItem 清單（見
    scheduler.scan_deficiency() 的分組邏輯）；confirmation 用指導教授姓名查通訊錄；
    collection（附件6 逾期）寄給該場次所有口試委員（2026-10-13：學生帶去、學生交回）。
    """
    def resolve(kind: str, obj) -> str | None:
        if kind == "deficiency":
            if not obj:
                return None
            return recipients.resolve_student(obj[0].case_key, contacts)
        if kind == "collection":
            return recipients.resolve_committees(obj.case_keys, contacts)
        if kind == "confirmation":
            return recipients.resolve_advisor(obj.advisor, contacts, case_keys=obj.case_keys)
        return None
    return resolve


def plan_reminders(
    *, master: Master, state_dir: str | Path, term: str | None = None,
    submissions_dir: str | Path | None = None, resolve_recipients: bool = False,
    date: _dt.date | None = None, settings: office_settings.OfficeSettings | None = None,
    include_student: bool = True,
) -> ReminderPlan:
    """從最新資料與已存追蹤狀態排程草稿及系辦待辦；不寄信、不改狀態。

    學生已重送、等待下一輪重檢的案件（issue_store 為 resubmitted）不排入補件
    催件：已送出新版本的案件不應再收到「尚未提交」催件。

    resolve_recipients=True 時改用通訊錄解析真實收件人（供 `remind --send`
    使用）；預設 False 一律用 placeholder 佔位地址，避免預覽模式意外把
    真實個資印到終端機或漏進測試輸出。學生 email 沿用 `_submission_contacts()`
    直接從學生填寫解析（跟 `process_round()` 一致，不依賴尚待維護的
    contacts.json students 區塊）；指導教授／系辦則讀 contacts.json（系辦
    手動維護）。
    """
    current_date = date or clock.today()
    settings = settings or office_settings.load(state_dir)
    master = _active_master(master, settings)
    if settings.current_term and term and term != settings.current_term:
        raise ValueError("封存學期不能排程寄送")
    term = settings.current_term or term
    tracking = tracking_snapshot(master=master, state_dir=state_dir, term=term)
    resolver = None
    if resolve_recipients:
        source_state.ensure_generation(master)
        students: dict[str, str] = {}
        case_advisors: dict[str, dict[str, str]] = {}
        if submissions_dir is not None:
            paths = list(ingest.student_submission_paths(submissions_dir))
            case_keys = {exam.案件鍵 for exam in master.exams}
            contact_bundle, _ = _submission_contacts(paths, case_keys)
            students = contact_bundle["students"]
            case_advisors = contact_bundle["case_advisors"]
        effective = _effective_contacts(master, {"students": students, "case_advisors": case_advisors})
        students, case_advisors = effective["students"], effective["case_advisors"]
        students = _with_fallback_emails(students, master, settings)
        persisted = recipients.load_contacts()
        resolver = _reminder_recipient_resolver({
            "students": students, "case_advisors": case_advisors,
            "advisors": persisted["advisors"], "office": persisted["office"],
            "case_committees": effective.get("case_committees", {}),
        })
    scheduler = ReminderScheduler(
        today=lambda: current_date, resolve_recipient=resolver,
        deficiency_lead_days=settings.deficiency_reminder_lead_days,
        confirmation_interval_days=settings.confirmation_reminder_interval_days,
        business_days=settings.business_days, holidays=settings.holiday_set,
    )
    scheduled = scheduler.scan_all(
        deficiency=tracking.deficiency,
        confirmation=tracking.confirmation,
        collection=tracking.collection,
    )
    resubmitted = _resubmitted_case_keys(state_dir)
    scheduled = [
        s for s in scheduled
        # 2026-10-12 起附件4 只寄給教授參考、不用回信：不催教授
        if s.kind != "confirmation"
        and not (s.kind == "deficiency"
                 and (not include_student or s.tracking_key[0] in resubmitted))
    ]
    return ReminderPlan(
        scheduled=scheduled,
        internal_todos=scheduler.internal_todos(tracking.deficiency),
    )


def _advisor_reminders(scheduled, *, master, state_dir, requests, today, settings):
    """（2026-10-12 起不再使用：教授不用回信、不催。）附件4 催辦信換成與原信一致的版本。"""
    by_key = {r.request_key: r for r in requests}
    reply_by = settings.add_days(today, settings.confirmation_days)
    out = []
    for item in scheduled:
        req = by_key.get(item.tracking_key) if item.kind == "confirmation" else None
        if req is not None:
            item = replace(item, draft=advisor_confirmation.reminder_draft(
                req, master, state_dir, to=item.draft.to, today=today, reply_by=reply_by,
                tracking=item.draft.tracking))
        out.append(item)
    return out


def _record_advisor_reminder(state_dir, reminder, *, sent_at: str | None = None) -> None:
    """附件4 催辦信寄出後記進本輪寄送紀錄（回覆催辦信也對得回本輪）。"""
    if reminder.kind != "confirmation":
        return
    draft = reminder.draft
    state = load_from_dir(state_dir)
    req = next((r for r in state.confirmation if r.request_key == reminder.tracking_key), None)
    if req is None:
        return
    advisor_confirmation.record_reminder(
        state_dir, req, message_id=notify.message_id_for(draft.idempotency_key),
        version=draft.metadata.get("version"), reply_by=draft.metadata.get("reply_by"), sent_at=sent_at)


@U.locked_state()
def mark_reminders_sent(
    state_dir: str | Path, sent: list[ScheduledReminder], *, date: _dt.date | None = None,
) -> None:
    """按原寄送日期、版本與提醒序號對帳；重啟恢復不重複增加次數。"""
    state_dir = Path(state_dir)
    state = load_from_dir(state_dir)
    sent_date = date or clock.today()
    changed = []
    for reminder in sent:
        tracking = reminder.draft.tracking
        def eligible(item):
            if item.last_reminder_date == sent_date:
                return False
            if tracking.get("version") is not None and tracking["version"] != item.content_version:
                return False
            ordinal = tracking.get("reminder_ord")
            return ordinal is None or ordinal == item.reminder_count + 1
        updated = False
        if reminder.kind == "deficiency":
            items = []
            for item in state.deficiency:
                if item.case_key == reminder.tracking_key[0] and item.last_reminder_date != sent_date:
                    item = deficiency_mark_reminded(item, date=sent_date)
                    updated = True
                items.append(item)
            state.deficiency = items
        elif reminder.kind in ("confirmation", "collection"):
            attr = reminder.kind
            items = []
            for item in getattr(state, attr):
                if item.request_key == reminder.tracking_key and eligible(item):
                    item = replace(item, reminder_count=item.reminder_count + 1,
                                   last_reminder_date=sent_date)
                    updated = True
                items.append(item)
            setattr(state, attr, items)
        if updated:
            changed.append(reminder)
    if not changed:
        return
    save_to_dir(state, state_dir)
    for reminder in changed:
        _audit(state_dir, "send_log", "reminder_sent", tracking_key=reminder.tracking_key,
               detail={"kind": reminder.kind, "sent_date": sent_date.isoformat()})


def _with_fallback_emails(students: dict[str, str], master: Master, settings) -> dict[str, str]:
    """沒有留 Email 的案件，依系辦設定的學校信箱格式補上（review-2026-09-23 P1-6）。"""
    merged = dict(students)
    for exam in master.exams:
        if exam.案件鍵 not in merged:
            fallback = settings.fallback_student_email(exam.學號)
            if fallback and _EMAIL_RE.fullmatch(fallback):
                merged[exam.案件鍵] = fallback
    return merged


def _effective_contacts(master: Master, contacts: dict) -> dict:
    """寄送以已套用专员修正的有效主檔為準。"""
    for exam in master.exams:
        if exam.student_email:
            contacts.setdefault("students", {})[exam.案件鍵] = exam.student_email
        if exam.advisor_emails:
            contacts.setdefault("case_advisors", {})[exam.案件鍵] = dict(exam.advisor_emails)
        committee_emails = [m.Email or getattr(master.committees.get(m.committee_id), "Email", "")
                            for m in exam.委員]
        if any(committee_emails):
            contacts.setdefault("case_committees", {})[exam.案件鍵] = [e for e in committee_emails if e]
    return contacts


def _submission_contacts(
    paths: list[Path], case_keys: set[str],
) -> tuple[dict, dict[str, int]]:
    """只用確實建入主檔的案件之目前填寫，解析學生、指導教授收件人與填寫版本。

    指導教授 E-mail 由學生填寫，按案件存（`case_advisors`）：同一位教授在不同
    學生的填寫可能不一致，寄信時以該清冊所含案件的填寫為準。
    """
    students: dict[str, str] = {}
    case_advisors: dict[str, dict[str, str]] = {}
    revisions: dict[str, int] = {}
    seen: set[str] = set()
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue  # 建檔器已將解析錯誤記入異常；此處沒有可信地址可寄
        if not isinstance(raw, dict):
            continue
        term, student_id = str(raw.get("term") or ""), str(raw.get("student_id") or "")
        if not term or not student_id:
            continue
        key = U.case_key(term, student_id)
        if key not in case_keys:
            continue
        if raw.get("case_key") not in (None, key):
            raise ValueError(f"表單案件鍵與學期／學號不一致，停止通知：{path.name}")
        if key in seen:
            raise ValueError(f"同一案件有多份填寫，停止通知：{key}")
        seen.add(key)
        email = str(raw.get("student_email") or "").strip()
        if _EMAIL_RE.fullmatch(email):
            students[key] = email
        advisor_emails = raw.get("advisor_emails") or {}
        if isinstance(advisor_emails, dict):
            case_advisors[key] = {
                str(name).strip(): str(addr).strip() for name, addr in advisor_emails.items()
                if _EMAIL_RE.fullmatch(str(addr).strip())
            }
        revision = raw.get("version", 1)
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError(f"表單版本格式錯誤，停止通知：{path.name}")
        if revision < 1:
            raise ValueError(f"表單版本必須為正整數，停止通知：{path.name}")
        revisions[key] = revision
    return {"students": students, "case_advisors": case_advisors}, revisions


def _recover_student_deliveries(master, state_dir, settings):
    """由已寄紀錄補回當時的業務結果，僅處理仍未標記寄出的有效缺件。"""
    store = issue_store.load(Path(state_dir) / "issue_records.json")
    active = set(master.by_key())
    for event in send_log.load(Path(state_dir) / "send_log.json").by_status(send_log.SENT):
        if event.type != notify.TYPE_STUDENT_DEFICIENCY:
            continue
        case_key = event.tracking.get("case_key")
        if case_key not in active:
            continue
        fields = event.tracking.get("issue_keys")
        codes = event.tracking.get("issue_codes", [])
        keys = {r.tracking_key for r in store.by_case(case_key)
                if r.status in (issue_store.DETECTED, issue_store.DRAFT_READY)
                and ((f"{r.issue_code}|{r.field}" in fields) if fields is not None else r.issue_code in codes)}
        if not keys:
            continue
        original_date = _sent_event_date(event)
        _advance_issue_records(state_dir, keys, issue_store.DRAFT_READY, date=original_date)
        _advance_issue_records(state_dir, keys, issue_store.REQUEST_SENT, date=original_date)
        sync_deficiency_deadlines(master=master, state_dir=state_dir, date=original_date,
            settings=settings, notified_case_keys=notified_student_cases(state_dir))


@U.locked_state()
def process_round(
    *,
    submission_paths: list[str | Path],
    master_path: str | Path,
    state_dir: str | Path,
    attachment2_path: str | Path | None = None,
    registries: RegistryPaths | None = None,
    send: bool = False,
    transport: Callable[..., bool | None] | None = None,
    settings: office_settings.OfficeSettings | None = None,
    date: _dt.date | None = None,
    submissions_dir: str | Path | None = None,
    now: _dt.datetime | None = None,
) -> ProcessResult:
    """執行一輪建檔及學生補件通知；重跑同版不重寄，重填新版可再通知。

    順序：建檔 → 缺件對帳（建立 detected 記錄，尚未寄出的不標已通知）→
    draft_ready → 寄信 → request_sent → 把真正寄出的案件標為已通知。
    """
    sd = Path(state_dir)
    paths = [Path(path) for path in submission_paths]
    if submissions_dir is None and paths:
        submissions_dir = paths[0].parent
    with U.build_lock(master_path):
        build = build_current_master(
            attachment2_path=attachment2_path,
            submission_paths=paths,
            master_path=master_path,
            registries=registries, submissions_dir=submissions_dir,
        )
    _audit(sd, "workflow", "master_built", detail={
        "case_count": len(build.master.exams),
        "issue_count": len(build.issues),
    })
    contacts, revisions = _submission_contacts(
        paths, {exam.案件鍵 for exam in build.master.exams},
    )
    contacts = _effective_contacts(build.master, contacts)
    settings = settings or office_settings.load(sd)
    build = replace(build, master=_active_master(build.master, settings),
        issues=[i for i in build.issues if not settings.current_term or not i.get("學期") or i["學期"] == settings.current_term])
    contacts["students"] = _with_fallback_emails(contacts["students"], build.master, settings)
    # 補件期間冒出的新問題隔天（下一輪每日自動化）才寄，信中只列新問題；
    # 同一個問題代碼不重複寄，之後的提醒交給補件截止前的催件（2026-10-01 Q7）
    already = notified_issue_keys(sd)
    first_seen = record_issue_first_seen(sd, build.issues, date=date, prune=True)
    today = date or clock.today()
    notifiable_issues = [
        i for i in build.issues
        if not _already_notified(i, already) and not _new_issue_too_fresh(i, already, first_seen, today)
    ]
    drafts = notify.drafts_for_student_issues(
        notifiable_issues, contacts=contacts, revisions=revisions,
        deadline={e.案件鍵: _effective_deadline(settings, sd, e.案件鍵) for e in build.master.exams},
        draft_case_keys=draft_case_keys(submissions_dir),
    )

    def _issue_key(i: dict) -> tuple[str, str, str]:
        return (str(i.get("案件鍵", "")), str(i.get("問題代碼", "")), str(i.get("欄位", "")))

    student_issues = [i for i in notifiable_issues if i.get("責任") == validation.OWNER_STUDENT]
    sync_deficiency_deadlines(master=build.master, state_dir=sd, date=date, settings=settings,
                              notified_case_keys=notified_student_cases(sd))
    _recover_student_deliveries(build.master, sd, settings)
    _advance_issue_records(
        state_dir, {_issue_key(i) for i in student_issues}, issue_store.DRAFT_READY, date=date,
    )

    log_path = Path(state_dir) / "send_log.json"
    sent: list[dict] = []
    if send:
        ready = [d for d in drafts if _send_window(settings, kind="student", today=date, now=now,
            state_dir=sd, case_key=d.get("case_key"))]
        for draft in ready:
            log = send_log.load(log_path)
            try:
                sent.extend(notify.send_drafts([draft], log=log, transport=transport))
            finally:
                send_log.save(log, log_path)
            _recover_student_deliveries(build.master, sd, settings)
    return ProcessResult(build=build, drafts=drafts, sent=sent, log_path=log_path)


AUTOMATION_STATUS_FILE = "automation_status.json"


@U.locked_state()
def record_automation_status(state_dir: str | Path, *, ok: bool, phase: str = "", error: str = "",
                             notes: tuple[str, ...] = ()) -> None:
    """每日自動化的最後一次結果，讓系辦總覽能顯示失敗（排程器裡的失敗沒人看得到）。"""
    _write_json(Path(state_dir) / AUTOMATION_STATUS_FILE, {
        "at": clock.now().strftime("%Y-%m-%d %H:%M:%S"),
        "ok": ok, "phase": phase, "error": error, "notes": list(notes),
    })


def load_automation_status(state_dir: str | Path) -> dict | None:
    path = Path(state_dir) / AUTOMATION_STATUS_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def run_proposal_agents(
    *, master: Master, state_dir: str | Path, llm, registries: RegistryPaths | None = None,
) -> tuple[list[dict], list[str]]:
    """依主檔跑三個提案型 agent（服務單位對應、來源異常修正、職稱判讀），回傳（新增建議, 備註）。

    只產生「待專員決定」的建議，不改主檔、金額或資格（docs/提案型agent設計.md）。各 agent 自己
    檢查開關（AI 建議頁）並跳過已有建議的主題；逐個 agent 隔離錯誤：一個失敗只記進備註與稽核
    紀錄，其他照跑。不持有全域狀態鎖呼叫模型——寫入建議時 ai_proposals.add 自己短暫上鎖並去重。
    """
    from . import affiliation_agent, ai_proposals, correction_agent, title_agent
    sd = Path(state_dir)
    if llm is None or ai_switch.is_disabled(sd):
        return [], []
    run_id = ai_proposals.new_run_id()

    def distance_table():
        table = DistanceTable.load(registries.distances if registries else None)
        table.aliases = ai_proposals.alias_map(sd)
        return table

    agents = (
        ("affiliation", lambda: affiliation_agent.run(master, sd, distance_table=distance_table(), llm=llm,
                                                      run_id=run_id)),
        ("correction", lambda: correction_agent.run(master, sd, llm=llm, run_id=run_id)),
        ("title", lambda: title_agent.run(master, sd, llm=llm, run_id=run_id)),
    )
    created: list[dict] = []
    notes: list[str] = []
    for name, run in agents:
        try:
            if ai_proposals.load_settings(sd)[name]:
                created += run()
        except Exception as exc:  # noqa: BLE001 - 非核心流程：逐個 agent 隔離
            notes.append(f"AI 建議（{ai_proposals.AGENTS[name]}）本輪略過（{type(exc).__name__}: {exc}）；其他流程照常完成")
            audit_log.record(sd / "audit_log.json", "ai_proposals", "run_failed",
                             detail={"agent": name, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
    return created, notes


def run_daily_automation(
    *,
    submission_paths: list[str | Path],
    master_path: str | Path,
    state_dir: str | Path,
    registries: RegistryPaths | None = None,
    llm=None,
    **kwargs,
) -> AutomationResult:
    """每日自動化：規則流程（`_run_daily_rules`，持有狀態鎖）→ 教授回信與信箱 agent（`_run_mail_stage`）
    → 提案型 agent。後兩段在鎖外呼叫模型，只在寫入時短暫上鎖（審查 F09）：模型慢回覆不會卡住
    學生送出、專員修正與委員回填。

    參數與時段行為見 `_run_daily_rules`。正式檢核開始後（有追蹤快照時）且有模型、AI 總開關
    未關閉時，再依本輪主檔跑提案型 agent，新增的建議放在 `proposals`、全部待專員決定。
    提案型 agent 不是核心流程：出錯只記進 notes 與稽核紀錄（`ai_proposals/run_failed`）。
    """
    fetch_replies = kwargs.pop("fetch_replies", None)
    classifier = kwargs.pop("classifier", None)
    result = _run_daily_rules(submission_paths=submission_paths, master_path=master_path,
                              state_dir=state_dir, registries=registries, **kwargs)
    if result.tracking is None:   # 填寫期間只建預覽版主檔，不讀信、不產生建議
        return result
    if fetch_replies is not None:
        result = _run_mail_stage(result, state_dir=state_dir, fetch_replies=fetch_replies, classifier=classifier,
                                 llm=llm, send=kwargs.get("send", False), transport=kwargs.get("transport"),
                                 date=kwargs.get("date"), now=kwargs.get("now"), settings=kwargs.get("settings"))
    try:
        proposals, notes = run_proposal_agents(master=result.process.build.master, state_dir=state_dir,
                                               llm=llm, registries=registries)
    except Exception as exc:  # noqa: BLE001
        proposals, notes = [], [f"AI 建議本輪略過（{type(exc).__name__}: {exc}）；其他流程照常完成"]
        audit_log.record(Path(state_dir) / "audit_log.json", "ai_proposals", "run_failed",
                         detail={"error": f"{type(exc).__name__}: {exc}"[:500]})
    return replace(result, proposals=tuple(proposals), notes=result.notes + tuple(notes))


@U.locked_state()
def _run_daily_rules(
    *,
    submission_paths: list[str | Path],
    master_path: str | Path,
    state_dir: str | Path,
    attachment2_path: str | Path | None = None,
    registries: RegistryPaths | None = None,
    submissions_dir: str | Path | None = None,
    send: bool = False,
    transport: Callable[..., bool | None] | None = None,
    date: _dt.date | None = None,
    now: _dt.datetime | None = None,
    settings: office_settings.OfficeSettings | None = None,
    templates: TemplatePaths | None = None,
) -> AutomationResult:
    """依系辦設定的時段，一次做完「今天該做的事」（見 settings.py 的時段說明）。

    - 填寫截止日（含）前／未設定／正式檢核尚未開始：只建預覽版主檔，不寄信、不動追蹤狀態。
    - 補件期間：建檔＋首次學生補件通知（`process_round`）→ 套用全系統補件
      截止日並對帳落盤 → 排程截止前催件、教授確認、附件6催繳。
    - 補件截止後：建檔＋對帳；停止所有學生通知，其餘提醒照常。
    - 補件期間與截止後，都對已放行案件中撥帳資料不齊的委員寄信索取個資，並把附件4
      （全學期一份）寄給學生資料齊全的指導教授參考。讀信與信箱 agent 在 `_run_mail_stage`（鎖外）。

    正式檢核已開始卻找不到附件2 檔案時拋錯——沒有附件2 就沒有正式案件，不能
    拿空主檔去起算期限。尚未到每日發送時刻時，`send=True` 也只預覽。任何一步
    拋例外就整個中止、不吞掉、不繼續跑後面幾步。多久呼叫一次由外部排程決定。
    """
    now = now or clock.now()
    today = date or now.date()
    sd = Path(state_dir)
    settings = settings or office_settings.load(sd)
    phase = settings.phase_on(today)
    freeze_cutoff(state_dir=sd, master_path=master_path, submissions_dir=submissions_dir,
                  registries=registries, settings=settings)
    notes: list[str] = []
    ai_stopped = ai_switch.disabled_reason(sd)
    if ai_stopped:
        # 緊急停止：不判讀、不自動放行、不自動回信；規則流程（建檔、檢核、通知、催件）照常
        notes.append(f"AI 總開關已關閉（{ai_stopped}）：本輪不呼叫模型、不執行 agent 自動動作，來信全部交專員")
    if send and now.hour < settings.send_hour:
        notes.append(f"尚未到每日發送時刻（{settings.send_hour}:00），本次只預覽")
        send = False
    log_path = sd / "send_log.json"

    if phase in (office_settings.PHASE_UNSET, office_settings.PHASE_FILLING,
                 office_settings.PHASE_AWAITING_ATT2):
        with U.build_lock(master_path):
            build = build_current_master(
                submission_paths=submission_paths, master_path=master_path,
                registries=registries, attachment2_path=attachment2_path,
                submissions_dir=submissions_dir,
            )
        if phase == office_settings.PHASE_AWAITING_ATT2:
            if settings.att2_first_uploaded is None:
                notes.append("填寫截止日已過但本期尚未上傳附件2：無法正式檢核，只建預覽版主檔")
            else:
                notes.append(f"正式檢核從 {settings.check_start} 開始：今天只建預覽版主檔，不寄信")
        else:
            notes.append("填寫截止日前：只建預覽版主檔，不寄信")
        return AutomationResult(
            phase=phase,
            process=ProcessResult(build=build, drafts=[], sent=[], log_path=log_path),
            tracking=None,
            reminders=ReminderPlan(scheduled=[], internal_todos=[]),
            reminders_sent=[],
            notes=tuple(notes),
        )

    attachment2 = attachment2_path or (registries.attachment2 if registries else None)
    if attachment2 is None or not Path(attachment2).is_file():
        raise ValueError("填寫截止日已過但尚未上傳附件2（Portal 匯出），無法建立正式案件；請系辦先上傳")

    student_round = phase == office_settings.PHASE_DEFICIENCY or any(
        case_access.is_unlocked(sd, e.案件鍵, today) for e in load_current_master(master_path).exams
    ) if Path(master_path).is_file() else phase == office_settings.PHASE_DEFICIENCY
    if student_round:
        process = process_round(
            submission_paths=submission_paths, master_path=master_path, state_dir=state_dir,
            attachment2_path=attachment2_path, registries=registries, send=send, transport=transport,
            settings=settings, date=today, submissions_dir=submissions_dir, now=now,
        )
    else:
        with U.build_lock(master_path):
            build = build_current_master(
                submission_paths=submission_paths, master_path=master_path,
                registries=registries, attachment2_path=attachment2_path,
                submissions_dir=submissions_dir,
            )
        process = ProcessResult(build=build, drafts=[], sent=[], log_path=log_path)
        notes.append("補件截止日已過：停止學生補件通知與催件")
    sync_deficiency_deadlines(master=process.build.master, state_dir=sd, date=today, settings=settings,
                              notified_case_keys=notified_student_cases(sd))
    tracking = tracking_snapshot(master=process.build.master, state_dir=sd, persist=True)
    plan = plan_reminders(
        master=process.build.master, state_dir=sd, submissions_dir=submissions_dir,
        resolve_recipients=send, date=today, settings=settings, include_student=student_round,
    )
    reminders_sent: list[ScheduledReminder] = []
    if send and plan.scheduled:
        reminders_sent = send_reminder_plan(state_dir=sd, reminders=plan.scheduled,
            transport=transport, date=today, settings=settings, now=now, master=process.build.master)
    committee_drafts, committee_sent = process_committee_requests(
        master=process.build.master, state_dir=sd, send=send, transport=transport, date=today,
    )
    advisor = process_advisor_confirmations(
        master=process.build.master, state_dir=sd, submissions_dir=submissions_dir, send=send,
        transport=transport, date=today, settings=settings,
        template=templates.attachment4 if templates else None,
    )
    notes.extend(advisor.notes)
    tracking = tracking_snapshot(master=process.build.master, state_dir=sd, persist=True)
    return AutomationResult(
        phase=phase, process=process, tracking=tracking, reminders=plan,
        reminders_sent=reminders_sent, notes=tuple(notes),
        committee_drafts=tuple(committee_drafts), committee_sent=tuple(committee_sent),
        advisor=advisor,
    )


def _run_mail_stage(
    result: AutomationResult, *, state_dir, fetch_replies: Callable[[], list],
    classifier: advisor_confirmation.Classifier | None, llm, send: bool, transport, date, now, settings,
) -> AutomationResult:
    """讀信 → 教授回信核對與判讀 → 信箱 agent。在全域狀態鎖外執行（審查 F09）：
    模型呼叫不持鎖，寫入收件匣、信件紀錄時各自短暫上鎖並去重。

    信箱 agent 不是核心流程：出錯只跳過並留紀錄，不能拖垮附件4 與催件。
    """
    sd = Path(state_dir)
    now = now or clock.now()
    today = date or now.date()
    settings = settings or office_settings.load(sd)
    if send and now.hour < settings.send_hour:   # 與規則流程同一個發送時刻判斷
        send = False
    if ai_switch.disabled_reason(sd):
        classifier, llm = None, None
    master = result.process.build.master
    notes = list(result.notes)
    mails = fetch_replies()
    replies = ingest_advisor_replies(master=master, state_dir=sd, mails=mails, classifier=classifier, date=today)
    mailbox: list = []
    try:
        mailbox = run_mailbox_agent(master=master, state_dir=sd, mails=mails, llm=llm,
                                    send=send, transport=transport, date=today, settings=settings)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"信箱 agent 本輪略過（{type(exc).__name__}: {exc}）；其他流程照常完成")
        audit_log.record(sd / "audit_log.json", "mailbox_agent", "run_failed",
                         detail={"error": f"{type(exc).__name__}: {exc}"[:500]})
    with U.state_lock(sd):
        tracking = tracking_snapshot(master=master, state_dir=sd, persist=True)
    return replace(result, tracking=tracking, notes=tuple(notes), replies=tuple(replies), mailbox=tuple(mailbox))


def generate_fee_preview(*, master_path, state_dir, output_dir, template):
    """Explicitly non-deliverable preview; never changes review or approval state."""
    import openpyxl
    from .generators import attachment6
    master = load_current_master(master_path)
    issues = [*validation.check(master), *_unreviewed_case_issues(master, state_dir)]
    output = Path(output_dir)
    result = []
    for summary in attachment6.generate(master, output, template=template, prefill=True):
        source = Path(summary["輸出"])
        wb = openpyxl.load_workbook(source)
        ws = wb.active
        ws.cell(1, 1).value = "資料未齊 — 預覽版，不得交付或核撥"
        ws.oddHeader.center.text = "資料未齊 — 預覽版，不得交付或核撥"
        ws.oddHeader.center.size = 16
        details = wb.create_sheet("待處理事項")
        details.append(["本檔僅供預覽，所有個資留白，不代表案件已放行或教授已確認"])
        details.append(["案件", "問題", "說明"])
        for item in issues:
            details.append([item.get("案件鍵", ""), item.get("問題代碼", ""), item.get("說明", "")])
        target = source.with_name("預覽-資料未齊-" + source.name)
        wb.save(target)
        source.unlink()
        result.append(target)
    return result
