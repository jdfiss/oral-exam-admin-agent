"""學生填寫與外部資料匯入，轉成 canonical Master 的輸入模組。

案件來源（2026-09-23 定案）：學生一律經 Portal 正式申請，系辦於填寫截止日
下載一次附件2（Portal 匯出）匯入，全部收入、不依審核狀態篩選。`read_att2`
是唯一的案件來源；`read_student_submissions` 只把學生在本系統填的附件 3
補充資料（口試日期時地、指導教授、委員名單與 E-mail）併入附件2 已有的
案件，不能自行建立案件。委員撥帳個資不經學生，改由系統向委員本人索取。

`read_att3_files` 保留給歷史附件3 Excel 匯入，主流程（CLI／網頁）不呼叫。
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

import openpyxl

from . import config as C
from . import util as U
from .model import Committee, Exam, Member


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _records(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if source.suffix.lower() == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)]

    worksheet = openpyxl.load_workbook(source, data_only=True).active
    rows = list(worksheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [_text(value) for value in rows[0]]
    return [
        {headers[index]: row[index] for index in range(min(len(headers), len(row))) if headers[index]}
        for row in rows[1:]
    ]


def _require_headers(records: list[dict[str, Any]], required: set[str], source: str | Path) -> None:
    if not records:
        raise ValueError(f"{source}: 沒有資料列")
    missing = sorted(required - set(records[0]))
    if missing:
        raise ValueError(f"{source}: 缺少必要欄位：{'、'.join(missing)}")


def issue(
    *,
    code: str,
    message: str,
    exam: Exam | None = None,
    student_id: str = "",
    student_name: str = "",
    source: str = "合併",
    field_name: str = "",
    blocking: bool = False,
) -> dict[str, object]:
    from .validation import ISSUE_OWNER, OWNER_OFFICE

    return {
        "案件鍵": exam.案件鍵 if exam else "",
        "學期": exam.學期 if exam else "",
        "學號": exam.學號 if exam else student_id,
        "姓名": exam.姓名 if exam else student_name,
        "嚴重度": "阻擋" if blocking else "警示",
        "問題代碼": code,
        "欄位": field_name,
        "說明": message,
        "阻擋產出": "是" if blocking else "否",
        "阻擋附件4": "是" if blocking else "否",
        "阻擋附件6": "是" if blocking else "否",
        "來源": source,
        "責任": ISSUE_OWNER.get(code, OWNER_OFFICE),
    }


def read_att2(committee_registry, path: str | Path | None = None) -> tuple[dict[str, Exam], list[dict[str, object]]]:
    """從附件2 建立 exams 與案件級暫定委員，不跨案件猜測身分。

    附件2 一案一委員一列，本身就同時帶案件層欄位（重複於每一列）與
    委員配對層欄位，不需要附件1 另外提供案件基本資料。
    """
    source = Path(path or C.SRC_ATT2)
    records = _records(source)
    _require_headers(
        records,
        {"學年度 / 學年學期", "學號", "姓名", "委員姓名", "委員服務單位", "委員職稱"},
        source,
    )
    exams: dict[str, Exam] = {}
    issues: list[dict[str, object]] = []
    order_by_case: dict[str, int] = {}
    seen_case_committees: set[tuple[str, str]] = set()
    records, superseded = _latest_portal_records(records)

    for row_number, row in records:
        term = _text(row.get("學年度 / 學年學期"))
        student_id = _text(row.get("學號"))
        if not term or not student_id:
            issues.append(
                issue(
                    code="ATTACHMENT_2_MISSING_CASE_KEY",
                    message=f"附件2第 {row_number} 列缺少學期或學號",
                    student_id=student_id,
                    student_name=_text(row.get("姓名")),
                    source="附件2",
                    field_name="學期／學號",
                )
            )
            continue
        key = U.case_key(term, student_id)
        exam = exams.get(key)
        if exam is None:
            exam = Exam(
                學期=term,
                學院=_text(row.get("學院")),
                系所=_text(row.get("系所")),
                學號=student_id,
                姓名=_text(row.get("姓名")),
                案件種類=_text(row.get("案件種類")),
                審核狀態=_text(row.get("審核狀態")),
                簽核完成時間=row.get("簽核完成時間"),
                題目含特殊字=_text(row.get("題目含特殊字")),
                論文題目=_portal_title(row),
                有附件2=True,
            )
            exams[key] = exam
        else:
            mismatch = [
                label
                for label, current, incoming in (
                    ("姓名", exam.姓名, _text(row.get("姓名"))),
                    ("學院", exam.學院, _text(row.get("學院"))),
                    ("系所", exam.系所, _text(row.get("系所"))),
                    ("案件種類", exam.案件種類, _text(row.get("案件種類"))),
                    ("審核狀態", exam.審核狀態, _text(row.get("審核狀態"))),
                    ("簽核完成時間", _text(exam.簽核完成時間), _text(row.get("簽核完成時間"))),
                    ("題目含特殊字", exam.題目含特殊字, _text(row.get("題目含特殊字"))),
                )
                if incoming and current and incoming != current
            ]
            if mismatch:
                issues.append(
                    issue(
                        code="ATTACHMENT_2_CASE_FIELD_CONFLICT",
                        message=f"附件2同一案件的{('、'.join(mismatch))}不一致，暫以第一列為準，請確認",
                        exam=exam,
                        source="附件2",
                        field_name="、".join(mismatch),
                    )
                )

        name, english_name = U.clean_name(row.get("委員姓名"))
        unit = _text(row.get("委員服務單位"))
        if not name and not unit:
            continue  # Portal 委員欄非必填（各系所規定不同）；由介面補或列 NO_COMMITTEE 例外
        if not name or not unit:
            issues.append(
                issue(
                    code="ATTACHMENT_2_MISSING_COMMITTEE_IDENTITY",
                    message=f"附件2第 {row_number} 列委員姓名與服務單位須同時填寫，此列略過",
                    exam=exam,
                    source="附件2",
                    field_name="委員姓名／服務單位",
                )
            )
            continue
        committee_id = committee_registry.new_provisional_id(
            key, name, unit, source="附件2"
        )
        committee_registry.upsert_identity(
            committee_id,
            name=name,
            english_name=english_name,
            unit=unit,
            school=_text(row.get("最高學歷學校名稱")),
            degree=_text(row.get("最高學歷學位名稱")),
            source="附件2",
        )
        case_committee_key = (key, committee_id)
        if case_committee_key in seen_case_committees:
            issues.append(
                issue(
                    code="DUPLICATE_ATTACHMENT_2_COMMITTEE",
                    message=f"附件2同一案件重複出現相同委員（第 {row_number} 列略過）",
                    exam=exam,
                    source="附件2",
                    field_name="案件鍵／委員編號",
                )
            )
            continue
        seen_case_committees.add(case_committee_key)
        order_by_case[key] = order_by_case.get(key, 0) + 1
        exam.委員.append(
            Member(
                committee_id=committee_id,
                委員順序=order_by_case[key],
                服務單位=unit,
                職稱=_text(row.get("委員職稱")),
                委員資格=_text(row.get("委員資格")),
                委員備註=_text(row.get("備註")),
                資料來源="附件2",
            )
        )

    for key, kinds in superseded.items():
        exam = exams.get(key)
        if exam is None:
            continue
        issues.append(issue(
            code="ATTACHMENT_2_MULTIPLE_RECORDS",
            message=("附件2同一學生有多筆 Portal 紀錄（" + "、".join(kinds)
                     + f"），以簽核完成時間最新的「{exam.案件種類}」為準，請確認"),
            exam=exam, source="附件2", field_name="案件種類",
        ))

    for exam in exams.values():
        exam.derive()
    return exams, issues


_PORTAL_TITLE_COLUMNS = ("論文題目", "中文論文題目", "論文名稱")


def _portal_title(row: dict[str, Any]) -> str:
    """附件2 若有論文題目欄（目前 Portal 匯出沒有，預留）即以它為準。"""
    return next((_text(row.get(col)) for col in _PORTAL_TITLE_COLUMNS if _text(row.get(col))), "")


_SIGNOFF_RE = re.compile(
    r"^(\d{2,4})[-/.](\d{1,2})[-/.](\d{1,2})"
    r"(?:[ T]+(上午|下午|AM|PM)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(上午|下午|AM|PM)?)?",
    re.IGNORECASE,
)


def _parse_signoff(text: str) -> datetime | None:
    """把文字的簽核完成時間轉成 datetime；接受西元或民國年、可帶時分秒與上午／下午。"""
    m = _SIGNOFF_RE.match(text)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if year < 1911:
        year += 1911  # 民國年
    hour, minute, second = int(m.group(5) or 0), int(m.group(6) or 0), int(m.group(7) or 0)
    meridiem = (m.group(4) or m.group(8) or "").upper()
    if meridiem in ("下午", "PM") and hour < 12:
        hour += 12
    elif meridiem in ("上午", "AM") and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


def _signoff_sort_key(value: Any) -> tuple:
    """簽核完成時間的排序鍵：依實際時間比較，不可用字串比（'2026/9/30' 會勝過 '2026/10/01'）。

    可解析的時間 > 無法解析的文字（仍以文字比，至少穩定）> 空白。
    """
    if isinstance(value, datetime):
        return (2, value)
    if isinstance(value, date):
        return (2, datetime(value.year, value.month, value.day))
    text = _text(value)
    parsed = _parse_signoff(text) if text else None
    if parsed is not None:
        return (2, parsed)
    return (1 if text else 0, text)


def _latest_portal_records(
    records: list[dict[str, Any]],
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, list[str]]]:
    """同一學生可能有多筆 Portal 紀錄（申請、考試委員異動、撤銷）。

    每筆紀錄以「案件種類＋簽核完成時間」辨識；只保留簽核完成時間最新那筆的列，
    代表目前狀態。回傳 (保留的 (列號, 列)，{案件鍵: 所有紀錄的案件種類})。
    """
    groups: dict[str, dict[tuple[str, str], list[tuple[int, dict[str, Any]]]]] = {}
    passthrough: list[tuple[int, dict[str, Any]]] = []
    for row_number, row in enumerate(records, start=2):
        term, student_id = _text(row.get("學年度 / 學年學期")), _text(row.get("學號"))
        if not term or not student_id:
            passthrough.append((row_number, row))
            continue
        record_id = (_text(row.get("案件種類")), _text(row.get("簽核完成時間")))
        groups.setdefault(U.case_key(term, student_id), {}).setdefault(record_id, []).append((row_number, row))
    kept = list(passthrough)
    superseded: dict[str, list[str]] = {}
    for key, by_record in groups.items():
        latest = max(by_record, key=lambda rid: _signoff_sort_key(by_record[rid][0][1].get("簽核完成時間")))
        kept.extend(by_record[latest])
        if len(by_record) > 1:
            superseded[key] = [rid[0] or "（未標示）" for rid in by_record]
    kept.sort(key=lambda item: item[0])
    return kept, superseded


@dataclass
class _Sheet:
    get: Callable[[int, int], Any]
    max_row: int
    max_col: int


@dataclass
class _Attachment3:
    student_id: str
    student_name: str
    exam_date: date | None
    exam_date_text: str
    exam_mode: str
    members: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


def _open_sheet(path: str | Path) -> _Sheet:
    source = Path(path)
    if source.suffix.lower() == ".xls":
        import xlrd

        sheet = xlrd.open_workbook(str(source)).sheet_by_index(0)

        def get(row: int, col: int) -> Any:
            if row <= sheet.nrows and col <= sheet.ncols:
                value = sheet.cell_value(row - 1, col - 1)
                return None if value == "" else value
            return None

        return _Sheet(get=get, max_row=sheet.nrows, max_col=sheet.ncols)

    worksheet = openpyxl.load_workbook(source, data_only=True).active
    return _Sheet(
        get=lambda row, col: worksheet.cell(row=row, column=col).value,
        max_row=worksheet.max_row,
        max_col=worksheet.max_column,
    )


def _find_cell(sheet: _Sheet, labels: tuple[str, ...], max_row: int = 30, max_col: int = 30) -> tuple[int, int] | None:
    """找第一個以 labels 其中之一開頭的儲存格。

    用 startswith 而非完全相等：附件3 的表頭常把中英文擠在同一格
    （例如「服務單位Unit」「口試日期:\\n(exam date)」），完全比對永遠對不到。
    """
    for row in range(1, min(sheet.max_row, max_row) + 1):
        for col in range(1, min(sheet.max_col, max_col) + 1):
            text = _text(sheet.get(row, col))
            if text and any(text.startswith(label) for label in labels):
                return row, col
    return None


def _parse_date(value: Any) -> tuple[date | None, str]:
    if isinstance(value, datetime):
        return value.date(), f"{value.month}/{value.day}"
    if isinstance(value, date):
        return value, f"{value.month}/{value.day}"
    text = _text(value)
    if not text:
        return None, ""
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if match:
        parsed = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return parsed, f"{parsed.month}/{parsed.day}"
    match = re.search(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)", text)
    if match:
        return None, f"{int(match.group(1))}/{int(match.group(2))}"
    return None, text


def _optional_int(value: Any) -> int | None:
    return U.as_int(value, None)


# 附件3 欄位分三類：身分（拿來比對既有委員，不新建）／撥帳（寫回 Committee）／配對（寫回 Member）
_BANKING_FIELDS = ("身分證字號", "金融機構", "帳號", "戶籍地址", "Email")
_PAIRING_FIELDS = ("附件3口試篇數", "口試費", "交通費", "附件3合計")


def _parse_attachment3(path: str | Path) -> _Attachment3:
    sheet = _open_sheet(path)
    date_position = _find_cell(sheet, ("口試日期:", "口試日期"))
    date_value = sheet.get(date_position[0], date_position[1] + 1) if date_position else None
    exam_date, exam_date_text = _parse_date(date_value)
    if not exam_date and not exam_date_text:
        for row in range(1, min(sheet.max_row, 5) + 1):
            for col in range(1, min(sheet.max_col, 5) + 1):
                candidate_date, candidate_text = _parse_date(sheet.get(row, col))
                if candidate_date or re.fullmatch(r"\d{1,2}/\d{1,2}", candidate_text):
                    exam_date, exam_date_text = candidate_date, candidate_text
                    break
            if exam_date or exam_date_text:
                break

    remote_position = _find_cell(sheet, ("是否視訊:", "是否視訊"))
    exam_mode = ""
    if remote_position:
        raw_remote = _text(sheet.get(remote_position[0], remote_position[1] + 1))
        if raw_remote in {"是", "V", "v", "✓", "有", "true", "True", "1"}:
            exam_mode = "視訊"
        elif raw_remote:
            exam_mode = "實體"
    else:
        for col in range(1, min(sheet.max_col, 8) + 1):
            text = _text(sheet.get(2, col))
            if "視訊" in text and re.search(r"[【\[]\s*[^】\]]+\s*[】\]]", text):
                exam_mode = "部分視訊" if "部分" in text else "視訊"
                break

    header_position = _find_cell(sheet, ("服務單位",))
    if header_position is None:
        raise ValueError("找不到附件3委員表格標題列（服務單位）")
    header_row = header_position[0]
    # 表頭是兩列：身分欄（姓名Name／服務單位Unit／身份證字號／姓名／學號）
    # 在 header_row，金額與撥帳欄（口試篇數／口試費／交通費／合計／
    # 金融機構名稱／帳號／戶籍地址／E-mail）在正上方那列，兩列合併掃才找得齊。
    header_texts: list[tuple[int, str]] = []
    for probe_row in (header_row - 1, header_row):
        if probe_row < 1:
            continue
        for col in range(1, sheet.max_col + 1):
            label = _text(sheet.get(probe_row, col))
            if label:
                header_texts.append((col, label))
    header_texts.sort(key=lambda item: item[0])

    def column(*labels: str, occurrence: int = 0) -> int | None:
        """欄位比對用 startswith：表頭常把中英文擠在同一格（見 _find_cell）。"""
        for label in labels:
            matches = [col for col, text in header_texts if text.startswith(label)]
            if occurrence < len(matches):
                return matches[occurrence]
        return None

    member_name_col = column("姓名", occurrence=0)
    member_unit_col = column("服務單位")
    student_name_col = column("姓名", occurrence=1)
    student_id_col = column("學號")
    if member_name_col is None or member_unit_col is None or student_id_col is None:
        raise ValueError("找不到附件3委員姓名、服務單位或學號欄位")

    field_columns = {
        "身分證字號": column("身分證字號", "身份證字號"),
        "金融機構": column("金融機構名稱", "金融機構"),
        "帳號": column("帳號", "銀行帳號"),
        "戶籍地址": column("戶籍地址"),
        "Email": column("E-mail", "Email", "email"),
        "附件3口試篇數": column("口試篇數"),
        "口試費": column("口試費"),
        "交通費": column("交通費"),
        "附件3合計": column("合計"),
    }
    student_ids: set[str] = set()
    student_names: set[str] = set()
    members: dict[tuple[str, str], dict[str, Any]] = {}
    for row in range(header_row + 1, sheet.max_row + 1):
        raw_name = _text(sheet.get(row, member_name_col))
        if raw_name.startswith("總計"):
            break
        if not raw_name:
            continue
        member_name, _english = U.clean_name(raw_name)
        member_unit = _text(sheet.get(row, member_unit_col))
        if not member_unit:
            raise ValueError("附件3委員服務單位不可空白")
        identity_key = (U.canon_name(member_name), U.norm_unit(member_unit))
        if identity_key in members:
            raise ValueError("附件3委員姓名與服務單位重複")
        student_id = _text(sheet.get(row, student_id_col))
        if student_id:
            student_ids.add(student_id)
        if student_name_col:
            student_name = _text(sheet.get(row, student_name_col))
            if student_name:
                student_names.add(student_name)
        values: dict[str, Any] = {}
        for field_name, col in field_columns.items():
            value = sheet.get(row, col) if col else None
            values[field_name] = _optional_int(value) if field_name in _PAIRING_FIELDS else _text(value)
        members[identity_key] = values

    if len(student_ids) != 1:
        raise ValueError(f"附件3應只對應一位學生，實際學號：{sorted(student_ids)}")
    if len(student_names) > 1:
        raise ValueError(f"附件3包含多個學生姓名：{sorted(student_names)}")
    return _Attachment3(
        student_id=next(iter(student_ids)),
        student_name=next(iter(student_names), ""),
        exam_date=exam_date,
        exam_date_text=exam_date_text,
        exam_mode=exam_mode,
        members=members,
    )


def read_att3_files(
    exams: dict[str, Exam], committee_registry, paths: list[str | Path]
) -> list[dict[str, object]]:
    """嚴格合併附件3；不以委員姓名猜案件，也不讓重複檔案覆蓋。

    附件3 先精確比對本案件的暫定委員；有身分證字號時才升級／合併為
    跨案件正式身分。對不到附件2時核對失敗，不會憑空新建案件委員。
    """
    issues: list[dict[str, object]] = []
    seen_cases: dict[str, Path] = {}
    for raw_path in paths:
        path = Path(raw_path)
        try:
            parsed = _parse_attachment3(path)
        except Exception as exc:  # 格式錯誤要轉成可稽核問題，不能中斷整批
            issues.append(
                issue(
                    code="ATTACHMENT_3_PARSE_ERROR",
                    message=f"{path.name}: {exc}",
                    source="附件3",
                    field_name="檔案格式",
                    blocking=True,
                )
            )
            continue

        matches = [exam for exam in exams.values() if exam.學號 == parsed.student_id]
        if len(matches) != 1:
            issues.append(
                issue(
                    code="ATTACHMENT_3_WITHOUT_APPLICATION",
                    message="附件3學號無法唯一對應附件2案件",
                    student_id=parsed.student_id,
                    student_name=parsed.student_name,
                    source="附件2/附件3",
                    field_name="學號",
                    blocking=True,
                )
            )
            continue
        exam = matches[0]
        if exam.案件鍵 in seen_cases:
            issues.append(
                issue(
                    code="DUPLICATE_ATTACHMENT_3",
                    message=f"同一案件收到多份附件3，未覆蓋：{path.name}",
                    exam=exam,
                    source="附件3",
                    field_name="附件版本",
                    blocking=True,
                )
            )
            continue
        seen_cases[exam.案件鍵] = path

        if parsed.student_name and U.canon_name(parsed.student_name) != U.canon_name(exam.姓名):
            issues.append(
                issue(
                    code="STUDENT_NAME_CONFLICT",
                    message=f"附件3學生姓名與附件2不一致：{parsed.student_name}",
                    exam=exam,
                    source="附件2/附件3",
                    field_name="學生姓名",
                    blocking=True,
                )
            )
        exam.口試日期 = parsed.exam_date
        exam.口試日期原文 = parsed.exam_date_text
        exam.口試方式 = parsed.exam_mode
        exam.有附件3 = True

        for (member_name, member_unit), values in parsed.members.items():
            matched = _match_exam_members(exam, committee_registry, member_name, member_unit)
            if len(matched) != 1:
                issues.append(
                    issue(
                        code="COMMITTEE_IDENTITY_MISMATCH",
                        message="附件3委員姓名與服務單位無法唯一對應附件2",
                        exam=exam,
                        source="附件2/附件3",
                        field_name="委員姓名／服務單位",
                        blocking=True,
                    )
                )
                continue
            _merge_member_identity(
                exam, matched[0], values, committee_registry, exams, issues, source="附件3"
            )

    for exam in exams.values():
        if exam.生效 and not exam.有附件3:
            issues.append(
                issue(
                    code="MISSING_ATTACHMENT_3",
                    message="缺附件3（尚未收到口試日期／委員撥帳資料）",
                    exam=exam,
                    source="附件3",
                    field_name="口試日期、視訊狀態、撥帳資料",
                )
            )
    return issues


def _match_exam_members(
    exam: Exam, committee_registry, member_name: str, member_unit: str
) -> list[Member]:
    """在既有案件委員名單中，找出姓名＋服務單位唯一對應的暫定委員。"""
    matched = []
    for candidate in exam.委員:
        profile = committee_registry.get(candidate.committee_id) or {}
        profile_name = _text(profile.get("委員姓名"))
        profile_unit = candidate.服務單位 or _text(profile.get("服務單位"))
        if (
            U.canon_name(profile_name) == member_name
            and U.norm_unit(profile_unit) == member_unit
        ):
            matched.append(candidate)
    return matched


_HONORIFICS = ("助理教授", "副教授", "講座教授", "特聘教授", "教授", "副研究員", "助研究員",
                "研究員", "博士", "老師", "先生", "女士")


def _bare_name(name: str) -> str:
    """比對用姓名：正規化後去掉結尾的稱謂（學生常寫「陳大文教授」）。"""
    text = U.canon_name(name)
    for suffix in _HONORIFICS:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)]
    return text


def _match_case_member(
    exam: Exam, committee_registry, name: str, unit: str, taken: set[str],
) -> Member | None:
    """在同一案件的委員名單中以姓名找人（一場口試同名機率極低）；同名時才用服務單位區分。

    服務單位不要求逐字相符：學生寫簡稱（如「○○大學○○系」）、Portal 寫全銜
    （如「國立○○大學○○學系」）仍應對得上（review-2026-09-23 P0-2）。
    """
    target = _bare_name(name)
    candidates = [
        m for m in exam.委員
        if m.committee_id not in taken
        and _bare_name(_text((committee_registry.get(m.committee_id) or {}).get("委員姓名"))) == target
    ]
    if len(candidates) > 1:
        candidates = [
            m for m in candidates
            if U.norm_unit(m.服務單位 or (committee_registry.get(m.committee_id) or {}).get("服務單位")) == U.norm_unit(unit)
        ]
    return candidates[0] if len(candidates) == 1 else None


def _merge_member_identity(
    exam: Exam,
    member: Member,
    values: dict[str, Any],
    committee_registry,
    exams: dict[str, Exam],
    issues: list[dict[str, object]],
    source: str,
) -> bool:
    """把附件3等效資料（撥帳、配對欄位）併入既有委員身分，必要時升級／合併身分。

    附件3與學生介面共用同一套身分升級規則：只有在提供身分證字號時，
    案件級暫定身分才會升級或合併為跨案件正式身分。
    回傳 False 代表偵測到同案兩筆委員解出同一身分的衝突，呼叫端應跳過此列。
    """
    identity_number = values.get("身分證字號")
    identity_owner = committee_registry.resolve_identity(identity_number)
    if (
        not identity_owner
        and identity_number
        and committee_registry.is_provisional(member.committee_id)
    ):
        source_id = member.committee_id
        identity_owner = committee_registry.new_confirmed_id(
            _text((committee_registry.get(source_id) or {}).get("委員姓名")),
            member.服務單位,
            identity_number,
            source=source,
        )
        committee_registry.merge(source_id, identity_owner)
        for candidate_exam in exams.values():
            for candidate_member in candidate_exam.委員:
                if candidate_member.committee_id == source_id:
                    candidate_member.committee_id = identity_owner
        member.committee_id = identity_owner
    if identity_owner and identity_owner != member.committee_id:
        if any(
            other is not member and other.committee_id == identity_owner
            for other in exam.委員
        ):
            issues.append(
                issue(
                    code="DUPLICATE_COMMITTEE_IDENTITY",
                    message="同一場口試的多筆委員資料具有相同身分識別",
                    exam=exam,
                    source=source,
                    field_name="身分證字號",
                    blocking=True,
                )
            )
            return False
        source_id = member.committee_id
        committee_registry.merge(source_id, identity_owner)
        for candidate_exam in exams.values():
            for candidate_member in candidate_exam.委員:
                if candidate_member.committee_id == source_id:
                    candidate_member.committee_id = identity_owner
        member.committee_id = identity_owner
    for field_name in _PAIRING_FIELDS:
        value = values.get(field_name)
        if not U.blank(value):
            setattr(member, field_name, value)
    member.資料來源 = source
    if any(not U.blank(values.get(f)) for f in _BANKING_FIELDS):
        committee_registry.upsert_banking(
            member.committee_id,
            id_no=values.get("身分證字號"),
            bank=values.get("金融機構"),
            account=values.get("帳號"),
            address=values.get("戶籍地址"),
            email=values.get("Email"),
            source=source,
        )
    return True


def attachment3_paths(directory: str | Path) -> list[Path]:
    folder = Path(directory)
    if not folder.exists():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.suffix.lower() in {".xls", ".xlsx", ".xlsm"}
        and not path.name.startswith(("~$", "_"))
    )


@dataclass
class _Submission:
    term: str
    student_id: str
    exam_date: date | None
    exam_date_text: str
    exam_start_time: str
    exam_end_time: str
    exam_location: str
    exam_mode: str
    thesis_title: str
    advisors: list[str] = field(default_factory=list)
    members: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


def _parse_advisors(raw: dict[str, Any]) -> list[str]:
    values = raw.get("advisors")
    if values is None:
        values = [raw.get("advisor")]
    elif isinstance(values, str):
        values = re.split(r"[、,，/]", values)
    names: list[str] = []
    for value in values or []:
        name = _text(value)
        if name and name not in names:
            names.append(name)
    return names


def _parse_submission(path: str | Path) -> _Submission:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    exam_date, exam_date_text = _parse_date(raw.get("exam_date"))
    members: dict[tuple[str, str], dict[str, Any]] = {}
    for row in raw.get("committees") or []:
        name, _english = U.clean_name(row.get("name"))
        unit = _text(row.get("unit"))
        if not name or not unit:
            continue
        identity_key = (U.canon_name(name), U.norm_unit(unit))
        members[identity_key] = {
            "委員姓名": name,
            "服務單位": unit,
            "職稱": _text(row.get("title")),
            "Email": _text(row.get("email")),
            "視訊出席": bool(row.get("remote")),
        }
    if len(members) != len(raw.get("committees") or []):
        raise ValueError("委員列缺姓名／單位或包含重複委員，請重新核對")
    return _Submission(
        term=_text(raw.get("term")),
        student_id=_text(raw.get("student_id")),
        exam_date=exam_date,
        exam_date_text=exam_date_text,
        exam_start_time=_text(raw.get("exam_start_time")),
        exam_end_time=_text(raw.get("exam_end_time")),
        exam_location=_text(raw.get("exam_location")),
        exam_mode=_text(raw.get("exam_mode")),
        thesis_title=_text(raw.get("thesis_title")),
        advisors=_parse_advisors(raw),
        members=members,
    )


def student_submission_paths(directory: str | Path) -> list[Path]:
    folder = Path(directory)
    if not folder.exists():
        return []
    return sorted(
        path
        for path in folder.iterdir()
        if path.suffix.lower() == ".json" and not path.name.startswith(("~$", "_"))
    )


def read_student_submissions(
    exams: dict[str, Exam], committee_registry, paths: list[str | Path]
) -> list[dict[str, object]]:
    """把學生在本系統填的附件 3 補充資料，併入附件2 已有的案件。

    - 案件只來自附件2；找不到對應案件（學期＋學號）的填寫列為例外
      `SUBMISSION_WITHOUT_PORTAL_CASE`，不建案。
    - 學期、學院、系所、姓名與委員職稱／學歷／資格以附件2 為準，介面只補
      口試日期時地、論文題目、指導教授與委員 E-mail。
    - 委員名單與 E-mail 必填（依本系規定；未填由 validation 列學生缺件）。
      附件2 沒有委員時，以介面名單建立案件級暫定委員；兩邊都有但對不上時
      列為例外 `COMMITTEE_MISMATCH`，以附件2 為準。
    - 委員撥帳個資不經學生，這裡不收也不寫。
    """
    issues: list[dict[str, object]] = []
    seen_cases: dict[str, Path] = {}
    for raw_path in paths:
        path = Path(raw_path)
        try:
            parsed = _parse_submission(path)
        except Exception as exc:  # 格式錯誤要轉成可稽核問題，不能中斷整批
            issues.append(
                issue(
                    code="SUBMISSION_PARSE_ERROR",
                    exam=exams.get(path.stem),
                    message=f"{path.name}: {exc}",
                    source="介面",
                    field_name="檔案格式",
                    blocking=True,
                )
            )
            continue

        if not parsed.term or not parsed.student_id:
            issues.append(
                issue(
                    code="SUBMISSION_PARSE_ERROR",
                    exam=exams.get(path.stem),
                    message=f"{path.name}: 介面填寫缺少學期或學號",
                    source="介面",
                    field_name="學期／學號",
                    blocking=True,
                )
            )
            continue

        key = U.case_key(parsed.term, parsed.student_id)
        exam = exams.get(key)
        if exam is None:
            issues.append(
                issue(
                    code="SUBMISSION_WITHOUT_PORTAL_CASE",
                    message=f"介面填寫在附件2 查無對應案件：{key}",
                    student_id=parsed.student_id,
                    source="介面",
                    field_name="案件鍵",
                )
            )
            continue
        if exam.案件鍵 in seen_cases:
            issues.append(
                issue(
                    code="DUPLICATE_SUBMISSION",
                    message=f"同一案件收到多筆介面填寫，未覆蓋：{path.name}",
                    exam=exam,
                    source="介面",
                    field_name="填寫版本",
                    blocking=True,
                )
            )
            continue
        seen_cases[exam.案件鍵] = path

        raw_contact = json.loads(path.read_text(encoding="utf-8"))
        exam.advisor_emails = raw_contact.get("advisor_emails", {})
        exam.student_email = str(raw_contact.get("student_email") or "")
        exam.口試日期 = parsed.exam_date
        exam.口試日期原文 = parsed.exam_date_text
        exam.口試開始時間 = parsed.exam_start_time
        exam.口試結束時間 = parsed.exam_end_time
        exam.口試地點 = parsed.exam_location
        exam.口試方式 = parsed.exam_mode
        if parsed.thesis_title and not exam.論文題目:
            exam.論文題目 = parsed.thesis_title  # 附件2 有題目時以 Portal 為準（決策二-1）
        if parsed.advisors:
            exam.指導教授 = "、".join(parsed.advisors)
            exam.指導教授來源 = "介面"
        exam.有附件3 = True
        exam.介面委員已填 = bool(parsed.members)

        _merge_submission_members(exam, parsed, committee_registry, issues)

    for exam in exams.values():
        exam.derive()
        if exam.生效 and not exam.有附件3:
            issues.append(
                issue(
                    code="MISSING_SUBMISSION",
                    message="尚未收到本系統的補充資料填寫（口試日期、指導教授、委員 E-mail）",
                    exam=exam,
                    source="介面",
                    field_name="介面填寫",
                )
            )
    return issues


def _merge_submission_members(
    exam: Exam, parsed: _Submission, committee_registry, issues: list[dict[str, object]]
) -> None:
    if not parsed.members:
        return
    if not exam.委員:
        # 附件2（Portal）此欄非必填：沒有委員時以介面名單建立案件級暫定身分。
        for values in parsed.members.values():
            committee_id = committee_registry.new_provisional_id(
                exam.案件鍵, values["委員姓名"], values["服務單位"], source="介面"
            )
            committee_registry.upsert_identity(
                committee_id, name=values["委員姓名"], unit=values["服務單位"], source="介面",
            )
            exam.委員.append(Member(
                committee_id=committee_id,
                委員順序=len(exam.委員) + 1,
                服務單位=values["服務單位"],
                職稱=values["職稱"],
                資料來源="介面",
            ))
            _apply_remote_attendance(exam.委員[-1], values)
            _record_member_email(committee_registry, committee_id, values)
        return

    unmatched: list[str] = []
    matched_ids: set[str] = set()
    for values in parsed.members.values():
        member = _match_case_member(exam, committee_registry, values["委員姓名"], values["服務單位"], matched_ids)
        if member is None:
            unmatched.append(values["委員姓名"])
            continue
        matched_ids.add(member.committee_id)
        if not member.職稱 and values["職稱"]:
            member.職稱 = values["職稱"]
        _apply_remote_attendance(member, values)
        _record_member_email(committee_registry, member.committee_id, values)
    missing = [
        _text((committee_registry.get(m.committee_id) or {}).get("委員姓名")) or m.committee_id
        for m in exam.委員
        if m.committee_id not in matched_ids
    ]
    if unmatched or missing:
        parts = []
        if unmatched:
            parts.append("介面有、附件2 沒有：" + "、".join(unmatched))
        if missing:
            parts.append("附件2 有、介面沒有：" + "、".join(missing))
        issues.append(
            issue(
                code="COMMITTEE_MISMATCH",
                message="委員名單與附件2 不一致，以附件2 為準（" + "；".join(parts) + "）",
                exam=exam,
                source="附件2/介面",
                field_name="委員名單",
            )
        )


def _apply_remote_attendance(member: Member, values: dict[str, Any]) -> None:
    """部分視訊時，勾選視訊出席的委員不支領交通費（review-2026-09-23 P1-17）。"""
    member.視訊出席 = bool(values.get("視訊出席"))
    if member.視訊出席:
        member.交通費 = 0
        member.交通費說明 = "視訊出席"


def _record_member_email(committee_registry, committee_id: str, values: dict[str, Any]) -> None:
    if values.get("Email"):
        committee_registry.upsert_banking(committee_id, email=values["Email"], source="介面")


def apply_committee_replies(
    exams: dict[str, Exam], committee_registry, replies: dict[str, dict],
) -> list[dict[str, object]]:
    """把委員本人回填（系辦手動登錄）的撥帳資料，依委員 E-mail 套回所有相關案件。

    有身分證字號就走與附件3 相同的強識別規則：暫定身分升級或合併為正式身分。
    """
    issues: list[dict[str, object]] = []
    identities = {}
    for exam in exams.values():
        for member in exam.委員:
            profile = committee_registry.get(member.committee_id) or {}
            email = _text(member.Email or profile.get("Email")).lower()
            identities.setdefault(email, set()).add((U.canon_name(member.委員姓名 or profile.get("委員姓名")),
                committee_registry._identity_key(profile.get("身分證字號"))))
    conflicts = {email for email, people in identities.items() if email and len(people) > 1}
    for exam in exams.values():
        for member in list(exam.委員):
            email = _text(member.Email or (committee_registry.get(member.committee_id) or {}).get("Email")).lower()
            if email in conflicts:
                issues.append(issue(code="COMMITTEE_EMAIL_CONFLICT", message="同信箱對應不同委員，個資未套用",
                                    exam=exam, source="委員回填", field_name="委員Email", blocking=True))
                continue
            reply = replies.get(email)
            if not reply:
                continue
            if reply.get("case_keys") and exam.案件鍵 not in reply["case_keys"]:
                continue
            if reply.get("name") and U.canon_name(reply["name"]) != U.canon_name(member.委員姓名 or
                    (committee_registry.get(member.committee_id) or {}).get("委員姓名")):
                continue
            values = {field: reply.get(field, "") for field in ("身分證字號", "金融機構", "帳號", "戶籍地址")}
            _merge_member_identity(exam, member, values, committee_registry, exams, issues, source="委員回填")
    return issues


def reuse_registry_by_name_unit(exams: dict[str, Exam], committee_registry) -> list[dict[str, object]]:
    """暫定身分的委員，若舊口委資料庫恰好一筆「姓名＋服務單位」相符的正式身分，直接沿用。

    2026-09-23 使用者確認：資料庫已有完整資料者直接沿用、不必寄信索取。姓名＋單位
    不是強識別，所以只在唯一相符時沿用，並列出 `COMMITTEE_MATCHED_BY_NAME`
    交專員在人工關卡確認；同一場口試已有該正式身分時不沿用，避免把兩人併成一人。
    """
    issues: list[dict[str, object]] = []
    for exam in exams.values():
        for member in exam.委員:
            if not committee_registry.is_provisional(member.committee_id):
                continue
            profile = committee_registry.get(member.committee_id) or {}
            name = _text(member.委員姓名 or profile.get("委員姓名"))
            candidates = [
                cid for cid in committee_registry.alias_candidates(name, member.服務單位)
                if not committee_registry.is_provisional(cid) and committee_registry.get(cid)
            ]
            if len(candidates) != 1:
                continue
            target = candidates[0]
            if any(other is not member and other.committee_id == target for other in exam.委員):
                continue
            source = member.committee_id
            committee_registry.merge(source, target)
            for candidate_exam in exams.values():
                for candidate_member in candidate_exam.委員:
                    if candidate_member.committee_id == source:
                        candidate_member.committee_id = target
            issues.append(issue(
                code="COMMITTEE_MATCHED_BY_NAME",
                message=f"委員「{name}」以姓名＋服務單位沿用舊口委資料庫 {target} 的資料，請確認是同一人",
                exam=exam, source="舊口委資料庫", field_name=f"委員：{name}",
            ))
    return issues


def materialize_committees(committee_registry, exams: dict[str, Exam]) -> dict[str, Committee]:
    """只把這批 exams 實際用到的 committee_id，從總表轉成 Committee 物件。

    總表可能累積了很多學期以前的委員，Master 只需要呈現這次相關的人。
    """
    used_ids = {member.committee_id for exam in exams.values() for member in exam.委員}
    committees: dict[str, Committee] = {}
    for committee_id in used_ids:
        d = committee_registry.get(committee_id) or {}
        committees[committee_id] = Committee(
            committee_id=committee_id,
            委員姓名=_text(d.get("委員姓名")),
            英文姓名=_text(d.get("英文姓名")),
            服務單位=_text(d.get("服務單位")),
            最高學歷學校=_text(d.get("最高學歷學校")),
            最高學位=_text(d.get("最高學位")),
            身分證字號=_text(d.get("身分證字號")),
            金融機構=_text(d.get("金融機構")),
            帳號=_text(d.get("帳號")),
            戶籍地址=_text(d.get("戶籍地址")),
            Email=_text(d.get("Email")),
        )
    return committees


def fill_advisors(exams: dict[str, Exam], committees: dict[str, Committee]) -> None:
    """指導教授由學生在介面填寫（可多位，以「、」分隔）；只推導委員是否為指導教授。"""
    for exam in exams.values():
        advisor_keys = {U.canon_name(name) for name in exam.指導教授們}
        for member in exam.委員:
            committee = committees.get(member.committee_id)
            member.是否指導教授 = bool(
                committee and U.canon_name(member.委員姓名 or committee.委員姓名) in advisor_keys
            )


def fill_fees(exams: dict[str, Exam], committees: dict[str, Committee], distance_table) -> list[str]:
    unknown: set[str] = set()
    for exam in exams.values():
        for member in exam.委員:
            if member.交通費說明 in ("視訊出席", "視訊口試"):
                member.交通費, member.交通費說明 = None, ""
            if exam.口試方式 == "視訊" or (exam.口試方式 == "部分視訊" and member.視訊出席):
                member.交通費 = 0
                member.交通費說明 = "視訊口試" if exam.口試方式 == "視訊" else "視訊出席"
            if member.口試費 is None:
                member.口試費 = exam.費率
            if member.交通費 is None and exam.口試方式 == "視訊":
                member.交通費 = 0
                member.交通費說明 = "視訊口試"
            if member.交通費 is None:
                committee = committees.get(member.committee_id)
                unit = member.服務單位 or (committee.服務單位 if committee else "")
                fee, note = distance_table.fare(unit)
                if fee is None:
                    if not U.is_fee_exempt_unit(unit):
                        unknown.add(unit)
                    if note:  # 距離表比對有歧義：寫明候選，檢核列 NO_DISTANCE 交專員
                        member.交通費說明 = note
                else:
                    member.交通費 = fee
                    if note and not member.交通費說明:
                        member.交通費說明 = note
    return sorted(unknown)
