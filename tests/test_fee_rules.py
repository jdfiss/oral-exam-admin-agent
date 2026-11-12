# -*- coding: utf-8 -*-
"""口試費規則（review-2026-09-23 P0-4、P1-17、P1-18）。"""
import datetime as dt

from openpyxl import load_workbook

from koushi import config as C
from koushi.generators import attachment6
from koushi.generators.grouping import aggregate_members
from koushi.model import Committee, Exam, Master, Member


def _master(n_students, dept="資訊工程學系碩士班", unit="遠方大學", advisor_is_member=False):
    committee = Committee(committee_id="C1", 委員姓名="林委員" if not advisor_is_member else "林指導",
                          服務單位=unit, 身分證字號="A1", 金融機構="銀行", 帳號="1", 戶籍地址="某地")
    exams = []
    for i in range(n_students):
        exams.append(Exam(
            學期="1141", 學號=f"S{i}", 姓名=f"學生{i}", 系所=dept, 指導教授="林指導",
            口試日期=dt.date(2026, 11, 1), 有附件2=True,
            委員=[Member(committee_id="C1", 服務單位=unit, 交通費=300,
                         是否指導教授=advisor_is_member)],
        ).derive())
    return Master(exams=exams, committees={"C1": committee})


def test_master_fee_capped_at_three_papers():
    master = _master(4)
    (row,) = aggregate_members(master, master.exams)
    assert row["口試篇數"] == 4
    assert row["支領篇數"] == 3
    assert row["合計"] == 3 * 1000 + 300
    assert "上限" in row["備註"]


def test_doctor_cap_is_six():
    master = _master(7, dept="資訊工程學系博士班")
    (row,) = aggregate_members(master, master.exams)
    assert row["合計"] == 6 * 2000 + 300


def test_under_cap_unchanged():
    master = _master(2)
    (row,) = aggregate_members(master, master.exams)
    assert row["合計"] == 2 * 1000 + 300 and row["支領篇數"] == 2


def test_attachment6_sheet_formula_and_total_respect_cap(tmp_path):
    master = _master(4)
    summaries = attachment6.generate(master, tmp_path / "out", template=C.TPL_ATT6)
    assert summaries[0]["總額"] == 3300
    ws = load_workbook(summaries[0]["輸出"]).active
    formulas = [c.value for row in ws.iter_rows() for c in row
                if isinstance(c.value, str) and c.value.startswith("=") and "MIN(" in c.value]
    assert formulas, "超過上限時合計公式應以 MIN 取上限"


def test_advisor_fee_exemption_toggle(monkeypatch):
    master = _master(1, advisor_is_member=True)
    (row,) = aggregate_members(master, master.exams)
    assert row["合計"] == 1000 + 300  # 預設：維持原行為
    monkeypatch.setattr(C, "ADVISOR_EXAM_FEE_EXEMPT", True)
    (row,) = aggregate_members(master, master.exams)
    assert row["合計"] == 300


def test_home_university_exemption_toggle(monkeypatch):
    committee = Committee(committee_id="C1", 委員姓名="他系委員", 服務單位="國立範例大學數學系")
    assert committee.撥帳齊全 is False  # 預設：他系仍需撥帳資料
    monkeypatch.setattr(C, "HOME_UNIV_FEE_EXEMPT", True)
    assert committee.撥帳齊全 is True
