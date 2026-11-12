# -*- coding: utf-8 -*-
"""示範用附件4／附件6 範本（koushi.demo_templates）：沒有官方範本的隊員也能跑展示，且內容不含真實資料。"""
from openpyxl import load_workbook

from koushi import demo_templates
from koushi.generators import attachment4, attachment6


def _master():
    from tests.test_generators_schema4 import _master as schema4_master
    return schema4_master()


def test_demo_templates_work_with_the_real_generators(tmp_path):
    att4 = demo_templates.build_attachment4(tmp_path / "附件4.docx")
    att6 = demo_templates.build_attachment6(tmp_path / "附件6.xlsx")
    out4 = tmp_path / "out4.docx"
    attachment4.generate(_master(), out4, template=att4, term="1142")
    assert out4.is_file()
    files = attachment6.generate(_master(), tmp_path / "out6", template=att6, term="1142")
    assert files
    ws = load_workbook(next((tmp_path / "out6").glob("*.xlsx"))).active
    assert ws.cell(attachment6.DATA_START, attachment6.COL["姓名"]).value == "測試委員"


def test_demo_templates_carry_no_identity(tmp_path):
    att4 = demo_templates.build_attachment4(tmp_path / "附件4.docx")
    att6 = demo_templates.build_attachment6(tmp_path / "附件6.xlsx")
    from docx import Document
    props4 = Document(att4).core_properties
    assert props4.author == "" and props4.last_modified_by == ""
    wb = load_workbook(att6)
    assert wb.properties.creator in ("", None)
    assert "示範範本" in wb.active.cell(attachment6.NOTE_ROW, 1).value


def test_ensure_does_not_overwrite_existing_templates(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_templates, "ATT4_DOCX", tmp_path / "附件4.docx")
    monkeypatch.setattr(demo_templates, "ATT6_XLSX", tmp_path / "附件6.xlsx")
    monkeypatch.setattr(demo_templates.C, "TPL_ATT4_OFFICIAL", tmp_path / "官方附件4.doc")
    first = demo_templates.ensure_demo_templates()
    assert all("已產生" in line for line in first)
    (tmp_path / "附件6.xlsx").write_bytes(b"official")   # 假裝是官方檔
    second = demo_templates.ensure_demo_templates()
    assert (tmp_path / "附件6.xlsx").read_bytes() == b"official" and all("已產生" not in line for line in second)
