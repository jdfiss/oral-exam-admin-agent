# -*- coding: utf-8 -*-
"""附件4 自動寄給指導教授，以及收教授回信（已確認流程決策第六節；2026-10-12 修訂）。

2026-10-12 起取消「教授確認附件4」關卡：附件4 只是寄給指導教授**參考核對**。
教授不用回信；沒回信不催、不擋任何後續步驟。教授發現資料有誤才回信，回信照常
收進來判讀、擬修正草稿給專員核准；教授回「確認無誤」仍記為已確認（無害的紀錄）。

寄送（2026-10-10 起附件4 全學期一份）：
- 附件4 是本學期所有通過寄送關卡案件的口試一覽，**每位指導教授收到同一份**（不再依教授拆檔）；
  檔案記在 `STATE_DIR/attachment4_documents.json`（學期 → 檔案、全文件版本、收錄案件）。
- 寄送請求是每學期、每位指導教授一筆；信件內文列出**這位教授自己學生的列**（姓名、
  學位、日期時間地點、委員），註明資料供參考、不需回覆，有誤再直接回信。
- 寄送關卡（哪些案件可以寄、收錄進附件4）集中在 `sendable_keys()`；這位教授名下案件
  全部通過關卡才寄。收件人是學生填的教授 E-mail（系辦通訊錄後備）。
- 主旨帶請求代碼 `A4-xxxxxxxx`，內文帶「確認代碼：A4-xxxxxxxx-版本」——版本只由這位教授
  自己的列計算，所以別位教授的案件變動不會重寄給他。
- 寄出後版本記在 `STATE_DIR/confirmation_mail.json`。之後這位教授自己的列變了（換日期、
  時地、委員、學生），一律重設並只對這位教授重寄最新的全學期附件4。

收信：
- `inbox.fetch_replies()` 讀信箱；`ingest_replies()` 以主旨代碼對到請求（沒有代碼時，
  寄件者恰好對應一筆已寄出的請求才對上），再做確定性核對：寄件者是否為當初的收件人、
  版本是否仍是目前版本、請求是否已寄出且尚未登錄回覆。
- 核對結果與原信存在 `STATE_DIR/confirmation_inbox.json`。判讀回信意圖是 LLM 的工作
  （`classifier`）；沒有 classifier 時一律交系辦在文件中心判讀（確認無誤／需修改／忽略）。
  classifier 判為「確認無誤」且核對全部通過時才自動記為已確認，其餘一律人工。
- `reminder_*` 是舊的催辦信（教授逾期未回）；2026-10-12 起不再排程，保留函式供舊資料相容。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Protocol

import logging

from . import audit_log, config as C, util as U
from .classifier import (
    ClassificationResult, ClassifierAdapter, ClassifierError,
    INTENT_CONFIRM, INTENT_MODIFY, INTENT_QUESTION, INTENT_UNKNOWN,
    INTENT_AUTO_REPLY, INTENT_UNRELATED,
)
from .drafts import STATUS_NO_RECIPIENT, STATUS_READY, compute_idempotency_key
from .engines.confirmation import CONFIRMED, MODIFIED, PENDING, ConfirmationRequest
from .generators import attachment4
from .model import Master

logger = logging.getLogger(__name__)

TYPE_ATTACHMENT4 = "attachment4"
MAIL_FILE = "confirmation_mail.json"
INBOX_FILE = "confirmation_inbox.json"
DOCUMENTS_FILE = "attachment4_documents.json"
REF_RE = re.compile(r"\bA4-[0-9a-f]{8}\b")

# 收件匣項目狀態
INBOX_PENDING = "pending_review"
INBOX_APPLIED = "applied"
INBOX_IGNORED = "ignored"

# Re-export intent constants for backward compatibility
__all__ = [
    "INTENT_CONFIRM", "INTENT_MODIFY", "INTENT_QUESTION", "INTENT_UNKNOWN",
    "INTENT_AUTO_REPLY", "INTENT_UNRELATED",
]


def ref_code(request: ConfirmationRequest) -> str:
    raw = "|".join(str(part) for part in request.request_key)
    return "A4-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _subset(master: Master, request: ConfirmationRequest) -> Master:
    keys = set(request.case_keys)
    return Master(exams=[e for e in master.exams if e.案件鍵 in keys], committees=master.committees)


def rows(master: Master, request: ConfirmationRequest) -> list[dict]:
    """這位教授自己的附件4 列（與全學期 Word 中這位教授那幾列相同的投影）。"""
    return [row for term_rows in attachment4.group(_subset(master, request).exams).values()
            for row in term_rows]


def content_version(master: Master, request: ConfirmationRequest) -> str:
    """這位教授的附件4 內容版本：只算**他自己學生的列**（附件4 每一格＋信中列出的時地與委員）。

    附件4 全學期一份，但版本不看別位教授的列——別人的案件改了，不影響這位教授的
    確認、比對與重寄（2026-10-10）。
    """
    keys = ("學期", "指導教授", "日期", "方式", "備註", C.PROGRAM_DOCTOR, C.PROGRAM_MASTER, C.PROGRAM_INSERV)
    payload = json.dumps([[str(r.get(k, "")) for k in keys] for r in rows(master, request)],
                         ensure_ascii=False, sort_keys=True)
    # Changes to logistics or the committee also invalidate an approval.
    exam_details = []
    for exam in sorted(_subset(master, request).exams, key=lambda e: e.案件鍵):
        members = [master.committees.get(m.committee_id) for m in exam.委員]
        exam_details.append([exam.案件鍵, str(exam.口試日期), exam.口試開始時間,
            exam.口試結束時間, exam.口試地點, exam.口試方式, exam.指導教授, exam.advisor_emails,
            [(m.委員姓名 or c.委員姓名, m.服務單位 or c.服務單位, m.職稱, m.視訊出席)
             for m, c in zip(exam.委員, members) if c]])
    payload += json.dumps(exam_details, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


def own_rows(master: Master, request: ConfirmationRequest) -> list[dict]:
    """信件內文要請教授核對的列：這位教授名下每位學生一列（依口試日期排序）。"""
    by_key = master.by_key()
    out = []
    for key in request.case_keys:
        exam = by_key.get(key)
        if exam is None:
            continue
        committee = []
        for member in exam.委員:
            identity = master.committees.get(member.committee_id)
            name = member.委員姓名 or (identity.委員姓名 if identity else "")
            unit = U.display_unit(member.服務單位 or (identity.服務單位 if identity else ""))
            detail = "　".join(x for x in (unit, member.職稱) if x)
            committee.append(name + (f"（{detail}）" if detail else "") + ("［視訊］" if member.視訊出席 else ""))
        time = "–".join(x for x in (exam.口試開始時間, exam.口試結束時間) if x)
        out.append({
            "案件鍵": key, "姓名": exam.姓名,
            "學位": DEGREE_LABELS.get(exam.學位別, exam.學位別), "班別": exam.班別,
            "日期": exam.日期字串, "時間": time, "地點": exam.口試地點,
            "方式": str(exam.口試方式 or "").strip(), "委員": committee,
        })
    return out


def rows_text(master: Master, request: ConfirmationRequest) -> str:
    """這位教授自己學生的列（信件內文用）：姓名、學位、日期時間地點、委員。"""
    lines = []
    for r in own_rows(master, request):
        when = "　".join(x for x in (r["日期"] or "日期未定", r["時間"]) if x)
        where = r["地點"] or "地點未填"
        method = f"（{r['方式']}）" if r["方式"] and r["方式"] not in ("實體", "實體口試") else ""
        lines.append(f"  • {r['姓名']}（{r['班別'] or r['學位']}）　{when}　{where}{method}")
        lines.append(f"    口試委員：{'、'.join(r['委員']) or '（未填）'}")
    return "\n".join(lines)


# ── 寄送紀錄 ──

def load_mail_state(state_dir: str | Path) -> dict[str, dict]:
    path = Path(state_dir) / MAIL_FILE
    if not path.is_file():
        return {}
    return U.read_json_state(path).get("sent", {})


def _save_mail_state(state_dir: Path, data: dict[str, dict]) -> None:
    U.atomic_write_json({"sent": data}, state_dir / MAIL_FILE)


@U.locked_state()
def record_sent(state_dir: str | Path, request: ConfirmationRequest, *, version: str, to: str,
                date: _dt.date, round_no: int | None = None, delivery_key: str | None = None,
                message_id: str | None = None, sent_at: str | None = None) -> None:
    state_dir = Path(state_dir)
    data = load_mail_state(state_dir)
    ref = ref_code(request)
    previous = data.get(ref, {})
    if delivery_key and previous.get("delivery_key") == delivery_key:
        return
    rounds = int(previous.get("rounds", 0))
    data[ref] = {
        "request_key": list(request.request_key), "version": version, "to": to,
        "sent_date": date.isoformat(),
        "rounds": rounds + 1 if round_no is None else max(rounds, round_no + 1),
        "delivery_key": delivery_key, "message_id": message_id,
        "sent_at": sent_at or clock.now().isoformat(),
    }
    _save_mail_state(state_dir, data)


def sent_rounds(state_dir: str | Path, request: ConfirmationRequest) -> int:
    """這筆請求已寄過幾次；重寄新版本時放進去重鍵，內容改回舊版也能再寄。"""
    return int(load_mail_state(state_dir).get(ref_code(request), {}).get("rounds", 0))


def reset_changed(
    master: Master, requests: list[ConfirmationRequest], state_dir: str | Path,
) -> tuple[list[ConfirmationRequest], list[ConfirmationRequest]]:
    """附件4 內容與寄出時版本不同的請求重設為草稿。回傳 (更新後清單, 被重設者)。

    只處理由本模組寄出、有版本紀錄的請求；系辦手動登錄「已寄出」的沒有版本可比。
    """
    sent = load_mail_state(state_dir)
    updated, reset = [], []
    for req in requests:
        record = sent.get(ref_code(req))
        current = content_version(master, req)
        evidence_version = record["version"] if record else req.content_version
        if (req.sent_date or req.response == CONFIRMED) and evidence_version != current:
            fresh = replace(req, response=PENDING, sent_date=None, deadline=None,
                            response_date=None, content_version=current, reminder_count=0,
                            last_reminder_date=None, notes="附件4 內容變動，重寄新版本")
            updated.append(fresh)
            reset.append(fresh)
        else:
            updated.append(req)
    return updated, reset


# ── 寄送關卡：哪些案件可以寄附件4、收錄進全學期附件4（隊長 2026-10-12 定案 A）──

def sendable_keys(master: Master, state_dir: str | Path) -> set[str]:
    """通過附件4 寄送關卡的案件鍵（寄給教授、收錄進全學期附件4 都只用這一個函式）。

    條件（2026-10-12 隊長定案 A）：案件生效，資料齊全到可以印進附件4——檢核沒有
    「阻擋附件4」的問題（validation 本來就用這個欄位決定案件能不能進附件4；「警示」
    照常產出、欄位留白），也不在截止政策阻擋清單（補件截止後仍有學生問題）。
    **不等專員放行**：委員資格審核只管委員個資索取與口試後產檔，不再管附件4 寄送。
    """
    from . import validation
    from .application import _document_eligibility_issues
    active = {e.案件鍵 for e in master.exams if e.生效}
    incomplete = {i.get("案件鍵") for i in validation.check(master) if i.get("阻擋附件4") == "是"}
    blocked = {i.get("案件鍵") for i in _document_eligibility_issues(master, state_dir)}
    return active - incomplete - blocked


def ready_to_send(master: Master, requests: list[ConfirmationRequest],
                  state_dir: str | Path) -> list[ConfirmationRequest]:
    """尚未寄出、且這位教授本學期名下案件**全部**通過寄送關卡（sendable_keys）的請求。

    同一位教授還有案件沒過關卡時先不寄（2026-10-10 決定，延續原本「全組已放行」的語意）；
    等最後一件過關，才把當時的全學期附件4 寄給他，一次看完自己全部的學生。
    """
    sendable = sendable_keys(master, state_dir)
    return [
        r for r in requests
        if r.sent_date is None and r.response == PENDING and r.advisor
        and r.case_keys and all(k in sendable for k in r.case_keys)
    ]


DEGREE_LABELS = {"碩": "碩士", "博": "博士"}
_VERSION_RE = re.compile(r"(?:確認代碼：A4-[0-9a-f]{8}-|附件4 版本：)([0-9a-f]{10})")


def subject_for(request: ConfirmationRequest) -> str:
    """附件4 寄送信主旨（含代碼 A4-xxxxxxxx，教授回信時系統靠它對回請求）。

    2026-10-12 起教授不用回信：主旨只提示「資料有誤請回信」。
    """
    return (f"[口試附件4 {ref_code(request)}] {U.term_year_label(request.term)} "
            "學位口試一覽表（全學期），資料有誤請回信")


def confirm_code(request: ConfirmationRequest, version: str) -> str:
    """信末的「確認代碼」：請求代碼＋附件4 版本，系統用來比對教授回覆的是哪一版。"""
    return f"{ref_code(request)}-{version}"


def _reply_instructions(ref: str, reply_by=None) -> str:
    """2026-10-12 起：資料僅供參考、不需回覆；有誤才直接回信（reply_by 不再使用，保留參數相容）。"""
    return ("以上資料僅供您參考核對，不需回覆。\n"
            f"如有需要修改之處，請直接回覆本信並寫明要修改的地方（請保留主旨中的代碼 {ref}）。\n")


def _full_list_note(attached: bool) -> str:
    where = "附檔" if attached else "全學期附件4"
    return (f"{where}是本學期所有已排定學位口試的一覽表（每位指導教授收到的是同一份），"
            "下列是您指導學生的資料：\n")


def _code_line(request: ConfirmationRequest, version: str | None) -> str:
    if not version:
        return ""
    return f"確認代碼：{confirm_code(request, version)}（系統比對附件4 版本用，回信時請保留本信引用）\n"


def draft(
    request: ConfirmationRequest, master: Master, *, to: str | None, deadline: _dt.date | None = None,
    attachment: Path | None = None, round_no: int = 0,
) -> dict:
    """附件4 寄送信（教授不用回信；deadline 不再寫進信裡，保留參數相容）。"""
    ref = ref_code(request)
    version = content_version(master, request)
    body = (
        f"{request.advisor} 老師您好，\n\n"
        f"附上 {U.term_year_label(request.term)} 學位口試一覽表（附件4）。"
        f"{_full_list_note(attachment is not None)}"
        f"{rows_text(master, request)}\n\n"
        f"{_reply_instructions(ref)}\n"
        f"{_code_line(request, version)}"
        f"— {C.OFFICE_SIGNATURE}"
    )
    tracking = {"ref": ref, "request_key": list(request.request_key), "version": version, "round": round_no}
    return {
        "type": TYPE_ATTACHMENT4,
        "to": to,
        "cc": None,
        "subject": subject_for(request),
        "body": body,
        "attachments": [str(attachment)] if attachment else [],
        "status": STATUS_READY if to else STATUS_NO_RECIPIENT,
        "tracking": tracking,
        "idempotency_key": compute_idempotency_key(TYPE_ATTACHMENT4, tracking),
        "metadata": {"advisor": request.advisor},
    }


# ── 催辦（逾期未回）：2026-10-12 起不再排程（教授不用回信），保留供舊資料相容 ──

def reminder_content(
    request: ConfirmationRequest, *, rows: str, today: _dt.date, reply_by: _dt.date | None,
    sent_date: _dt.date | None = None, version: str | None = None, attached: bool = False,
) -> tuple[str, str]:
    """催辦信的 (主旨, 內文)：與原附件4 確認信同主旨、同代碼、同回覆方式、同署名。

    原訂期限已過時明講「已逾期」並給新的回覆期限（reply_by），不再寫過期的日期。
    """
    ref = ref_code(request)
    original = request.deadline.computed_date if request.deadline else None
    ordinal = request.reminder_count + 1
    sent_text = f"系辦已於 {sent_date.isoformat()} 寄出" if sent_date else "系辦先前已寄出"
    if original and original < today:
        due = f"原訂回覆期限 {original.isoformat()} 已逾期，"
    elif original:
        due = f"原訂回覆期限為 {original.isoformat()}，"
    else:
        due = ""
    reply_by_text = reply_by.isoformat() if reply_by else None
    body = (
        f"【第 {ordinal} 次提醒】\n\n"
        f"{request.advisor} 老師您好，\n\n"
        f"{sent_text} {U.term_year_label(request.term)} 學位口試一覽表（附件4）請您核對您指導學生的資料，"
        f"目前尚未收到您的回覆。{due}敬請儘速回覆。\n\n"
        f"{'附檔為目前最新的全學期附件4（每位指導教授收到同一份）；' if attached else ''}"
        "請核對您指導學生的資料如下：\n"
        f"{rows}\n\n"
        f"{_reply_instructions(ref, reply_by_text)}\n"
        f"{_code_line(request, version)}"
        f"— {C.OFFICE_SIGNATURE}"
    )
    return subject_for(request), body


# ── 全學期附件4（每位教授收到同一份）──

def document_master(master: Master, term: str, included_keys) -> Master:
    """這學期要印進附件4 的案件：生效、且通過寄送關卡（專員已放行、未被截止政策擋下）。"""
    keys = set(included_keys)
    return Master(exams=[e for e in master.active(term) if e.案件鍵 in keys],
                  committees=master.committees)


def document_version(master: Master) -> str:
    """全學期附件4 的內容版本（整份文件每一格）；只用來判斷檔案要不要重產，不用於確認比對。"""
    keys = ("學期", "指導教授", "日期", "方式", "備註", C.PROGRAM_DOCTOR, C.PROGRAM_MASTER, C.PROGRAM_INSERV)
    payload = json.dumps([[str(r.get(k, "")) for k in keys]
                          for term_rows in attachment4.group(master.exams).values() for r in term_rows],
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]


def load_documents(state_dir: str | Path) -> dict[str, dict]:
    path = Path(state_dir) / DOCUMENTS_FILE
    if not path.is_file():
        return {}
    return U.read_json_state(path).get("terms", {})


@U.locked_state()
def record_document(state_dir: str | Path, term: str, *, path: Path, version: str,
                    case_keys, generated_at: str | None = None) -> None:
    """記下這學期最新產出的全學期附件4（催辦信附的就是這一份）。"""
    state_dir = Path(state_dir)
    data = load_documents(state_dir)
    data[str(term)] = {"path": str(Path(path).resolve()), "version": version,
                       "case_keys": sorted(case_keys),
                       "generated_at": generated_at or clock.now().isoformat()}
    U.atomic_write_json({"terms": data}, state_dir / DOCUMENTS_FILE)


def find_attachment(state_dir: str | Path, request: ConfirmationRequest) -> Path | None:
    """目前最新的全學期附件4 檔；檔案不存在或沒有收錄這位教授的全部案件時回傳 None。"""
    record = load_documents(state_dir).get(str(request.term))
    if not record:
        return None
    path = Path(record.get("path", ""))
    if not path.is_file() or not set(request.case_keys) <= set(record.get("case_keys", ())):
        return None
    return path


def reminder_draft(
    request: ConfirmationRequest, master: Master, state_dir: str | Path, *, to: str | None,
    today: _dt.date, reply_by: _dt.date | None, tracking: dict,
):
    """逾期未回的附件4 催辦信（NotificationDraft）。

    - 主旨與原信相同（含代碼），In-Reply-To／References 指向原信，教授回覆催辦信也對得回本輪。
    - 附上目前最新的全學期附件4；只在這位教授自己的列仍是寄出時的版本、且最新檔案有收錄
      他全部案件時才附，避免把與信中內容不一致的檔案寄出。
    - tracking 沿用排程器的（決定去重鍵與提醒次數對帳），不在這裡改。
    """
    from .drafts import TYPE_CONFIRMATION, NotificationDraft
    record = load_mail_state(state_dir).get(ref_code(request), {})
    current = content_version(master, request)
    same_version = bool(record) and record.get("version") == current
    attachment = find_attachment(state_dir, request) if same_version else None
    sent_date = request.sent_date
    if sent_date is None:
        try:
            sent_date = _dt.date.fromisoformat(record.get("sent_date", ""))
        except (TypeError, ValueError):
            sent_date = None
    subject, body = reminder_content(request, rows=rows_text(master, request), today=today,
                                     reply_by=reply_by, sent_date=sent_date, version=current,
                                     attached=attachment is not None)
    thread = sorted(thread_ids(record), key=lambda mid: mid != record.get("message_id")) if same_version else []
    return NotificationDraft(
        type=TYPE_CONFIRMATION, to=to, cc=None, subject=subject, body=body,
        status=STATUS_READY if to else STATUS_NO_RECIPIENT, tracking=dict(tracking),
        metadata={"advisor": request.advisor, "ref": ref_code(request), "version": current,
                  "reply_by": reply_by.isoformat() if reply_by else None,
                  "generated_at": clock.now().strftime("%Y-%m-%d %H:%M:%S")},
        attachments=(str(attachment),) if attachment else (),
        in_reply_to=record.get("message_id", "") if same_version else "", references=tuple(thread),
    )


@U.locked_state()
def record_reminder(state_dir: str | Path, request: ConfirmationRequest, *, message_id: str,
                    version: str | None, reply_by: str | None, sent_at: str | None = None) -> bool:
    """催辦信寄出後記在本輪寄送紀錄：回覆催辦信也算回覆本輪，回覆期限延到催辦信寫的日期。

    只記在版本相同的紀錄上（重寄新版時 record_sent 會整筆換掉，舊催辦一併失效）。
    """
    state_dir = Path(state_dir)
    data = load_mail_state(state_dir)
    record = data.get(ref_code(request))
    if not record or not message_id or (version and record.get("version") != version):
        return False
    reminders = record.setdefault("reminders", [])
    if any(r.get("message_id") == message_id for r in reminders):
        return False
    reminders.append({"message_id": message_id, "version": record.get("version"), "reply_by": reply_by,
                      "sent_at": sent_at or clock.now().isoformat()})
    _save_mail_state(state_dir, data)
    return True


def thread_ids(record: dict) -> set[str]:
    """本輪寄出的信（原信＋同版本的催辦信）的 Message-ID；回覆其中任何一封都算回覆本輪。"""
    ids = {record["message_id"]} if record.get("message_id") else set()
    ids |= {r["message_id"] for r in record.get("reminders", [])
            if r.get("message_id") and r.get("version") == record.get("version")}
    return ids


def reply_window_end(record: dict, request: ConfirmationRequest) -> _dt.date | None:
    """回覆期限：原訂期限與催辦信寫的新期限取較晚者。"""
    dates = [request.deadline.computed_date] if request.deadline else []
    for r in record.get("reminders", []):
        if r.get("version") == record.get("version") and r.get("reply_by"):
            try:
                dates.append(_dt.date.fromisoformat(r["reply_by"]))
            except ValueError:
                pass
    return max(dates) if dates else None


def write_term_document(master: Master, term: str, *, template: Path, output_dir: Path) -> Path:
    """全學期附件4（master 已是 document_master() 篩好的案件）。template 需是 .docx。

    沿用官方範本的產生器：一張表、依學期分段、同一指導教授的列相鄰並合併、學位依班別分欄。
    每次重產覆寫同一個檔名，所以檔案永遠是最新版本。
    """
    # 檔名用學期名稱（「115學年度上學期」），教授收到的附件不出現內部學期代碼
    safe = re.sub(r"[^\w\-]", "_", U.term_year_label(term) or str(term))
    out = Path(output_dir) / f"附件4_{safe}_學位口試一覽表.docx"
    attachment4.generate(master, out, template=template, term=term)
    return out


# ── 收信 ──

@dataclass(frozen=True)
class AttachmentMeta:
    """附件 metadata（不含內容，只用來判斷回信有沒有附加檔案）。"""
    filename: str
    content_type: str
    size: int
    sha256: str


@dataclass(frozen=True)
class InboundMail:
    message_id: str
    from_addr: str
    subject: str
    received_at: str
    body: str                                    # 原始全文（含引用），供稽核與版本核對
    reply_text: str = ""                         # 去除引用後的回覆本文，**只有它能送進模型**
    attachments: tuple[AttachmentMeta, ...] = ()  # 真正附件（排除內嵌簽名圖）
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    received_at_trusted: bool = False

    @property
    def has_real_attachments(self) -> bool:
        """有真正附件（教授可能附改過的 Word）→ 不自動放行。"""
        return len(self.attachments) > 0


class Classifier(Protocol):
    """LLM 判讀回信意圖。

    新版回傳 ClassificationResult；舊版回傳 tuple[str, str] 仍支援（見 _call_classifier）。
    """
    def __call__(self, mail: InboundMail, request: ConfirmationRequest) -> ClassificationResult | tuple[str, str]: ...


@dataclass
class InboxItem:
    message_id: str
    from_addr: str
    subject: str
    received_at: str
    body: str
    ref: str | None
    token: str | None  # application.request_token；對不到請求時為 None
    reply_text: str = ""                         # 去引用後的回覆本文
    has_real_attachments: bool = False            # 有真正附件（非內嵌簽名圖）
    in_reply_to: str = ""
    references: tuple[str, ...] = ()
    received_at_trusted: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    intent: str | None = None
    intent_detail: str = ""
    # 完整判讀結果（含 model / prompt_version / raw_output），供稽核追查
    classification: dict | None = None
    content_version: str | None = None
    sent_round: int | None = None
    classifier_error: str | None = None          # 分類器錯誤訊息（逐封捕捉）
    classifier_note: str = ""                    # 沒有 AI 判讀的原因（未設定模型、AI 已停止、沒有文字）
    auto_note: str = ""                          # AI 判為確認但沒有自動確認的原因（開關、白名單、注入…）
    status: str = INBOX_PENDING
    handled_by: str = ""
    handled_at: str = ""

    @property
    def checks_passed(self) -> bool:
        return bool(self.checks) and all(self.checks.values())


def load_inbox(state_dir: str | Path) -> list[InboxItem]:
    path = Path(state_dir) / INBOX_FILE
    if not path.is_file():
        return []
    return [InboxItem(**item) for item in U.read_json_state(path).get("items", [])]


def save_inbox(state_dir: str | Path, items: list[InboxItem]) -> None:
    from dataclasses import asdict
    U.atomic_write_json({"items": [asdict(i) for i in items]}, Path(state_dir) / INBOX_FILE)


def _addr(value: str) -> str:
    match = re.search(r"[^<>\s,;]+@[^<>\s,;]+", value or "")
    return match.group(0).lower() if match else ""


def match_request(
    mail: InboundMail, requests: list[ConfirmationRequest], state_dir: str | Path,
) -> tuple[ConfirmationRequest | None, str | None, list[str]]:
    """以主旨代碼找請求；沒有代碼時，寄件者恰好是一筆等待中請求的收件人才對上。"""
    by_ref = {ref_code(r): r for r in requests}
    found = REF_RE.search(mail.subject or "") or REF_RE.search(mail.body or "")
    if found:
        ref = found.group(0)
        return by_ref.get(ref), ref, ([] if ref in by_ref else ["代碼已不在目前的確認請求中（可能已重寄新版本）"])
    sent = load_mail_state(state_dir)
    sender = _addr(mail.from_addr)
    candidates = [r for r in requests if r.response == PENDING and r.sent_date
                  and _addr(sent.get(ref_code(r), {}).get("to", "")) == sender]
    if len(candidates) == 1:
        return candidates[0], ref_code(candidates[0]), ["主旨沒有代碼，依寄件者對應"]
    return None, None, []


def check(mail: InboundMail, request: ConfirmationRequest, master: Master,
          state_dir: str | Path) -> dict[str, bool]:
    record = load_mail_state(state_dir).get(ref_code(request), {})
    body_version = _VERSION_RE.search(mail.body or "")
    current = content_version(master, request)
    thread = bool(record) and mail.in_reply_to.strip() in thread_ids(record)
    timing = False
    try:
        received = _dt.datetime.fromisoformat(mail.received_at)
        sent_at = _dt.datetime.fromisoformat(record.get("sent_at", ""))
        zone = _dt.timezone(_dt.timedelta(hours=8))
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=zone)
        # 2026-10-12 起教授不用回信、沒有回覆期限：只核對可信收件時間在本輪寄出之後
        timing = bool(mail.received_at_trusted and received.tzinfo and sent_at <= received)
    except (ValueError, TypeError):
        pass
    return {
        "寄件者為收件教授": bool(record) and _addr(mail.from_addr) == _addr(record.get("to", "")),
        "附件4 為目前版本": bool(record) and record.get("version") == current
        and (body_version.group(1) == current if body_version else thread),
        "回覆本輪寄送信件": thread,
        "可信收件時間在寄出之後": timing,
        "已寄出且尚未登錄回覆": request.sent_date is not None and request.response == PENDING,
    }


def _call_classifier(
    classifier: Classifier, mail: InboundMail, request: ConfirmationRequest,
) -> ClassificationResult:
    """呼叫 classifier 並統一回傳 ClassificationResult。

    必須提供完整且可核對逐字依據的 ClassificationResult。
    """
    result = classifier(mail, request)
    if isinstance(result, ClassificationResult):
        if not result.evidence.strip() or result.evidence not in mail.reply_text:
            raise ClassifierError("判讀依據不是原回覆的逐字引用，轉人工處理")
        if type(result.has_reservation) is not bool:
            raise ClassifierError("保留意見欄位型別錯誤")
        if any(not m.quote or m.quote not in mail.reply_text for m in result.modifications):
            raise ClassifierError("修改依據不是原回覆的逐字引用")
        return result
    raise ClassifierError("分類器未提供完整判讀欄位，請人工判讀")


@U.locked_state()
def ingest_replies(
    mails: list[InboundMail], *, master: Master, requests: list[ConfirmationRequest],
    state_dir: str | Path, token_of: Callable[[ConfirmationRequest], str],
    classifier: Classifier | None = None, no_classifier_reason: str = "",
) -> tuple[list[InboxItem], list[InboxItem]]:
    """把新信存進收件匣。回傳 (新存入的, 可自動記為已確認的)。

    自動確認只在 classifier 判為 INTENT_CONFIRM 且核對全部通過時成立；呼叫端負責
    實際寫入確認狀態並把項目標為 applied。對不到任何請求的信不存。

    逐封捕捉 timeout、API 錯誤、schema 錯誤（LLM前置研究第四節第 6 點）：
    錯誤記在 InboxItem.classifier_error，該封轉人工，繼續處理下一封。
    """
    state_dir = Path(state_dir)
    inbox = load_inbox(state_dir)
    seen = {i.message_id for i in inbox}
    added, auto = [], []
    for mail in mails:
        if not mail.message_id or mail.message_id in seen:
            continue
        request, ref, notes = match_request(mail, requests, state_dir)
        if ref is None:
            continue
        item = InboxItem(
            message_id=mail.message_id, from_addr=mail.from_addr, subject=mail.subject,
            received_at=mail.received_at, body=mail.body, ref=ref,
            token=token_of(request) if request else None,
            reply_text=mail.reply_text,
            has_real_attachments=mail.has_real_attachments,
            in_reply_to=mail.in_reply_to, references=mail.references,
            received_at_trusted=mail.received_at_trusted,
            notes=notes,
        )
        if request is not None:
            item.checks = check(mail, request, master, state_dir)
            record = load_mail_state(state_dir).get(ref_code(request), {})
            item.content_version = record.get("version")
            item.sent_round = record.get("rounds")
            # reply_text 為空（只附檔不寫字）→ 直接轉人工，不呼叫模型
            if not item.reply_text.strip():
                item.classifier_note = "回信沒有可判讀的文字（可能只有附件），交專員判讀，因此未自動確認"
            elif classifier is None:
                item.classifier_note = (no_classifier_reason
                                        or "未設定模型：回信判讀交專員，因此未自動確認")
            if classifier is not None and item.reply_text.strip():
                try:
                    cr = _call_classifier(classifier, mail, request)
                    item.intent = cr.intent
                    item.intent_detail = cr.evidence
                    item.classification = cr.to_dict()
                    if (cr.intent == INTENT_CONFIRM and item.checks_passed
                            and not cr.has_reservation
                            and not cr.modifications and not cr.questions
                            and not notes and not item.has_real_attachments):
                        auto.append(item)
                except (ClassifierError, Exception) as exc:
                    # 逐封捕捉，不中斷整批；該封轉人工
                    item.classifier_error = str(exc)
                    logger.warning("分類器錯誤（%s）：%s", mail.message_id, exc)
        inbox.append(item)
        seen.add(mail.message_id)
        added.append(item)
    save_inbox(state_dir, inbox)
    log_path = state_dir / "audit_log.json"
    for item in added:
        audit_log.record(log_path, "confirmation_inbox", "received",
                                  tracking_key=(item.ref or "",),
                                  detail={"from": _addr(item.from_addr), "checks": item.checks,
                                          "intent": item.intent,
                                          "classifier_error": item.classifier_error})
    return added, auto


@U.locked_state()
def mark_handled(state_dir: str | Path, message_id: str, *, status: str, actor: str,
                 now: _dt.datetime | None = None) -> InboxItem:
    if status not in (INBOX_APPLIED, INBOX_IGNORED):
        raise ValueError(f"未知的處理結果：{status}")
    inbox = load_inbox(state_dir)
    for item in inbox:
        if item.message_id == message_id:
            item.status, item.handled_by = status, actor
            item.handled_at = (now or clock.now()).strftime("%Y-%m-%d %H:%M:%S")
            save_inbox(state_dir, inbox)
            return item
    raise ValueError("找不到這封回信（可能已被處理）")


def response_for_intent(intent: str) -> str | None:
    return {INTENT_CONFIRM: CONFIRMED, INTENT_MODIFY: MODIFIED}.get(intent)


# ── 步驟 4：自動放行決策 ──────────────────────────────────────

# 拒絕原因常數
REJECT_DISABLED = "auto_confirm_disabled"          # Feature flag 關閉
REJECT_NOT_WHITELISTED = "advisor_not_whitelisted"  # 教授不在白名單
REJECT_VERSION_CHANGED = "version_changed"          # Compare-and-Set 失敗：附件4 已變動
REJECT_SHADOW = "shadow_mode"                       # Shadow mode：只建議不放行
REJECT_SUSPICIOUS = "suspected_injection"           # 信中疑似夾帶操縱 AI 的指令
REJECT_LABELS = {
    REJECT_DISABLED: "自動確認未開啟（期限設定頁），交專員判讀",
    REJECT_NOT_WHITELISTED: "這位教授不在自動確認白名單，交專員判讀",
    REJECT_VERSION_CHANGED: "附件4 已改版，這封回覆的是舊版，不能確認",
    REJECT_SHADOW: "影子模式：AI 建議可自動確認，但只建議不放行，交專員判讀",
    REJECT_SUSPICIOUS: "疑似夾帶操縱 AI 的指令，不自動確認",
    "request_not_found": "對不到目前的確認請求",
    "reply_evidence_changed": "核對結果已變動（版本或寄送輪次不同），交專員判讀",
}


@dataclass(frozen=True)
class AutoConfirmDecision:
    """一封候選信的自動放行決策結果。"""
    item: InboxItem
    approved: bool                          # True＝實際放行，False＝不放行（含 shadow）
    reason: str = ""                        # 拒絕原因（approved=False 時）
    shadow: bool = False                    # True＝本來可以放行但因 shadow mode 而不放行

    @property
    def would_approve(self) -> bool:
        """在非 shadow mode 下是否會放行（用於 shadow mode 報表）。"""
        return self.approved or self.shadow


def mail_from_item(item):
    return InboundMail(message_id=item.message_id, from_addr=item.from_addr, subject=item.subject,
        received_at=item.received_at, body=item.body, reply_text=item.reply_text,
        in_reply_to=item.in_reply_to, references=tuple(item.references),
        received_at_trusted=item.received_at_trusted)


def evaluate_auto_confirms(
    candidates: list[InboxItem],
    *,
    master: Master,
    requests: list[ConfirmationRequest],
    state_dir: str | Path,
    settings: "OfficeSettings",
) -> list[AutoConfirmDecision]:
    """評估候選信是否可自動放行。

    candidates 是 ingest_replies() 回傳的 auto list（已通過 classifier + checks）。
    本函式再做三層過濾（另外疑似提示注入的信一律不放行）：
      1. Feature flag：auto_confirm_enabled 必須為 True
      2. 白名單：教授姓名必須在 auto_confirm_whitelist 中
      3. Compare-and-Set：附件4 版本必須與寄出時一致（確認時刻再驗一次）

    通過所有過濾後，若 shadow mode 開啟，標記為 shadow（建議放行但不實際放行）。
    """
    from .settings import OfficeSettings  # 避免循環 import

    state_dir = Path(state_dir)
    by_ref = {ref_code(r): r for r in requests}
    whitelist = set(settings.auto_confirm_whitelist)
    decisions: list[AutoConfirmDecision] = []

    for item in candidates:
        # 1. Feature flag
        if not settings.auto_confirm_enabled:
            decisions.append(AutoConfirmDecision(item=item, approved=False, reason=REJECT_DISABLED))
            continue

        # 分類器可能被信中的指令誤導：疑似注入一律不自動放行（含 HTML 隱藏文字）
        from .mailbox_agent import looks_like_injection
        if looks_like_injection(item.reply_text) or looks_like_injection(item.body):
            decisions.append(AutoConfirmDecision(item=item, approved=False, reason=REJECT_SUSPICIOUS))
            continue

        # 2. 白名單
        request = by_ref.get(item.ref)
        if request is None:
            decisions.append(AutoConfirmDecision(item=item, approved=False, reason="request_not_found"))
            continue
        record = load_mail_state(state_dir).get(item.ref, {})
        if not record or record.get("version") != content_version(master, request):
            decisions.append(AutoConfirmDecision(item=item, approved=False, reason=REJECT_VERSION_CHANGED))
            continue
        fresh = check(mail_from_item(item), request, master, state_dir)
        if (not fresh or not all(fresh.values()) or item.content_version != content_version(master, request)
                or item.sent_round != record.get("rounds")):
            decisions.append(AutoConfirmDecision(item=item, approved=False, reason="reply_evidence_changed"))
            continue
        if request.advisor not in whitelist:
            decisions.append(AutoConfirmDecision(item=item, approved=False, reason=REJECT_NOT_WHITELISTED))
            continue

        # 3. Compare-and-Set：在放行當下再驗一次版本，防止 ingest 與放行之間附件4 被修改
        record = load_mail_state(state_dir).get(item.ref, {})
        current_ver = content_version(master, request)
        if not record or record.get("version") != current_ver:
            decisions.append(AutoConfirmDecision(item=item, approved=False, reason=REJECT_VERSION_CHANGED))
            continue

        # 全部通過 → 看 shadow mode
        if settings.auto_confirm_shadow:
            decisions.append(AutoConfirmDecision(item=item, approved=False, reason=REJECT_SHADOW, shadow=True))
        else:
            decisions.append(AutoConfirmDecision(item=item, approved=True))

    return decisions


@U.locked_state()
def execute_auto_confirms(
    decisions: list[AutoConfirmDecision],
    *,
    requests: list[ConfirmationRequest],
    state_dir: str | Path,
    date: _dt.date | None = None,
    master: Master | None = None,
) -> tuple[list[ConfirmationRequest], list[AutoConfirmDecision]]:
    """執行放行決策：approved=True 的項目記為已確認。

    回傳 (更新後的 requests, 實際放行的 decisions)。
    所有決策（含 shadow、拒絕）都寫入稽核日誌。
    """
    from .engines.confirmation import record_response

    state_dir = Path(state_dir)
    date = date or clock.today()
    if master is not None:
        from .settings import load as load_settings
        decisions = [evaluate_auto_confirms([d.item], master=master, requests=requests,
            state_dir=state_dir, settings=load_settings(state_dir))[0] if d.approved else d for d in decisions]
    by_ref = {ref_code(r): r for r in requests}
    applied: list[AutoConfirmDecision] = []

    log_path = state_dir / "audit_log.json"

    for decision in decisions:
        # 寫入稽核日誌
        audit_log.record(
            log_path, "auto_confirm", "evaluated",
            tracking_key=(decision.item.ref or "",),
            detail={
                "message_id": decision.item.message_id,
                "approved": decision.approved,
                "shadow": decision.shadow,
                "reason": decision.reason,
                "advisor": by_ref.get(decision.item.ref, ConfirmationRequest(
                    group_key=(), term="", advisor="", degree="", date_text="",
                    student_names=(), case_keys=(),
                )).advisor,
            },
        )

        if not decision.approved:
            continue

        request = by_ref.get(decision.item.ref)
        if request is None:
            continue

        # 記為已確認
        updated = record_response(request, CONFIRMED, date=date,
            notes="LLM 自動放行：" + decision.item.message_id)
        by_ref[decision.item.ref] = updated
        from .engines.state import load_from_dir, save_to_dir
        persisted = load_from_dir(state_dir)
        persisted.confirmation = [by_ref[ref_code(r)] for r in requests]
        save_to_dir(persisted, state_dir)

        # 確認狀態落盤後才標記收件匣，重啟可補回判讀結果
        mark_handled(state_dir, decision.item.message_id, status=INBOX_APPLIED,
                     actor="auto_confirm")
        applied.append(decision)

    # 重建 requests list，保持原順序
    ref_to_req = {ref_code(r): r for r in requests}
    ref_to_req.update(by_ref)
    updated_requests = [ref_to_req[ref_code(r)] for r in requests]

    return updated_requests, applied