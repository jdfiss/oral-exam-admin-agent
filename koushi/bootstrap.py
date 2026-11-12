# -*- coding: utf-8 -*-
"""一次性回填：從既有的附件4（學位口試一覽表）反推對照表。

附件1／2 都沒有「指導教授」欄，附件3 也沒有，但附件4／5 都需要它。
好消息是舊的附件4 本身就是一張 指導教授 -> 學生 的對照表，
且同時帶有口試日期與視訊註記 —— 正好補上主檔最缺的三個欄位。

這支只在導入時跑一次。之後 師生對照表 由系辦維護，附件3 提供日期。
"""
import re
import datetime as _dt
from pathlib import Path

from . import config as C
from . import document_conversion
from . import util as U

# 附件4 表頭列數（標題1 + 兩層表頭2）
_HEADER_ROWS = 3
_SECTION_RE = re.compile(r"^\d{3}學年度[上下]學期$")


def logical_cells(row):
    """python-docx 對橫向合併的儲存格會重複回傳同一個 cell。

    必須用底層 <w:tc> 元素判定是否為同一格 —— 若改用文字比對，
    兩個相鄰的空白欄會被誤併，導致後面的欄位整排左移
    （例：'王教授|空|空|李教授' 會被壓成三欄，日期跑到專班欄）。
    """
    out, seen = [], None
    for c in row.cells:
        if c._tc is seen:
            continue
        seen = c._tc
        out.append(c.text.strip())
    return out


def parse_att4(path=None):
    """解析附件4，回傳 list of dict：
    {學期段, 指導教授, 博士班[], 碩士一般[], 碩士專班[], 日期, 時間, 方式, 備註}
    """
    from docx import Document
    source = Path(path or C.TPL_DIR / "附件4-學位口試一覽.doc")
    with document_conversion.docx_template(source) as converted:
        doc = Document(str(converted))
    if not doc.tables:
        return []
    rows, section = [], ""
    for i, r in enumerate(doc.tables[0].rows):
        if i < _HEADER_ROWS:
            continue
        cells = logical_cells(r)
        joined = "".join(cells).strip()
        if _SECTION_RE.match(joined) or (len(cells) <= 2 and "學年度" in joined):
            section = joined
            continue
        # 補齊到 8 個邏輯欄：指導/博/碩一般/碩專班/日期/時間/方式/備註
        cells = (cells + [""] * 8)[:8]
        advisor = cells[0].replace("\n", " ").strip()
        if not advisor:
            continue
        rows.append({
            "學期段": section,
            "指導教授": advisor,
            C.PROGRAM_DOCTOR: U.split_names(cells[1]),
            C.PROGRAM_MASTER: U.split_names(cells[2]),
            C.PROGRAM_INSERV: U.split_names(cells[3]),
            "日期": cells[4].strip(),
            "時間": cells[5].strip(),
            "方式": cells[6].strip(),
            "備註": cells[7].strip(),
        })
    return rows


def seed(exams, advisor_reg, att4_path=None, fill_schedule=True):
    """把附件4 的資訊灌進 exams 與師生對照表。

    回傳統計 dict。exams 為 {案件鍵: Exam}。
    """
    rows = parse_att4(att4_path)
    # 學生姓名 -> (指導教授, 日期, 方式)
    lookup = {}
    for r in rows:
        for prog in (C.PROGRAM_DOCTOR, C.PROGRAM_MASTER, C.PROGRAM_INSERV):
            for nm in r[prog]:
                lookup.setdefault(nm, (r["指導教授"], r["日期"], r["方式"]))

    stat = {"附件4 資料列": len(rows), "解析出的學生": len(lookup),
            "對到案件": 0, "補指導教授": 0, "補口試日期": 0, "補口試方式": 0,
            "對不到": []}

    # 同名學生在同學期可能不只一位，先找出來避免誤填
    dupe = {}
    for e in exams.values():
        dupe.setdefault(e.姓名.strip(), []).append(e)

    for e in exams.values():
        hit = lookup.get(e.姓名.strip())
        if not hit:
            continue
        if len(dupe.get(e.姓名.strip(), [])) > 1:
            stat["對不到"].append("%s(同名%d人，需人工指定)"
                                 % (e.姓名, len(dupe[e.姓名.strip()])))
            continue
        advisor, date_txt, mode = hit
        stat["對到案件"] += 1
        if not e.指導教授 and advisor:
            e.指導教授 = advisor
            e.指導教授來源 = "附件4 回填"
            advisor_reg.upsert(e.學期, e.學號, e.姓名, advisor, "附件4 回填")
            stat["補指導教授"] += 1
        if fill_schedule and not e.口試日期 and not e.口試日期原文 and date_txt:
            d, txt = U.parse_date(date_txt)
            if d or txt:
                e.口試日期, e.口試日期原文 = d, txt
                stat["補口試日期"] += 1
        if fill_schedule and not e.口試方式 and mode:
            e.口試方式 = "部分視訊" if "部分" in mode else "視訊"
            stat["補口試方式"] += 1

    missing = [e.姓名 for e in exams.values()
               if e.姓名.strip() not in lookup and e.生效]
    stat["附件4 查無的學生"] = missing
    return stat
