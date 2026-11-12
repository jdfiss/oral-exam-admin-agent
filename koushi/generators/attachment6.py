# -*- coding: utf-8 -*-
"""附件6 —— 學生口試費印領清冊（Excel）。

同學期、同學位別、同指導教授且同日期的學生形成一份清冊，不需要口試
時間或場次資料。共同委員彙總為一列，交通費在同一份清冊只計一次。

    合計 = min(口試篇數, 上限) × 口試費單價 + 交通費   （交通費一趟只給一次；上限見 config.FEE_PAPER_CAP）

人工做這段最容易漏篇數、也最容易把交通費重複計。
"""
import copy
import re
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter as gcl

from .. import config as C
from .. import util as U
from .doc_properties import scrub_xlsx
from .grouping import aggregate_members, roster_groups

DATA_START = 5           # 範本中第一列資料
DATA_END = 10            # 範本中最後一列資料
TOTAL_ROW = 11           # 「總計：」列
NOTE_ROW = 15            # 「說明：」列

COL = {
    "姓名": 1, "服務單位": 2, "身分證字號": 3, "口試篇數": 4,
    "口試費": 5, "交通費": 6, "合計": 7,
    "指導學生姓名": 8, "指導學生學號": 9,
    "金融機構": 10, "帳號": 11, "戶籍地址": 12, "備註": 13,
}
NCOL = 13

_CN = "〇一二三四五六七八九"


def _cn_digits(s):
    return "".join(_CN[int(ch)] if ch.isdigit() else ch for ch in str(s))


def title_for(term):
    """1142 -> '<校名>   一一四學年度 第  二 學期 口試費印領清冊'（校名取 config.UNIV_NAME）"""
    t = str(term).strip()
    year, half = t[:3], t[3:] or "1"
    return "%s   %s學年度 第  %s 學期 口試費印領清冊" % (
        C.UNIV_NAME, _cn_digits(year), _cn_digits(half))


# 範本說明列「依110年5月31日109學年度第4次校務基金管理委員會修正通過，論文審查口試費：…」
# 的校內會議出處；示範模式拿掉出處，費率文字（審查費、上限、交通費報支）保留。
_REGULATION_SOURCE_RE = re.compile(r"依\s*\d{2,3}\s*年[^，,。]*?(?:委員會|會議|會)[^，,。]*?(?:通過|決議|核定)\s*[，,]?")


def neutral_note(text):
    """拿掉說明列裡特定學校的會議出處；不是字串時原樣回傳。"""
    if not isinstance(text, str):
        return text
    return _REGULATION_SOURCE_RE.sub("", text)


def aggregate_group(master, exams):
    """把同一清冊組依委員強識別彙總成附件6資料列。"""
    rows = []
    for member_row in aggregate_members(master, exams):
        rows.append({
            "姓名": member_row["委員姓名"],
            "服務單位": member_row["服務單位"],
            "身分證字號": member_row["身分證字號"],
            "口試篇數": member_row["口試篇數"],
            "口試費": member_row["口試費"],
            "交通費": member_row["交通費"],
            "合計": member_row["合計"],
            "支領篇數": member_row["支領篇數"],
            "指導學生姓名": member_row["學生"],
            "指導學生學號": member_row["學號"],
            "金融機構": member_row["金融機構"],
            "帳號": member_row["帳號"],
            "戶籍地址": member_row["戶籍地址"],
            "備註": member_row["備註"],
        })
    return rows


def _clear_from(ws, start_row):
    """清掉 start_row 以下的所有內容與合併，準備重排。"""
    for rng in list(ws.merged_cells.ranges):
        if rng.min_row >= start_row:
            ws.unmerge_cells(str(rng))
    if ws.max_row >= start_row:
        for row in ws.iter_rows(min_row=start_row, max_row=ws.max_row):
            for cell in row:
                cell.value = None


_BANK_FIELDS = ("身分證字號", "金融機構", "帳號", "戶籍地址")


def generate_one(rows, term, out_path, template=None, prefill=False):
    template = template or C.TPL_ATT6
    wb = load_workbook(str(template))
    ws = wb[wb.sheetnames[0]]

    # 先把範本的樣式與尾段內容取下來
    data_style = {c: (copy.copy(ws.cell(DATA_START, c)._style)
                      if ws.cell(DATA_START, c).has_style else None)
                  for c in range(1, NCOL + 1)}
    data_height = ws.row_dimensions[DATA_START].height
    total_label = ws.cell(TOTAL_ROW, 1).value
    total_style = {c: (copy.copy(ws.cell(TOTAL_ROW, c)._style)
                       if ws.cell(TOTAL_ROW, c).has_style else None)
                   for c in range(1, NCOL + 1)}
    total_height = ws.row_dimensions[TOTAL_ROW].height
    note_text = ws.cell(NOTE_ROW, 1).value
    note_style = (copy.copy(ws.cell(NOTE_ROW, 1)._style)
                  if ws.cell(NOTE_ROW, 1).has_style else None)
    note_height = ws.row_dimensions[NOTE_ROW].height

    _clear_from(ws, DATA_START)
    capacity = DATA_END - DATA_START + 1
    extra_rows = max(0, len(rows) - capacity)
    if extra_rows:
        ws.insert_rows(TOTAL_ROW, amount=extra_rows)
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 1
        ws.page_setup.scale = None
    total_at = TOTAL_ROW + extra_rows
    note_at = NOTE_ROW + extra_rows

    ws.cell(2, 1).value = title_for(term)

    # 範本固定保留六個資料槽；只有超過六位委員時才向下擴充。
    for r in range(DATA_START, total_at):
        for c in range(1, NCOL + 1):
            if data_style[c] is not None:
                ws.cell(r, c)._style = copy.copy(data_style[c])
        if data_height:
            ws.row_dimensions[r].height = data_height

    for offset, row in enumerate(rows):
        r = DATA_START + offset
        for name, col in COL.items():
            if prefill and name in _BANK_FIELDS:
                ws.cell(r, col).value = None
                continue
            v = row.get(name)
            U.literal_cell(ws.cell(r, col), None if U.blank(v) else v)
        # 篇數超過上限時，公式以上限計（口試篇數欄仍顯示實際篇數）。
        papers = "{d}{r}".format(d=gcl(COL["口試篇數"]), r=r)
        if row.get("支領篇數") is not None and row["支領篇數"] < row["口試篇數"]:
            papers = "MIN({d}{r},{cap})".format(d=gcl(COL["口試篇數"]), r=r, cap=row["支領篇數"])
        ws.cell(r, COL["合計"]).value = "={p}*{e}{r}+{f}{r}".format(
            p=papers, e=gcl(COL["口試費"]), f=gcl(COL["交通費"]), r=r)

    # 共同委員的指導學生相同，依原始範本把 H／I 欄垂直合併。
    run_start = 0
    while run_start < len(rows):
        run_end = run_start
        key = (rows[run_start].get("指導學生姓名"), rows[run_start].get("指導學生學號"))
        while run_end + 1 < len(rows):
            next_key = (
                rows[run_end + 1].get("指導學生姓名"),
                rows[run_end + 1].get("指導學生學號"),
            )
            if next_key != key:
                break
            run_end += 1
        if run_end > run_start:
            for col in (COL["指導學生姓名"], COL["指導學生學號"]):
                ws.merge_cells(
                    start_row=DATA_START + run_start,
                    start_column=col,
                    end_row=DATA_START + run_end,
                    end_column=col,
                )
        run_start = run_end + 1

    for c in range(1, NCOL + 1):
        if total_style[c] is not None:
            ws.cell(total_at, c)._style = copy.copy(total_style[c])
    ws.cell(total_at, 1).value = total_label
    ws.cell(total_at, 2).value = ("=SUM(%s%d:%s%d)" % (
        gcl(COL["合計"]), DATA_START, gcl(COL["合計"]), total_at - 1)
                           if rows else 0)
    if total_height:
        ws.row_dimensions[total_at].height = total_height

    ws.cell(note_at, 1).value = neutral_note(note_text) if C.att6_neutral_note() else note_text
    if note_style is not None:
        ws.cell(note_at, 1)._style = copy.copy(note_style)
    if note_height:
        ws.row_dimensions[note_at].height = note_height
    ws.merge_cells(start_row=note_at, start_column=1,
                   end_row=note_at, end_column=NCOL)

    # 範本的建立者、最後修改者等身分欄位不得跟著產出檔流出
    scrub_xlsx(wb, title_for(term))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    return {"輸出": str(out_path), "委員列": len(rows),
            "總額": sum(x["合計"] for x in rows)}


def _safe(s):
    """檔名用：去掉 Windows 不允許的字元。"""
    return re.sub(r'[\\/:*?"<>|]', "_", str(s)).strip()


def _date_tag(date_txt):
    """'6/18' -> '0618'；'7/9.7/20' -> '0709-0720'；空 -> '未定'"""
    pairs = re.findall(r"(\d{1,2})\s*/\s*(\d{1,2})", str(date_txt or ""))
    if not pairs:
        return _safe(date_txt) or "未定"
    return "-".join("%02d%02d" % (int(m), int(d)) for m, d in pairs)


def generate(master, out_dir, template=None, term=None, prefill=False):
    """一個清冊組一個檔。回傳每份清冊的摘要。

    prefill=True 時產生預填版：帶入已知資訊但銀行四欄留白。

    每次重跑會先清掉上一輪的產出 —— 否則日期或分組改變後，
    舊檔名還留在資料夾裡，系辦會拿到已經作廢的清冊。
    """
    groups = roster_groups(master.active(term))
    out_dir.mkdir(parents=True, exist_ok=True)
    stale_paths = list(out_dir.glob("附件6-*.xlsx"))
    results = []
    generated_paths = set()
    for _group_key, exams in groups.items():
        first = exams[0]
        term_code = str(first.學期)
        advisor = first.指導教授 or "（未指定）"
        date_txt = first.日期字串
        rows = aggregate_group(master, exams)
        fallback = f"-{_safe(_group_key[-1])}" if _group_key[-1] else ""
        fname = "附件6-%s-%s-%s-%s%s.xlsx" % (
            term_code, _safe(first.學位別), _safe(advisor), _date_tag(date_txt), fallback)
        res = generate_one(rows, term_code, out_dir / fname, template, prefill=prefill)
        generated_paths.add((out_dir / fname).resolve())
        res.update({"學期": term_code, "指導教授": advisor, "口試日期": date_txt,
                    "學生數": len(exams)})
        results.append(res)
    for stale in stale_paths:
        if stale.resolve() not in generated_paths:
            stale.unlink()
    return results
