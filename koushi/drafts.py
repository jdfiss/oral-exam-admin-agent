# -*- coding: utf-8 -*-
"""信件草稿產生器（DraftTemplateEngine）—— 依模板類型（補件信／確認信／
催繳信）把追蹤引擎的資料轉成待審閱的通知草稿。

只組草稿內容，不寄信、不查真實通訊錄、不判斷「現在該不該通知」——那是
各追蹤引擎（是否逾期）與排程器（何時該催）的責任。收件人一律由呼叫端
傳入；沒有的話草稿仍會產生但標為 no_recipient，交由人工處理，絕不臆造
地址。

每封草稿都附 tracking（案件鍵／追蹤鍵）與 idempotency_key：同一追蹤狀態
重複產生草稿會得到相同的 key，供之後的寄送記錄判斷是否為重複通知。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from . import config as C
from .engines.collection import CollectionItem
from .engines.confirmation import ConfirmationRequest
from .engines.deficiency import DeficiencyItem

TYPE_DEFICIENCY = "deficiency"
TYPE_CONFIRMATION = "confirmation"
TYPE_COLLECTION = "collection"

STATUS_READY = "ready"
STATUS_NO_RECIPIENT = "no_recipient"

# 測試 / 介面尚未上線前一律用這個網域佔位，絕不寄到真實地址。
TEST_RECIPIENT_DOMAIN = "example.invalid"


def placeholder_recipient(local_part: str) -> str:
    """組一個測試用佔位地址，保證落在 TEST_RECIPIENT_DOMAIN。"""
    safe = "".join(ch if ch.isalnum() else "-" for ch in local_part).strip("-") or "case"
    return f"{safe}@{TEST_RECIPIENT_DOMAIN}"


_DATE_FMT = "%Y-%m-%d"


def _date_text(d: _dt.date | None) -> str:
    return d.strftime(_DATE_FMT) if d else "（尚未設定，無截止日）"


def compute_idempotency_key(type_: str, tracking: dict) -> str:
    """同一情境類型＋追蹤狀態一定算出同一把 key，供 send_log 判斷是否重複通知。

    獨立成函式而不是只當 NotificationDraft 的 property，是因為
    `koushi/mail/notify.py` 現在也需要用同一套規則算 key——它組信的方式
    跟這裡的模板不一樣，但追蹤鍵的去重邏輯應該共用一套，不能各算各的。
    """
    raw = json.dumps(
        {"type": type_, "tracking": tracking},
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_TEMPLATES = {
    TYPE_DEFICIENCY: {
        "subject": "[口試資料補正通知] {student_name}（{case_key}）— 共 {issue_count} 項待處理",
        "body": (
            "{student_name} 同學，您好：\n\n"
            "口試申請資料經檢核，以下項目尚待補正：\n\n"
            "{issue_lines}\n\n"
            "請於 {deadline_text} 前補正，逾期可能影響口試排程。如有疑問請洽系辦。\n\n"
            "— {signature}"
        ),
    },
    # TYPE_CONFIRMATION（附件4 催辦）不用這裡的模板：主旨、內文與原附件4 確認信共用
    # advisor_confirmation.reminder_content()，兩封信的代碼、回覆方式與署名才會一致。
    TYPE_COLLECTION: {
        # 2026-10-13：附件6 交付版交給學生帶去口試、學生交回；逾期提醒寄給該場口試委員
        "subject": "[附件6簽收提醒] {date_text} 口試場次（第 {reminder_ord} 次提醒）",
        "body": (
            "委員老師您好：\n\n"
            "感謝您擔任 {term} 學期 {degree} 口試（{date_text}）的口試委員。"
            "這場口試的附件6（口試費清冊）尚未交回系辦：\n\n"
            "{student_lines}\n\n"
            "若您尚未在附件6 上簽名，敬請協助簽名，簽好後由學生繳回系辦。{reminder_note}\n\n"
            "— {signature}"
        ),
    },
}


@dataclass(frozen=True)
class NotificationDraft:
    """一封待審閱的通知草稿；產生不代表已核准寄送。"""

    type: str
    to: str | None
    cc: str | None
    subject: str
    body: str
    status: str
    tracking: dict
    metadata: dict
    # 附件檔路徑與信件串（In-Reply-To／References）；附件4 催辦信用來附上目前版本並接回原信
    attachments: tuple[str, ...] = ()
    in_reply_to: str = ""
    references: tuple[str, ...] = ()

    @property
    def idempotency_key(self) -> str:
        return compute_idempotency_key(self.type, self.tracking)


def group_deficiency_by_case(items: list[DeficiencyItem]) -> dict[str, list[DeficiencyItem]]:
    """把缺件追蹤項目依 case_key 分組，供逐學生產生一封信。"""
    groups: dict[str, list[DeficiencyItem]] = {}
    for item in items:
        groups.setdefault(item.case_key, []).append(item)
    return groups


class DraftTemplateEngine:
    """依情境類型把追蹤引擎的資料填入模板，產生待審閱草稿。"""

    def __init__(
        self,
        templates: dict | None = None,
        *,
        now: Callable[[], _dt.datetime] | None = None,
    ):
        self._templates = templates or _TEMPLATES
        self._now = now or clock.now

    def _render(self, type_: str, context: dict) -> tuple[str, str]:
        tpl = self._templates[type_]
        context = {"signature": C.OFFICE_SIGNATURE, **context}
        return tpl["subject"].format(**context), tpl["body"].format(**context)

    def _generated_at(self) -> str:
        return self._now().strftime("%Y-%m-%d %H:%M:%S")

    def deficiency_draft(
        self,
        case_key: str,
        student_name: str,
        items: list[DeficiencyItem],
        *,
        to: str | None = None,
        cc: str | None = None,
        reminder_date: _dt.date | None = None,
    ) -> NotificationDraft:
        """一位學生的補件通知；items 須全部屬於同一 case_key。

        reminder_date：截止前每日催件時傳入當天日期，寫進 tracking，讓每天的
        催件各自有一把 idempotency_key（同一天重跑仍去重，隔天才會再寄）。
        """
        if not items:
            raise ValueError("items 不可為空")
        if any(i.case_key != case_key for i in items):
            raise ValueError("items 含有不同 case_key，須先依案件分組")

        lines = [f"  • {i.field}：{i.description}" for i in items]
        deadlines = [i.deadline for i in items if i.deadline]
        deadline_text = (
            _date_text(max(d.computed_date for d in deadlines)) if deadlines else _date_text(None)
        )
        subject, body = self._render(TYPE_DEFICIENCY, {
            "student_name": student_name,
            "case_key": case_key,
            "issue_count": len(items),
            "issue_lines": "\n".join(lines),
            "deadline_text": deadline_text,
        })
        tracking = {
            "case_key": case_key,
            "tracking_keys": sorted(i.tracking_key for i in items),
            "issue_codes": sorted({i.issue_code for i in items}),
        }
        if reminder_date is not None:
            tracking["reminder_date"] = reminder_date.isoformat()
        metadata = {
            "student_name": student_name,
            "owner": items[0].owner,
            "generated_at": self._generated_at(),
        }
        return NotificationDraft(
            type=TYPE_DEFICIENCY, to=to, cc=cc, subject=subject, body=body,
            status=STATUS_READY if to else STATUS_NO_RECIPIENT,
            tracking=tracking, metadata=metadata,
        )

    def confirmation_draft(
        self,
        request: ConfirmationRequest,
        *,
        to: str | None = None,
        cc: str | None = None,
    ) -> NotificationDraft:
        """一場次的附件4 確認催辦信（沒有主檔可列附件4 內容時的精簡版）。

        與原附件4 確認信同主旨、同代碼、同回覆方式（「確認無誤」）與系辦署名；排程實際寄出時
        `application.plan_reminders()` 會換成 advisor_confirmation.reminder_draft()（含附件4
        內容、附檔與信件串）。信中只列學生姓名，不列學號等內部識別碼。
        """
        from .advisor_confirmation import reminder_content
        rows = "\n".join(f"  • {name}" for name in request.student_names)
        subject, body = reminder_content(request, rows=rows, today=self._now().date(), reply_by=None,
                                         sent_date=request.sent_date)
        tracking = {
            "request_key": list(request.request_key),
            "case_keys": sorted(request.case_keys),
            "version": request.content_version, "reminder_ord": request.reminder_count + 1,
        }
        metadata = {
            "advisor": request.advisor,
            "status": request.status,
            "generated_at": self._generated_at(),
        }
        return NotificationDraft(
            type=TYPE_CONFIRMATION, to=to, cc=cc, subject=subject, body=body,
            status=STATUS_READY if to else STATUS_NO_RECIPIENT,
            tracking=tracking, metadata=metadata,
        )

    def collection_draft(
        self,
        item: CollectionItem,
        *,
        to: str | None = None,
        cc: str | None = None,
    ) -> NotificationDraft:
        """一場次的附件6催繳信。reminder_ord 依 item.reminder_count 自動累進。"""
        student_lines = "\n".join(
            f"  • {name}（{key}）" for name, key in zip(item.student_names, item.case_keys)
        )
        reminder_ord = item.reminder_count + 1
        reminder_note = (
            f"（已提醒 {item.reminder_count} 次，上次：{_date_text(item.last_reminder_date)}）"
            if item.reminder_count else ""
        )
        subject, body = self._render(TYPE_COLLECTION, {
            "advisor": item.advisor,
            "term": item.term,
            "degree": item.degree,
            "date_text": item.date_text or "（日期未定）",
            "student_lines": student_lines,
            "reminder_ord": reminder_ord,
            "reminder_note": reminder_note,
        })
        tracking = {
            "request_key": list(item.request_key),
            "version": item.content_version,
            "case_keys": sorted(item.case_keys),
            "reminder_ord": reminder_ord,
        }
        metadata = {
            "advisor": item.advisor,
            "reminder_count": item.reminder_count,
            "generated_at": self._generated_at(),
        }
        return NotificationDraft(
            type=TYPE_COLLECTION, to=to, cc=cc, subject=subject, body=body,
            status=STATUS_READY if to else STATUS_NO_RECIPIENT,
            tracking=tracking, metadata=metadata,
        )
