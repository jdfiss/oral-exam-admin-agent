# -*- coding: utf-8 -*-
"""口試資料補充介面與系辦工作台。

案件來源是系辦於填寫截止日上傳的附件2（Portal 匯出）；學生頁只補充附件 3
所需資料，送出即為有效版本，不經系辦審核（2026-09-23 定案）。截止日匯入後
由 `koushi.ingest.read_student_submissions` 以學期＋學號併入附件2 的案件。
學生登入方式仍待 Portal 端能力確定，現有學號登入僅為開發原型，本模組不可
直接當作正式對外服務。

案件鍵＝學期-學號（見 submit()），學期由學生在新增申請時選定；同一
學號如果跨學期有多筆口試案件（重考、延畢等），各自是獨立記錄，不會
互相覆蓋。案件鍵的學號永遠取自登入 session、不接受表單或路徑參數裡的
值，這樣學生才不可能建立或覆寫別人的案件；學期與學號都經過格式檢查
（僅允許英數字），避免被用來跳出 submissions 目錄。

送出永遠是覆寫自己那個案件的記錄；覆寫前把舊版整份搬進 versions/
留底（檔名帶版本號與時間戳），resolved 之後若又被判定缺件也查得到
每一版填了什麼，不會因為覆寫就丟掉歷史。

啟動前請設定環境變數 KOUSHI_WEB_SECRET_KEY（Flask session 簽章金鑰）
與 KOUSHI_OFFICE_PASSWORD（系辦登入密碼）；沒設定會用隨機值頂替，
伺服器仍能啟動但系辦登不進去，`main()` 會印警告到 stderr。所有 POST
路由都需要表單裡帶正確的 csrf_token（值來自對應 GET 頁面的 session），
防止跨站偽造請求。
"""

from __future__ import annotations

import calendar
import io
import json
import os
import re
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import date, datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, abort, render_template, request, redirect, url_for, flash, session,
    send_file, send_from_directory, g,
)

from .. import ai_switch, clock, committee_verification, judge
from ..runtime import load_local_env
load_local_env()
from .. import config as C
from .. import util as U
from .. import send_log, issue_store
from .. import settings as office_settings
from .. import advisor_confirmation, ai_digest, case_access, committee_requests, corrections, mailbox_agent
from ..application import (
    submit_application, office_files_status,
    save_office_registry_file, auto_build_if_ready, load_auto_build_status,
    notified_student_cases, sync_deficiency_deadlines, submission_block_reason,
    submission_self_check, tracking_snapshot,
    load_current_master, generate_artifacts, mark_case_resubmitted,
    review_queue, clear_case_review, record_confirmation, record_collection, request_token,
    load_automation_status, student_case_status, record_student_submission, PUBLIC_URL_ENV,
    handle_advisor_reply, record_att2_upload, att2_changes, acknowledge_att2_removed,
    first_deficiency_mail_delay, open_cases_for_term, STUDENT_NOT_OPEN_MESSAGE,
    correction_rows, save_case_correction, save_draft, load_draft,
    advisor_reply_cards, advisor_request_states,
)
from ..responsibility import STUDENT
from ..scheduler import ReminderScheduler
from ..workflow import RegistryPaths, TemplatePaths


_SECRET_KEY_ENV = "KOUSHI_WEB_SECRET_KEY"
_OFFICE_PASSWORD_ENV = "KOUSHI_OFFICE_PASSWORD"


@dataclass(frozen=True)
class WebConfig:
    """Web 模組的全部可變設定，測試一次換掉整個 config 就好。"""

    submissions_dir: Path
    master_path: Path
    state_dir: Path
    registries: RegistryPaths
    doc_output_root: Path
    office_password: str
    demo_root: Path | None = None
    production: bool = False
    judge_password: str | None = None   # 評審模式（koushi.judge）；只在示範資料夾上設定

    @property
    def judge_mode(self) -> bool:
        return bool(self.demo_root and self.judge_password)

    @property
    def templates(self) -> TemplatePaths:
        if self.demo_root:
            from ..demo import template_paths
            return template_paths(self.demo_root)
        return TemplatePaths(attachment4=C.TPL_ATT4, attachment6=C.TPL_ATT6)

    @property
    def versions_dir(self) -> Path:
        return self.submissions_dir / "versions"

    @property
    def build_registries(self) -> RegistryPaths:
        """建檔用的來源路徑：委員回填一律取自本設定的 state_dir，不依賴呼叫端記得設定。"""
        return replace(self.registries,
                       committee_replies=self.state_dir / committee_requests.REPLIES_FILE,
                       corrections=self.state_dir / corrections.FILENAME)

    @classmethod
    def from_env(cls) -> WebConfig:
        if judge.enabled() and not os.environ.get("KOUSHI_DEMO_ROOT"):
            raise RuntimeError("評審模式只能搭配示範資料（python -m koushi.demo judge），不可用於正式資料")
        if os.environ.get("KOUSHI_DEMO_ROOT"):
            from ..demo import web_config
            return web_config(Path(os.environ["KOUSHI_DEMO_ROOT"]))
        return cls(
            production=os.environ.get("KOUSHI_ENV") == "production",
            submissions_dir=C.SRC_SUBMISSIONS_DIR,
            master_path=C.MASTER,
            state_dir=C.STATE_DIR,
            registries=RegistryPaths(
                members=C.REG_MEMBER, distances=C.DISTANCE_CSV, attachment2=C.PORTAL_ATT2,
            ),
            doc_output_root=C.OUTPUT_ROOT / "web",
            office_password=os.environ.get(_OFFICE_PASSWORD_ENV) or secrets.token_urlsafe(16),
        )

app = Flask(__name__)
app.secret_key = os.environ.get(_SECRET_KEY_ENV) or secrets.token_hex(32)
from . import labels as _labels  # noqa: E402 — 畫面中文標籤（學期、問題代碼、軌跡步驟）
_labels.register(app)


def configure_app(flask_app: Flask, config: WebConfig) -> None:
    flask_app.config["_web"] = config
    # O03／O04：正式環境 cookie 只走 HTTPS；上傳上限 20MB（超過回 413）。
    flask_app.config.update(
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.production, MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    )


def cfg() -> WebConfig:
    return app.config["_web"]


configure_app(app, WebConfig.from_env())
# AI 總開關：make_llm() 沒帶狀態目錄時，以目前網頁設定的狀態目錄判斷是否已緊急停止
ai_switch.use_state_dir(lambda: cfg().state_dir)


@app.after_request
def _no_store_sensitive_pages(response):
    """O05：系辦、委員、學生頁面與下載含個資，不留瀏覽器／代理快取，連結不外洩來源網址。"""
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if not request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# 畫面上一律稱正式文件名稱，不說「附件2／附件4／附件6」（2026-10-13 使用者要求）。只改網頁上看得到的字：
# 標籤之間的文字與 title／placeholder／aria-label；不動原信全文（pre）、可編輯的回信（textarea）、
# 程式碼、連結與檔名（後面接 - _ 或數字的不換），所以寄出的信、下載檔名都不受影響。
_ATTACHMENT_NAMES = [
    (re.compile(r"(學位)?口試一覽表?\s?[（(]附件\s?[4四][）)]"), "學位口試一覽表"),
    (re.compile(r"(學生)?口試費清冊\s?[（(]附件\s?[6六][）)]"), "學生口試費清冊"),
    (re.compile(r"附件\s?[4四]\s?[（(]?(學位)?口試一覽表?[）)]?"), "學位口試一覽表"),
    (re.compile(r"附件\s?[6六]\s?[（(]?(學生)?口試費清冊[）)]?"), "學生口試費清冊"),
    (re.compile(r"附件\s?[4四](?![\d\-_])"), "學位口試一覽表"),
    (re.compile(r"附件\s?[6六](?![\d\-_])"), "學生口試費清冊"),
    (re.compile(r"附件\s?[2二](?![\d\-_])"), "系所審核名單"),
]
_HTML_PIECES = re.compile(r"(<(textarea|pre|script|style)\b.*?</\2\s*>|<!--.*?-->|<[^>]*>)", re.S | re.I)
_VISIBLE_ATTRS = re.compile(r'\b(title|placeholder|aria-label)="([^"]*)"')


def _rename_attachments(text: str) -> str:
    for pattern, name in _ATTACHMENT_NAMES:
        text = pattern.sub(name, text)
    return text


def _rename_attachments_in_html(html: str) -> str:
    out, pos = [], 0
    for m in _HTML_PIECES.finditer(html):
        out.append(_rename_attachments(html[pos:m.start()]))
        piece = m.group(0)
        if not m.group(2) and not piece.startswith("<!--"):      # 一般標籤：只換看得到的屬性
            piece = _VISIBLE_ATTRS.sub(lambda a: f'{a.group(1)}="{_rename_attachments(a.group(2))}"', piece)
        out.append(piece)
        pos = m.end()
    out.append(_rename_attachments(html[pos:]))
    return "".join(out)


@app.after_request
def _official_attachment_names(response):
    if response.mimetype == "text/html" and not response.direct_passthrough:
        response.set_data(_rename_attachments_in_html(response.get_data(as_text=True)))
    return response


_OFFICE_FILE_LABELS = {
    "attachment2": "附件2（Portal 匯出）",
    "distances": "交通費歷史資料",
    "members": "口試委員歷史資料",
}

# 通訊記錄頁（send_log.SendEvent）用的中文標籤；type 對照 koushi/drafts.py
# 的 TYPE_* 常數，status 對照 koushi/send_log.py 的狀態常數。
_SEND_TYPE_LABELS = {
    "deficiency": "缺件補件通知",
    "confirmation": "附件4 提醒（已停用）",
    "collection": "附件6催繳通知",
    "attachment4": "附件4 寄指導教授參考",
    "committee_request": "委員個資索取",
    "committee_link_update": "委員填寫連結更新",
}
_SEND_STATUS_LABELS = {
    send_log.DRAFT: "草稿中",
    send_log.APPROVED: "已核准待寄",
    send_log.REJECTED: "已退回不寄",
    send_log.SENT: "已寄出",
    send_log.FAILED: "寄送失敗",
}

# 案件詳情抽屜的缺件生命週期時間軸（issue_store.IssueRecord.status）用的中文標籤。
_ISSUE_STATUS_LABELS = {
    issue_store.DETECTED: "系統已偵測",
    issue_store.DRAFT_READY: "通知草稿已產生",
    issue_store.REQUEST_SENT: "已寄出通知",
    issue_store.RESUBMITTED: "學生已重新送出",
    issue_store.REVALIDATED: "已重新檢核",
    issue_store.RESOLVED: "已解決",
}

# 學期／學號只允許英數字：兩者都會被組進案件鍵、再組進檔案路徑，格式
# 檢查是唯一防止路徑跳脫 submissions 目錄的手段，不能只靠「路徑存不存在」。
_TERM_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")
_STUDENT_ID_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
_CASE_KEY_RE = re.compile(r"^[A-Za-z0-9]{1,10}-[A-Za-z0-9]{1,20}$")
_LINK_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


def _ensure_dir():
    cfg().submissions_dir.mkdir(parents=True, exist_ok=True)


def _student_submissions(student_id: str) -> list[dict]:
    """該學號自己送出過的所有案件記錄（跨學期）。"""
    _ensure_dir()
    out = []
    for f in sorted(cfg().submissions_dir.glob("*.json"), reverse=True):
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("student_id") != student_id:
            continue
        out.append({
            "case_key": f.stem,
            "term": data.get("term", ""),
            "version": data.get("version", 1),
            "committee_count": len(data.get("committees", [])),
            "submitted_at": data.get("submitted_at", ""),
        })
    return out


def _load_submissions() -> list[dict]:
    """系辦儀表板用：全部案件的送出記錄（不分學生）。"""
    _ensure_dir()
    settings = office_settings.load(cfg().state_dir)
    degrees = _degrees()
    submissions = []
    for f in sorted(cfg().submissions_dir.glob("*.json"), reverse=True):
        data = json.loads(f.read_text(encoding="utf-8"))
        submissions.append({
            "hints": submission_self_check(data, settings=settings, degree=degrees.get(f.stem)),
            "id": f.stem,
            "term": data.get("term", ""),
            "student_id": data.get("student_id", ""),
            "student_email": data.get("student_email", ""),
            "exam_date": data.get("exam_date", ""),
            "submitted_at": data.get("submitted_at", ""),
            "version": data.get("version", 1),
            "committee_count": len(data.get("committees", [])),
        })
    return submissions


def _degrees() -> dict[str, str]:
    """案件鍵 → 學位別（碩／博）；主檔還沒建過時回空 dict。"""
    if not cfg().master_path.is_file():
        return {}
    try:
        return {e.案件鍵: e.學位別 for e in load_current_master(cfg().master_path).exams}
    except (OSError, ValueError, U.StateDataError):
        return {}


def _office_tracking():
    """系辦儀表板／文件中心共用：主檔還沒建過就回 None，畫面自行處理不顯示。"""
    if not cfg().master_path.is_file():
        return None
    master = load_current_master(cfg().master_path)
    return tracking_snapshot(master=master, state_dir=cfg().state_dir)


def _doc_output_dir(phase: str) -> Path:
    return cfg().doc_output_root / phase


def _doc_status_path() -> Path:
    return cfg().doc_output_root / "documents_status.json"


def _load_doc_status() -> dict:
    path = _doc_status_path()
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_doc_status(phase: str, entry: dict) -> None:
    path = _doc_status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _load_doc_status()
    if entry.get("status") == "failed" and phase in data:
        data[phase]["last_attempt"] = entry
    else:
        data[phase] = entry
    U.atomic_write_json(data, path)


_CSRF_SESSION_KEY = "_csrf_token"


def _csrf_token() -> str:
    """取得（必要時建立）這個 session 的 CSRF token，供表單埋隱藏欄位。"""
    token = session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token


app.jinja_env.globals["csrf_token"] = _csrf_token


@app.before_request
def _csrf_protect():
    # 只有這四個表單會送 POST，且都先經過對應的 GET 頁面拿到 token，
    # 正常操作不受影響；沒有先 GET 過（或 token 對不上）一律拒絕。
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        expected = session.get(_CSRF_SESSION_KEY, "")
        if not expected or not secrets.compare_digest(sent, expected):
            abort(400, "表單已過期，請重新整理頁面再試一次")


# ── 評審模式權限（伺服器端白名單，不只是隱藏按鈕）──────────────
# 評審可檢視所有頁面，並在示範資料上做人工關卡操作（放行、核准／退回 AI 建議、
# 處理信箱 agent、問答助理、模擬回信、重設情境、AI 緊急停止——只影響本輪示範狀態目錄）；
# 可下載本輪產生的附件4／附件6（示範假資料）與已遮蔽的 agent 軌跡 JSON。
# 不可改系統設定、上傳來源檔、撤銷對照、匯出 Excel 總表／通訊記錄或下載封存檔。
# 未列入的 POST 一律拒絕（預設拒絕）。
JUDGE_POST_ALLOWED = frozenset({
    "login_judge", "login_office", "login_student", "submit", "change_request", "committee_reply",
    "clear_case", "save_correction", "unlock_case", "dismiss_change_requests", "acknowledge_removed_case",
    "record_committee_reply", "record_confirmation_route", "handle_advisor_reply_route",
    "record_collection_route", "mailbox_action", "mailbox_approve_all", "mailbox_reply", "mailbox_mark", "run_ai_proposals", "decide_ai_proposal",
    "office_assistant_ask", "generate_documents", "source_issues_page", "office_demo",
    "judge_outbox_reply", "ai_switch_toggle",
})
JUDGE_GET_DENIED = frozenset({
    "office_overview_export", "office_communications_export",
    "download_archive_file", "archived_term",
})
# 稽核只記這些欄位（不記密碼、驗證碼、撥帳資料）
_JUDGE_AUDIT_FIELDS = ("action", "decision", "phase", "scenario", "days", "agent", "status", "message_id",
                       "action_id", "target", "field", "level", "question", "outbox_id", "disabled", "reason",
                       "note", "value")


def _is_judge() -> bool:
    return bool(session.get("judge"))


def _judge_audit(action: str, **detail) -> None:
    from .. import audit_log
    audit_log.record(cfg().state_dir / "audit_log.json", "judge", action,
                     detail={"actor": judge.label(), **detail})


@app.before_request
def _judge_guard():
    if not _is_judge():
        return None
    endpoint = request.endpoint or ""
    if not cfg().judge_mode:
        session.clear()  # 評審 session 只在評審模式有效
        abort(403, "評審模式未啟用")
    if request.method == "POST":
        allowed = endpoint in JUDGE_POST_ALLOWED
    else:
        allowed = endpoint not in JUDGE_GET_DENIED
    if not allowed:
        _judge_audit("denied", endpoint=endpoint, method=request.method, path=request.path)
        abort(403, f"{judge.label()}帳號沒有這項操作的權限（示範環境僅開放檢視與人工關卡操作）")
    if request.method == "POST":
        fields = {k: request.form.get(k, "")[:200] for k in _JUDGE_AUDIT_FIELDS if k in request.form}
        _judge_audit("action", endpoint=endpoint, path=request.path, fields=fields)
    return None


_UNLOCKED_POSTS = frozenset({"office_assistant_ask", "run_ai_proposals"})


@app.before_request
def _lock_mutation():
    # 等模型回應的請求不能佔住全域狀態鎖（否則其他人按任何按鈕都會卡住）：問答助理只讀資料；
    # 產生 AI 建議只讀主檔，寫入建議時 ai_proposals.add 自己會短暫上鎖並去重
    if request.method == "POST" and request.endpoint not in _UNLOCKED_POSTS:
        guard = U.state_lock(cfg().state_dir)
        guard.__enter__()
        g.mutation_guard = guard
        active = office_settings.load(cfg().state_dir).current_term
        case_match = re.match(r"^/office/case/([^/]+)", request.path)
        case_key = case_match.group(1) if case_match else ""
        if active and case_key and not case_key.startswith(active + "-"):
            abort(409, "封存學期不可修改，請切至目前學期")
        from ..application import freeze_cutoff
        freeze_cutoff(state_dir=cfg().state_dir, master_path=cfg().master_path,
            submissions_dir=cfg().submissions_dir, registries=cfg().build_registries)


@app.teardown_request
def _unlock_mutation(error=None):
    guard = g.pop("mutation_guard", None)
    if guard is not None:
        guard.__exit__(None, None, None)


def _status_path() -> Path:
    return Path(cfg().master_path).parent / "auto_build_status.json"


def _try_auto_build():
    """學生送出或系辦上傳建檔資料後呼叫；三檔未齊或建檔失敗都不影響已完成的動作。

    建檔成功且已過填寫截止日時，同步缺件追蹤並套用系辦設定的補件截止日；
    填寫期間只建預覽、不起算任何期限。建檔失敗時不執行（避免用不完整的
    主檔起算期限）。併行保護由 auto_build_if_ready 內的 build_lock 負責。
    """
    result = auto_build_if_ready(
        submissions_dir=cfg().submissions_dir, master_path=cfg().master_path,
        registries=cfg().build_registries, status_path=_status_path(),
    )
    if result.status == "built":
        settings = office_settings.load(cfg().state_dir)
        if settings.phase_on(clock.now().date()) not in (
            office_settings.PHASE_UNSET, office_settings.PHASE_FILLING,
            office_settings.PHASE_AWAITING_ATT2,
        ):
            sync_deficiency_deadlines(master=result.build.master, state_dir=cfg().state_dir,
                                      settings=settings,
                                      notified_case_keys=notified_student_cases(cfg().state_dir))
    return result


def _flash_auto_build(result):
    if result.status == "built":
        n = len(result.build.master.exams)
        flash(f"案件資料已自動更新（共 {n} 案）。", "success")
    elif result.status == "failed":
        flash(f"案件資料自動更新失敗，畫面仍是上次的資料：{result.error}", "error")
    elif result.status == "busy":
        flash("資料已儲存；另一個建檔正在進行，主檔稍後更新", "info")
    # skipped：三檔尚未備齊，不用每次上傳都提醒


def login_required(role=None):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if "role" not in session:
                flash("請先登入", "error")
                return redirect(url_for("login"))
            if role and session["role"] != role:
                flash("無此頁面的權限", "error")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ── 登入 ──────────────────────────────────────────────

@app.route("/")
def login():
    if "role" in session:
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/login/student", methods=["POST"])
def login_student():
    if cfg().production:
        abort(403, "正式環境須使用Portal身分驗證；介接完成前不開放學號登入")
    student_id = request.form.get("student_id", "").strip()
    if not _STUDENT_ID_RE.match(student_id):
        flash("請輸入正確格式的學號（僅限英數字）", "error")
        return redirect(url_for("login"))
    session["role"] = "student"
    session["student_id"] = student_id
    return redirect(url_for("dashboard"))


@app.route("/login/office", methods=["POST"])
def login_office():
    password = request.form.get("password", "")
    if password != cfg().office_password:
        flash("密碼錯誤", "error")
        return redirect(url_for("login"))
    session.pop("judge", None)
    session["role"] = "office"
    return redirect(url_for("office_overview"))


@app.route("/login/judge", methods=["POST"])
def login_judge():
    """評審專用登入：只在評審模式（示範資料、不寄信）存在，密碼與系辦分開。"""
    if not cfg().judge_mode or not judge.login_enabled():
        abort(404)
    password = request.form.get("password", "")
    if not secrets.compare_digest(password.encode(), (cfg().judge_password or "").encode()):
        _judge_audit("login_failed")
        flash("密碼錯誤", "error")
        return redirect(url_for("login"))
    csrf = session.get(_CSRF_SESSION_KEY)
    session.clear()  # 不沿用登入前的學生／系辦身分
    session.update(role="office", judge=True, handler=judge.label())
    if csrf:
        session[_CSRF_SESSION_KEY] = csrf
    _judge_audit("login")
    return redirect(url_for("office_overview"))


@app.route("/logout")
def logout():
    if _is_judge() and cfg().judge_mode:
        _judge_audit("logout")
    session.clear()
    return redirect(url_for("login"))


# ── 儀表板 ────────────────────────────────────────────

@app.route("/dashboard")
@login_required()
def dashboard():
    role = session["role"]
    if role == "student":
        student_id = session["student_id"]
        cases = _student_submissions(student_id)
        settings = office_settings.load(cfg().state_dir)
        current_term = settings.current_term
        # 本期案件即使學生還沒填，截止後也可能有待補項目（例如尚未填委員名單）。
        case_keys = [c["case_key"] for c in cases]
        if current_term and U.case_key(current_term, student_id) not in case_keys:
            case_keys.insert(0, U.case_key(current_term, student_id))
        statuses = {key: student_case_status(key, cfg().state_dir) for key in case_keys}
        current_key = U.case_key(current_term, student_id) if current_term else None
        return render_template("student_dashboard.html", cases=cases, student_id=student_id,
                               current_term=current_term, statuses=statuses,
                               student_open=settings.student_open,
                               has_draft=bool(current_key and load_draft(cfg().submissions_dir, current_key)),
                               change_requests=case_access.requests(cfg().state_dir, case_key=current_key)
                               if current_key else [],
                               progress=_student_progress(current_key, len(statuses[current_key].pending))
                               if current_key else [],
                               current_key=U.case_key(current_term, student_id) if current_term else None)
    return redirect(url_for("office_overview"))


def _student_progress(case_key: str, pending_count: int) -> list[tuple[str, str, str]]:
    """學生首頁的整體進度（唯讀）：(步驟, 狀態, 樣式)。只說進度，不顯示系辦內部例外或委員個資。"""
    if not cfg().master_path.is_file():
        return []
    try:
        master = load_current_master(cfg().master_path)
    except (OSError, ValueError, U.StateDataError):
        return []
    exam = master.by_key().get(case_key)
    if exam is None:
        return []
    from .. import case_review
    rows = [("你的補充資料", "還有必須補正的項目" if pending_count else "已齊全", "missing" if pending_count else "ready")]
    cleared = case_key in case_review.cleared_keys(master, cfg().state_dir)
    rows.append(("系辦審核（放行）", "已放行" if cleared else
                 ("等你補件完成後由系辦審核" if pending_count else "系辦審核中"), "ready" if cleared else "review"))
    tracking = _office_tracking()
    confirm = next((r for r in tracking.confirmation.requests if case_key in r.case_keys), None) if tracking else None
    # 2026-10-12 起附件4 只寄給指導教授參考，教授不需回覆；資料齊全就寄，不等系辦放行
    confirm_text = {"draft": "資料齊全後寄給指導教授參考", "pending": "已寄給指導教授參考（教授不需回覆）",
                    "confirmed": "已寄給指導教授，教授回覆資料無誤",
                    "modified": "指導教授回信要求修改，系辦處理中"}
    status = confirm.status if confirm else "draft"
    rows.append(("口試一覽表（附件4）寄給指導教授", confirm_text.get(status, confirm_text["pending"]),
                 "review" if status in ("draft", "modified") else "ready"))
    requests = [r for r in committee_requests.plan(master, cfg().state_dir) if case_key in r.case_keys]
    if not cleared:
        committee = ("放行後系統直接向委員索取，不需要你處理", "neutral")
    elif any(not r.received for r in requests):
        committee = ("系統已向委員索取，等待委員回填（不需要你處理）", "review")
    else:
        committee = ("委員資料已齊全", "ready")
    rows.append(("口試委員撥款資料", *committee))
    return rows


# ── 系辦工作台（三頁式：工作總覽／匯入資料／文件中心）──────────

def _review_rows():
    """人工關卡清單；主檔還沒建過時回傳空清單。"""
    if not cfg().master_path.is_file():
        return []
    return review_queue(load_current_master(cfg().master_path), cfg().state_dir)


def _overview_rows(submissions: list[dict], deficiency_by_case: dict) -> list[dict]:
    """附件2 上傳後以附件2 案件為主：沒填寫的標「未填寫」，附件2 查無的填寫標「附件2 查無」。

    附件2 上傳前只能列出學生填寫（review-2026-09-23 P1-6／P1-8）。
    """
    by_key = {s["id"]: s for s in submissions}
    master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
    settings = office_settings.load(cfg().state_dir)
    late_added = att2_changes(cfg().state_dir, settings).late_added
    rows = []
    if master and master.exams:
        for exam in master.exams:
            sub = by_key.get(exam.案件鍵)
            email = (sub or {}).get("student_email") or settings.fallback_student_email(exam.學號) or ""
            rows.append({
                "id": exam.案件鍵, "term": exam.學期, "student_id": exam.學號, "student_name": exam.姓名,
                "exam_date": exam.口試日期.isoformat() if exam.口試日期 else exam.口試日期原文,
                "status": ("已填寫" if sub else "未填寫") if exam.生效 else "已撤銷",
                "version": (sub or {}).get("version"), "hints": (sub or {}).get("hints", []),
                "student_email": email, "no_email": not email,
                "deficiency_count": _student_items(deficiency_by_case.get(exam.案件鍵, [])),
                "other_count": len(deficiency_by_case.get(exam.案件鍵, []))
                - _student_items(deficiency_by_case.get(exam.案件鍵, [])),
                "late_added": exam.案件鍵 in late_added,
            })
        known = {exam.案件鍵 for exam in master.exams}
        for sub in submissions:
            if sub["id"] not in known:
                rows.append({**sub, "student_name": "", "status": "附件2查無", "no_email": False,
                             "deficiency_count": 0, "other_count": 0})
        return rows
    for sub in submissions:
        items = deficiency_by_case.get(sub["id"], [])
        rows.append({**sub, "student_name": "", "status": "已填寫", "no_email": False,
                     "deficiency_count": _student_items(items), "other_count": len(items) - _student_items(items)})
    return rows


def _student_items(items) -> int:
    """學生責任的缺件數；委員撥帳資料等由系統向委員索取的項目不算學生缺件。"""
    return sum(1 for i in items if i.owner == STUDENT)


def _office_banners() -> list[tuple[str, str]]:
    """總覽頂端的警示：截止日未設定、截止後附件2 未上傳、首次補件信晚寄、自動化失敗。"""
    banners = []
    settings = office_settings.load(cfg().state_dir)
    today = clock.now().date()
    phase = settings.phase_on(today)
    if not settings.student_open:
        banners.append(("info", "尚未設定本期學期或填寫截止日：學生端尚未開放填寫，系統也不會寄出任何補件信，"
                                "請到「期限設定」頁設定。"))
    if settings.fill_deadline and today > settings.fill_deadline and settings.att2_first_uploaded is None:
        late = (today - settings.fill_deadline).days
        banners.append(("error", f"填寫截止日（{settings.fill_deadline}）已過 {late} 天，本期尚未上傳附件2："
                                 "無法正式檢核與寄補件信；補件截止日從附件2 上傳隔天起算。"))
    delay = first_deficiency_mail_delay(cfg().state_dir, settings)
    if delay:
        banners.append(("error", f"首次補件信比正式檢核開始日（{settings.check_start}）晚了 {delay} 天寄出；"
                                 f"補件截止日（{settings.deficiency_deadline}）不會自動延後，"
                                 "如需延後請到「期限設定」頁手動覆寫。"))
    status = load_automation_status(cfg().state_dir)
    if status and not status.get("ok"):
        banners.append(("error", f"每日自動化最近一次（{status.get('at')}）執行失敗：{status.get('error')}"))
    stale = _automation_stale_reason(status, settings)
    if stale:
        banners.append(("error", stale))
    return banners


def _automation_stale_reason(status, settings, *, now=None):
    """O09：正式環境且學期進行中，排程若超過一天沒更新狀態，代表每日自動化可能沒跑。
    示範模式的每日流程是手動觸發，不適用。"""
    if not cfg().production or not settings.current_term:
        return None
    now = now or clock.now()
    if status is None:
        return "正式環境尚無每日自動化執行紀錄：請確認排程已註冊並成功執行。"
    try:
        last = datetime.strptime(status["at"], "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError, TypeError):
        return None
    hours = (now.replace(tzinfo=None) - last).total_seconds() / 3600
    if hours >= 25:
        return (f"每日自動化最近一次執行是 {status['at']}，已超過 {int(hours // 24)} 天未更新："
                "排程可能沒有執行，請檢查正式主機的排程與服務狀態。")
    return None


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _parse_month(value: str | None, today: date) -> date:
    """`YYYY-MM` 轉成該月 1 日；空值或格式不對一律退回當月，不報錯。"""
    m = _MONTH_RE.match(value or "")
    if m and 1 <= int(m.group(2)) <= 12:
        return date(int(m.group(1)), int(m.group(2)), 1)
    return today.replace(day=1)


def _shift_month(first: date, delta: int) -> date:
    index = first.year * 12 + first.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def _calendar_view(rows: list[dict], month: date) -> dict:
    """工作總覽的月曆：每週一列、週日開頭，格內是當天口試的案件。

    口試日期沒填或格式不對的案件不會出現在格子裡，另外計數讓頁面提示
    「仍列在清單視圖」，免得系辦以為資料不見了。
    """
    by_day: dict[date, list[dict]] = {}
    undated = 0
    for row in rows:
        try:
            day = date.fromisoformat(row.get("exam_date") or "")
        except ValueError:
            undated += 1
            continue
        by_day.setdefault(day, []).append(row)
    weeks = [
        [{"date": d, "in_month": d.month == month.month, "cases": by_day.get(d, [])} for d in week]
        for week in calendar.Calendar(firstweekday=6).monthdatescalendar(month.year, month.month)
    ]
    return {
        "weeks": weeks, "undated": undated,
        "label": f"{month.year} 年 {month.month} 月",
        "month": month.strftime("%Y-%m"),
        "prev": _shift_month(month, -1).strftime("%Y-%m"),
        "next": _shift_month(month, 1).strftime("%Y-%m"),
    }


def _xlsx_download(sheet_title: str, headers: list[str], rows: list[list], filename: str):
    """把一張表寫成 .xlsx 直接下載；只在記憶體裡產生，不在伺服器留檔。"""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append(["" if v is None else v for v in row])
        # openpyxl 會把「=」開頭的字串存成公式；學生／收件人填的內容一律當純文字，避免 Excel 開檔時執行。
        for cell in ws[ws.max_row]:
            if cell.data_type == "f":
                cell.data_type = "s"
    for column, header in zip(ws.columns, headers):
        width = max([len(str(header))] + [len(str(c.value or "")) for c in column])
        ws.column_dimensions[column[0].column_letter].width = min(max(width * 1.6, 10), 60)
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _overview_status_label(row: dict) -> str:
    if row["status"] == "已填寫" and row.get("version"):
        return f"已填寫（第 {row['version']} 版）"
    return row["status"]


def _office_overview_rows() -> list[dict]:
    tracking = _office_tracking()
    deficiency_by_case = tracking.deficiency.by_case if tracking else {}
    return _overview_rows(_load_submissions(), deficiency_by_case)


def _office_overview_response():
    tracking = _office_tracking()
    reviews = _review_rows()
    deficiency_by_case = tracking.deficiency.by_case if tracking else {}
    rows = _overview_rows(_load_submissions(), deficiency_by_case)
    active_view = "calendar" if request.args.get("view") == "calendar" else "list"
    month = _parse_month(request.args.get("month"), clock.now().date())
    stats = {
        "total": len(rows),
        "not_filled": sum(1 for r in rows if r["status"] == "未填寫"),
        "unmatched": sum(1 for r in rows if r["status"] == "附件2查無"),
    }
    student_overdue_count = len(
        [i for i in tracking.deficiency.overdue if i.owner == STUDENT]
    ) if tracking else 0
    waiting_student = {r.case_key for r in reviews if r.pending
                       and _student_items(deficiency_by_case.get(r.case_key, []))}
    return render_template(
        "office_overview.html", active_page="overview",
        submissions=rows, stats=stats, banners=_office_banners(),
        student_overdue_count=student_overdue_count, waiting_student=waiting_student,
        reviews=reviews, review_pending_count=sum(1 for r in reviews if r.pending),
        build_status=load_auto_build_status(_status_path()),
        active_view=active_view, cal=_calendar_view(rows, month),
        removed_cases=att2_changes(cfg().state_dir).removed_notified,
        change_request_cases=sorted({r.case_key for r in case_access.requests(cfg().state_dir, pending_only=True)}),
        mailbox_counts=mailbox_agent.counts(cfg().state_dir),
        awaiting_mails=[m for m in reversed(mailbox_agent.load_mails(cfg().state_dir))
                        if m.get("status") == mailbox_agent.MAIL_AWAITING],
        MA=mailbox_agent, mail_anchor=_mail_anchor,
        ai=_ai_overview(),
        handler=session.get("handler", ""),
    )


def _ai_overview() -> dict:
    """總覽「AI 今日動態／待你決定」區塊的資料；讀不到時只顯示提示，不擋住總覽。"""
    from .. import llm as llm_module
    state = cfg().state_dir
    try:
        master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
        activity = ai_digest.activity(state, today=clock.now().date(), master=master)
    except (OSError, ValueError, KeyError) as exc:
        activity = None
        app.logger.warning("AI 摘要讀取失敗：%s", exc)
    status = load_automation_status(state) or {}
    return {"activity": activity, "model": llm_module.describe_active_model(state),
            "stopped": ai_switch.is_disabled(state), "last_run": status.get("at", "")}


def _pop_release_note(case_key: str) -> str:
    saved = session.get("release_note") or {}
    if saved.get("case") != case_key:
        return ""
    session.pop("release_note", None)
    return saved.get("note", "")


def _case_display(case_key: str) -> str:
    """提示訊息用：學生姓名（學號）；主檔查不到姓名時用中文學期＋學號，不直接印案件鍵。"""
    try:
        master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
    except (OSError, ValueError, U.StateDataError):
        master = None
    exam = master.by_key().get(case_key) if master else None
    _, _, sid = case_key.partition("-")
    if exam and exam.姓名:
        return f"{exam.姓名}（{sid}）"
    return _labels.case_label(case_key)


@app.route("/office/att2-removed/<case_key>/ack", methods=["POST"])
@login_required(role="office")
def acknowledge_removed_case(case_key):
    """專員確認「附件2 重傳後消失、已寄過補件信」的案件已處理（2026-10-01 Q4）。"""
    if not _CASE_KEY_RE.match(case_key):
        flash("找不到該筆案件", "error")
        return redirect(url_for("office_overview"))
    try:
        acknowledge_att2_removed(cfg().state_dir, case_key, actor=_handler_from_form(),
                                 note=request.form.get("note", ""))
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash(f"已確認 {_case_display(case_key)}", "success")
    return redirect(url_for("office_overview"))


@app.route("/office/overview")
@login_required(role="office")
def office_overview():
    return _office_overview_response()


@app.route("/office/overview/calendar")
@login_required(role="office")
def office_overview_calendar():
    """只回傳月曆區塊；office.js 翻月時用它換掉畫面上的月曆，不重整整頁。"""
    month = _parse_month(request.args.get("month"), clock.now().date())
    return render_template("_overview_calendar.html", cal=_calendar_view(_office_overview_rows(), month))


@app.route("/office/overview/export")
@login_required(role="office")
def office_overview_export():
    """全部案件匯出 Excel；一律是完整清單，不受頁面上的搜尋／分類篩選影響（存查用）。"""
    rows = _office_overview_rows()
    return _xlsx_download(
        "全部案件",
        ["學生姓名", "學號", "學期", "口試日期", "填寫狀態", "缺件數", "學生信箱", "自我檢查提醒", "案件鍵"],
        [[r.get("student_name") or "", r["student_id"], r["term"], r.get("exam_date") or "（未填）",
          _overview_status_label(r), r.get("deficiency_count", 0),
          r.get("student_email") or "（無信箱）", "；".join(r.get("hints") or []), r["id"]]
         for r in rows],
        f"口試案件總覽_{clock.now():%Y%m%d}.xlsx",
    )


def _mask_id(value: str) -> str:
    value = str(value or "").strip()
    return f"{value[:2]}****{value[-3:]}" if len(value) >= 6 else ("已填" if value else "")


def _identity_checks(issues, exam, master) -> dict[str, list[dict]]:
    """「以姓名對到舊資料，請確認同一人」：學生填的委員資料 vs 舊口委資料庫，逐欄並排給專員比對。"""
    checks: dict[str, list[dict]] = {}
    if not (exam and master):
        return checks
    for code, text in issues:
        if code != "COMMITTEE_MATCHED_BY_NAME":
            continue
        found = re.search(r"\b(C\d+)\b", str(text))
        member = next((m for m in exam.委員 if found and m.committee_id == found.group(1)), None)
        committee = master.committees.get(found.group(1)) if found else None
        if member is None or committee is None:
            continue
        rows = [("姓名", member.委員姓名, committee.委員姓名), ("服務單位", member.服務單位, committee.服務單位),
                ("職稱", member.職稱, ""), ("E-mail", member.Email, committee.Email),
                ("最高學歷", "", " ".join(x for x in (committee.最高學歷學校, committee.最高學位) if x)),
                ("身分證字號", "", _mask_id(committee.身分證字號)),
                ("撥帳資料", "", "齊全" if committee.banking_complete() else "不齊")]
        checks[text] = [
            {"label": label, "student": s or "—", "registry": r or "—",
             "differ": bool(s and r and s.strip().lower() != r.strip().lower())}
            for label, s, r in rows]
    return checks


@app.route("/office/case/<case_key>")
@login_required(role="office")
def office_case_detail(case_key):
    """案件詳情抽屜的局部內容；由 office.js 以 fetch() 取回並塞進 dialog。"""
    if not _CASE_KEY_RE.match(case_key):
        return "找不到該筆申請", 404
    # 案件可能只有附件2（學生還沒填），也可能只有填寫（附件2 查無）；兩者都沒有才是 404。
    path = cfg().submissions_dir / f"{case_key}.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    review = next((r for r in _review_rows() if r.case_key == case_key), None)
    if data is None and review is None:
        return "找不到該筆申請", 404
    tracking = _office_tracking()
    deficiencies = tracking.deficiency.by_case.get(case_key, []) if tracking else []
    issue_records = sorted(
        issue_store.load(cfg().state_dir / "issue_records.json").by_case(case_key),
        key=lambda r: r.detected_at or datetime.min.date(),
    )
    master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
    settings = office_settings.load(cfg().state_dir)
    exam = master.by_key().get(case_key) if master else None
    member_fees = []
    for member in (exam.委員 if exam else []):
        committee = master.committees.get(member.committee_id)
        unit = member.服務單位 or (committee.服務單位 if committee else "")
        fee = _fee_text(member, unit)
        member_fees.append({"name": member.委員姓名 or (committee.委員姓名 if committee else ""),
                            "unit": unit, "title": member.職稱, "fee": fee, "missing": fee == "查無距離"})
    identity_checks = _identity_checks(
        [(i.get("問題代碼"), i.get("說明", "")) for i in (review.exceptions if review else [])]
        + [(r.issue_code, r.description) for r in issue_records]
        + [(d.issue_code, d.description) for d in deficiencies], exam, master)
    return render_template(
        "office_case_detail.html", data=data, case_key=case_key, review=review, member_fees=member_fees,
        identity_checks=identity_checks,
        deficiencies=deficiencies,
        issue_records=issue_records, issue_status_labels=_ISSUE_STATUS_LABELS,
        corrections=correction_rows(master, cfg().state_dir, case_key) if master else [],
        change_requests=case_access.requests(cfg().state_dir, case_key=case_key),
        unlock=case_access.unlock_for(cfg().state_dir, case_key),
        ai_panel=ai_digest.case_panel(cfg().state_dir, case_key, master),
        mail_anchor=_mail_anchor,
        release_note=_pop_release_note(case_key),
        default_until=settings.deficiency_deadline or settings.projected_deficiency_deadline,
        handler=session.get("handler", ""),
    )


@app.route("/office/case/<case_key>/correction", methods=["POST"])
@login_required(role="office")
def save_correction(case_key):
    """專員修正層（2026-10-01 Q10／Q11）：修正存成另一層，重建主檔後套用。"""
    if not _CASE_KEY_RE.match(case_key):
        flash("找不到該筆案件", "error")
        return redirect(url_for("office_overview"))
    # target＝「委員姓名|欄位」（案件層欄位的委員姓名為空）；舊表單送 field／member 也接受
    member, _, field = request.form.get("target", "").rpartition("|")
    if not field:
        field, member = request.form.get("field", ""), request.form.get("member", "")
    try:
        actor = _handler_from_form()
        if request.form.get("action") == "remove":
            corrections.remove_correction(cfg().state_dir, case_key=case_key, field=field,
                                          member=member, actor=actor)
            message = "已移除修正，恢復使用來源資料"
        else:
            value = request.form.get("value", "")
            if field == "視訊出席":  # 畫面顯示「是／否」，資料仍存 true／false
                value = {"是": "true", "否": "false"}.get(value.strip(), value)
            save_case_correction(master_path=cfg().master_path, state_dir=cfg().state_dir,
                                 case_key=case_key, field=field, member=member,
                                 value=value, actor=actor,
                                 reason=request.form.get("reason", ""))
            message = "已儲存修正"
    except (ValueError, FileNotFoundError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("office_overview"))
    flash(f"{_case_display(case_key)}：{message}", "success")
    _flash_auto_build(_try_auto_build())
    return redirect(url_for("office_overview"))


@app.route("/office/case/<case_key>/unlock", methods=["POST"])
@login_required(role="office")
def unlock_case(case_key):
    """開放此案學生修改到指定日期（申請異動、逾期個案處理；2026-10-01 Q8／Q13）。"""
    if not _CASE_KEY_RE.match(case_key):
        flash("找不到該筆案件", "error")
        return redirect(url_for("office_overview"))
    try:
        raw = request.form.get("until", "").strip()
        try:
            until = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("開放期限格式錯誤") from exc
        case_access.grant_unlock(cfg().state_dir, case_key=case_key, until=until,
                                 actor=_handler_from_form(), reason=request.form.get("reason", ""))
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash(f"已開放 {_case_display(case_key)} 修改到 {until}", "success")
    return redirect(url_for("office_overview"))


@app.route("/office/case/<case_key>/dismiss-requests", methods=["POST"])
@login_required(role="office")
def dismiss_change_requests(case_key):
    if not _CASE_KEY_RE.match(case_key):
        flash("找不到該筆案件", "error")
        return redirect(url_for("office_overview"))
    try:
        case_access.dismiss_requests(cfg().state_dir, case_key=case_key, actor=_handler_from_form(),
                                     note=request.form.get("note", ""))
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash(f"已將 {_case_display(case_key)} 的異動申請標為已處理", "success")
    return redirect(url_for("office_overview"))


@app.route("/office/case/<case_key>/clear", methods=["POST"])
@login_required(role="office")
def clear_case(case_key):
    """人工關卡：專員審核委員資格與例外後按「已處理」放行（每案一次，可重新放行）。"""
    if not _CASE_KEY_RE.match(case_key):
        flash("找不到該筆案件", "error")
        return redirect(url_for("office_overview"))
    try:
        clear_case_review(
            master_path=cfg().master_path, state_dir=cfg().state_dir, case_key=case_key,
            actor=_handler_from_form(), note=request.form.get("note", ""),
        )
    except (ValueError, FileNotFoundError) as exc:
        flash(str(exc), "error")
        # 保留已填的處理說明，重開案件讓專員補上處理人即可再送出
        session["release_note"] = {"case": case_key, "note": request.form.get("note", "")[:1000]}
        return redirect(url_for("office_overview", case=case_key))
    flash(f"審核完成 {_case_display(case_key)}：系統接下來會向委員索取個資（系統內部稱「已放行」）", "success")
    return redirect(url_for("office_overview"))


@app.route("/office/committees")
@login_required(role="office")
def office_committees():
    """委員個資索取：已放行案件中撥帳資料不齊的委員、寄送與回填狀態，以及手動登錄。"""
    requests = []
    if cfg().master_path.is_file():
        requests = committee_requests.plan(load_current_master(cfg().master_path), cfg().state_dir)
    today = clock.now().date()
    base_url = os.environ.get(PUBLIC_URL_ENV, "").strip() or request.url_root
    links = {
        r.key: committee_requests.reply_url(base_url, committee_requests.link_token(cfg().state_dir, r.email, request=r))
        for r in requests if not r.received
    }
    pending_keys = {r.key for r in requests if not r.received}
    # 已回填的委員：主檔重建後撥帳資料齊全就不在索取清單裡，另列出來（只列姓名、時間、來源，不顯示個資）
    received = sorted(
        ({"name": v.get("name") or "（未記姓名）", "at": v.get("received_at", ""),
          "source": "委員線上填寫" if v.get("source") == committee_requests.SOURCE_ONLINE else "系辦登錄",
          "case_keys": v.get("case_keys", [])}
         for k, v in committee_requests.load_replies(cfg().state_dir).items() if k not in pending_keys),
        key=lambda r: r["at"], reverse=True)
    return render_template(
        "office_committees.html", active_page="committees", requests=requests, today=today, links=links,
        received=received, public_url_set=bool(os.environ.get(PUBLIC_URL_ENV, "").strip()),
    )


@app.route("/office/committees/reply", methods=["POST"])
@login_required(role="office")
def record_committee_reply():
    """系辦手動登錄委員回覆的撥帳資料；登錄後重建主檔，資料寫入輸出的口委資料庫。"""
    email = request.form.get("email", "")
    master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
    req = next((r for r in committee_requests.plan(master, cfg().state_dir)
                if r.key == committee_requests.email_key(email)), None) if master else None
    fields = req.fields if req else committee_requests.BANKING_FIELDS
    try:
        if req is None or request.form.get("identity_binding") != req.binding:
            raise ValueError("委員請求已變更或身分有衝突，請重新核對目前名單")
        active = office_settings.load(cfg().state_dir).current_term
        if active and any(not k.startswith(active + "-") for k in req.case_keys):
            raise ValueError("封存學期不可回填")
        committee_requests.save_reply(
            cfg().state_dir, email=email, actor=_handler_from_form(),
            values={field: request.form.get(field, "") for field in fields},
            confirm={field: request.form.get(f"{field}_確認", "")
                     for field in committee_requests.CONFIRM_FIELDS if field in fields},
            foreign_id=request.form.get("foreign_id") == "1", fields=fields,
            name=req.name if req else "", case_keys=req.case_keys if req else (),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("office_committees"))
    flash(f"已登錄 {email} 的{'身分資料' if req and req.id_only else '撥帳資料'}", "success")
    _flash_auto_build(_try_auto_build())
    return redirect(url_for("office_committees"))


# ── 委員線上填寫撥帳資料（專屬連結，不需登入）────────────────

def _committee_request_for_token(token: str):
    """代碼對應的委員索取請求；代碼無效或該委員已不在索取清單回 None。"""
    if not _LINK_TOKEN_RE.match(token) or not cfg().master_path.is_file():
        return None
    key = committee_requests.email_for_token(cfg().state_dir, token)
    if key is None:
        return None
    master = load_current_master(cfg().master_path)
    from .. import source_state
    try:
        source_state.require_current(cfg().master_path, allow_legacy=False)
    except ValueError:
        return None
    active = office_settings.load(cfg().state_dir).current_term
    return next((r for r in committee_requests.plan(master, cfg().state_dir)
        if r.key == key and committee_requests.token_matches_request(cfg().state_dir, token, r)
        and (not active or all(k.startswith(active + "-") for k in r.case_keys))), None)


@app.route("/committee/reply/<token>", methods=["GET", "POST"])
def committee_reply(token):
    """委員從索取信的專屬連結填寫撥帳資料；送出後頁面不回顯任何個資。"""
    req = _committee_request_for_token(token)
    if req is None:
        return render_template("committee_reply.html", req=None), 404
    if req.received:
        return render_template("committee_reply.html", req=req, done=True)
    grant_key = "committee_verified_" + token
    verified = session.get(grant_key, 0) > time.time()
    if request.method == "POST":
        action = request.form.get("action", "submit")
        try:
            if action == "send-code":
                transport = None
                if cfg().demo_root:
                    from ..demo import transport_for
                    transport = transport_for(cfg())
                committee_verification.send_code(cfg().state_dir, token=token, email=req.email,
                    secret=app.secret_key, transport=transport)
                flash(f"驗證碼已放入「模擬寄件匣」（{judge.label()}模式不寄出真實信件，請到系辦工作台的模擬寄件匣查看），10 分鐘內有效"
                      if cfg().judge_mode else "驗證碼已寄至登記信箱，10 分鐘內有效", "success")
            elif action == "verify-code":
                session[grant_key] = committee_verification.verify_code(cfg().state_dir,
                    token=token, code=request.form.get("code", ""), secret=app.secret_key)
                return redirect(url_for("committee_reply", token=token))
            elif not verified:
                raise ValueError("請先完成信箱驗證")
            else:
                committee_requests.save_reply(
                    cfg().state_dir, email=req.email, actor="委員本人",
                    source=committee_requests.SOURCE_ONLINE,
                    values={field: request.form.get(field, "") for field in req.fields},
                    confirm={field: request.form.get(f"{field}_確認", "") for field in req.confirm_fields},
                    foreign_id=request.form.get("foreign_id") == "1", fields=req.fields,
                    name=req.name, case_keys=req.case_keys,
                )
                session.pop(grant_key, None)
                _try_auto_build()
                return render_template("committee_reply.html", req=req, done=True)
        except (ValueError, OSError) as exc:
            flash(str(exc), "error")
            if action == "submit":
                # 保留已填的內容（只清空「再輸入一次」欄位，請委員重新核對），不必整份重填
                entered = {field: request.form.get(field, "") for field in req.fields}
                return render_template("committee_reply.html", req=req, done=False, verified=verified,
                    fields=req.fields, confirm_fields=req.confirm_fields, entered=entered,
                    foreign_checked=request.form.get("foreign_id") == "1"), 422
    return render_template("committee_reply.html", req=req, done=False, verified=verified,
        fields=req.fields, confirm_fields=req.confirm_fields, entered={})



@app.route("/office/communications")
@login_required(role="office")
def office_communications():
    """通訊記錄：寄信/催件的稽核軌跡（send_log.json），讓系辦不用看程式也能
    掌握「系統幫我寄了什麼信、寄到誰、狀態如何」——沒有任何地方需要她手動
    操作，純監看用途。檔案不存在（還沒跑過任何寄送）時顯示空清單，不當機。
    """
    log = send_log.load(cfg().state_dir / "send_log.json")
    events = sorted(log.all(), key=lambda e: e.created_at, reverse=True)
    stats = {
        "pending": sum(1 for e in events if e.status in (send_log.DRAFT, send_log.APPROVED)),
        "sent": sum(1 for e in events if e.status == send_log.SENT),
        "failed": sum(1 for e in events if e.status == send_log.FAILED),
    }
    return render_template(
        "office_communications.html", active_page="communications",
        events=events, stats=stats,
        type_labels=_SEND_TYPE_LABELS,
        status_labels={**_SEND_STATUS_LABELS, send_log.SENT: "已放入模擬寄件匣"} if cfg().judge_mode
        else _SEND_STATUS_LABELS,
    )


@app.route("/office/communications/export")
@login_required(role="office")
def office_communications_export():
    """通訊記錄匯出 Excel：send_log.json 全部事件，新的在前（跟頁面同順序）。"""
    log = send_log.load(cfg().state_dir / "send_log.json")
    events = sorted(log.all(), key=lambda e: e.created_at, reverse=True)
    return _xlsx_download(
        "通訊記錄",
        ["類型", "收件人", "主旨", "狀態", "建立時間", "寄出時間", "案件鍵", "備註"],
        [[_SEND_TYPE_LABELS.get(e.type, e.type), e.to or "（查無收件人，需人工處理）", e.subject,
          _SEND_STATUS_LABELS.get(e.status, e.status), e.created_at, e.sent_at or "",
          e.tracking.get("case_key", ""), e.note]
         for e in events],
        f"通訊記錄_{clock.now():%Y%m%d}.xlsx",
    )


@app.route("/office/import")
@login_required(role="office")
def office_import():
    return render_template(
        "office_import.html", active_page="import",
        files_status=office_files_status(cfg().registries),
        file_labels=_OFFICE_FILE_LABELS,
        build_status=load_auto_build_status(_status_path()),
    )


def _scheduler(today=None) -> ReminderScheduler:
    settings = office_settings.load(cfg().state_dir)
    return ReminderScheduler(
        today=today, deficiency_lead_days=settings.deficiency_reminder_lead_days,
        business_days=settings.business_days, holidays=settings.holiday_set,
    )


# ── 系辦設定（期限與催件參數）──────────────────────────

@app.route("/office/settings", methods=["GET", "POST"])
@login_required(role="office")
def office_settings_page():
    current = office_settings.load(cfg().state_dir)
    pending_switch = None
    if request.method == "POST":
        try:
            updated = office_settings.from_form(request.form, base=current)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            switching = office_settings.term_changing(request.form, current)
            if switching and request.form.get("confirm_term_switch") != "1":
                # 切換學期前先列出舊學期未結案件，要系辦確認（2026-10-01 Q16）
                master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
                pending_switch = {
                    "old": current.current_term, "new": updated.current_term,
                    "open_cases": open_cases_for_term(master, cfg().state_dir, current.current_term)
                    if master else [],
                    "form": [(k, v) for k, v in request.form.items(multi=True)
                             if k not in ("csrf_token", "confirm_term_switch")],
                }
            else:
                if switching and current.current_term:
                    from .. import source_state
                    source_state.freeze(cfg().state_dir / "archives" / current.current_term,
                        {"state": cfg().state_dir, "submissions": cfg().submissions_dir,
                         "master": cfg().master_path.parent, "documents": cfg().doc_output_root,
                         "attachment2.xlsx": cfg().registries.attachment2,
                         "members.xlsx": cfg().registries.members, "distances.csv": cfg().registries.distances},
                        detail={"term": current.current_term, "new_term": updated.current_term})
                office_settings.save(updated, cfg().state_dir)
                flash("已切換本期學期並儲存設定" if switching else "已儲存期限與催件設定", "success")
                return redirect(url_for("office_settings_page"))
    today = clock.now().date()
    return render_template(
        "office_settings.html", active_page="settings", settings=current,
        phase_label=office_settings.PHASE_LABELS[current.phase_on(today)],
        pending_switch=pending_switch,
    )


@app.route("/office/ai-settings", methods=["GET", "POST"])
@login_required(role="office")
def office_ai_settings():
    """AI 開關：AI 總開關、來信自動收件通知、問答助理（與期限無關，從期限設定移出）。"""
    current = office_settings.load(cfg().state_dir)
    if request.method == "POST":
        updated = replace(
            current, mailbox_auto_reply_enabled=request.form.get("mailbox_auto_reply_enabled") == "1",
            assistant_enabled=request.form.get("assistant_enabled", "1" if current.assistant_enabled else "0") != "0")
        office_settings.save(updated, cfg().state_dir)
        flash("已儲存 AI 開關", "success")
        return redirect(url_for("office_ai_settings"))
    return render_template("office_ai_settings.html", active_page="ai_settings", settings=current)


@app.route("/office/documents")
@login_required(role="office")
def office_documents():
    tracking = _office_tracking()
    internal_todos = _scheduler().internal_todos(tracking.deficiency) if tracking else []
    student_overdue = [i for i in tracking.deficiency.overdue if i.owner == STUDENT] if tracking else []
    return render_template(
        "office_documents.html", active_page="documents",
        tracking=tracking, internal_todos=internal_todos, student_overdue=student_overdue,
        doc_status=_load_doc_status(), master_exists=cfg().master_path.is_file(),
        token=request_token, handler=session.get("handler", ""),
        status_labels=_TRACK_STATUS_LABELS,
        inbox_pending=[i for i in advisor_confirmation.load_inbox(cfg().state_dir)
                       if i.status == advisor_confirmation.INBOX_PENDING],
        reply_cards=advisor_reply_cards(master_path=cfg().master_path, state_dir=cfg().state_dir),
        advisor_mail=advisor_confirmation.load_mail_state(cfg().state_dir),
        att4_documents=advisor_confirmation.load_documents(cfg().state_dir),
        ref=advisor_confirmation.ref_code,
        inbox_recent=advisor_confirmation.load_inbox(cfg().state_dir)[-10:],
    )


_TRACK_STATUS_LABELS = {
    "draft": "尚未寄出", "pending": "已寄出", "confirmed": "已確認", "modified": "需修改",
    "overdue": "逾期未回", "outstanding": "已交付待收回", "collected": "已收回（合格）",
    "needs_correction": "收回但需補正",
}


def _handler_from_form() -> str:
    """人工操作的處理人（review-2026-09-23 P1-11）；記在 session 方便下次預填。

    評審帳號一律記為「評審」，不採用表單填的名字。
    """
    if _is_judge():
        return judge.label()
    handler = request.form.get("handler", "").strip()
    if not handler:
        raise ValueError("請填寫處理人姓名")
    session["handler"] = handler
    return handler


@app.route("/office/confirmation", methods=["POST"])
@login_required(role="office")
def record_confirmation_route():
    """登錄附件4 已寄出／教授回覆無誤／需修改（教授改用電話、紙本回覆時的人工入口）。"""
    try:
        action = request.form.get("action", "")
        note = request.form.get("note", "").strip()
        if action == "confirmed":
            method = request.form.get("confirmation_method", "").strip()
            if not method or not note:
                raise ValueError("人工確認須填確認方式與確認內容")
            note = f"確認方式：{method}；確認時間：{clock.now().isoformat()}；內容：{note}"
        if not request.form.get("expected_version"):
            raise ValueError("請重新整理文件中心以核對目前附件4版本")
        record_confirmation(
            expected_version=request.form["expected_version"],
            master_path=cfg().master_path, state_dir=cfg().state_dir,
            token=request.form.get("token", ""), action=action,
            actor=_handler_from_form(), note=note,
        )
    except (ValueError, FileNotFoundError) as exc:
        flash(str(exc), "error")
    else:
        flash("已登錄附件4 寄送／教授回覆狀態", "success")
    return redirect(url_for("office_documents") + "#tracking-records")


@app.route("/office/advisor-reply", methods=["POST"])
@login_required(role="office")
def handle_advisor_reply_route():
    """系辦判讀一封教授回信（LLM 判讀接上前的主要入口，之後保留為人工覆核）。"""
    try:
        outcome = handle_advisor_reply(
            master_path=cfg().master_path, state_dir=cfg().state_dir,
            message_id=request.form.get("message_id", ""), action=request.form.get("action", ""),
            actor=_handler_from_form(), note=request.form.get("note", ""),
        )
    except (ValueError, FileNotFoundError) as exc:
        flash(str(exc), "error")
    else:
        flash(outcome or "已處理教授回信", "success")
    return redirect(url_for("office_documents") + "#advisor-replies")


@app.route("/office/mailbox")
@login_required(role="office")
def office_mailbox():
    """信箱 agent：每封來信的身分、案件、分類、摘要、建議動作與執行軌跡。"""
    sd = cfg().state_dir
    mails = list(reversed(mailbox_agent.load_mails(sd)))
    status_filter = request.args.get("status", "")
    if status_filter in mailbox_agent.MAIL_STATUS_LABELS:
        mails = [m for m in mails if m["status"] == status_filter]
    else:
        status_filter = ""
    return render_template(
        "office_mailbox.html", active_page="mailbox", mails=mails, MA=mailbox_agent,
        traces=mailbox_agent.traces_by_run(sd), counts=mailbox_agent.counts(sd),
        settings=office_settings.load(sd), status_filter=status_filter, anchor=_mail_anchor,
        handler=session.get("handler", ""),
        request_states=advisor_request_states(master_path=cfg().master_path, state_dir=sd),
    )


def _mail_anchor(message_id: str) -> str:
    return "mail-" + re.sub(r"[^\w-]", "", message_id or "")


def _mailbox_transport():
    if cfg().demo_root:
        from ..demo import transport_for
        return transport_for(cfg())
    return None


@app.route("/office/mailbox/action", methods=["POST"])
@login_required(role="office")
def mailbox_action():
    """專員核准／退回信箱 agent 的草稿，或把建議標為已處理（處理人必填）。"""
    message_id = request.form.get("message_id", "")
    decision = request.form.get("decision", "")
    try:
        record = mailbox_agent.resolve_action(
            cfg().state_dir, message_id=message_id, action_id=request.form.get("action_id", ""),
            decision=decision, actor=_handler_from_form(), note=request.form.get("note", ""),
            body=request.form.get("body"), master_path=cfg().master_path, transport=_mailbox_transport(),
        )
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        flash(str(exc), "error")
    else:
        action = next(a for a in record["actions"] if a["id"] == request.form.get("action_id"))
        result = _judge_sent(action.get("result") or mailbox_agent.ACT_STATUS_LABELS[action["status"]])
        flash(f"{action['label']}：{result}", "success")
        if decision == "approve" and action["type"] == mailbox_agent.A_APPLY_CORRECTION:
            _flash_auto_build(_try_auto_build())
    return redirect(url_for("office_mailbox", open=_mail_anchor(message_id)) + "#" + _mail_anchor(message_id))


@app.route("/office/mailbox/approve-all", methods=["POST"])
@login_required(role="office")
def mailbox_approve_all():
    """同一封信的草稿一次核准或退回（專員已在同一畫面看過全部內容）；依信中順序逐項執行，任一項失敗就停下。
    回信草稿可在畫面上直接修改，以 body_<action_id> 送出。"""
    message_id = request.form.get("message_id", "")
    decision = "reject" if request.form.get("decision") == "reject" else "approve"
    try:
        actor = _handler_from_form()
        record = next(m for m in mailbox_agent.load_mails(cfg().state_dir) if m["message_id"] == message_id)
        ids = [a["id"] for a in record["actions"]
               if a["status"] == mailbox_agent.ACT_PENDING and a["policy"] == mailbox_agent.DRAFT]
        if not ids:
            raise ValueError("這封信沒有等你核准的草稿")
        if decision == "reject":
            # 畫面上不顯示的「到文件中心判讀」與修正草稿是同一件事，退回時一併結掉（核准時由修正自動結掉）
            ids += [a["id"] for a in record["actions"]
                    if a["status"] == mailbox_agent.ACT_PENDING and a["type"] == mailbox_agent.A_REVIEW_A4]
        corrected = False
        for action_id in ids:
            record = mailbox_agent.resolve_action(
                cfg().state_dir, message_id=message_id, action_id=action_id, decision=decision,
                actor=actor, note=request.form.get("note", ""), body=request.form.get(f"body_{action_id}"),
                master_path=cfg().master_path, transport=_mailbox_transport(),
            )
            action = next(a for a in record["actions"] if a["id"] == action_id)
            result = _judge_sent(action.get("result") or mailbox_agent.ACT_STATUS_LABELS[action["status"]])
            flash(f"{action['label']}：{result}", "success")
            corrected |= decision == "approve" and action["type"] == mailbox_agent.A_APPLY_CORRECTION
        if corrected:
            _flash_auto_build(_try_auto_build())
    except StopIteration:
        flash("找不到這封信", "error")
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        flash(str(exc), "error")
    return redirect(url_for("office_mailbox", open=_mail_anchor(message_id)) + "#" + _mail_anchor(message_id))


@app.route("/office/mailbox/reply", methods=["POST"])
@login_required(role="office")
def mailbox_reply():
    """AI 沒擬回信時，專員自己寫回信寄出（寄出後整封標為已處理）。"""
    message_id = request.form.get("message_id", "")
    try:
        record = mailbox_agent.staff_reply(cfg().state_dir, message_id=message_id, actor=_handler_from_form(),
                                           body=request.form.get("body", ""), transport=_mailbox_transport())
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        flash(str(exc), "error")
    else:
        flash(f"回覆來信：{_judge_sent(record['actions'][-1]['result'])}", "success")
    return redirect(url_for("office_mailbox", open=_mail_anchor(message_id)) + "#" + _mail_anchor(message_id))


@app.route("/office/mailbox/mark", methods=["POST"])
@login_required(role="office")
def mailbox_mark():
    message_id = request.form.get("message_id", "")
    try:
        record = mailbox_agent.mark_mail(cfg().state_dir, message_id=message_id, status=request.form.get("status", ""),
                                         actor=_handler_from_form(), note=request.form.get("note", ""))
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        label = mailbox_agent.MAIL_STATUS_LABELS.get(record["status"], record["status"])
        flash("；".join(x for x in (f"這封信已標為「{label}」", record.pop("sync_note", "")) if x), "success")
    return redirect(url_for("office_mailbox", open=_mail_anchor(message_id)) + "#" + _mail_anchor(message_id))


@app.route("/office/collection", methods=["POST"])
@login_required(role="office")
def record_collection_route():
    """登錄附件6 已交付／已收回（合格）／需補正。"""
    try:
        if not request.form.get("expected_version"):
            raise ValueError("請重新整理文件中心以核對目前附件6版本")
        record_collection(
            expected_version=request.form["expected_version"],
            master_path=cfg().master_path, state_dir=cfg().state_dir,
            token=request.form.get("token", ""), action=request.form.get("action", ""),
            actor=_handler_from_form(), note=request.form.get("note", ""),
        )
    except (ValueError, FileNotFoundError) as exc:
        flash(str(exc), "error")
    else:
        flash("已登錄附件6 回收狀態", "success")
    return redirect(url_for("office_documents") + "#tracking-records")


@app.route("/office/documents/generate", methods=["POST"])
@login_required(role="office")
def generate_documents():
    phase = request.form.get("phase")
    if phase not in ("pre-exam", "post-exam", "preview"):
        flash("未知的產檔階段", "error")
        return redirect(url_for("office_documents"))
    if not cfg().master_path.is_file():
        flash("尚未建立主檔，無法產生文件", "error")
        return redirect(url_for("office_documents"))

    try:
        templates = cfg().templates
        if phase == "preview":
            from ..application import generate_fee_preview
            files = generate_fee_preview(master_path=cfg().master_path, state_dir=cfg().state_dir,
                output_dir=_doc_output_dir(phase), template=templates.attachment6)
            from .. import source_state
            generation = source_state.require_current(cfg().master_path, allow_legacy=False)
            _save_doc_status(phase, {"status": "built", "at": clock.now().isoformat(),
                "generation": generation["generation"],
                "file_hashes": {p.name: source_state.digest(p) for p in files},
                "attachment6_files": [p.name for p in files]})
            flash(f"已產生 {len(files)} 份資料未齊預覽版，不得交付或核撥", "success")
            return redirect(url_for("office_documents"))
        result = generate_artifacts(
            master_path=cfg().master_path, output_dir=_doc_output_dir(phase),
            templates=templates, phase=phase, state_dir=cfg().state_dir,
        )
    except (ValueError, RuntimeError, FileNotFoundError, OSError) as exc:
        _save_doc_status(phase, {
            "status": "failed",
            "at": clock.now().strftime("%Y-%m-%d %H:%M"),
            "error": str(exc),
        })
        flash(f"產檔失敗：{exc}", "error")
        return redirect(url_for("office_documents"))

    entry = {
        "status": "built",
        "at": clock.now().strftime("%Y-%m-%d %H:%M"),
        "issue_count": len(result.issues),
        "issues": result.issues,
    }
    from .. import source_state
    generation = source_state.require_current(cfg().master_path)
    entry["generation"] = generation["generation"] if generation else None
    if not result.attachment6_files:
        entry["status"] = "empty"
    if phase == "pre-exam":
        entry["attachment4_files"] = [result.attachment4.relative_to(_doc_output_dir(phase)).as_posix()]
        entry["attachment6_files"] = [p.relative_to(_doc_output_dir(phase)).as_posix() for p in result.attachment6_files]
        if result.attachment6_files:
            flash(f"已產生附件4與 {len(result.attachment6_files)} 份預填附件6", "success")
        else:
            flash("已產生附件4（可下載）；本次沒有符合資格的交付版附件6，原因見「本次排除與待處理清單」", "success")
    else:
        entry["attachment6_files"] = [p.relative_to(_doc_output_dir(phase)).as_posix() for p in result.attachment6_files]
        flash(f"已產生 {len(result.attachment6_files)} 份附件6", "success")
    if result.manifest:
        detail = json.loads(result.manifest.read_text(encoding="utf-8"))
        entry["included_attachment4"] = detail["included_attachment4"]
        entry["included_attachment6"] = detail["included_attachment6"]
        entry["excluded_cases"] = detail["excluded_cases"]
    entry["file_hashes"] = {name: source_state.digest(_doc_output_dir(phase) / name)
        for name in [*entry.get("attachment4_files", []), *entry.get("attachment6_files", [])]}
    _save_doc_status(phase, entry)
    return redirect(url_for("office_documents"))


@app.route("/office/documents/download/<phase>/<path:filename>")
@login_required(role="office")
def download_document(phase, filename):
    if phase not in ("pre-exam", "post-exam", "preview"):
        abort(404)
    status = _load_doc_status().get(phase) or {}
    allowed = set(status.get("attachment4_files", []))
    allowed.update(status.get("attachment6_files", []))
    if filename not in allowed:
        abort(404)
    from .. import source_state
    try:
        if not status.get("generation"):
            raise ValueError("文件缺少來源版本，請重新產檔")
        source_state.require_current(cfg().master_path, expected_generation=status.get("generation"),
                                     allow_legacy=False)
    except ValueError as exc:
        abort(409, str(exc))
    root = _doc_output_dir(phase).resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        abort(404)
    expected = status.get("file_hashes", {}).get(filename)
    if phase != "preview" and (not expected or source_state.digest(path) != expected):
        abort(409, "文件校驗失敗，請重新產檔")
    if _is_judge():  # 評審只會在示範資料夾下載（judge guard 已確認評審模式），仍留稽核
        _judge_audit("download_document", phase=phase, filename=filename)
    return send_from_directory(root, filename, as_attachment=True)


@app.route("/office/source-issues", methods=["GET", "POST"])
@login_required(role="office")
def source_issues_page():
    from .. import source_issues
    master = load_current_master(cfg().master_path)
    issues = [i for i in master.source_issues if not i.get("案件鍵")]
    if request.method == "POST":
        item = next((i for i in issues if source_issues.key(i) == request.form.get("issue_key")), None)
        if item is None:
            abort(409, "來源異常已改版，請重新核對")
        try:
            source_issues.resolve(cfg().state_dir, issue=item, actor=_handler_from_form(),
                reason=request.form.get("reason", ""))
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("source_issues_page"))
    done = source_issues.records(cfg().state_dir)
    return render_template("office_source_issues.html", items=[
        {"issue": i, "key": source_issues.key(i), "resolution": done.get(source_issues.key(i))} for i in issues])


# ── AI 建議（提案型 agent：AI 提案、專員決定；docs/提案型agent設計.md）────────

def _ai_distance_table():
    from .. import ai_proposals
    from ..registry import DistanceTable
    table = DistanceTable.load(cfg().registries.distances)
    table.aliases = ai_proposals.alias_map(cfg().state_dir)
    return table


@app.route("/office/ai-proposals")
@login_required(role="office")
def office_ai_proposals():
    from .. import ai_proposals as P, agent_trace, title_agent
    state = cfg().state_dir
    items = list(reversed(P.load(state)))
    try:
        units = sorted(_ai_distance_table().rows)
    except ValueError:
        units = []
    from .. import correction_agent
    from ..offline_llm import OFFLINE_MODEL
    master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
    resolved, case_names = set(), {}
    if master is not None:
        case_names = {e.案件鍵: e.姓名 for e in master.exams if e.姓名}
        for p in items:
            if p["agent"] == "correction" and p["status"] == P.PENDING and p.get("issue"):
                if correction_agent.already_resolved(master, state, {**p["issue"], "案件鍵": p.get("case_key")}):
                    resolved.add(p["id"])
    return render_template(
        "office_ai_proposals.html", offline_model=OFFLINE_MODEL, resolved_corrections=resolved,
        case_names=case_names, decision_text=_ai_decision_text,
        active_page="ai", agents=P.AGENTS, settings=P.load_settings(state),
        pending={a: [p for p in items if p["agent"] == a and p["status"] == P.PENDING] for a in P.AGENTS},
        decided=[p for p in items if p["status"] != P.PENDING][:30], status_labels=P.STATUS_LABELS,
        traces={p["id"]: agent_trace.load_trace(state, tracking_key=p["subject_key"]) for p in items[:200]},
        aliases=P.load_aliases(state), title_levels=P.load_title_levels(state), levels=title_agent.LEVELS,
        distance_units=units, handler=session.get("handler", ""),
    )


def _ai_decision_text(p: dict) -> str:
    """「最近的專員決定」的決定內容：不直接印出 dict。"""
    d = p.get("decision")
    if not isinstance(d, dict):
        return str(d or "")
    if "target" in d:
        return f"對應到「{d['target']}」" if d["target"] else "不對應（維持查無距離）"
    if "level" in d:
        return f"級別：{d['level']}" if d["level"] else "不採用"
    if "new" in d:
        return f"「{d.get('old') or '空白'}」改為「{d['new']}」"
    return _labels.readable(d)


@app.route("/office/ai-proposals/run", methods=["POST"])
@login_required(role="office")
def run_ai_proposals():
    from .. import affiliation_agent, correction_agent, llm as llm_module, title_agent
    from ..ai_proposals import new_run_id
    if not cfg().master_path.is_file():
        flash("尚未建立主檔，無法產生 AI 建議", "error")
        return redirect(url_for("office_ai_proposals"))
    stopped = ai_switch.is_disabled(cfg().state_dir)
    llm = None if stopped else llm_module.make_llm()
    master, run_id = load_current_master(cfg().master_path), new_run_id()
    wanted = request.form.get("agent", "all")
    created = []
    try:
        if wanted in ("all", "affiliation"):
            created += affiliation_agent.run(master, cfg().state_dir, distance_table=_ai_distance_table(),
                                             llm=llm, run_id=run_id)
        if wanted in ("all", "correction"):
            created += correction_agent.run(master, cfg().state_dir, llm=llm, run_id=run_id)
        if wanted in ("all", "title"):
            created += title_agent.run(master, cfg().state_dir, llm=llm, run_id=run_id)
    except ValueError as exc:
        flash(str(exc), "error")
    errors = sum(1 for p in created if p.get("error"))
    flash(f"本輪新增 {len(created)} 筆 AI 建議" + (f"（{errors} 筆模型失敗，留給專員處理）" if errors else "")
          + ("" if llm else "；AI 總開關已關閉，只做規則比對" if stopped else "；未設定模型，只做規則比對"),
          "success")
    return redirect(url_for("office_ai_proposals"))


@app.route("/office/ai-proposals/<proposal_id>/decide", methods=["POST"])
@login_required(role="office")
def decide_ai_proposal(proposal_id):
    from .. import affiliation_agent, ai_proposals as P, correction_agent, title_agent
    proposal = P.get(cfg().state_dir, proposal_id)
    action, note = request.form.get("action", ""), request.form.get("note", "")
    try:
        if proposal is None or action not in ("approve", "override", "reject"):
            raise ValueError("找不到這筆 AI 建議或操作不正確")
        actor = _handler_from_form()
        if action == "override" and proposal["agent"] == "affiliation" and not request.form.get("target", "").strip():
            raise ValueError("「改指定」須先在下拉選單選擇一個距離表單位；要採用 AI 建議請按「採用建議」")
        if action == "override" and proposal["agent"] == "title" and not request.form.get("level", "").strip():
            raise ValueError("「改判」須先在下拉選單選擇級別")
        if action == "override" and proposal["agent"] == "correction" and not request.form.get("value", "").strip():
            raise ValueError("「改值後套用」須先填入要改用的值")
        if proposal["agent"] == "affiliation":
            affiliation_agent.decide(cfg().state_dir, proposal_id, action=action, actor=actor, note=note,
                                     target=request.form.get("target", ""),
                                     distance_units=list(_ai_distance_table().rows))
        elif proposal["agent"] == "correction":
            correction_agent.decide(cfg().state_dir, proposal_id, action=action, actor=actor, note=note,
                                    value=request.form.get("value", ""), master_path=cfg().master_path)
        else:
            title_agent.decide(cfg().state_dir, proposal_id, action=action, actor=actor, note=note,
                               level=request.form.get("level", ""))
    except (ValueError, FileNotFoundError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("office_ai_proposals"))
    flash("已記錄專員決定", "success")
    if action != "reject" and proposal["agent"] in ("affiliation", "correction"):
        _flash_auto_build(_try_auto_build())
        if proposal["agent"] == "affiliation":
            _flash_affiliation_fees(proposal.get("subject", ""))
    return redirect(url_for("office_ai_proposals"))


_FARE_NOTE_RE = re.compile(r"約([\d.]+)km\*(\d+)\*(\d+)=\d+")


def _fee_text(member, unit: str = "") -> str:
    """畫面用：主檔已算好的交通費與說明（只改寫說明文字，不重算金額）。"""
    exempt = U.is_fee_exempt_unit(unit or member.服務單位)
    if member.交通費 is None:
        return "不發（本系委員）" if exempt else "查無距離"
    note = member.交通費說明 or ""
    parts = []
    found = _FARE_NOTE_RE.search(note)
    if found:
        km, rate, trips = found.groups()
        parts.append(f"單程 {float(km):g} 公里 × 來回 {trips} × 每公里 {rate} 元")
        mapped = re.search(r"依專員確認對照：([^）)]+)", note)
        if mapped:
            parts.append(f"距離依「{mapped.group(1)}」")
    elif note:
        parts.append(note)
    elif member.交通費 == 0 and exempt:
        parts.append("本系委員")
    return f"{member.交通費} 元" + (f"（{'，'.join(parts)}）" if parts else "")


def _flash_affiliation_fees(unit: str) -> None:
    """採用服務單位對應後，把主檔重算出的交通費直接告訴專員。"""
    from .. import ai_proposals as P
    try:
        master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
    except (OSError, ValueError, U.StateDataError):
        return
    if master is None or not unit:
        return
    key = P.alias_key(unit)
    lines = []
    for exam in master.exams:
        for member in exam.委員:
            committee = master.committees.get(member.committee_id)
            member_unit = member.服務單位 or (committee.服務單位 if committee else "")
            if P.alias_key(member_unit) != key:
                continue
            name = member.委員姓名 or (committee.委員姓名 if committee else "") or "委員"
            lines.append(f"{name}（{exam.姓名 or exam.學號}的口試）交通費 {_fee_text(member, member_unit)}")
    if lines:
        flash("採用後的結果：" + "；".join(lines), "success")


@app.route("/office/ai-proposals/settings", methods=["POST"])
@login_required(role="office")
def save_ai_settings():
    from .. import ai_proposals as P
    try:
        P.save_settings(cfg().state_dir, {a: request.form.get(a) == "1" for a in P.AGENTS},
                        actor=_handler_from_form())
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("已儲存 AI 建議開關", "success")
    return redirect(url_for("office_ai_proposals"))


@app.route("/office/ai-proposals/alias/remove", methods=["POST"])
@login_required(role="office")
def remove_ai_alias():
    from .. import ai_proposals as P
    try:
        P.remove_alias(cfg().state_dir, unit_key=request.form.get("key", ""), actor=_handler_from_form(),
                       reason=request.form.get("reason", ""))
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("office_ai_proposals"))
    flash("已撤銷服務單位對照", "success")
    _flash_auto_build(_try_auto_build())
    return redirect(url_for("office_ai_proposals"))


@app.route("/office/archives")
@login_required(role="office")
def office_archives():
    folder = cfg().state_dir / "archives"
    terms = [p.name for p in sorted(folder.glob("*"))
             if _TERM_RE.fullmatch(p.name) and (p / "manifest.json").is_file()]
    return render_template("office_archives.html", terms=terms, archive=None)


@app.route("/office/archives/<term>")
@login_required(role="office")
def archive_files(term):
    if not _TERM_RE.fullmatch(term):
        abort(404)
    path = cfg().state_dir / "archives" / term / "manifest.json"
    if not path.is_file():
        abort(404)
    archive = json.loads(path.read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = {}
    labels = {"state": "系統狀態紀錄（寄送、確認、稽核）", "submissions": "學生填寫", "master": "主檔",
              "documents": "產出文件（附件4／附件6）", "attachment2.xlsx": "附件2（Portal 匯出）",
              "members.xlsx": "口試委員歷史資料", "distances.csv": "交通費距離表"}
    for name in sorted(archive.get("files", {})):
        head = name.replace("\\", "/").split("/", 1)[0]
        groups.setdefault(labels.get(head, "其他"), []).append(name)
    return render_template("office_archives.html", terms=[], term=term, archive=archive,
                           archive_groups=sorted(groups.items()), active_page="archives")


@app.route("/office/archives/<term>/files/<path:filename>")
@login_required(role="office")
def download_archive_file(term, filename):
    if not _TERM_RE.fullmatch(term):
        abort(404)
    from .. import source_state
    folder = (cfg().state_dir / "archives" / term).resolve()
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        abort(404)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    path = (folder / filename).resolve()
    if filename not in data["files"] or not path.is_relative_to(folder) or not path.is_file():
        abort(404)
    if source_state.digest(path) != data["files"][filename]:
        abort(409, "封存檔案校驗失敗，請核對備份")
    return send_from_directory(folder, filename, as_attachment=True)


@app.route("/office/archives/<term>/manifest")
@login_required(role="office")
def archived_term(term):
    if not _TERM_RE.fullmatch(term):
        abort(404)
    return send_from_directory(cfg().state_dir / "archives" / term, "manifest.json", as_attachment=True)


_ERROR_PAGES = {
    400: ("無法處理這個要求", "表單已過期或內容不完整，請回上一頁重新整理後再試一次。"),
    403: ("沒有這項操作的權限", "這個帳號不能執行這項操作。"),
    404: ("找不到頁面", "這個網址不存在，或資料已經更新、檔案已不提供下載。"),
    409: ("資料已經更新", "畫面上的資料已不是最新版本，這次操作沒有執行。"),
    413: ("檔案太大", "上傳檔案超過 20MB 上限，這次沒有上傳任何檔案。"),
}
_ERROR_HINTS = {
    403: "評審帳號可檢視所有頁面並操作人工關卡；修改系統設定、上傳來源檔、匯出 Excel 與下載封存檔在評審模式不開放（會記入稽核）。",
    409: "通常是主檔在你開啟頁面後已重建（例如學生補件、AI 建議被採用）。請回文件中心重新產生文件，再下載新的檔案。",
}


def _error_page(error):
    code = getattr(error, "code", 500) or 500
    heading, default = _ERROR_PAGES.get(code, ("發生錯誤", "這次操作沒有完成，請回上一頁再試一次。"))
    description = getattr(error, "description", "") or ""
    # Werkzeug 預設的英文說明不顯示；程式 abort() 帶的中文原因才顯示
    message = description if description and re.search(r"[一-鿿]", description) else default
    office = session.get("role") == "office"
    if office:
        back_url, back_label = (request.referrer if request.referrer and request.referrer.startswith(request.host_url)
                                else url_for("office_overview")), "回到上一頁"
    elif session.get("role") == "student":
        back_url, back_label = url_for("dashboard"), "回到我的口試申請"
    else:
        back_url, back_label = url_for("login"), "回到登入頁"
    hint = _ERROR_HINTS.get(code) if (code != 403 or _is_judge()) else None
    if hint and code == 403:
        hint = hint.replace(judge.ACTOR, judge.label())
    return render_template("error.html", layout="office_base.html" if office else "base.html",
                           heading=heading, message=message, hint=hint,
                           back_url=back_url, back_label=back_label), code


for _code in _ERROR_PAGES:
    app.register_error_handler(_code, _error_page)


@app.errorhandler(U.StateDataError)
def unreadable_state(error):
    return render_template("state_error.html", message=str(error)), 503


@app.errorhandler(U.BuildLockTimeout)
def busy_state(error):
    return render_template("state_busy.html",
                           message="系統正在執行其他作業（例如每日流程或寄信），請稍候一分鐘再試；剛才的操作沒有執行。"), 503


@app.context_processor
def source_freshness_context():
    reason = None
    if request.path.startswith("/office") and cfg().master_path.is_file():
        from .. import source_state
        try:
            source_state.require_current(cfg().master_path, allow_legacy=False)
        except ValueError as exc:
            reason = str(exc)
    return {"master_stale_reason": reason}


@app.context_processor
def case_names_context():
    """系辦頁面用：案件鍵 → 學生姓名，畫面顯示姓名而不是「學期-學號」。"""
    if not request.path.startswith("/office") or not cfg().master_path.is_file():
        return {"case_names": {}}
    try:
        master = load_current_master(cfg().master_path)
    except (OSError, ValueError, U.StateDataError):
        return {"case_names": {}}
    return {"case_names": {e.案件鍵: e.姓名 for e in master.exams if e.姓名}}


@app.context_processor
def institution_context():
    """校系名稱一律取自設定（KOUSHI_UNIV_NAME 等），範本不寫死真實校名。"""
    return {"univ_name": C.UNIV_NAME, "dept_name": C.DEPT_NAME, "office_name": C.OFFICE_SIGNATURE}


@app.context_processor
def demo_context():
    if cfg().demo_root:
        from ..demo import active
        _, meta = active(cfg().demo_root)
        return {"demo_info": meta, "demo_now": clock.now()}
    return {"demo_info": None}


@app.context_processor
def judge_context():
    context = {"judge_mode": cfg().judge_mode, "is_judge": _is_judge(), "judge_banner": judge.banner(),
               "who": judge.label(), "judge_login": cfg().judge_mode and judge.login_enabled()}
    if cfg().judge_mode:
        context["judge_model"] = _llm.describe_active_model(cfg().state_dir)
    return context


@app.template_filter("judge_sent")
def _judge_sent(text):
    """評審模式不寄真實信件：畫面上的「已寄出」改說「已放入模擬寄件匣」。"""
    text = str(text or "")
    if not cfg().judge_mode:
        return text
    return (text.replace("已寄出回信", f"已放入模擬寄件匣（{judge.label()}模式不寄出真實信件）")
            .replace("先前已寄出", "先前已放入模擬寄件匣"))


@app.context_processor
def ai_switch_context():
    if not request.path.startswith("/office"):
        return {}
    return {"ai_stop_reason": ai_switch.disabled_reason(cfg().state_dir),
            "ai_stop_env": ai_switch.env_disabled(), "ai_switch_status": ai_switch.load(cfg().state_dir)}


@app.route("/office/ai-switch", methods=["POST"])
@login_required(role="office")
def ai_switch_toggle():
    """AI 總開關（緊急停止）：停用全部 LLM 呼叫與 agent 自動動作，退回人工流程。"""
    disabled = request.form.get("disabled") == "1"
    try:
        ai_switch.set_disabled(cfg().state_dir, disabled=disabled, actor=_handler_from_form(),
                               reason=request.form.get("reason", ""))
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("已啟用 AI 緊急停止：所有 AI 判讀與自動動作停止，改由人工處理" if disabled
              else "已恢復 AI 功能", "success")
    return redirect(url_for("office_ai_settings"))


@app.route("/office/judge/outbox")
@login_required(role="office")
def judge_outbox():
    """評審模式的模擬寄件匣：系統「本來會寄出」的每一封信，以及評審模擬的回信。"""
    if not cfg().judge_mode:
        abort(404)
    replies: dict[str, list[dict]] = {}
    for r in judge.inbox(cfg().state_dir):
        replies.setdefault(r.get("reply_to_outbox", ""), []).append(r)
    mails = list(reversed(judge.outbox(cfg().state_dir)))
    for no, m in zip(range(len(mails), 0, -1), mails):
        m["no"] = no
    _label_outbox(mails)
    return render_template("office_judge_outbox.html", active_page="judge_outbox",
                           mails=mails, replies=replies, stranger=judge.STRANGER)


def _label_outbox(mails: list[dict]) -> None:
    """寄件匣每封信加上類型標籤、短標題，同一份附件4 標出最新版／舊版（mails 已是新到舊）。"""
    seen_att4: set[str] = set()
    for m in mails:
        subject = str(m.get("subject", ""))
        is_reply = subject.lower().startswith("re:")
        rest = re.sub(r"^re:\s*", "", subject, flags=re.I)
        tag_match = re.match(r"^\[([^\]]*)\]\s*", rest)
        tag = re.sub(r"\s+[A-Za-z0-9]+-[0-9a-f]+$", "", tag_match.group(1)).strip() if tag_match else ""
        m["title"] = rest[tag_match.end():] if tag_match else rest
        m["version"] = ""
        if is_reply:
            m["kind"] = "系辦回信"
        else:
            m["kind"] = tag.removeprefix("口試") or "通知"
            if "附件4" in tag and m.get("attachments"):
                m["version"] = "舊版" if tag_match.group(1) in seen_att4 else "最新版"
                seen_att4.add(tag_match.group(1))


@app.route("/office/judge/outbox/reply", methods=["POST"])
@login_required(role="office")
def judge_outbox_reply():
    if not cfg().judge_mode:
        abort(404)
    try:
        judge.simulate_reply(cfg().state_dir, outbox_id=request.form.get("outbox_id", ""),
                             text=request.form.get("text", ""), stranger=request.form.get("stranger") == "1")
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("已放入模擬收件匣；到「模擬時間與每日流程」頁按「執行本日流程」後，系統才會讀取", "success")
    return redirect(url_for("judge_outbox"))


@app.route("/office/demo", methods=["GET", "POST"])
@login_required(role="office")
def office_demo():
    if not cfg().demo_root:
        abort(404)
    from .. import demo
    from ..runtime import readiness
    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "reset":
                demo.initialize(cfg().demo_root, request.form.get("scenario", "normal"))
                configure_app(app, demo.web_config(cfg().demo_root))
                if not _is_judge():  # 評審的處理人固定為「評審」
                    session.pop("handler", None)
                _try_auto_build()
                flash("已重設為新一輪展示，過往資料與信件保留", "success")
            elif action == "advance":
                clock.advance(cfg().demo_root, int(request.form.get("days", "1")))
                flash("已推進模擬日期，請執行本日流程", "success")
            elif action in ("preview", "send"):
                result = demo.run_automation(cfg(), send=action == "send")
                flash("已完成本日流程：" + office_settings.PHASE_LABELS[result.phase], "success")
                for note in result.notes:
                    flash(note, "info")
                if result.proposals:
                    flash(f"AI 建議：本輪新增 {len(result.proposals)} 筆，待專員在「AI 建議」頁決定", "info")
            else:
                raise ValueError("未知的展示操作")
        except Exception as exc:
            from ..application import record_automation_status
            record_automation_status(cfg().state_dir, ok=False, error=str(exc))
            flash("展示操作失敗：" + str(exc), "error")
        return redirect(url_for("office_demo"))
    _, meta = demo.active(cfg().demo_root)
    settings = office_settings.load(cfg().state_dir)
    return render_template("office_demo.html", active_page="demo", meta=meta,
        scenarios=demo.SCENARIOS, ready=readiness(), mailboxes=demo.MAILBOXES,
        model_label=_llm.describe_active_model(cfg().state_dir), model_offline=_llm.offline_selected(),
        ready_labels={"MAIL_SENDER": "寄信帳號", "MAIL_PASSWORD": "寄信密碼", "OPENAI_API_KEY": "模型金鑰",
                      "KOUSHI_WEB_SECRET_KEY": "網站簽章金鑰", "KOUSHI_OFFICE_PASSWORD": "系辦登入密碼"},
        phase_label=office_settings.PHASE_LABELS[settings.phase_on(clock.today())],
        automation=load_automation_status(cfg().state_dir))


# ── 學生表單 ──────────────────────────────────────────

@app.route("/form")
@login_required(role="student")
def form():
    term = request.args.get("term", "").strip()
    settings = office_settings.load(cfg().state_dir)
    if not settings.student_open:
        flash(STUDENT_NOT_OPEN_MESSAGE, "error")
        return redirect(url_for("dashboard"))
    if term != settings.current_term:
        flash(f"本期受理的學期是 {_labels.term_label(settings.current_term)}，請從首頁的「開始填寫」進入", "error")
        return redirect(url_for("dashboard"))
    # 案件鍵的學號永遠來自 session，不接受任何客戶端傳來的學號：學生
    # 只能決定「這是哪個學期」，不可能組出屬於別人的案件鍵。
    case_key = U.case_key(term, session["student_id"])
    data = None
    path = cfg().submissions_dir / f"{case_key}.json"
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    # 有暫存就接著暫存填（暫存只會比上次送出新：送出成功時會刪掉暫存）
    draft = load_draft(cfg().submissions_dir, case_key)
    return render_template("form.html", data=draft or data, case_key=case_key, term=term,
                           student_id=session["student_id"], from_draft=draft is not None,
                           **_form_period(settings, case_key))


def _form_period(settings, case_key: str) -> dict:
    """學生表單的期間說明：填寫期間「截止日前可修改」；補件期間改說補件截止日。"""
    phase = settings.phase_on(clock.now().date())
    deadline = student_case_status(case_key, cfg().state_dir).deficiency_deadline
    return {"deficiency_period": phase in (office_settings.PHASE_DEFICIENCY, office_settings.PHASE_CLOSED),
            "edit_deadline": deadline or settings.fill_deadline}


@app.route("/submit", methods=["POST"])
@login_required(role="student")
def submit():
    term = str(request.form.get("term", "")).strip()
    settings = office_settings.load(cfg().state_dir)
    if not settings.student_open:
        flash(STUDENT_NOT_OPEN_MESSAGE, "error")
        return redirect(url_for("dashboard"))
    if term != settings.current_term:
        flash(f"本期受理的學期是 {_labels.term_label(settings.current_term)}，請從首頁的「開始填寫」進入", "error")
        return redirect(url_for("dashboard"))
    case_key = U.case_key(term, session["student_id"])
    blocked = submission_block_reason(case_key, cfg().state_dir)
    if blocked:
        flash(blocked, "error")
        return redirect(url_for("dashboard"))
    if request.form.get("action") == "draft":
        # 暫存：不檢查、不算送出（2026-10-01 Q2）
        try:
            save_draft(student_id=session["student_id"], fields=request.form,
                       submissions_dir=cfg().submissions_dir)
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("已暫存，尚未送出；填完後請記得按「送出」", "info")
        return redirect(url_for("dashboard"))
    try:
        if "base_version" not in request.form:
            raise ValueError("編輯版本遺失，請重新載入表單")
        data = submit_application(
            student_id=session["student_id"], fields=request.form,
            submissions_dir=cfg().submissions_dir, versions_dir=cfg().versions_dir,
            degree=(load_current_master(cfg().master_path).by_key().get(case_key).學位別
                    if cfg().master_path.is_file() and case_key in load_current_master(cfg().master_path).by_key() else None),
        )
    except ValueError as exc:
        flash(str(exc), "error")
        from ..application import _parse_advisor_emails, _parse_committees
        entered = {k: request.form.get(k, "") for k in (
            "student_email", "thesis_title", "exam_date", "exam_start_time", "exam_end_time",
            "exam_location", "exam_mode", "base_version")}
        entered.update(advisors=list(_parse_advisor_emails(request.form)),
                       advisor_emails=_parse_advisor_emails(request.form), committees=_parse_committees(request.form))
        return render_template("form.html", data=entered, case_key=case_key, term=term,
            student_id=session["student_id"], from_draft=False,
            **_form_period(settings, case_key)), 409 if "已被更新" in str(exc) else 422
    record_student_submission(cfg().state_dir, data)
    mark_case_resubmitted(case_key, cfg().state_dir)
    # 送出即有效：填寫期間刷新預覽版主檔、補件期間即時重檢。建檔結果只給
    # 系辦看（匯入資料頁），不對學生顯示。
    _try_auto_build()
    flash(f"已送出 {_labels.term_label(data['term'])}的口試補充資料（第 {data['version']} 版）", "success")
    for hint in submission_self_check(data, settings=office_settings.load(cfg().state_dir),
                                      degree=_degrees().get(case_key)):
        flash(f"請確認：{hint}", "info")
    return redirect(url_for("dashboard"))


@app.route("/change-request", methods=["POST"])
@login_required(role="student")
def change_request():
    """資料鎖定時，學生申請異動（2026-10-01 Q8）；只會送到專員介面，不會自動解鎖。"""
    settings = office_settings.load(cfg().state_dir)
    if not settings.student_open:
        flash(STUDENT_NOT_OPEN_MESSAGE, "error")
        return redirect(url_for("dashboard"))
    case_key = U.case_key(settings.current_term, session["student_id"])
    try:
        case_access.submit_request(cfg().state_dir, case_key=case_key, reason=request.form.get("reason", ""))
    except ValueError as exc:
        flash(str(exc), "error")
    else:
        flash("已送出異動申請，系辦處理後會開放修改或與你聯絡", "success")
    return redirect(url_for("dashboard"))


# ── 檢視（雙角色共用）────────────────────────────────

@app.route("/view/<case_key>")
@login_required()
def view(case_key):
    # 案件鍵格式檢查是唯一防線：case_key 直接組進檔案路徑，格式不符
    # (例如夾帶路徑分隔字元) 一律拒絕，不能只靠後面的擁有者比對。
    if not _CASE_KEY_RE.match(case_key):
        flash("找不到該筆申請", "error")
        return redirect(url_for("dashboard"))
    path = cfg().submissions_dir / f"{case_key}.json"
    if not path.is_file():
        flash("找不到該筆申請", "error")
        return redirect(url_for("dashboard"))
    data = json.loads(path.read_text(encoding="utf-8"))
    if session["role"] == "student" and data.get("student_id") != session["student_id"]:
        flash("無法檢視他人的申請", "error")
        return redirect(url_for("dashboard"))
    return render_template("view.html", data=data, case_key=case_key)


# ── 系辦上傳建檔資料 ──────────────────────────────────

@app.route("/office/upload-registries", methods=["POST"])
@login_required(role="office")
def upload_registries():
    updated, errors = [], []
    for kind, label in _OFFICE_FILE_LABELS.items():
        file = request.files.get(kind)
        if not file or not file.filename:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            # 瀏覽器給的檔名不可信（可能含 ../ 或絕對路徑），只取最後一段，且保留副檔名供格式判斷
            safe_name = Path(file.filename.replace("\\", "/")).name
            if safe_name in ("", ".", ".."):
                safe_name = "upload"
            tmp_path = Path(tmp) / safe_name
            file.save(tmp_path)
            try:
                saved = save_office_registry_file(kind, tmp_path, registries=cfg().registries)
            except ValueError as exc:
                errors.append(f"{label}：{exc}")
                continue
        if kind == "attachment2":
            record_att2_upload(cfg().state_dir, saved, day=clock.now().date())
        updated.append(label)

    if not updated and not errors:
        flash("請至少選擇一份檔案", "error")
        return redirect(url_for("office_import"))
    if updated:
        flash(f"已更新：{'、'.join(updated)}", "success")
    for err in errors:
        flash(err, "error")
    if updated:
        _flash_auto_build(_try_auto_build())
    return redirect(url_for("office_import"))


# ── 系辦問答助理（唯讀；docs/問答助理設計.md）──────────────

from .. import assistant as _assistant, assistant_tools as _assistant_tools, llm as _llm

_ASSISTANT_SUGGESTIONS = (
    "哪些案件卡在待專員放行？", "附件4 已經寄給哪些教授？", "學生缺件有幾件已經逾期？",
    "還有哪些委員沒有回填個資？", "這學期的補件截止日是哪天？", "最近 7 天寄出幾封信？有失敗的嗎？",
)


def _assistant_llm():
    """(模型, 停用原因)：AI 總開關關閉、沒設定 LLM 或系辦在設定頁關閉時停用。"""
    stopped = ai_switch.disabled_reason(cfg().state_dir)
    if stopped:
        return None, f"AI 總開關已關閉，問答助理停用（{stopped}）。"
    llm = _llm.make_llm()
    if llm is None:
        return None, "尚未設定 LLM（GEMINI_API_KEY 或 OPENAI_API_KEY），問答助理停用。"
    if not office_settings.load(cfg().state_dir).assistant_enabled:
        return None, "問答助理已在「期限設定」頁關閉。"
    return llm, None


@app.context_processor
def assistant_context():
    if not request.path.startswith("/office"):
        return {}
    configured = not ai_switch.is_disabled(cfg().state_dir) and _llm.make_llm() is not None
    return {"assistant_llm_configured": configured,
            "assistant_nav": configured and office_settings.load(cfg().state_dir).assistant_enabled}


@app.route("/office/assistant")
@login_required(role="office")
def office_assistant():
    _, reason = _assistant_llm()
    return render_template("office_assistant.html", active_page="assistant", disabled_reason=reason,
                           suggestions=_ASSISTANT_SUGGESTIONS)


@app.route("/office/assistant/ask", methods=["POST"])
@login_required(role="office")
def office_assistant_ask():
    """回傳一則回答的 HTML 片段；只讀資料，不取狀態寫入鎖（見 _lock_mutation）。"""
    llm, reason = _assistant_llm()
    if llm is None:
        return f'<div class="answer-note">{reason}</div>', 403
    master = load_current_master(cfg().master_path) if cfg().master_path.is_file() else None
    tracking = _office_tracking()
    snapshot = _assistant_tools.build_snapshot(
        state_dir=cfg().state_dir, today=clock.now().date(),
        overview_rows=_overview_rows(_load_submissions(), tracking.deficiency.by_case if tracking else {}),
        reviews=_review_rows(), tracking=tracking, master=master, track_labels=_TRACK_STATUS_LABELS)
    ans = _assistant.ask(request.form.get("question", ""), snapshot=snapshot, llm=llm, state_dir=cfg().state_dir)
    aliaser = ans.aliaser
    return render_template(
        "_assistant_answer.html", ans=ans,
        seg=lambda text: _assistant.segments(text, aliaser), tables=_assistant.output_tables,
        case_names=aliaser.case_names if aliaser else {},
        legend=aliaser.legend(ans.tokens) if aliaser else [],
        pretty=lambda obj: json.dumps(obj, ensure_ascii=False, indent=1),
    )


# ── Agent 軌跡查核與指標（唯讀；koushi/agent_runs.py、koushi/agent_metrics.py）──────

from .. import agent_metrics as _agent_metrics, agent_runs as _agent_runs

_TASK_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
app.jinja_env.globals["trace_task_id"] = _agent_runs.task_id


def _trace_filters() -> dict:
    agent = request.args.get("agent", "")
    return {
        "agent": agent if agent in _agent_runs.AGENT_LABELS else "",
        "date_from": request.args.get("date_from", "") if _DATE_RE.match(request.args.get("date_from", "")) else "",
        "date_to": request.args.get("date_to", "") if _DATE_RE.match(request.args.get("date_to", "")) else "",
        "needs_human": request.args.get("needs_human") == "1",
        "has_error": request.args.get("has_error") == "1",
    }


@app.route("/office/agent-trace")
@login_required(role="office")
def office_agent_trace():
    """Agent 軌跡查核：每一次 agent 執行的摘要，可篩選；只讀、內容已遮蔽。"""
    filters = _trace_filters()
    runs = _agent_runs.list_runs(cfg().state_dir, **filters)
    return render_template("office_agent_trace.html", active_page="agent_trace", runs=runs[:300],
                           total=len(runs), filters=filters, agents=_agent_runs.AGENT_LABELS,
                           titles=_trace_titles())


def _trace_titles() -> dict[str, str]:
    """軌跡清單的可讀主題：信箱 agent 顯示信件主旨、AI 建議顯示建議主題（皆再遮蔽一次）。"""
    from .. import ai_proposals
    titles = {}
    sd = cfg().state_dir
    for m in mailbox_agent.load_mails(sd):
        titles[_agent_runs.task_id("mailbox", m.get("run_id"), [m.get("message_id")])] = \
            "來信：" + str(m.get("subject") or "（無主旨）")
    for p in ai_proposals.load(sd):
        subject = p.get("subject") or ""
        if p.get("agent") == "correction" and p.get("issue"):
            subject = f"{_labels.issue_label((p['issue'] or {}).get('問題代碼'))}（{(p['issue'] or {}).get('欄位') or ''}）"
        titles[_agent_runs.task_id(p.get("agent"), p.get("run_id"), [p.get("subject_key")])] = subject
    return {k: _agent_runs.sanitize(v) for k, v in titles.items()}


@app.route("/office/agent-trace/<tid>")
@login_required(role="office")
def agent_trace_run(tid):
    if not _TASK_ID_RE.match(tid):
        abort(404)
    run = _agent_runs.get_run(cfg().state_dir, tid)
    if run is None:
        abort(404)
    return render_template("office_agent_trace_run.html", active_page="agent_trace", run=run,
                           s=run["summary"], title=_trace_titles().get(tid, ""),
                           pretty=lambda obj: json.dumps(obj, ensure_ascii=False, indent=1))


@app.route("/office/agent-trace/<tid>/export.json")
@login_required(role="office")
def agent_trace_export(tid):
    """單次執行的可查核 JSON（已遮蔽，不含送模型前就去除的個資）；評審模式也可下載。"""
    if not _TASK_ID_RE.match(tid):
        abort(404)
    run = _agent_runs.get_run(cfg().state_dir, tid)
    if run is None:
        abort(404)
    if _is_judge():
        _judge_audit("export_trace", task_id=tid)
    data = _agent_runs.export_run(run, exported_at=clock.now().strftime("%Y-%m-%d %H:%M:%S"))
    response = app.response_class(json.dumps(data, ensure_ascii=False, indent=2),
                                  mimetype="application/json; charset=utf-8")
    response.headers["Content-Disposition"] = f'attachment; filename="agent-trace-{tid}.json"'
    return response


@app.route("/office/flow")
@login_required(role="office")
def office_flow():
    """流程圖：一學期的正常流程與每一步的例外、處理方式（靜態說明，連到各頁）。"""
    return render_template("office_flow.html", active_page="flow")


@app.route("/office/agent-metrics")
@login_required(role="office")
def office_agent_metrics():
    """Agent 指標儀表板：由目前狀態目錄的軌跡即時計算（與 eval/agent_metrics.py 同一套邏輯）。"""
    filters = _trace_filters()
    events = _agent_metrics.load_events(cfg().state_dir)
    if filters["date_from"] or filters["date_to"]:
        events = [e for e in events
                  if (not filters["date_from"] or str(e.get("at", ""))[:10] >= filters["date_from"])
                  and (not filters["date_to"] or str(e.get("at", ""))[:10] <= filters["date_to"])]
    metrics = _agent_metrics.compute(events)
    tasks = sum(m.get("tasks") or 0 for m in metrics.values())
    human = sum((m.get("human_intervention_rate") or 0) * (m.get("tasks") or 0) for m in metrics.values())
    mails = mailbox_agent.load_mails(cfg().state_dir)
    highlights = {
        "tasks": tasks, "human_rate": (human / tasks) if tasks else None,
        "repairs": sum(m.get("repairs") or 0 for m in metrics.values()),
        "repaired_tasks": sum(m.get("repaired_tasks") or 0 for m in metrics.values()),
        "injections": sum(1 for m in mails if (m.get("analysis") or {}).get("suspicious") or m.get("injection_flag")),
        "injections_auto": sum(1 for m in mails if ((m.get("analysis") or {}).get("suspicious") or m.get("injection_flag"))
                               and any(a.get("policy") == "auto" and a.get("type") != mailbox_agent.A_CONFIRMED_BY_FLOW
                                       for a in m.get("actions", []))),
    }
    return render_template("office_agent_metrics.html", active_page="agent_metrics", metrics=metrics,
                           highlights=highlights, offline=_llm.offline_selected(),
                           filters=filters, agents=_agent_runs.AGENT_LABELS, events=len(events),
                           definitions=_agent_metrics.METRIC_DEFINITIONS, prices=_agent_metrics.DEFAULT_PRICES,
                           prices_as_of=_agent_metrics.PRICES_AS_OF,
                           active_model=_llm.describe_active_model(cfg().state_dir))


def main():
    if not os.environ.get(_SECRET_KEY_ENV):
        print(f"[警告] 未設定環境變數 {_SECRET_KEY_ENV}，本次啟動使用隨機值，"
              "重啟後所有登入 session 會失效。", file=sys.stderr)
    if not os.environ.get(_OFFICE_PASSWORD_ENV):
        print(f"[警告] 未設定環境變數 {_OFFICE_PASSWORD_ENV}，本次啟動使用隨機密碼，"
              "系辦將無法登入，請設定後重新啟動。", file=sys.stderr)
    debug = os.environ.get("KOUSHI_WEB_DEBUG") == "1"
    app.run(debug=debug, port=5000, use_reloader=False)


if __name__ == "__main__":
    main()
