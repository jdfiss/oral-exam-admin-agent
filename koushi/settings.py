# -*- coding: utf-8 -*-
"""系辦可調的期限與催件參數（2026-09-23 定案：催件日期由系辦自行設定，不寫死）。

全系統一套，存在 `STATE_DIR/settings.json`。沒有設定檔時使用 config.py 的
預設值。學生與教授（附件4 確認）分組；委員（個資索取）不催件，只設口試前
幾天仍未交個資列例外（2026-10-01）。

截止日：
    填寫截止日     系辦設定的日期；當天（含）以前學生可自由修改。
    正式檢核開始日 max(填寫截止日隔天, 附件2 第一次上傳隔天)（2026-10-01）。
    補件截止日     正式檢核開始日＋補件期限天數（依曆日／工作日設定），硬截止；
                   系辦可手動覆寫。附件2 重傳不重算，自動化失敗也不自動順延。

附件2 第一次上傳日由系統在上傳時記錄（不在設定頁填），切換本期學期時清空。

時段（phase_on）：
    unset          尚未設定填寫截止日 —— 視同填寫期間，只建預覽、不寄信
    filling        填寫截止日（含）以前 —— 只建預覽、不寄信、不催（2026-09-23 確認）
    awaiting_att2  填寫已截止、正式檢核尚未開始（附件2 未上傳或剛上傳）—— 只建預覽
    deficiency     正式檢核開始日起到補件截止日（含）—— 正式檢核、寄補件信與催件
    closed         補件截止日之後 —— 學生輸入鎖定、停止學生催件
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Mapping

from . import audit_log, config as C, util as U
from .deadline import Deadline, add_business_days, business_days_between

PHASE_UNSET = "unset"
PHASE_FILLING = "filling"
PHASE_AWAITING_ATT2 = "awaiting_att2"
PHASE_DEFICIENCY = "deficiency"
PHASE_CLOSED = "closed"

PHASE_LABELS = {
    PHASE_UNSET: "尚未設定填寫截止日（視同填寫期間）",
    PHASE_FILLING: "填寫期間",
    PHASE_AWAITING_ATT2: "填寫已截止，等待附件2 後開始檢核",
    PHASE_DEFICIENCY: "補件期間",
    PHASE_CLOSED: "補件已截止",
}

_FILENAME = "settings.json"


@dataclass(frozen=True)
class OfficeSettings:
    # ── 學生 ──
    fill_deadline: _dt.date | None = None
    deficiency_days: int = C.DEFICIENCY_DEADLINE_DAYS
    deficiency_reminder_lead_days: int = C.DEFICIENCY_REMINDER_LEAD_DAYS
    # ── 共用 ──
    send_hour: int = 9
    business_days: bool = True
    # ── 教授（附件4 確認）──
    confirmation_days: int = C.CONFIRMATION_DEADLINE_DAYS
    collection_days: int = 5
    confirmation_reminder_interval_days: int = 2
    # ── 自動放行（步驟 4）──
    auto_confirm_enabled: bool = False            # Feature flag：自動放行功能總開關
    auto_confirm_shadow: bool = True              # Shadow mode：True＝只建議不放行，False＝實際放行
    auto_confirm_whitelist: tuple[str, ...] = ()  # 教授白名單（姓名）；空＝全部教授都不能自動放行
    # ── 信箱 agent ──
    mailbox_auto_reply_enabled: bool = False      # 固定範本收件通知；預設關（docs/信箱agent設計.md）
    # ── 其他（review-2026-09-23 P1-6／P1-9／P1-13）──
    current_term: str = ""                        # 本期受理學期；設定後學生端固定帶入
    holidays: tuple[_dt.date, ...] = ()           # 國定假日，工作日計算時排除
    student_email_template: str = ""              # 學生信箱後備格式，例：{學號}@cc.example.edu
    # ── 2026-10-01 ──
    deficiency_deadline_override: _dt.date | None = None  # 補件截止日手動覆寫
    committee_exception_lead_days: int = 3        # 口試前幾天委員仍未交個資列例外
    att2_first_uploaded: _dt.date | None = None   # 本期附件2 第一次上傳日（系統記錄）
    # ── 系辦問答助理（唯讀；有設定 LLM 時才顯示）──
    assistant_enabled: bool = True

    def __post_init__(self):
        for name in ("deficiency_days", "deficiency_reminder_lead_days",
                     "confirmation_days", "confirmation_reminder_interval_days",
                     "committee_exception_lead_days", "collection_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{_LABELS[name]}必須是 0 以上的整數")
        if isinstance(self.send_hour, bool) or not isinstance(self.send_hour, int) \
                or not 0 <= self.send_hour <= 23:
            raise ValueError(f"{_LABELS['send_hour']}必須是 0–23 的整數")
        if self.current_term and not re.fullmatch(r"[A-Za-z0-9]{1,10}", self.current_term):
            raise ValueError("本期學期僅限英數字（例：1141）")
        template = self.student_email_template
        if template and ("{學號}" not in template or "@" not in template):
            raise ValueError("學生信箱後備格式需包含 {學號} 與 @，例：{學號}@cc.example.edu")

    @property
    def holiday_set(self) -> set[_dt.date]:
        return set(self.holidays)

    def fallback_student_email(self, student_id: str) -> str | None:
        if not self.student_email_template or not student_id:
            return None
        return self.student_email_template.replace("{學號}", str(student_id))

    # ── 日數計算 ──

    def add_days(self, base: _dt.date, days: int) -> _dt.date:
        if self.business_days:
            return add_business_days(base, days, self.holiday_set)
        return base + _dt.timedelta(days=days)

    def days_between(self, start: _dt.date, end: _dt.date) -> int:
        """start 到 end 之間的天數（不含 start，含 end），依曆日／工作日設定。"""
        if self.business_days:
            return business_days_between(start, end, self.holiday_set)
        return max((end - start).days, 0)

    @property
    def student_open(self) -> bool:
        """學生端是否開放填寫：本期學期與填寫截止日都設好才開放（2026-10-01）。"""
        return bool(self.current_term) and self.fill_deadline is not None

    @property
    def check_start(self) -> _dt.date | None:
        """正式檢核開始日；填寫截止日未設或本期附件2 還沒上傳時為 None。"""
        if self.fill_deadline is None or self.att2_first_uploaded is None:
            return None
        one = _dt.timedelta(days=1)
        return max(self.fill_deadline + one, self.att2_first_uploaded + one)

    @property
    def computed_deficiency_deadline(self) -> _dt.date | None:
        """公式算出的補件截止日（不含手動覆寫）。"""
        if self.check_start is None:
            return None
        return self.add_days(self.check_start, self.deficiency_days)

    @property
    def deficiency_deadline(self) -> _dt.date | None:
        if self.fill_deadline is None:
            return None
        return self.deficiency_deadline_override or self.computed_deficiency_deadline

    @property
    def projected_deficiency_deadline(self) -> _dt.date | None:
        """補件截止日；附件2 還沒上傳時，假設會在填寫截止日當天上傳來推估。"""
        if self.fill_deadline is None:
            return None
        if self.deficiency_deadline is not None:
            return self.deficiency_deadline
        return self.add_days(self.fill_deadline + _dt.timedelta(days=1), self.deficiency_days)

    def student_deadline(self) -> Deadline | None:
        """補件截止日，以 Deadline 形式給缺件追蹤使用（全部學生共用同一天）。"""
        if self.deficiency_deadline is None:
            return None
        return Deadline(
            base_date=self.check_start or self.fill_deadline, business_days=self.deficiency_days,
            computed_date=self.deficiency_deadline,
        )

    def phase_on(self, day: _dt.date) -> str:
        if self.fill_deadline is None:
            return PHASE_UNSET
        if day <= self.fill_deadline:
            return PHASE_FILLING
        if self.check_start is None or day < self.check_start:
            # 手動覆寫的補件截止日已過時，即使附件2 一直沒上傳也視為截止
            if self.deficiency_deadline_override and day > self.deficiency_deadline_override:
                return PHASE_CLOSED
            return PHASE_AWAITING_ATT2
        if day <= self.deficiency_deadline:
            return PHASE_DEFICIENCY
        return PHASE_CLOSED


_LABELS = {
    "fill_deadline": "填寫截止日",
    "deficiency_days": "補件期限天數",
    "deficiency_reminder_lead_days": "補件截止前幾天開始催",
    "send_hour": "每日發送時刻",
    "business_days": "日數計算方式",
    "collection_days": "附件6回收期限天數",
    "confirmation_days": "教授確認期限天數",
    "confirmation_reminder_interval_days": "教授逾期後催辦間隔天數",
    "auto_confirm_enabled": "自動放行功能",
    "auto_confirm_shadow": "自動放行 Shadow Mode",
    "auto_confirm_whitelist": "自動放行教授白名單",
    "mailbox_auto_reply_enabled": "信箱自動收件通知",
    "current_term": "本期學期",
    "holidays": "國定假日",
    "student_email_template": "學生信箱後備格式",
    "deficiency_deadline_override": "補件截止日手動覆寫",
    "committee_exception_lead_days": "口試前幾天委員未交個資列例外",
    "att2_first_uploaded": "附件2 第一次上傳日",
    "assistant_enabled": "系辦問答助理",
}

_DATE_FIELDS = ("fill_deadline", "deficiency_deadline_override", "att2_first_uploaded")


def _to_dict(settings: OfficeSettings) -> dict:
    data = asdict(settings)
    for name in _DATE_FIELDS:
        value = getattr(settings, name)
        data[name] = value.isoformat() if value else None
    data["holidays"] = [d.isoformat() for d in settings.holidays]
    data["auto_confirm_whitelist"] = list(settings.auto_confirm_whitelist)
    return data


def _from_dict(data: Mapping) -> OfficeSettings:
    known = {f.name for f in fields(OfficeSettings)}
    values = {k: v for k, v in data.items() if k in known}
    for name in _DATE_FIELDS:
        raw = values.get(name)
        values[name] = _dt.date.fromisoformat(raw) if raw else None
    values["holidays"] = tuple(sorted(_dt.date.fromisoformat(d) for d in values.get("holidays") or ()))
    values["auto_confirm_whitelist"] = tuple(values.get("auto_confirm_whitelist") or ())
    return OfficeSettings(**values)


def load(state_dir: str | Path) -> OfficeSettings:
    """讀設定；檔案不存在時回傳預設值。檔案損毀時拋錯，不默默改用預設值。"""
    path = Path(state_dir) / _FILENAME
    if not path.is_file():
        return OfficeSettings()
    try:
        return _from_dict(U.read_json_state(path))
    except (ValueError, TypeError) as exc:
        # 保留 StateDataError 型別：Web 端顯示 503 恢復頁，不當一般輸入錯誤處理。
        raise U.StateDataError(f"系辦設定檔無法讀取：{path}（{exc}）") from exc


@U.locked_state()
def save(settings: OfficeSettings, state_dir: str | Path, *, actor: str = "office") -> Path:
    state_dir = Path(state_dir)
    before, after = _to_dict(load(state_dir)), _to_dict(settings)
    path = U.atomic_write_json(after, state_dir / _FILENAME)
    changed = {k: {"from": before.get(k), "to": after[k]} for k in after if before.get(k) != after[k]}
    log_path = state_dir / "audit_log.json"
    audit_log.record(log_path, "settings", "updated",
                              detail={"actor": actor, "changed": changed})
    return path


def from_form(form: Mapping[str, str], *, base: OfficeSettings | None = None) -> OfficeSettings:
    """把系辦設定頁的表單值轉成 OfficeSettings；格式錯誤時拋 ValueError（訊息可直接顯示）。"""
    base = base or OfficeSettings()
    values: dict = {}
    for name in ("fill_deadline", "deficiency_deadline_override"):
        raw = str(form.get(name, "")).strip()
        try:
            values[name] = _dt.date.fromisoformat(raw) if raw else None
        except ValueError as exc:
            raise ValueError(f"{_LABELS[name]}格式錯誤") from exc
    for name in ("deficiency_days", "deficiency_reminder_lead_days",
                 "send_hour", "confirmation_days", "confirmation_reminder_interval_days",
                 "committee_exception_lead_days", "collection_days"):
        raw = str(form.get(name, "")).strip()
        if not raw:
            values[name] = getattr(base, name)
            continue
        try:
            values[name] = int(raw)
        except ValueError as exc:
            raise ValueError(f"{_LABELS[name]}必須是整數") from exc
    values["business_days"] = str(form.get("business_days", "1")) != "0"
    values["current_term"] = str(form.get("current_term", base.current_term)).strip()
    values["student_email_template"] = str(form.get("student_email_template", base.student_email_template)).strip()
    if "holidays" in form:
        holidays = []
        for line in re.split(r"[\s,，、]+", str(form.get("holidays", ""))):
            if not line:
                continue
            try:
                holidays.append(_dt.date.fromisoformat(line))
            except ValueError as exc:
                raise ValueError(f"國定假日格式錯誤：{line}（請用 YYYY-MM-DD）") from exc
        values["holidays"] = tuple(sorted(set(holidays)))
    # ── 自動放行設定 ──
    values["auto_confirm_enabled"] = str(form.get("auto_confirm_enabled", "1" if base.auto_confirm_enabled else "0")) != "0"
    values["auto_confirm_shadow"] = str(form.get("auto_confirm_shadow", "1" if base.auto_confirm_shadow else "0")) != "0"
    values["mailbox_auto_reply_enabled"] = str(form.get(
        "mailbox_auto_reply_enabled", "1" if base.mailbox_auto_reply_enabled else "0")) == "1"
    # 沒有 LLM 時設定頁不顯示這個欄位，表單沒送就維持原值
    values["assistant_enabled"] = str(form.get("assistant_enabled", "1" if base.assistant_enabled else "0")) != "0"
    if "auto_confirm_whitelist" in form:
        names = [n.strip() for n in re.split(r"[,，、\n]+", str(form["auto_confirm_whitelist"])) if n.strip()]
        values["auto_confirm_whitelist"] = tuple(names)
    if values["current_term"] != base.current_term:
        # 換學期：上一期的附件2 上傳日與補件截止日覆寫都不再適用
        values["att2_first_uploaded"] = None
        values["deficiency_deadline_override"] = None
    return replace(base, **values)


def term_changing(form: Mapping[str, str], current: OfficeSettings) -> bool:
    """這次設定是否要把已設定的本期學期切換成另一個學期（需要系辦確認）。"""
    new_term = str(form.get("current_term", current.current_term)).strip()
    return bool(current.current_term) and new_term != current.current_term


def record_att2_upload(state_dir: str | Path, *, day: _dt.date, actor: str = "office") -> OfficeSettings:
    """附件2 上傳時呼叫：本期第一次上傳才記錄日期，重傳不改（補件截止日不重算）。"""
    current = load(state_dir)
    if current.att2_first_uploaded is not None:
        return current
    updated = replace(current, att2_first_uploaded=day)
    save(updated, state_dir, actor=actor)
    return updated
