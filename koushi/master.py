# -*- coding: utf-8 -*-
"""主檔 Excel 的讀與寫。

主檔是由權威來源確定性產生的唯讀快照；Excel 版面供查閱與稽核，不作為人工補值入口：
    口試主表   一場口試一筆
    委員明細   一場口試 × 一位委員一筆（配對層：職稱、口試費、交通費……）
    委員總表   一位委員一筆（身分層：姓名、單位、學歷；非敏感）
    撥帳資料   一位委員一筆（身分證、帳號、地址；敏感，獨立分頁方便管控）
    資料異常   哪些案件還不能產出、缺什麼、委員身分比對有沒有疑慮
    欄位來源   每個欄位從哪來（給系辦查）
委員明細／委員總表／撥帳資料以「委員編號」（committee_id）串接，
委員姓名由委員總表提供；委員明細的服務單位是該次口試的歷史快照，
讓同一委員跨期換單位時仍保留正確案件事實。
重跑 ingest 時，未重送的既有附件3事實會自動延續。
"""
import datetime as _dt
from pathlib import Path
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter as gcl

from . import config as C
from . import util as U
from .model import Committee, Exam, Master, Member, FIELD_SOURCE

SCHEMA_VERSION = 5
SCHEMA_MARKER = f"koushi-master-schema:{SCHEMA_VERSION}"

SHEET_EXAM = "口試主表"
SHEET_MEMBER = "委員明細"
SHEET_COMMITTEE = "委員總表"
SHEET_BANK = "撥帳資料"
SHEET_ISSUE = "資料異常"
SHEET_SOURCE = "欄位來源"

EXAM_COLS = [
    "案件鍵", "學期", "學院", "系所", "學位別", "班別", "學號", "姓名",
    "案件種類", "審核狀態", "簽核完成時間", "題目含特殊字", "論文題目", "生效",
    "指導教授", "指導教授來源",
    "口試日期", "口試日期原文", "口試開始時間", "口試結束時間", "口試地點", "口試方式",
    "有附件2", "有附件3", "介面委員已填", "委員人數", "資料狀態",
]

MEMBER_COLS = [
    "案件鍵", "學期", "學號", "學生姓名", "委員順序", "委員編號", "委員姓名", "服務單位",
    "職稱", "委員資格", "委員備註", "是否指導教授",
    "單程公里", "口試費", "交通費", "交通費說明",
    "附件3口試篇數", "附件3合計", "資料來源",
]

COMMITTEE_COLS = [
    "委員編號", "委員姓名", "英文姓名", "服務單位", "校內外",
    "最高學歷學校", "最高學位", "資料來源",
]

BANK_COLS = [
    "委員編號", "委員姓名", "服務單位",
    "身分證字號", "金融機構", "帳號", "戶籍地址", "Email",
    "撥帳齊全", "資料來源",
]

ISSUE_COLS = ["案件鍵", "學期", "學號", "姓名", "嚴重度", "問題代碼",
              "欄位", "說明", "阻擋產出", "阻擋附件4", "阻擋附件6",
              "責任", "來源"]

_HDR_FILL = PatternFill("solid", fgColor="DDEBF7")
_HDR_FONT = Font(bold=True)
_WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
_BLOCK_FILL = PatternFill("solid", fgColor="FCE4E4")


def _yn(b):
    return "是" if b else "否"


def _isyes(v):
    return str(v).strip() in ("是", "Y", "y", "True", "TRUE", "1")


# ────────────────────────── 寫出 ──────────────────────────
def _exam_row(e):
    return [
        e.案件鍵, e.學期, e.學院, e.系所, e.學位別, e.班別, e.學號, e.姓名,
        e.案件種類, e.審核狀態, e.簽核完成時間, e.題目含特殊字, e.論文題目, _yn(e.生效),
        e.指導教授, e.指導教授來源,
        e.口試日期, e.口試日期原文, e.口試開始時間, e.口試結束時間, e.口試地點, e.口試方式,
        _yn(e.有附件2), _yn(e.有附件3), _yn(e.介面委員已填), len(e.委員), e.資料狀態,
    ]


def _member_row(e, m, committee):
    return [
        e.案件鍵, e.學期, e.學號, e.姓名, m.委員順序, m.committee_id,
        committee.委員姓名 if committee else "", m.服務單位,
        m.職稱, m.委員資格, m.委員備註, _yn(m.是否指導教授),
        m.單程公里, m.口試費, m.交通費, m.交通費說明,
        m.附件3口試篇數, m.附件3合計, m.資料來源,
    ]


def _committee_row(c):
    return [
        c.committee_id, c.委員姓名, c.英文姓名, c.服務單位, c.校內外,
        c.最高學歷學校, c.最高學位, "附件2／附件3",
    ]


def _bank_row(c):
    return [
        c.committee_id, c.委員姓名, c.服務單位,
        c.身分證字號, c.金融機構, c.帳號, c.戶籍地址, c.Email,
        _yn(c.撥帳齊全), "附件3",
    ]


def _style_header(ws, ncol, widths=None):
    for c in range(1, ncol + 1):
        cell = ws.cell(1, c)
        cell.fill = _HDR_FILL
        cell.font = _HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    if widths:
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[gcl(i)].width = w


def save(master, issues, path=None, log=None):
    path = Path(path or C.MASTER)
    wb = openpyxl.Workbook()
    wb.properties.description = SCHEMA_MARKER

    ws = wb.active
    ws.title = SHEET_EXAM
    ws.append(EXAM_COLS)
    for e in master.exams:
        ws.append(_exam_row(e))
        if not e.生效:
            for c in range(1, len(EXAM_COLS) + 1):
                ws.cell(ws.max_row, c).fill = _BLOCK_FILL
        elif e.資料狀態 != "齊全":
            ws.cell(ws.max_row, EXAM_COLS.index("資料狀態") + 1).fill = _WARN_FILL
    _style_header(ws, len(EXAM_COLS),
                  [16, 7, 10, 24, 7, 10, 12, 10, 14, 16, 19, 10, 36, 6,
                   12, 24, 12, 11, 12, 12, 24, 10, 8, 8, 10, 34])

    wm = wb.create_sheet(SHEET_MEMBER)
    wm.append(MEMBER_COLS)
    for e in master.exams:
        for m in e.委員:
            wm.append(_member_row(e, m, master.committees.get(m.committee_id)))
    _style_header(wm, len(MEMBER_COLS),
                  [16, 7, 12, 10, 8, 10, 12, 30, 14, 34, 30, 10, 10, 8, 8, 30, 12, 10, 10])

    wc = wb.create_sheet(SHEET_COMMITTEE)
    wc.append(COMMITTEE_COLS)
    for cid in sorted(master.committees):
        wc.append(_committee_row(master.committees[cid]))
    _style_header(wc, len(COMMITTEE_COLS), [10, 12, 20, 30, 8, 24, 12, 16])

    wb_bank = wb.create_sheet(SHEET_BANK)
    wb_bank.append(BANK_COLS)
    for cid in sorted(master.committees):
        c = master.committees[cid]
        wb_bank.append(_bank_row(c))
        if not c.撥帳齊全 and not U.is_fee_exempt_unit(c.服務單位):
            wb_bank.cell(wb_bank.max_row, BANK_COLS.index("撥帳齊全") + 1).fill = _WARN_FILL
    _style_header(wb_bank, len(BANK_COLS), [10, 12, 30, 16, 18, 22, 36, 24, 10, 10])

    wi = wb.create_sheet(SHEET_ISSUE)
    wi.append(ISSUE_COLS)
    for row in issues:
        wi.append([row.get(c) for c in ISSUE_COLS])
        if _isyes(row.get("阻擋產出")):
            for c in range(1, len(ISSUE_COLS) + 1):
                wi.cell(wi.max_row, c).fill = _BLOCK_FILL
    _style_header(wi, len(ISSUE_COLS), [16, 7, 12, 10, 8, 24, 20, 46, 10, 10, 10, 10, 12])

    wissues = wb.create_sheet("來源異常")
    wissues.append(ISSUE_COLS)
    for row in master.source_issues:
        wissues.append([row.get(c) for c in ISSUE_COLS])

    wsrc = wb.create_sheet(SHEET_SOURCE)
    wsrc.append(["欄位", "來源", "說明"])
    note = {
        "附件2": "系所審核名單(委員資料)",
        "附件3": "口試費印領清冊（每場一份，每位學生各自寄回）",
        "待接入": "主檔預留欄位；目前來源無值時留白，不人工代填",
    }
    for f, s in FIELD_SOURCE.items():
        wsrc.append([f, s, note.get(s, "")])
    _style_header(wsrc, 3, [18, 26, 40])
    wsrc.append([])
    wsrc.append(["產生時間", _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    if log:
        for k, v in log.items():
            wsrc.append([k, str(v)])

    effective = wb.create_sheet("有效資料")
    effective.append(["案件鍵", "JSON"])
    import json
    for e in master.exams:
        effective.append([e.案件鍵, json.dumps({"advisor_emails": e.advisor_emails, "student_email": e.student_email,
            "members": [{"association_id": m.association_id, "委員姓名": m.委員姓名, "Email": m.Email,
                         "視訊出席": m.視訊出席} for m in e.委員]}, ensure_ascii=False)])
    return U.atomic_save_workbook(wb, path, literal_strings=True)


# ────────────────────────── 讀回 ──────────────────────────
def load(path=None):
    """讀回主檔，完整保留口試案件欄位。檔案不存在回傳 None。"""
    path = Path(path or C.MASTER)
    if not path.exists():
        return None
    wb = openpyxl.load_workbook(path, data_only=True)
    required_sheets = [SHEET_EXAM, SHEET_MEMBER, SHEET_COMMITTEE, SHEET_BANK,
                       SHEET_ISSUE, SHEET_SOURCE]
    missing_sheets = [name for name in required_sheets if name not in wb.sheetnames]
    if missing_sheets:
        raise ValueError(f"主檔格式不相容，缺少工作表：{'、'.join(missing_sheets)}")
    if wb.properties.description != SCHEMA_MARKER:
        raise ValueError(
            f"主檔格式不相容，預期 schema {SCHEMA_VERSION}；"
            "請用新版 build 重新建立或先執行遷移"
        )

    _validate_headers(wb[SHEET_EXAM], EXAM_COLS)
    _validate_headers(wb[SHEET_MEMBER], MEMBER_COLS)
    _validate_headers(wb[SHEET_COMMITTEE], COMMITTEE_COLS)
    _validate_headers(wb[SHEET_BANK], BANK_COLS)

    ws = wb[SHEET_EXAM]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(h or "").strip() for h in rows[0]]
    exams = {}
    for raw in rows[1:]:
        d = {hdr[i]: raw[i] for i in range(min(len(hdr), len(raw)))}
        if U.blank(d.get("學號")):
            continue
        e = Exam()
        for f in ("學期", "學號", "姓名", "學院", "系所", "案件種類", "審核狀態",
                  "題目含特殊字", "論文題目", "指導教授", "指導教授來源",
                  "口試日期原文", "口試開始時間", "口試結束時間", "口試地點", "口試方式"):
            v = d.get(f)
            setattr(e, f, "" if v is None else str(v).strip())
        e.學期 = str(e.學期).strip()
        e.學號 = str(e.學號).strip()
        e.簽核完成時間 = d.get("簽核完成時間")
        dt = d.get("口試日期")
        if isinstance(dt, (_dt.datetime, _dt.date)):
            e.口試日期 = dt.date() if isinstance(dt, _dt.datetime) else dt
        e.有附件2 = _isyes(d.get("有附件2"))
        e.有附件3 = _isyes(d.get("有附件3"))
        e.介面委員已填 = _isyes(d.get("介面委員已填"))
        e.derive()
        if e.案件鍵 in exams:
            raise ValueError(f"案件總表包含重複案件：{e.案件鍵}")
        exams[e.案件鍵] = e

    committees = {}
    wc = wb[SHEET_COMMITTEE]
    crows = list(wc.iter_rows(values_only=True))
    chdr = [str(h or "").strip() for h in crows[0]]
    for raw in crows[1:]:
        d = {chdr[i]: raw[i] for i in range(min(len(chdr), len(raw)))}
        cid = str(d.get("委員編號") or "").strip()
        if not cid:
            continue
        if cid in committees:
            raise ValueError(f"委員總表包含重複委員編號：{cid}")
        c = Committee(committee_id=cid)
        for f in ("委員姓名", "英文姓名", "服務單位", "最高學歷學校", "最高學位"):
            v = d.get(f)
            setattr(c, f, "" if v is None else str(v).strip())
        committees[cid] = c

    wbk = wb[SHEET_BANK]
    brows = list(wbk.iter_rows(values_only=True))
    bhdr = [str(h or "").strip() for h in brows[0]]
    banking_ids = set()
    for raw in brows[1:]:
        d = {bhdr[i]: raw[i] for i in range(min(len(bhdr), len(raw)))}
        cid = str(d.get("委員編號") or "").strip()
        c = committees.get(cid)
        if c is None:
            if cid:
                raise ValueError(f"撥帳資料指向不存在的委員編號：{cid}")
            continue
        if cid in banking_ids:
            raise ValueError(f"撥帳資料包含重複委員編號：{cid}")
        banking_ids.add(cid)
        for f in ("身分證字號", "金融機構", "帳號", "戶籍地址", "Email"):
            v = d.get(f)
            setattr(c, f, "" if v is None else str(v).strip())

    wm = wb[SHEET_MEMBER]
    mrows = list(wm.iter_rows(values_only=True))
    mhdr = [str(h or "").strip() for h in mrows[0]]
    member_keys = set()
    for raw in mrows[1:]:
        d = {mhdr[i]: raw[i] for i in range(min(len(mhdr), len(raw)))}
        case_key = str(d.get("案件鍵") or "").strip()
        e = exams.get(case_key)
        cid = str(d.get("委員編號") or "").strip()
        if not case_key and not cid:
            continue
        if e is None:
            raise ValueError(f"委員明細指向不存在的案件：{case_key}")
        if cid not in committees:
            raise ValueError(f"委員明細指向不存在的委員編號：{cid}")
        member_key = (case_key, cid)
        if member_key in member_keys:
            raise ValueError(f"委員明細包含重複的案件與委員關聯：{case_key}／{cid}")
        member_keys.add(member_key)
        m = Member(committee_id=cid)
        for f in ("服務單位", "職稱", "委員資格", "委員備註", "資料來源"):
            v = d.get(f)
            setattr(m, f, "" if v is None else str(v).strip())
        m.委員順序 = U.as_int(d.get("委員順序"), 0)
        m.是否指導教授 = _isyes(d.get("是否指導教授"))
        m.單程公里 = U.as_float(d.get("單程公里"))
        m.口試費 = U.as_int(d.get("口試費"), None)
        m.交通費 = U.as_int(d.get("交通費"), None)
        m.交通費說明 = str(d.get("交通費說明") or "").strip()
        m.附件3口試篇數 = U.as_int(d.get("附件3口試篇數"), None)
        m.附件3合計 = U.as_int(d.get("附件3合計"), None)
        e.委員.append(m)

    if "有效資料" in wb:
        import json
        for key, payload in wb["有效資料"].iter_rows(min_row=2, values_only=True):
            if key in exams:
                values = json.loads(payload)
                e = exams[key]
                e.advisor_emails = values.get("advisor_emails", {})
                e.student_email = values.get("student_email", "")
                for m, data in zip(e.委員, values.get("members", [])):
                    for field, value in data.items():
                        setattr(m, field, value)
    for e in exams.values():
        e.derive()
    return Master(
        exams=sorted(exams.values(), key=lambda x: (x.學期, x.學號)),
        committees=committees,
        source_issues=[dict(zip(ISSUE_COLS, row)) for row in
                       wb["來源異常"].iter_rows(min_row=2, values_only=True)] if "來源異常" in wb else [],
    )


def _validate_headers(worksheet, expected: list[str]) -> None:
    actual = [str(cell.value or "").strip() for cell in worksheet[1]]
    missing = [header for header in expected if header not in actual]
    if missing:
        raise ValueError(
            f"主檔格式不相容，工作表「{worksheet.title}」缺少欄位：{'、'.join(missing)}"
        )
