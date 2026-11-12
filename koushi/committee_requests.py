# -*- coding: utf-8 -*-
"""向口試委員本人索取撥帳個資（2026-09-23 定案：委員個資不經學生）。

流程：專員在人工關卡放行案件後，系統對該案中「撥帳資料不齊」的校外委員寄信
索取身分證、銀行、帳號、戶籍地址。舊口委資料庫已有完整資料者直接沿用、不寄。
本系委員免撥帳資料，但舊口委資料庫查無此人（暫定身分）時，只索取身分證字號
（2026-10-01：否則暫定身分會一直擋住附件6）。

- 以**委員 E-mail**為單位：同一位委員出現在多個案件只寄一封，回填套用到他
  所有案件（暫定身分編號是逐案產生的，不能拿來當跨案件的鍵）。
- 回填管道：委員從索取信裡的**專屬連結**線上填寫（每位委員一組隨機代碼，存
  `STATE_DIR/committee_links.json`），或系辦依委員回信手動登錄。連結需部署時設定
  `KOUSHI_PUBLIC_URL`；未設定時索取信改請委員回信。個資傳輸需以 HTTPS 部署。
- 回填存在 `STATE_DIR/committee_replies.json`，每次建檔由 workflow 疊加進委員
  總表並寫入輸出的口委資料庫；不直接改系辦上傳的原始檔，避免系辦重新上傳時被覆蓋。
- 委員未回覆不催件（2026-10-01 Q12）；口試前 N 天（系辦設定）仍未回填就列例外交專員。
"""

from __future__ import annotations

import datetime as _dt
from . import clock
import json
import hashlib
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from . import audit_log, case_review, config as C, util as U
from .drafts import STATUS_NO_RECIPIENT, STATUS_READY, compute_idempotency_key
from .model import Master

TYPE_COMMITTEE_REQUEST = "committee_request"
# 委員綁定的案件組變了（例如加入第二案）時改寄新連結；不是催件（委員不催件）。
TYPE_COMMITTEE_LINK_UPDATE = "committee_link_update"
REPLIES_FILE = "committee_replies.json"
REQUESTS_FILE = "committee_requests.json"
LINKS_FILE = "committee_links.json"
SOURCE_OFFICE = "系辦手動登錄"
SOURCE_ONLINE = "委員線上填寫"
# 口試日前幾天（含當天與之後）仍未回填，就列為例外交專員；預設值，系辦可在設定頁調整。
EXCEPTION_DAYS_BEFORE_EXAM = 3
BANKING_FIELDS = ("身分證字號", "金融機構", "帳號", "戶籍地址")
# 登錄時要求再輸入一次的欄位：打錯的身分證若剛好是別人的會被合併成同一人（review-2026-09-23 P2-20）。
CONFIRM_FIELDS = ("身分證字號", "帳號")
# 本系委員免撥帳，只需身分證字號確認身分（2026-10-01）。
ID_ONLY_FIELDS = ("身分證字號",)
_ACCOUNT_DIGITS = (8, 16)
# 專屬連結有效到相關案件中最晚的口試日再加幾天（口試當天未交的在現場補齊，留一點緩衝）。
LINK_GRACE_DAYS = 3
# 沒有口試日期可參考時（例如直接呼叫、案件未填日期）的最短有效期。
LINK_MIN_DAYS = 7

# 身分證／居留證首字母對應數字（內政部編碼，I、O、W 等不依字母順序）。
_ID_LETTERS = dict(zip("ABCDEFGHJKLMNPQRSTUVXYWZIO", range(10, 36)))
_ID_RE = re.compile(r"^[A-Z][A-D0-9]\d{8}$")


def valid_taiwan_id(value: str) -> bool:
    """國民身分證、新式（第二碼 8／9）與舊式（第二碼 A–D）居留證的檢查碼驗證。"""
    value = value.strip().upper()
    if not _ID_RE.fullmatch(value):
        return False
    if value[1].isdigit():
        if value[1] not in "1289":
            return False
        second = int(value[1])
    else:
        second = _ID_LETTERS[value[1]] % 10
    head = _ID_LETTERS[value[0]]
    digits = [head // 10, head % 10, second, *map(int, value[2:])]
    weights = (1, 9, 8, 7, 6, 5, 4, 3, 2, 1, 1)
    return sum(d * w for d, w in zip(digits, weights)) % 10 == 0


def _normalize_account(value: str) -> str:
    return re.sub(r"[\s-]", "", value)


def validate_reply(
    values: dict[str, str], *, confirm: dict[str, str] | None = None, foreign_id: bool = False,
    fields: tuple[str, ...] = BANKING_FIELDS,
) -> dict[str, str]:
    """檢查並整理委員撥帳資料；不合格拋 ValueError（訊息給系辦看，不含個資）。

    `confirm` 是再輸入一次的欄位（見 CONFIRM_FIELDS），有給就必須一致。
    `foreign_id` 表示無本國身分證（護照號碼等），略過檢查碼驗證。
    `fields` 是這位委員要提供的欄位；本系委員只需 ID_ONLY_FIELDS。
    """
    cleaned = {field: str(values.get(field) or "").strip() for field in fields}
    missing = [field for field, value in cleaned.items() if not value]
    if missing:
        raise ValueError("委員資料不完整，缺：" + "、".join(missing))
    if not foreign_id:
        cleaned["身分證字號"] = cleaned["身分證字號"].upper()
        if not valid_taiwan_id(cleaned["身分證字號"]):
            raise ValueError("身分證字號格式或檢查碼不正確；外籍委員請勾選「非本國身分證」")
    if "帳號" in cleaned:
        cleaned["帳號"] = _normalize_account(cleaned["帳號"])
        low, high = _ACCOUNT_DIGITS
        if not (cleaned["帳號"].isdigit() and low <= len(cleaned["帳號"]) <= high):
            raise ValueError(f"帳號應為 {low}–{high} 位數字（可含空白或連字號）")
    if confirm is not None:
        for field in (f for f in CONFIRM_FIELDS if f in cleaned):
            again = str(confirm.get(field) or "").strip()
            if field == "帳號":
                again = _normalize_account(again)
            elif not foreign_id:
                again = again.upper()
            if again != cleaned[field]:
                raise ValueError(f"兩次輸入的{field}不一致")
    return cleaned


def email_key(email: str) -> str:
    return str(email or "").strip().lower()


@dataclass(frozen=True)
class CommitteeRequest:
    email: str
    name: str
    case_keys: tuple[str, ...]
    earliest_exam: _dt.date | None
    sent_date: _dt.date | None
    received: bool
    id_only: bool = False  # 本系委員：免撥帳，只需身分證字號確認身分
    latest_exam: _dt.date | None = None  # 相關案件中最晚的口試日：專屬連結有效到這天之後幾天

    @property
    def key(self) -> str:
        return email_key(self.email)

    @property
    def binding(self) -> str:
        payload = {"email": self.key, "name": U.canon_name(self.name),
                   "cases": sorted(self.case_keys), "id_only": self.id_only}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    @property
    def fields(self) -> tuple[str, ...]:
        return ID_ONLY_FIELDS if self.id_only else BANKING_FIELDS

    @property
    def confirm_fields(self) -> tuple[str, ...]:
        return tuple(f for f in CONFIRM_FIELDS if f in self.fields)

    def missing_before_exam(self, today: _dt.date, lead_days: int = EXCEPTION_DAYS_BEFORE_EXAM) -> bool:
        if self.received or self.earliest_exam is None:
            return False
        return today >= self.earliest_exam - _dt.timedelta(days=lead_days)


# ── 回填（系辦手動登錄）──

def load_replies(state_dir: str | Path) -> dict[str, dict]:
    return read_replies_file(Path(state_dir) / REPLIES_FILE)


def read_replies_file(path: str | Path) -> dict[str, dict]:
    path = Path(path)
    if not path.is_file():
        return {}
    return U.read_json_state(path).get("replies", {})


@U.locked_state()
def save_reply(
    state_dir: str | Path, *, email: str, values: dict[str, str], actor: str = "office",
    now: _dt.datetime | None = None, confirm: dict[str, str] | None = None, foreign_id: bool = False,
    source: str = SOURCE_OFFICE, fields: tuple[str, ...] = BANKING_FIELDS,
    name: str = "", case_keys: tuple[str, ...] = (),
) -> None:
    key = email_key(email)
    if not key:
        raise ValueError("委員 E-mail 不可空白")
    cleaned = validate_reply(values, confirm=confirm, foreign_id=foreign_id, fields=fields)
    state_dir = Path(state_dir)
    replies = load_replies(state_dir)
    replies[key] = {**cleaned, "received_at": (now or clock.now()).strftime("%Y-%m-%d %H:%M:%S"),
                    "source": source, "name": name, "case_keys": list(case_keys)}
    U.atomic_write_json({"replies": replies}, state_dir / REPLIES_FILE)
    log_path = state_dir / "audit_log.json"
    # 稽核只記誰、何時登錄了哪位委員，不寫入個資本身。
    audit_log.record(log_path, "committee_request", "reply_recorded",
                              tracking_key=(key,), detail={"actor": actor, "source": source})


# ── 專屬連結 ──

def _load_links(state_dir: Path) -> dict[str, str]:
    path = state_dir / LINKS_FILE
    if not path.is_file():
        return {}
    return U.read_json_state(path).get("tokens", {})


def _link_expiry(request: CommitteeRequest | None) -> float:
    """專屬連結到期時刻（epoch 秒，真實時間）。

    已確認流程決策第五節第 5 條：連結用到委員填寫為止，索取信不重寄、不催件。所以有效期
    不能是固定 7 天，而是到相關案件最晚的口試日再加 LINK_GRACE_DAYS 天（當天結束）；
    至少 LINK_MIN_DAYS 天。
    """
    minimum = time.time() + LINK_MIN_DAYS * 86400
    latest = (request.latest_exam or request.earliest_exam) if request is not None else None
    if latest is None:
        return minimum
    end = _dt.datetime.combine(latest + _dt.timedelta(days=LINK_GRACE_DAYS), _dt.time.max)
    return max(minimum, end.timestamp())


@U.locked_state()
def link_token(state_dir: str | Path, email: str, *, request: CommitteeRequest | None = None) -> str:
    """委員的專屬填寫代碼；同一位委員（E-mail）、同一組綁定固定同一組，沒有就建立。

    綁定（姓名、案件組）變了就換新代碼、舊連結失效；新連結由每日自動化以「連結更新」
    信寄給委員（`link_updates_needed`）。口試日延後時只延長有效期，代碼不變。
    """
    state_dir = Path(state_dir)
    key = email_key(email)
    path = state_dir / LINKS_FILE
    data = U.read_json_state(path) if path.is_file() else {}
    tokens = data.setdefault("tokens", {})
    expires = data.setdefault("expires", {})
    bindings = data.setdefault("bindings", {})
    binding = request.binding if request is not None else bindings.get(key)
    if key not in tokens or time.time() >= expires.get(key, 0) or bindings.get(key) != binding:
        tokens[key] = secrets.token_urlsafe(32)
        expires[key] = _link_expiry(request)
        bindings[key] = binding
        U.atomic_write_json(data, path)
    elif request is not None and _link_expiry(request) > expires.get(key, 0):
        expires[key] = _link_expiry(request)
        U.atomic_write_json(data, path)
    return tokens[key]


def email_for_token(state_dir: str | Path, token: str) -> str | None:
    path = Path(state_dir) / LINKS_FILE
    if not path.is_file():
        return None
    data = U.read_json_state(path)
    for key, value in data.get("tokens", {}).items():
        if secrets.compare_digest(value, token) and time.time() < data.get("expires", {}).get(key, 0):
            return key
    return None


def token_matches_request(state_dir, token, request):
    path = Path(state_dir) / LINKS_FILE
    if not path.is_file() or email_for_token(state_dir, token) != request.key:
        return False
    data = U.read_json_state(path)
    return data.get("bindings", {}).get(request.key) == request.binding


def reply_url(base_url: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/committee/reply/{token}"


# ── 索取清單 ──

def _load_requests_state(state_dir: Path) -> dict:
    path = Path(state_dir) / REQUESTS_FILE
    return U.read_json_state(path) if path.is_file() else {}


def _load_sent_dates(state_dir: Path) -> dict[str, _dt.date]:
    raw = _load_requests_state(state_dir).get("sent", {})
    return {k: _dt.date.fromisoformat(v) for k, v in raw.items()}


def mailed_binding(state_dir: str | Path, key: str) -> str | None:
    """最近一次寄給這位委員的連結所綁定的請求（姓名＋案件組）；沒紀錄回 None。

    舊資料沒有這筆紀錄時，退回看連結檔記的綁定（那就是當時寄出的連結）。
    """
    recorded = _load_requests_state(Path(state_dir)).get("link_bindings", {}).get(key)
    if recorded is not None:
        return recorded
    path = Path(state_dir) / LINKS_FILE
    return U.read_json_state(path).get("bindings", {}).get(key) if path.is_file() else None


@U.locked_state()
def record_link_mailed(state_dir: str | Path, request: CommitteeRequest) -> None:
    """記下這次寄出的連結綁定哪一組案件，之後案件組變動才知道要寄「連結更新」。"""
    state_dir = Path(state_dir)
    data = _load_requests_state(state_dir)
    data.setdefault("link_bindings", {})[request.key] = request.binding
    U.atomic_write_json(data, state_dir / REQUESTS_FILE)


def link_updates_needed(requests: list[CommitteeRequest], state_dir: str | Path) -> list[CommitteeRequest]:
    """已寄過索取信、還沒回填，而綁定的案件組（或姓名）已變動的委員：要寄新連結。"""
    return [r for r in requests
            if not r.received and r.sent_date is not None
            and mailed_binding(state_dir, r.key) not in (None, r.binding)]


def plan(master: Master, state_dir: str | Path) -> list[CommitteeRequest]:
    """已放行案件中撥帳資料不齊、或身分仍暫定的委員（需有 E-mail），依 E-mail 合併成一筆請求。

    本系委員免撥帳（撥帳齊全恆為真），但舊口委資料庫查無此人時身分是暫定（P...），
    會一直擋附件6；這種委員只索取身分證字號（id_only）。
    """
    state_dir = Path(state_dir)
    cleared = case_review.cleared_keys(master, state_dir)
    replies = load_replies(state_dir)
    sent_dates = _load_sent_dates(state_dir)
    groups: dict[str, dict] = {}
    identities = {}
    for exam in master.exams:
        if not exam.生效:
            continue
        for member in exam.委員:
            committee = master.committees.get(member.committee_id)
            if committee:
                key = email_key(member.Email or committee.Email)
                identities.setdefault(key, set()).add((U.canon_name(member.委員姓名 or committee.委員姓名),
                    str(committee.身分證字號 or "").strip().upper()))
    conflicts = {key for key, people in identities.items() if len(people) > 1}
    for exam in master.exams:
        if not exam.生效 or exam.案件鍵 not in cleared:
            continue
        for member in exam.委員:
            committee = master.committees.get(member.committee_id)
            if committee is None:
                continue
            needs_banking = not committee.banking_complete(member.服務單位 or committee.服務單位)
            if not needs_banking and not str(member.committee_id).startswith("P"):
                continue
            key = email_key(member.Email or committee.Email)
            if not key or key in conflicts:
                continue  # 缺 E-mail 是學生缺件（MISSING_COMMITTEE_EMAIL），不在這裡處理
            group = groups.setdefault(key, {"email": member.Email or committee.Email, "name": member.委員姓名 or committee.委員姓名,
                                            "cases": set(), "dates": set(), "banking": False})
            group["banking"] = group["banking"] or needs_banking
            group["cases"].add(exam.案件鍵)
            if exam.口試日期:
                group["dates"].add(exam.口試日期)
    return [
        CommitteeRequest(
            email=g["email"], name=g["name"], case_keys=tuple(sorted(g["cases"])),
            earliest_exam=min(g["dates"]) if g["dates"] else None,
            latest_exam=max(g["dates"]) if g["dates"] else None,
            sent_date=sent_dates.get(key),
            received=key in replies and U.canon_name(replies[key].get("name")) == U.canon_name(g["name"])
                and g["cases"].issubset(set(replies[key].get("case_keys", []))),
            id_only=not g["banking"],
        )
        for key, g in sorted(groups.items())
    ]


@U.locked_state()
def mark_sent(state_dir: str | Path, requests: list[CommitteeRequest], *, date: _dt.date) -> None:
    state_dir = Path(state_dir)
    data = _load_requests_state(state_dir)
    sent = data.setdefault("sent", {})
    for request in requests:
        sent.setdefault(request.key, date.isoformat())
    U.atomic_write_json(data, state_dir / REQUESTS_FILE)


def draft(request: CommitteeRequest, *, student_names: dict[str, str], link: str | None = None) -> dict:
    """索取個資信草稿；格式與 notify.send_drafts() 相容，走 send_log 去重。

    有專屬連結時請委員線上填寫；沒有時（未設定 KOUSHI_PUBLIC_URL）請委員回信。
    """
    # 對外信只列學生姓名，不列學號／案件鍵等內部識別碼（委員不需要，也避免外流）
    cases = "\n".join(f"  - {student_names.get(k) or '（學生姓名未登錄）'}" for k in request.case_keys)
    if request.id_only:
        purpose = "本系委員不需提供撥款資料；為建立口試委員資料，敬請提供身分證字號"
        how = (f"{purpose}，請透過下列專屬連結填寫：\n  {link}\n此連結僅供您本人使用，請勿轉寄。\n\n"
               if link else f"{purpose}，請回覆本信（系辦收到後登錄系統）。\n\n")
    elif link:
        how = ("為辦理口試費撥款，敬請透過下列專屬連結填寫撥款資料\n"
               "（身分證字號、金融機構（含分行）、帳號、戶籍地址）：\n"
               f"  {link}\n"
               "此連結僅供您本人使用，請勿轉寄。\n\n")
    else:
        how = ("為辦理口試費撥款，敬請回覆本信提供以下資料（系辦收到後登錄系統）：\n"
               "  身分證字號、金融機構（含分行）、帳號、戶籍地址\n\n")
    body = (
        f"{request.name} 老師您好，\n\n"
        "感謝您擔任下列學生的學位口試委員：\n"
        f"{cases}\n\n"
        f"{how}"
        "若您不便以此方式提供，口試當天亦可於簽收單上填寫。\n"
        "如有疑問，請聯繫系辦。\n\n"
        f"— {C.OFFICE_SIGNATURE}"
    )
    tracking = {"email": request.key, "case_keys": list(request.case_keys),
                "identity_binding": request.binding}
    valid = "@" in request.email
    return {
        "type": TYPE_COMMITTEE_REQUEST,
        "to": request.email if valid else None,
        "cc": None,
        "subject": ("[口試委員資料] 敬請提供身分證字號" if request.id_only
                    else "[口試委員撥帳資料] 敬請提供口試費撥款資料"),
        "body": body,
        "status": STATUS_READY if valid else STATUS_NO_RECIPIENT,
        "tracking": tracking,
        "idempotency_key": compute_idempotency_key(TYPE_COMMITTEE_REQUEST, tracking),
        "metadata": {"committee": request.name},
    }


def draft_link_update(request: CommitteeRequest, *, student_names: dict[str, str], link: str) -> dict:
    """「填寫連結更新」信：委員綁定的案件組變了，先前的連結已失效，附上新連結。

    不是催件（委員不催件，決策五-4）：只在案件組變動時寄一次；同一組綁定走 send_log 去重。
    """
    cases = "\n".join(f"  - {student_names.get(k, k)}（{k}）" for k in request.case_keys)
    what = "身分證字號" if request.id_only else "撥款資料（身分證字號、金融機構（含分行）、帳號、戶籍地址）"
    body = (
        f"{request.name} 老師您好，\n\n"
        "您擔任口試委員的案件有更新，目前包含下列學生：\n"
        f"{cases}\n\n"
        f"先前寄給您的填寫連結已失效；如尚未填寫{what}，請改用下列專屬連結：\n"
        f"  {link}\n"
        "此連結僅供您本人使用，請勿轉寄。本信僅通知連結更新，並非催促。\n\n"
        "若您不便以此方式提供，口試當天亦可於簽收單上填寫。\n"
        "如有疑問，請聯繫系辦。\n\n"
        f"— {C.OFFICE_SIGNATURE}"
    )
    tracking = {"email": request.key, "case_keys": list(request.case_keys),
                "identity_binding": request.binding}
    valid = "@" in request.email
    return {
        "type": TYPE_COMMITTEE_LINK_UPDATE,
        "to": request.email if valid else None,
        "cc": None,
        "subject": "[口試委員資料] 填寫連結更新通知",
        "body": body,
        "status": STATUS_READY if valid else STATUS_NO_RECIPIENT,
        "tracking": tracking,
        "idempotency_key": compute_idempotency_key(TYPE_COMMITTEE_LINK_UPDATE, tracking),
        "metadata": {"committee": request.name},
    }
