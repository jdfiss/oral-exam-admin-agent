# -*- coding: utf-8 -*-
"""回信處理 agent：把教授「需要修改」的回信對應到具體的專員修正欄位。

LLM 只負責從自由文字抽出「列＋欄位＋新值＋逐字引用」（classifier.Modification
的形狀）；本模組用**確定性規則**把它對到主檔的案件、委員與 corrections.py 可修正的
欄位，算出修改前後對照（diff），並以固定範本擬一封回覆教授的草稿。

這裡不寫入任何狀態。專員在「信箱 agent」頁按下核准後，才逐筆呼叫
`application.save_case_correction()`（記錄處理人與原因），之後由既有流程重建主檔、
重寄新版附件4 供教授參考（2026-10-12 起教授不需確認，已確認流程決策修訂紀錄）。

對不到的修改（找不到是哪位學生、日期／時間看不懂、要換委員）一律標為 unmapped，
只顯示給專員，不會出現在可核准的修正裡。
"""

from __future__ import annotations

import datetime as _dt
import re

from . import clock, config as C, corrections, util as U
from .model import Committee, Exam, Master

# 本模組不呼叫模型（純確定性對應）；它處理的修改內容由信箱 agent 的 prompt
# （mailbox_agent.PROMPT_VERSION）抽出，軌跡上記的是那一版。
PROMPT_VERSION = None

MAPPED = "mapped"
UNMAPPED = "unmapped"
NO_CHANGE = "no_change"

_CN_NUM = {"零": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
           "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}

_FIELD_KEYWORDS = (
    ("口試日期", ("日期", "改期", "date", "哪天", "改到")),
    ("時間", ("時間", "時段", "幾點", "time", "開始")),
    ("口試地點", ("地點", "教室", "會議室", "地方", "location", "room")),
    ("方式", ("方式", "線上", "視訊", "實體", "遠端", "現場", "online", "mode")),
    ("職稱", ("職稱",)),
    ("服務單位", ("服務單位", "單位")),
    ("論文題目", ("題目", "論文名稱")),
    ("換委員", ("換委員", "更換委員", "委員改為", "替換")),
)


def _cn_to_int(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in _CN_NUM:
        return _CN_NUM[text]
    if len(text) == 2 and text[0] == "十" and text[1] in _CN_NUM:
        return 10 + _CN_NUM[text[1]]
    return None


def parse_date(value: str, *, reference: _dt.date | None = None) -> _dt.date | None:
    """2026-11-05、2026/11/5、11/5、11月5日 → date；看不懂回 None。"""
    text = U.canon_name(value) if value else ""
    m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.search(r"(?<!\d)(\d{1,2})[/月](\d{1,2})(?!\d)", text)
        if not m:
            return None
        mo, d = int(m.group(1)), int(m.group(2))
        y = (reference or clock.today()).year
    try:
        return _dt.date(y, mo, d)
    except ValueError:
        return None


def _parse_clock(text: str) -> str | None:
    text = U.canon_name(text).lower()
    pm = any(k in text for k in ("下午", "晚上", "pm"))
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"([0-9一二兩三四五六七八九十]{1,3})\s*(?:點|時|pm|am)(半)?", text)
        if not m:
            return None
        hour = _cn_to_int(m.group(1))
        if hour is None:
            return None
        minute = 30 if m.group(2) else 0
    if pm and hour < 12:
        hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_time_range(value: str) -> tuple[str | None, str | None]:
    """「14:00-16:00」「下午三點」→ (開始, 結束)；結束可為 None。"""
    parts = re.split(r"\s*(?:-|~|～|－|至|到)\s*", value or "", maxsplit=1)
    start = _parse_clock(parts[0])
    end = _parse_clock(parts[1]) if len(parts) == 2 else None
    if end and start and ("下午" in parts[0] or "pm" in parts[0].lower()) and int(end[:2]) < 12:
        end = f"{int(end[:2]) + 12:02d}{end[2:]}"
    return start, end


def _field_kind(field: str, new_value: str, quote: str) -> str | None:
    whole = f"{field} {new_value} {quote}"
    member_swap = "委員" in whole and any(w in whole for w in ("換", "替", "改為", "改成", "改請"))
    if member_swap and not any(w in whole for w in ("職稱", "單位", "線上", "視訊", "實體")):
        return "換委員"
    hay = f"{field} {new_value}".lower()
    for kind, words in _FIELD_KEYWORDS:
        if any(w in hay for w in words):
            return kind
    hay = quote.lower()
    for kind, words in _FIELD_KEYWORDS:
        if any(w in hay for w in words):
            return kind
    return None


def _member_name(master: Master, member) -> str:
    return member.委員姓名 or (master.committees.get(member.committee_id) or Committee()).委員姓名


def _find_member(master: Master, exam: Exam, text: str):
    """文字中提到的委員：全名優先，其次「姓＋教授／老師」且同姓只有一位。"""
    text = U.canon_name(text)
    full = [m for m in exam.委員 if _member_name(master, m) and U.canon_name(_member_name(master, m)) in text]
    if len(full) == 1:
        return full[0]
    by_surname = [m for m in exam.委員 if _member_name(master, m)
                  and any(f"{U.canon_name(_member_name(master, m))[0]}{t}" in text for t in ("教授", "老師"))]
    return by_surname[0] if len(by_surname) == 1 else None


def _target_exam(master: Master, case_keys, text: str) -> tuple[Exam | None, str]:
    exams = [master.by_key()[k] for k in case_keys if k in master.by_key()]
    if len(exams) == 1:
        return exams[0], ""
    if not exams:
        return None, "找不到對應的案件"
    canon = U.canon_name(text)
    hits = [e for e in exams if (e.姓名 and U.canon_name(e.姓名) in canon) or (e.學號 and e.學號 in canon)]
    if len(hits) == 1:
        return hits[0], ""
    return None, "這封信對應多位學生，無法判斷是哪一位，請專員指定"


def _mode_value(text: str) -> str | None:
    text = text.lower()
    if "部分" in text:
        return "部分視訊"
    if any(k in text for k in ("線上", "視訊", "遠端", "online")):
        return "視訊"
    if any(k in text for k in ("實體", "現場", "offline", "onsite")):
        return ""
    return None


def plan_corrections(master: Master, case_keys, modifications) -> list[dict]:
    """把 LLM 抽出的修改內容對到專員修正欄位；回傳 diff 列（不寫入）。"""
    changes: list[dict] = []
    for mod in modifications:
        row, field, new_value, quote = (str(mod.get(k, "")) for k in ("row", "field", "new_value", "quote"))
        text = " ".join((row, field, new_value, quote))
        base = {"quote": quote, "requested": f"{field}：{new_value}".strip("：")}
        exam, why = _target_exam(master, case_keys, text)
        if exam is None:
            changes.append({**base, "status": UNMAPPED, "reason": why})
            continue
        kind = _field_kind(field, new_value, quote)
        member = _find_member(master, exam, text)
        targets: list[tuple[str, str, str]] = []   # (欄位, 委員, 新值)
        reason = ""
        if kind == "口試日期":
            parsed = parse_date(new_value, reference=exam.口試日期)
            if parsed:
                targets.append(("口試日期", "", parsed.isoformat()))
            else:
                reason = f"看不懂的日期：{new_value}"
        elif kind == "時間":
            start, end = parse_time_range(new_value)
            if start:
                targets.append(("口試開始時間", "", start))
                if end:
                    targets.append(("口試結束時間", "", end))
            else:
                reason = f"看不懂的時間：{new_value}"
        elif kind == "口試地點":
            targets.append(("口試地點", "", new_value.strip()))
        elif kind == "方式":
            mode = _mode_value(new_value or quote)
            if mode is None:
                reason = f"看不懂的口試方式：{new_value}"
            elif member is not None and mode != "部分視訊":
                targets.append(("視訊出席", member.association_id or member.committee_id,
                                "true" if mode == "視訊" else "false"))
            else:
                targets.append(("口試方式", "", mode))
        elif kind in ("職稱", "服務單位"):
            if member is None:
                reason = "找不到要修改的是哪一位委員"
            else:
                targets.append((kind, member.association_id or member.committee_id, new_value.strip()))
        elif kind == "論文題目":
            targets.append(("論文題目", "", new_value.strip()))
        elif kind == "換委員":
            reason = "更換委員會撤銷放行，需專員在案件頁修改委員名單"
        else:
            reason = "無法對應到可修正的欄位"
        if not targets:
            changes.append({**base, "case_key": exam.案件鍵, "status": UNMAPPED, "reason": reason})
            continue
        for fld, mem, value in targets:
            before = corrections.source_value(exam, master.committees, fld, mem) or ""
            label = corrections.MEMBER_FIELDS.get(fld) if mem else corrections.EXAM_FIELDS.get(fld, fld)
            if mem:
                label = f"{_member_name(master, next(m for m in exam.委員 if (m.association_id or m.committee_id) == mem))}：{label}"
            row_out = {**base, "case_key": exam.案件鍵, "student": exam.姓名, "field": fld,
                       "member": mem, "label": label, "before": before, "after": value}
            try:
                corrections._validate(fld, value, mem)
            except ValueError as exc:
                changes.append({**row_out, "status": UNMAPPED, "reason": str(exc)})
                continue
            if before == value:
                changes.append({**row_out, "status": NO_CHANGE, "reason": "與目前資料相同"})
            else:
                changes.append({**row_out, "status": MAPPED, "reason": ""})
    return changes


def _show(value: str) -> str:
    return {"": "（空白）", "true": "視訊出席", "false": "實體出席"}.get(value, value)


# 回信裡「已更正」的宣告；含這句的回信必須等修改核准寫入後才能寄（審查 F07）
APPLIED_CLAIM = "系辦已依您的來信更正"

def draft_reply(advisor: str, changes: list[dict]) -> str:
    """固定範本的回覆草稿（專員核准前可修改）。"""
    mapped = [c for c in changes if c["status"] == MAPPED]
    other = [c for c in changes if c["status"] == UNMAPPED]
    lines = [f"{advisor or ''} 老師您好：".strip(), "",
             "謝謝您回覆口試一覽表（附件4）。"]
    if mapped:
        lines.append(APPLIED_CLAIM + "下列內容：")
        lines += [f"  • {c.get('student', '')} {c['label']}：{_show(c['before'])} → {_show(c['after'])}" for c in mapped]
    if other:
        lines.append("下列內容系辦會再與您確認：")
        lines += [f"  • {c['requested']}" for c in other]
    lines += ["", "更正後系統會寄出新版附件4 供您參考，不需回覆。", "", f"— {C.OFFICE_SIGNATURE}"]
    return "\n".join(lines)
