# -*- coding: utf-8 -*-
"""附件4 —— 學位口試一覽表（Word）。

一個案件固定一列，依班別落在三個學生欄之一。範本雖有時間欄，
但依目前表單契約固定留白；不需要也不讀取口試實際時間。

作法上不重畫表格，而是把原始附件4 當範本：保留標題與兩層表頭，
複製一列既有資料列的 XML 當版型，逐列填字 —— 字型、框線、欄寬全部原封不動。
"""
import copy
import re
from collections import OrderedDict

from .. import config as C
from .. import util as U
from ..bootstrap import logical_cells
from .doc_properties import scrub_docx

HEADER_ROWS = 3          # 標題列 + 兩層表頭
SECTION_ROW = 3          # 範本中第一個「114學年度上學期」列
DATA_ROW = 4             # 範本中第一列資料


def _set_text(cell, text):
    """填字並保留原有字型：沿用第一個 run 的格式，其餘 run 移除。"""
    text = "" if text is None else str(text)
    para = cell.paragraphs[0]
    for extra in cell.paragraphs[1:]:
        extra._element.getparent().remove(extra._element)
    runs = para.runs
    if runs:
        runs[0].text = text
        for r in runs[1:]:
            r._element.getparent().remove(r._element)
    else:
        para.add_run(text)


def _set_title_year(cell, years):
    """範本標題寫死「114學年度」；只換年度數字的 run，其餘字型與文字不動。
    跨多個學年度時不猜，保留範本原文。"""
    if len(years) != 1:
        return
    year = next(iter(years))
    runs = [r for p in cell.paragraphs for r in p.runs]
    for i, run in enumerate(runs):
        following = runs[i + 1].text if i + 1 < len(runs) else ""
        if re.fullmatch(r"\s*\d{2,3}\s*", run.text) and following.lstrip().startswith("學年度"):
            run.text = year
            return
        if re.search(r"\d{2,3}(?=\s*學年度)", run.text):
            run.text = re.sub(r"\d{2,3}(?=\s*學年度)", year, run.text, count=1)
            return


def _apply_title_note(cell, note):
    """範本標題「114學年度學位考試一覽表          軟工碩士班/AI碩士班/外籍生」右側的班別提醒。
    note 為 None 時不動；否則把「一覽表」之後的文字換成 note（空字串＝移除，連同中間的空白）。"""
    if note is None:
        return
    runs = [r for p in cell.paragraphs for r in p.runs]
    anchor = next((i for i, r in enumerate(runs) if "一覽表" in r.text), None)
    if anchor is None:
        return
    head, _, tail = runs[anchor].text.partition("一覽表")
    after = runs[anchor + 1:]
    if not note:
        runs[anchor].text = head + "一覽表"
        for r in after:
            r.text = ""
        return
    if after:
        after[0].text = note
        for r in after[1:]:
            r.text = ""
    else:
        runs[anchor].text = head + "一覽表" + tail + note


def generated_title(cell) -> str:
    """標題格去掉右側班別提醒後的文字（文件屬性的標題用）。"""
    text = re.sub(r"\s+", " ", cell.text).strip()
    m = re.match(r".*?一覽表", text)
    return m.group(0) if m else text


def _logical_tcs(row):
    """回傳邏輯欄對應的 cell 物件（合併欄只取一次）。"""
    out, seen = [], None
    for c in row.cells:
        if c._tc is seen:
            continue
        seen = c._tc
        out.append(c)
    return out


def _date_sort_key(txt):
    """'6/18' -> (6,18)；'?'／'待定' 排最後。"""
    import re
    m = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})", str(txt or ""))
    if m:
        return (0, int(m.group(1)), int(m.group(2)))
    return (1, 99, 99)


def _merge_vertical_runs(table, indexed_rows, column, key_fn):
    """依相鄰資料列的語意鍵垂直合併指定欄，保留第一格文字與格式。"""
    start = 0
    while start < len(indexed_rows):
        key = key_fn(indexed_rows[start][1])
        end = start
        while (
            key is not None
            and end + 1 < len(indexed_rows)
            and key_fn(indexed_rows[end + 1][1]) == key
        ):
            end += 1
        if end > start:
            first_row = indexed_rows[start][0]
            last_row = indexed_rows[end][0]
            value = table.cell(first_row, column).text
            for offset in range(start + 1, end + 1):
                _set_text(table.cell(indexed_rows[offset][0], column), "")
            merged = table.cell(first_row, column).merge(table.cell(last_row, column))
            _set_text(merged, value)
        start = end + 1


def _normalize_directional_ooxml(document):
    """把 LibreOffice 產生的 start/end 正規化為相容性較廣的 left/right。

    附件四是由舊版 ``.doc`` 即時轉成 ``.docx``。LibreOffice 會在框線與
    段落對齊使用較新的邏輯方向名稱；部分瀏覽器型 DOCX 預覽器會忽略它們，
    導致直向框線消失，並把靠右標題誤畫成靠左。表單是固定的左至右版面，
    因此可安全地轉回傳統實體方向名稱。
    """
    from docx.oxml.ns import qn

    root = document.element
    for parent in root.xpath(
        ".//w:tcBorders | .//w:tblBorders | .//w:tblCellMar | .//w:tcMar"
    ):
        for child in parent:
            if child.tag == qn("w:start"):
                child.tag = qn("w:left")
            elif child.tag == qn("w:end"):
                child.tag = qn("w:right")

    for alignment in root.xpath(".//w:jc"):
        value = alignment.get(qn("w:val"))
        if value == "start":
            alignment.set(qn("w:val"), "left")
        elif value == "end":
            alignment.set(qn("w:val"), "right")


def _ensure_table_grid_borders(table):
    """補上 Google 文件匯入時可穩定辨識的整表格線。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    properties = table._tbl.tblPr
    borders = properties.find(qn("w:tblBorders"))
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        properties.insert_element_before(
            borders, "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook"
        )

    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = borders.find(qn(f"w:{side}"))
        if border is None:
            border = OxmlElement(f"w:{side}")
            borders.append(border)
        border.set(qn("w:val"), "single")
        border.set(qn("w:sz"), "4")
        border.set(qn("w:space"), "0")
        border.set(qn("w:color"), "auto")


def group(exams):
    """回傳 OrderedDict: 學期 -> list of 列 dict。"""
    active = [e for e in exams if e.生效]
    active.sort(
        key=lambda e: (
            str(e.學期), e.指導教授, _date_sort_key(e.日期字串), e.案件鍵
        )
    )
    aggregated = OrderedDict()
    for e in active:
        notes = sorted({
            m.委員備註 for m in e.委員
            if m.委員備註 and m.委員備註 != "指導教授"
        })
        method = str(e.口試方式 or "").strip()
        if method in {"實體", "實體口試"}:
            method = ""
        key = (
            str(e.學期), e.指導教授, e.日期字串,
            method, "；".join(notes),
        )
        row = aggregated.get(key)
        if row is None:
            row = {
                "案件鍵": e.案件鍵,
                "學期": str(e.學期),
                "指導教授": e.指導教授,
                "日期": e.日期字串,
                "時間": "",  # 範本欄位保留，但契約明定不填值。
                "方式": method,
                "備註": "；".join(notes),
                C.PROGRAM_DOCTOR: [], C.PROGRAM_MASTER: [], C.PROGRAM_INSERV: [],
            }
            aggregated[key] = row
        row[e.班別].append(e.姓名)

    out = OrderedDict()
    for row in aggregated.values():
        out.setdefault(row["學期"], []).append(row)
    return out


def generate(master, out_path, template=None, term=None, title=None):
    from docx import Document

    template = template or C.TPL_ATT4
    doc = Document(str(template))
    tbl = doc.tables[0]

    rows = list(tbl.rows)
    section_rows = [row for row in rows[HEADER_ROWS:] if len(_logical_tcs(row)) == 1]
    section_tpl = copy.deepcopy((section_rows[-1] if section_rows else rows[SECTION_ROW])._tr)
    data_tpl = copy.deepcopy(rows[DATA_ROW]._tr)

    # 清掉範本裡的舊資料，只留標題與表頭
    for r in rows[HEADER_ROWS:]:
        tbl._tbl.remove(r._tr)

    grouped = group(master.active(term))
    if title:
        _set_text(tbl.rows[0].cells[0], title)
    else:
        _set_title_year(tbl.rows[0].cells[0], {str(t)[:3] for t in grouped})
        _apply_title_note(tbl.rows[0].cells[0], C.att4_title_note())
    doc_title = generated_title(tbl.rows[0].cells[0])
    written = 0
    indexed_rows = []
    for term_code in sorted(grouped):
        tr = copy.deepcopy(section_tpl)
        tbl._tbl.append(tr)
        _set_text(_logical_tcs(tbl.rows[-1])[0], U.term_year_label(term_code))

        for b in grouped[term_code]:
            tr = copy.deepcopy(data_tpl)
            tbl._tbl.append(tr)
            cells = _logical_tcs(tbl.rows[-1])
            values = [
                b["指導教授"],
                "、".join(b[C.PROGRAM_DOCTOR]),
                "、".join(b[C.PROGRAM_MASTER]),
                "、".join(b[C.PROGRAM_INSERV]),
                b["日期"], b["時間"], b["方式"], b["備註"],
            ]
            for cell, val in zip(cells, values):
                _set_text(cell, val)
            indexed_rows.append((len(tbl.rows) - 1, b))
            written += 1

    _merge_vertical_runs(
        tbl,
        indexed_rows,
        0,
        lambda row: (row["學期"], row["指導教授"]),
    )
    _merge_vertical_runs(
        tbl,
        indexed_rows,
        4,
        lambda row: (row["學期"], row["日期"]) if row["日期"] else None,
    )

    _normalize_directional_ooxml(doc)
    _ensure_table_grid_borders(tbl)
    # 範本的作者、最後修改者與舊標題不得跟著產出文件流出
    scrub_docx(doc, doc_title)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return {"輸出": str(out_path), "資料列": written,
            "學期": list(grouped.keys())}
