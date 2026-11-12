# -*- coding: utf-8 -*-
"""附件4 寄送追蹤引擎（舊稱「教授確認引擎」）。

2026-10-12 起取消「教授確認附件4」這個關卡：
1. 資料備齊就把附件4（口試一覽）寄給指導教授參考核對。
2. 教授不用回信；沒回信不催、不逾期、不擋任何後續步驟（口試後附件6、學期結案）。
3. 教授有錯才回信：回信照常收進信箱、由信箱 agent／文件中心判讀並擬修正草稿給專員核准；
   教授回「確認無誤」仍記為已確認（MODIFIED／CONFIRMED 只是紀錄，不是關卡）。

附件4 全學期一份（2026-10-10）：同一學期所有通過寄送關卡的案件印在同一份附件4，寄給每位
有案件的指導教授；寄送請求則是「每學期、每位指導教授」一筆，只涵蓋這位教授自己
指導的案件（共同指導的案件同時算在每位指導教授名下）。
"""

from __future__ import annotations

import datetime as _dt
from .. import clock
from dataclasses import dataclass, replace

from ..deadline import Deadline, confirmation_deadline
from ..generators.attachment4 import _date_sort_key
from ..model import Master


PENDING = "pending"
CONFIRMED = "confirmed"
MODIFIED = "modified"
OVERDUE = "overdue"  # 2026-10-12 起不再產生（教授不用回信，沒有逾期）；保留常數供舊資料與介面對照


@dataclass(frozen=True)
class ConfirmationRequest:
    """寄給一位指導教授的本學期附件4（信中列出「自己指導學生那幾列」）的寄送／回信追蹤。

    degree／date_text 是這位教授名下案件的學位別與口試日期（多個以「、」串接），只供顯示。
    """
    group_key: tuple
    term: str
    advisor: str
    degree: str
    date_text: str
    student_names: tuple[str, ...]
    case_keys: tuple[str, ...]
    response: str = PENDING
    sent_date: _dt.date | None = None
    deadline: Deadline | None = None
    response_date: _dt.date | None = None
    notes: str = ""
    content_version: str = ""
    reminder_count: int = 0
    last_reminder_date: _dt.date | None = None

    @property
    def request_key(self) -> tuple[str, ...]:
        """（學期, 指導教授）：每學期每位指導教授一筆（2026-10-10 起附件4 全學期一份）。

        舊版（每場次一筆）存下來的請求也換算成這個鍵，對帳時依案件鍵決定沿用或重設。
        """
        return (self.term, self.advisor)

    def status_on(self, day: _dt.date) -> str:
        """用明確日期判定狀態，讓排程測試與批次執行可固定時鐘。

        2026-10-12 起附件4 只是寄給教授參考、不用回信：已寄出未回覆一律是 PENDING
        （「已寄出」），過了寄出時記下的期限也不會變成逾期，因此不會產生催辦。
        """
        if self.response in (CONFIRMED, MODIFIED):
            return self.response
        if self.sent_date:
            return PENDING
        return "draft"

    @property
    def status(self) -> str:
        return self.status_on(clock.today())


def _unique(values) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def advisor_groups(exams) -> dict[tuple[str, str], list]:
    """（學期, 指導教授）→ 這位教授名下的生效案件（依口試日期、案件鍵排序）。

    共同指導的案件同時列在每位指導教授名下；沒填指導教授的案件歸在空字串名下
    （不會寄出）。沒有委員名單的案件與以往一樣不建寄送請求。
    """
    active = [e for e in exams if e.生效 and e.委員]
    active.sort(key=lambda e: (str(e.學期), _date_sort_key(e.日期字串), e.案件鍵))
    groups: dict[tuple[str, str], list] = {}
    for exam in active:
        for advisor in exam.指導教授們 or [""]:
            groups.setdefault((str(exam.學期), advisor), []).append(exam)
    return dict(sorted(groups.items()))


def create_requests(
    master: Master,
    *,
    term: str | None = None,
) -> list[ConfirmationRequest]:
    """每學期、每位指導教授各一筆寄送請求，涵蓋這位教授名下的全部案件。

    附件4 本身是全學期一份、每位教授收到同一份（2026-10-10）；寄送與回信追蹤逐位進行，
    內容版本只看這位教授自己的列。共同指導（2026-09-23）：每位指導教授各自收到一份。
    2026-10-12 起教授不用回信，未回覆不擋口試後步驟。
    """
    requests: list[ConfirmationRequest] = []
    for (term_code, advisor), exams in advisor_groups(master.active(term)).items():
        requests.append(ConfirmationRequest(
            group_key=(term_code, advisor),
            term=term_code,
            advisor=advisor,
            degree="、".join(_unique(e.學位別 for e in exams)),
            date_text="、".join(_unique(e.日期字串 for e in exams)),
            student_names=tuple(e.姓名 for e in exams),
            case_keys=tuple(e.案件鍵 for e in exams),
        ))
    from ..advisor_confirmation import content_version
    return [replace(r, content_version=content_version(master, r)) for r in requests]


def reconcile(
    master: Master,
    previous: list[ConfirmationRequest],
    *,
    term: str | None = None,
) -> list[ConfirmationRequest]:
    """用目前主檔對帳：保留已有追蹤狀態的請求，補上新增的指導教授。

    這位教授名下的案件鍵變了（新增／移除學生）代表教授當初看到、確認過的
    自己那幾列跟現在不一樣，舊的確認結果不能沿用——重設回 draft，連寄出時間
    與截止日都清掉，等於要重新寄一次確認信。別位教授的案件變動不影響這筆。
    """
    current = create_requests(master, term=term)
    # 舊版（每場次一筆）存下的請求換算成新鍵後可能重複，取第一筆；案件鍵不同本來就重設。
    prev_map: dict = {}
    for r in previous:
        prev_map.setdefault(r.request_key, r)
    result: list[ConfirmationRequest] = []
    for req in current:
        prev = prev_map.get(req.request_key)
        if prev is None or prev.case_keys != req.case_keys:
            result.append(req)  # 新教授，或名下案件已變動：本來就是預設的 draft 狀態
        else:
            result.append(replace(prev, group_key=req.group_key, degree=req.degree,
                                  date_text=req.date_text, student_names=req.student_names,
                                  case_keys=req.case_keys))
    return result


def mark_sent(
    requests: list[ConfirmationRequest],
    *,
    date: _dt.date | None = None,
    holidays: set[_dt.date] | None = None,
    deadline: Deadline | None = None,
) -> list[ConfirmationRequest]:
    """標記已寄出。只更新尚未寄出的請求。

    deadline 只留作紀錄（教授不用回信，狀態不會因此變逾期，見 status_on）；
    由呼叫端依系辦設定算好傳入，未傳時用預設。
    """
    date = date or clock.today()
    dl = deadline or confirmation_deadline(date, holidays)
    return [
        replace(r, sent_date=date, deadline=dl) if r.sent_date is None else r
        for r in requests
    ]


def record_response(
    request: ConfirmationRequest,
    response: str,
    *,
    date: _dt.date | None = None,
    notes: str = "",
) -> ConfirmationRequest:
    """記錄教授回覆。response 為 CONFIRMED 或 MODIFIED。"""
    if response not in (CONFIRMED, MODIFIED):
        raise ValueError(f"無效回覆：{response!r}，需為 {CONFIRMED!r} 或 {MODIFIED!r}")
    return replace(request, response=response, response_date=date or clock.today(), notes=notes)


def apply_responses(
    requests: list[ConfirmationRequest],
    responses: dict[tuple[str, ...], str],
    *,
    date: _dt.date | None = None,
) -> list[ConfirmationRequest]:
    """批次套用回覆。responses key = request_key, value = CONFIRMED/MODIFIED。"""
    date = date or clock.today()
    result = []
    for r in requests:
        resp = responses.get(r.request_key)
        if resp:
            result.append(record_response(r, resp, date=date))
        else:
            result.append(r)
    return result


@dataclass(frozen=True)
class ConfirmationReport:
    """確認引擎的彙總報告。"""
    requests: tuple[ConfirmationRequest, ...]

    @property
    def draft(self) -> list[ConfirmationRequest]:
        return [r for r in self.requests if r.status == "draft"]

    @property
    def pending(self) -> list[ConfirmationRequest]:
        return [r for r in self.requests if r.status == PENDING]

    @property
    def confirmed(self) -> list[ConfirmationRequest]:
        return [r for r in self.requests if r.status == CONFIRMED]

    @property
    def modified(self) -> list[ConfirmationRequest]:
        return [r for r in self.requests if r.status == MODIFIED]

    @property
    def overdue(self) -> list[ConfirmationRequest]:
        return [r for r in self.requests if r.status == OVERDUE]

    @property
    def all_confirmed(self) -> bool:
        return all(r.status == CONFIRMED for r in self.requests)


def report(requests: list[ConfirmationRequest]) -> ConfirmationReport:
    return ConfirmationReport(requests=tuple(requests))


# ── 序列化 ──────────────────────────────────────────

_DATE_FMT = "%Y-%m-%d"


def request_to_dict(r: ConfirmationRequest) -> dict:
    d: dict = {
        "group_key": list(r.group_key),
        "term": r.term,
        "advisor": r.advisor,
        "degree": r.degree,
        "date_text": r.date_text,
        "student_names": list(r.student_names),
        "case_keys": list(r.case_keys),
        "response": r.response,
        "notes": r.notes,
        "content_version": r.content_version,
        "reminder_count": r.reminder_count,
        "last_reminder_date": r.last_reminder_date.isoformat() if r.last_reminder_date else None,
    }
    if r.sent_date:
        d["sent_date"] = r.sent_date.strftime(_DATE_FMT)
    if r.deadline:
        d["deadline_base"] = r.deadline.base_date.strftime(_DATE_FMT)
        d["deadline_days"] = r.deadline.business_days
        d["deadline_date"] = r.deadline.computed_date.strftime(_DATE_FMT)
    if r.response_date:
        d["response_date"] = r.response_date.strftime(_DATE_FMT)
    return d


def request_from_dict(d: dict) -> ConfirmationRequest:
    sent = None
    if d.get("sent_date"):
        sent = _dt.datetime.strptime(d["sent_date"], _DATE_FMT).date()
    deadline = None
    if d.get("deadline_date"):
        deadline = Deadline(
            base_date=_dt.datetime.strptime(d["deadline_base"], _DATE_FMT).date(),
            business_days=int(d["deadline_days"]),
            computed_date=_dt.datetime.strptime(d["deadline_date"], _DATE_FMT).date(),
        )
    resp_date = None
    if d.get("response_date"):
        resp_date = _dt.datetime.strptime(d["response_date"], _DATE_FMT).date()
    return ConfirmationRequest(
        group_key=tuple(d["group_key"]),
        term=d["term"],
        advisor=d["advisor"],
        degree=d["degree"],
        date_text=d["date_text"],
        student_names=tuple(d["student_names"]),
        case_keys=tuple(d["case_keys"]),
        response=d.get("response", PENDING),
        sent_date=sent,
        deadline=deadline,
        response_date=resp_date,
        notes=d.get("notes", ""),
        content_version=d.get("content_version", ""),
        reminder_count=int(d.get("reminder_count", 0)),
        last_reminder_date=_dt.date.fromisoformat(d["last_reminder_date"]) if d.get("last_reminder_date") else None,
    )
