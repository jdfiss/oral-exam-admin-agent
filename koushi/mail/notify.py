"""缺件通知組信模組 — 讀主檔、找缺件、按學生組信、寄出。

用法：
    python -m koushi.mail.notify                    # 預覽草稿（不寄）
    python -m koushi.mail.notify --send             # 實際寄出
    python -m koushi.mail.notify --send --to me     # 全部寄到自己信箱（測試用）

收件人由 recipients 解析。查無 email 的草稿標為 no_recipient，保留在
清單中供系辦人工處理，不會寄出，也不會臆造收件地址。
"""

import os
from collections import defaultdict
from pathlib import Path
from typing import Callable

from .. import master, validation, config as C, send_log
from ..drafts import TYPE_DEFICIENCY, STATUS_READY, compute_idempotency_key
from ..scheduler import ScheduledReminder
from ..validation import OWNER_STUDENT, OWNER_OFFICE, OWNER_POST_EXAM, OWNER_NONE
from . import recipients

# ── 信件類型 ──────────────────────────────────────────
# 跟 koushi/drafts.py 共用同一個型別字串，send_log 去重才不會因為兩邊
# 型別名稱不同而認錯。這個模組寄信的方式（字串拼接、含阻擋/提醒分節）
# 跟 drafts.py 的模板引擎不一樣，暫不合併，但追蹤鍵格式要一致。
TYPE_STUDENT_DEFICIENCY = TYPE_DEFICIENCY

# ── 草稿狀態 ──────────────────────────────────────────
STATUS_READY = "ready"
STATUS_NO_RECIPIENT = "no_recipient"


def default_send_log_path() -> Path:
    """所有正式寄件入口共用的跨批次紀錄，亦供系辦工作台讀取。"""
    return C.STATE_DIR / "send_log.json"


def load_issues(master_path: str | Path | None = None):
    path = Path(master_path) if master_path else C.MASTER
    m = master.load(path)
    if m is None:
        raise FileNotFoundError(f"找不到主檔：{path}")
    issues = validation.check(m)
    return m, issues


def issue_key(issue: dict) -> str:
    return f"{issue.get('問題代碼') or ''}|{issue.get('欄位') or ''}"


def group_by_student(issues: list[dict]) -> dict[str, list[dict]]:
    groups = defaultdict(list)
    for issue in issues:
        groups[issue["案件鍵"]].append(issue)
    return dict(groups)


def compose_email(
    student_name: str,
    case_key: str,
    issues: list[dict],
    to: str | None = None,
    *,
    revision: int | None = None,
    deadline=None,
    has_draft: bool = False,
) -> dict:
    """組一封學生補件通知。

    deadline 為全系統補件截止日（系辦設定）；有值時寫進信中，因為補件截止是
    硬截止，學生必須知道日期（review-2026-09-23 P1-7）。信中不顯示內部問題代碼。

    to 為 None 表示通訊錄查無此學生 email，草稿仍會產生但標為
    no_recipient，交由系辦人工處理。
    """
    blocking = [i for i in issues if i.get("阻擋產出") == "是"]
    warnings = [i for i in issues if i.get("阻擋產出") != "是"]

    def plain(text) -> str:  # 系統內部用語「介面填寫」換成學生看得懂的說法
        return (str(text or "").replace("在介面填寫", "在系統填寫").replace("介面填寫", "系統補充資料")
                .replace("尚未收到本系統的補充資料填寫", "尚未在系統送出補充資料"))

    lines = [
        f"{student_name} 同學您好，",
        "",
        f"您的學位口試申請（案件編號：{case_key}）經系統檢核後，發現以下資料需要補正：",
        "",
    ]

    if blocking:
        lines.append("【必須補正】以下問題會阻擋口試流程，請優先處理（登入後首頁「必須補正的項目」就是這些）：")
        for i, issue in enumerate(blocking, 1):
            lines.append(f"  {i}. {plain(issue['說明'])}")
            lines.append(f"     相關欄位：{plain(issue['欄位'])}")
        lines.append("")

    if warnings:
        lines.append("【提醒項目】以下問題不阻擋口試，送出整份表單時一併補上即可：")
        for i, issue in enumerate(warnings, 1):
            lines.append(f"  {i}. {plain(issue['說明'])}")
            lines.append(f"     相關欄位：{plain(issue['欄位'])}")
        lines.append("")

    if has_draft and any(i.get("問題代碼") == "MISSING_SUBMISSION" for i in issues):
        lines.append("您在系統中有暫存但尚未送出的資料，登入後可以接著填寫，完成後請記得按「送出」。")
        lines.append("")
    if deadline:
        lines.append(f"補件截止日：{deadline}（當天以後系統將鎖定，不再接受修改）")
        lines.append("")
    url = os.environ.get("KOUSHI_PUBLIC_URL", "").strip().rstrip("/")
    lines.extend([
        (f"請登入口試資料填寫系統（{url}/ ，以學號登入），修改後按「送出」即完成補件。" if url
         else "請登入口試資料填寫系統（以學號登入），修改後按「送出」即完成補件。"),
        "如有疑問，請聯繫系辦。",
        "",
        f"— {C.OFFICE_SIGNATURE}",
    ])

    count = (f"{len(blocking)} 項必須補正" + (f"、{len(warnings)} 項提醒" if warnings else "")) if blocking         else f"{len(warnings)} 項提醒"
    subject = f"[口試資料補正通知] {student_name}（{case_key}）— {count}"

    # tracking 只放會影響「這封信有沒有寄過」判斷的欄位；收件人、內文
    # 格式變動不該讓同一個缺件狀態被當成新通知重寄一次。
    tracking = {
        "case_key": case_key,
        "issue_codes": sorted({str(i.get("問題代碼") or "") for i in issues}),
        # 問題代碼＋欄位：同一個問題不重複寄（2026-10-01 Q7），同代碼的不同委員仍算新問題
        "issue_keys": sorted({issue_key(i) for i in issues}),
    }
    if revision is not None:
        tracking["submission_version"] = revision

    return {
        "type": TYPE_STUDENT_DEFICIENCY,
        "to": to,
        "cc": None,
        "subject": subject,
        "body": "\n".join(lines),
        "case_key": case_key,
        "status": STATUS_READY if to else STATUS_NO_RECIPIENT,
        "tracking": tracking,
        "idempotency_key": compute_idempotency_key(TYPE_STUDENT_DEFICIENCY, tracking),
        "metadata": {
            "issue_codes": tracking["issue_codes"],
            "student_name": student_name,
            "blocking_count": len(blocking),
            "warning_count": len(warnings),
            "owner": issues[0].get("責任", "") if issues else "",
        },
    }


def drafts_for_student_issues(
    issues: list[dict],
    *,
    contacts: dict,
    revisions: dict[str, int] | None = None,
    deadline=None,
    draft_case_keys: set[str] | None = None,
) -> list[dict]:
    """將本次建檔的學生責任問題組成草稿；收件人由已通過的當前表單提供。

    draft_case_keys：有暫存但尚未送出的案件；未填寫的信中提醒可以接續暫存（2026-10-01 Q2-1）。
    """
    student_issues = [
        issue for issue in issues
        if issue.get("責任") == OWNER_STUDENT and issue.get("案件鍵")
    ]
    drafts = []
    for case_key, case_issues in group_by_student(student_issues).items():
        drafts.append(compose_email(
            str(case_issues[0].get("姓名") or ""), case_key, case_issues,
            to=recipients.resolve_student(case_key, contacts),
            revision=(revisions or {}).get(case_key),
            deadline=deadline.get(case_key) if isinstance(deadline, dict) else deadline,
            has_draft=case_key in (draft_case_keys or set()),
        ))
    return drafts


def build_drafts(
    master_path: str | Path | None = None,
    owner: str | None = None,
    contacts: dict | None = None,
) -> list[dict]:
    m, issues = load_issues(master_path)
    if not issues:
        print("✓ 主檔無缺件，不需要寄通知。")
        return []

    skip = {OWNER_NONE, OWNER_POST_EXAM}
    if owner:
        filtered = [i for i in issues if i.get("責任") == owner]
    else:
        filtered = [i for i in issues if i.get("責任") not in skip]

    if not filtered:
        print(f"✓ 篩選後無缺件（owner={owner or '全部'}）。")
        return []

    if contacts is None:
        contacts = recipients.load_contacts()

    groups = group_by_student(filtered)
    drafts = []
    for case_key, student_issues in groups.items():
        name = student_issues[0]["姓名"]
        to = recipients.resolve_student(case_key, contacts)
        drafts.append(compose_email(name, case_key, student_issues, to=to))

    return drafts


def sendable(drafts: list[dict]) -> list[dict]:
    return [d for d in drafts if d.get("status") == STATUS_READY]


def unsendable(drafts: list[dict]) -> list[dict]:
    return [d for d in drafts if d.get("status") != STATUS_READY]


def preview_drafts(drafts: list[dict]):
    print(f"\n{'='*60}")
    print(f"共 {len(drafts)} 封草稿")
    print(f"{'='*60}")
    for i, d in enumerate(drafts, 1):
        meta = d.get("metadata", {})
        print(f"\n--- 草稿 {i}/{len(drafts)} ---")
        if d.get("status") == STATUS_NO_RECIPIENT:
            print(f"收件人：（通訊錄查無 email — 需系辦人工處理）")
        else:
            print(f"收件人：{d['to']}")
        print(f"主  旨：{d['subject']}")
        print(f"阻擋：{meta.get('blocking_count', 0)} 項 | "
              f"提醒：{meta.get('warning_count', 0)} 項")
        print(f"\n{d['body']}")
        print()

    blocked = unsendable(drafts)
    if blocked:
        print(f"{'='*60}")
        print(f"⚠ 有 {len(blocked)} 封查無收件人，不會寄出：")
        for d in blocked:
            print(f"    {d['case_key']}  {d.get('metadata', {}).get('student_name', '')}")
        print(f"{'='*60}")


def _accepts(transport, name: str) -> bool:
    """transport 是否收得下這個關鍵字參數（明列或 **kwargs）。舊的簡易 transport 不受影響。"""
    import inspect
    try:
        params = inspect.signature(transport).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def message_id_for(key):
    import hashlib
    return "<" + hashlib.sha256(key.encode("utf-8")).hexdigest() + "@koushi.local>"


def send_drafts(
    drafts: list[dict],
    override_to: str | None = None,
    *,
    log: send_log.SendEventLog | None = None,
    transport: Callable[..., bool | None] | None = None,
) -> list[dict]:
    """寄出草稿。回傳實際寄出的草稿清單。

    override_to 有值時（測試模式）連 no_recipient 的草稿也會寄到指定信箱，
    方便在介面層上線前驗證組信結果——這個路徑不經過 send_log，不會被
    去重擋下來，才能反覆測試。

    override_to 為 None（正式寄送）時，每封信都先經過 send_log 的
    draft → approved → sent 狀態鏈：已經成功寄過的追蹤鍵（同案件、同一批
    缺件代碼組合）會被 is_duplicate() 擋下，不會重複通知同一位學生。
    log 不傳就用全新的記憶體實例（不跨次執行去重）；要跨次執行去重，
    呼叫端要自己 load 既有記錄傳進來，寄完後也要自己 save（見
    `__main__` 區塊的用法）。
    """
    from .email_sender import send_email
    transport = transport or send_email

    targets = drafts if override_to else sendable(drafts)

    if override_to:
        sent = []
        for d in targets:
            if transport(to=override_to, subject=d["subject"], body=d["body"], cc=d.get("cc")) is not False:
                sent.append(d)
        return sent

    if log is None:
        log = send_log.SendEventLog()

    sent = []
    for d in targets:
        key = d["idempotency_key"]
        if log.is_duplicate(key):
            continue

        event = log.get(key)
        if event is None:
            log.record(idempotency_key=key, type=d["type"], to=d["to"],
                       subject=d["subject"], tracking=d.get("tracking", {}))
            event = log.get(key)

        if event.status == send_log.REJECTED:
            continue
        if event.status == send_log.FAILED:
            log.retry_after_failure(key)
        elif event.status == send_log.DRAFT:
            log.record_review(key, approved=True, reviewer="system-auto")
        # APPROVED（或剛核准完）：可以寄了。

        draft = d
        result = log.dispatch(key, lambda draft=draft: transport(
            to=draft["to"], subject=draft["subject"], body=draft["body"], cc=draft.get("cc"),
            # 只有帶附件的草稿才傳 attachments，既有不支援附件的 transport 不受影響。
            **({"attachments": draft["attachments"]} if draft.get("attachments") else {}),
            **({"message_id": message_id_for(key)} if _accepts(transport, "message_id") else {}),
        ))
        if result.status == send_log.SENT:
            sent.append(d)

    return sent


def send_scheduled(
    reminders: list[ScheduledReminder],
    *,
    log: send_log.SendEventLog | None = None,
    transport: Callable[..., bool | None] | None = None,
) -> list[ScheduledReminder]:
    """實際寄出 ReminderScheduler 排程出的催件草稿。

    走跟 send_drafts() 一樣的 send_log draft→approved→sent 狀態鏈去重
    （同一天、同一追蹤鍵不會重寄）；只處理 draft.status == STATUS_READY
    （真實收件人）的項目，STATUS_NO_RECIPIENT 直接跳過，不佔 idempotency_key。

    節流欄位（reminder_count／last_reminder_date）不在這裡回寫——呼叫端
    （`cli.py` 的 `remind --send`）拿到回傳的已寄出清單後，自己對照
    tracking_key 呼叫對應引擎的 mark_reminded() 並落盤；教授確認（confirmation）
    引擎沒有節流欄位，靠這裡的 send_log 去重就夠。
    """
    from .email_sender import send_email
    transport = transport or send_email
    if log is None:
        log = send_log.SendEventLog()

    sent: list[ScheduledReminder] = []
    for reminder in reminders:
        draft = reminder.draft
        if draft.status != STATUS_READY:
            continue
        key = draft.idempotency_key
        if log.is_duplicate(key):
            continue

        event = log.get(key)
        if event is None:
            log.record(idempotency_key=key, type=draft.type, to=draft.to,
                       subject=draft.subject, tracking=draft.tracking)
            event = log.get(key)

        if event.status == send_log.REJECTED:
            continue
        if event.status == send_log.FAILED:
            log.retry_after_failure(key)
        elif event.status == send_log.DRAFT:
            log.record_review(key, approved=True, reviewer="system-auto")

        extra = {}
        if draft.attachments:
            extra["attachments"] = list(draft.attachments)
        if _accepts(transport, "message_id"):
            extra["message_id"] = message_id_for(key)
        if draft.in_reply_to and _accepts(transport, "in_reply_to"):
            extra["in_reply_to"] = draft.in_reply_to
        if draft.references and _accepts(transport, "references"):
            extra["references"] = list(draft.references)
        result = log.dispatch(key, lambda d=draft, extra=extra: transport(
            to=d.to, subject=d.subject, body=d.body, cc=d.cc, **extra,
        ))
        if result.status == send_log.SENT:
            sent.append(reminder)

    return sent


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="缺件通知組信")
    parser.add_argument("--master", help="主檔路徑（預設用最新）")
    parser.add_argument("--send", action="store_true", help="實際寄出（預設只預覽）")
    parser.add_argument("--to", help="覆蓋收件人（測試用，例如 --to me 寄到自己）")
    parser.add_argument("--owner", choices=["student", "office"],
                        help="只顯示某責任的缺件（預設全部）")
    args = parser.parse_args()

    master_path = args.master or C.MASTER
    drafts = build_drafts(master_path, owner=args.owner)

    if not drafts:
        raise SystemExit(0)

    preview_drafts(drafts)

    if args.send:
        override = None
        if args.to == "me":
            from .email_sender import sender_address
            override = sender_address()
        elif args.to:
            override = args.to

        ready = drafts if override else sendable(drafts)
        if not ready:
            print("\n沒有可寄出的草稿（全部查無收件人）。")
            raise SystemExit(0)

        print(f"\n寄送中（收件人：{override or '各學生信箱'}）...")
        if override:
            sent = send_drafts(drafts, override_to=override)
        else:
            log_path = default_send_log_path()
            log = send_log.load(log_path)
            sent = send_drafts(drafts, log=log)
            send_log.save(log, log_path)
        print(f"\n✓ 共寄出 {len(sent)} 封")
        skipped = len(drafts) - len(sent)
        if skipped:
            print(f"  跳過 {skipped} 封（查無收件人，或該追蹤鍵先前已寄過，"
                  f"見 {default_send_log_path()}）")
    else:
        print("\n（預覽模式，加 --send 才會寄出）")
