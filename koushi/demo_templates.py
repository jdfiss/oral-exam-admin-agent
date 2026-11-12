# -*- coding: utf-8 -*-
"""產生「示範用」附件4／附件6 範本：版面照官方範本、內容全部虛構，不含任何真實資料。

官方範本含真實師生資料，不能進 git，也不該在隊員之間到處傳。展示、截圖、錄影只要這兩份示範範本：

    .venv\\Scripts\\python.exe -m koushi.demo_templates

寫到 resource/template/。已經有官方範本時**不覆蓋**（正式環境一律用官方範本）。
附件4 寫成 .docx（不需 LibreOffice 轉檔）；config.TPL_ATT4 在找不到官方 .doc 時會自動改用它。
"""
from __future__ import annotations

from pathlib import Path

from . import config as C

ATT4_DOCX = C.TPL_DIR / "附件4-學位口試一覽.docx"
ATT6_XLSX = C.TPL_DIR / "附件6-學生口試費清冊.xlsx"
DEMO_NOTE = ("說明：\n一、論文審查口試費：碩士班學生每名 1000 元、博士班每名 2000 元；同一委員同一清冊支領篇數設有上限。\n"
             "二、交通費依距離表與規定辦理；視訊出席不支給交通費。\n三、本表為示範範本，內容皆為虛構。")


def build_attachment4(path: Path) -> Path:
    """一張表：標題列、表頭兩列、學期分段列（底色）、資料列；與官方範本同樣的列結構。"""
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    document = Document()
    table = document.add_table(rows=7, cols=8)
    table.style = "Table Grid"
    table.cell(0, 0).merge(table.cell(0, 7)).text = "115學年度學位考試一覽表"
    table.cell(1, 1).merge(table.cell(1, 3)).text = "口試學生"
    for column, value in enumerate(("指導教授", "博士班", "碩士一般", "碩士專班", "口試日期", "時間", "方式", "備註")):
        table.cell(2, column).text = value
    for row, fill, label in ((3, "FFCC99", "舊學期列"), (5, "F6C5AC", "主資料學期列")):
        cell = table.cell(row, 0).merge(table.cell(row, 7))
        cell.text = label
        shading = OxmlElement("w:shd")
        shading.set(qn("w:fill"), fill)
        cell._tc.get_or_add_tcPr().append(shading)
    for paragraph in table.rows[0].cells[0].paragraphs:
        for run in paragraph.runs:
            run.font.size = Pt(16)
            run.font.bold = True
    props = document.core_properties
    props.author = props.last_modified_by = ""
    props.title = "學位考試一覽表（示範範本）"
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    return path


def build_attachment6(path: Path) -> Path:
    """第 2 列標題、第 4 列欄名、第 5–10 列資料、第 11 列總計、第 15 列說明；欄位順序同 generators.attachment6.COL。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, Side

    from .generators.attachment6 import COL, DATA_END, DATA_START, NCOL, NOTE_ROW, TOTAL_ROW

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    thin = Side(style="thin", color="000000")
    grid = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=NCOL)
    ws.cell(2, 1).value = "示範大學   一一五學年度 第  一 學期 口試費印領清冊"
    ws.cell(2, 1).font = Font(size=16, bold=True)
    ws.cell(2, 1).alignment = center
    for name, col in COL.items():
        cell = ws.cell(4, col, name)
        cell.font = Font(bold=True)
        cell.alignment = center
        cell.border = grid
    for r in range(DATA_START, DATA_END + 1):
        for c in range(1, NCOL + 1):
            ws.cell(r, c).border = grid
            ws.cell(r, c).alignment = center
        ws.row_dimensions[r].height = 28
    ws.cell(TOTAL_ROW, 1).value = "總計："
    for c in range(1, NCOL + 1):
        ws.cell(TOTAL_ROW, c).border = grid
    ws.merge_cells(start_row=NOTE_ROW, start_column=1, end_row=NOTE_ROW, end_column=NCOL)
    ws.cell(NOTE_ROW, 1).value = DEMO_NOTE
    ws.cell(NOTE_ROW, 1).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[NOTE_ROW].height = 80
    for col, width in zip("ABCDEFGHIJKLM", (12, 26, 14, 8, 10, 10, 10, 12, 12, 14, 18, 26, 18)):
        ws.column_dimensions[col].width = width
    wb.properties.creator = wb.properties.lastModifiedBy = ""
    wb.properties.title = "口試費印領清冊（示範範本）"
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


def ensure_demo_templates() -> list[str]:
    """缺什麼補什麼；官方範本已在就不動。回傳每一份的處理結果。"""
    messages = []
    if Path(C.TPL_ATT4_OFFICIAL).is_file():
        messages.append(f"附件4：已有官方範本 {C.TPL_ATT4_OFFICIAL.name}，不產生示範範本")
    elif ATT4_DOCX.is_file():
        messages.append(f"附件4：示範範本已存在 {ATT4_DOCX}")
    else:
        messages.append(f"附件4：已產生示範範本 {build_attachment4(ATT4_DOCX)}")
    if ATT6_XLSX.is_file():
        messages.append(f"附件6：範本已存在 {ATT6_XLSX}（不覆蓋）")
    else:
        messages.append(f"附件6：已產生示範範本 {build_attachment6(ATT6_XLSX)}")
    return messages


def main() -> None:
    for line in ensure_demo_templates():
        print(line)
    print("完成。這兩份是虛構的示範範本，可用於展示、截圖與錄影；正式環境請改放官方範本。")


if __name__ == "__main__":
    main()
