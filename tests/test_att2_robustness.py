# -*- coding: utf-8 -*-
"""附件2 的資料問題逐案列為例外，不讓一筆 Portal 資料擋住整批建檔（review-2026-09-23 P0-3）。"""
import openpyxl

from koushi import ingest, validation
from koushi.registry import CommitteeRegistry

from tests.att2_fixture import HEADERS


def _write(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def _row(sid, kind, signed, name, unit, title="教授", status="電子表單簽核完成"):
    return ["1141", "資電學院", "資訊工程學系碩士班", sid, "學生" + sid, "否", kind, status,
            name, unit, title, "", "", "符合各學位考試委員職級之教師或院士", "", signed]


def _codes(issues):
    return {i["問題代碼"] for i in issues}


def test_latest_portal_record_wins_and_is_flagged(tmp_path):
    path = _write(tmp_path / "a.xlsx", [
        _row("A", "學位考試申請", "2026-07-01 10:00:00", "舊委員", "甲大學"),
        _row("A", "考試委員異動", "2026-08-01 10:00:00", "新委員", "乙大學"),
    ])
    exams, issues = ingest.read_att2(CommitteeRegistry(), path)
    exam = exams["1141-A"]
    assert exam.案件種類 == "考試委員異動"
    assert [m.服務單位 for m in exam.委員] == ["乙大學"]
    flagged = next(i for i in issues if i["問題代碼"] == "ATTACHMENT_2_MULTIPLE_RECORDS")
    assert flagged["責任"] == validation.OWNER_OFFICE and flagged["阻擋產出"] == "否"


def test_latest_portal_record_compares_text_dates_as_dates(tmp_path):
    """文字日期不可直接字串比：'2026/10/01' 比 '2026/9/30' 新。"""
    path = _write(tmp_path / "a.xlsx", [
        _row("A", "考試委員異動", "2026/10/01 09:00", "新委員", "乙大學"),
        _row("A", "學位考試申請", "2026/9/30 17:00", "舊委員", "甲大學"),
    ])
    exams, _issues = ingest.read_att2(CommitteeRegistry(), path)
    assert exams["1141-A"].案件種類 == "考試委員異動"
    assert [m.服務單位 for m in exams["1141-A"].委員] == ["乙大學"]


def test_latest_cancel_record_makes_case_inactive(tmp_path):
    path = _write(tmp_path / "a.xlsx", [
        _row("A", "學位考試申請", "2026-07-01 10:00:00", "委員", "甲大學"),
        _row("A", "學位考試撤銷", "2026-08-01 10:00:00", "委員", "甲大學"),
    ])
    exams, _issues = ingest.read_att2(CommitteeRegistry(), path)
    assert exams["1141-A"].生效 is False


def test_bad_rows_are_case_exceptions_not_fatal(tmp_path):
    path = _write(tmp_path / "a.xlsx", [
        _row("A", "學位考試申請", "2026-07-01", "只有姓名", ""),       # 單位缺
        _row("A", "學位考試申請", "2026-07-01", "陳委員", "甲大學"),
        _row("A", "學位考試申請", "2026-07-01", "陳委員", "甲大學"),    # 重複
        _row("", "學位考試申請", "2026-07-01", "某委員", "丙大學"),     # 缺學號
    ])
    exams, issues = ingest.read_att2(CommitteeRegistry(), path)
    assert len(exams["1141-A"].委員) == 1
    codes = _codes(issues)
    assert {"ATTACHMENT_2_MISSING_COMMITTEE_IDENTITY", "DUPLICATE_ATTACHMENT_2_COMMITTEE",
            "ATTACHMENT_2_MISSING_CASE_KEY"} <= codes
    from koushi.workflow import FATAL_INGEST_CODES
    assert not codes & FATAL_INGEST_CODES
    assert all(i["責任"] == validation.OWNER_OFFICE for i in issues)


def test_portal_thesis_title_is_read_when_column_exists(tmp_path):
    """附件2 若有論文題目欄，以 Portal 題目為準（決策二-1）；目前匯出沒有此欄時留給介面填。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS + ["論文題目"])
    ws.append(_row("A", "學位考試申請", "2026-07-01 10:00:00", "委員", "甲大學") + ["Portal 題目"])
    wb.save(tmp_path / "a.xlsx")
    exams, _issues = ingest.read_att2(CommitteeRegistry(), tmp_path / "a.xlsx")
    assert exams["1141-A"].論文題目 == "Portal 題目"
