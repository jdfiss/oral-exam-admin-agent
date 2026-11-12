# -*- coding: utf-8 -*-
"""產出的附件4／附件6 不得帶出範本的作者、最後修改者、舊標題等身分資訊（docProps）。

正式範本的 core.xml 有原作者姓名與帳號、舊學年度標題；產出檔若沿用就會一路寄給教授、
放進下載檔。這裡用帶「假身分」的測試範本產檔，再檢查 core.xml／app.xml／custom.xml
不含這些字串，也不含匿名審查禁用詞（tests/test_anonymity.py 的清單）。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook

from koushi import config as C
from koushi.generators import attachment4, attachment6
from tests.test_anonymity import _denylist
from tests.test_generators_schema4 import _attachment4_template, _master

FAKE_IDENTITY = ("範本作者甲 (someone)", "範本修改者乙", "96學年度學位考試一覽表", "某某公司", "某某主管",
                 "範本備註", "範本關鍵字", "範本主旨", "範本類別")
PROPERTY_PARTS = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")


def _properties_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return "\n".join(z.read(n).decode("utf-8") for n in PROPERTY_PARTS if n in z.namelist())


def _assert_clean(path: Path, title: str):
    text = _properties_text(path)
    for secret in FAKE_IDENTITY:
        assert secret not in text, secret
    lowered = text.lower()
    hits = [i + 1 for i, term in enumerate(_denylist()) if term in lowered]
    assert not hits, f"文件屬性含匿名審查禁用詞（第 {hits} 個）"
    for tag in ("dc:creator", "cp:lastModifiedBy"):
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text)
        assert m is None or m.group(1) in ("", C.OFFICE_SIGNATURE), (tag, m and m.group(1))
    m = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", text)
    assert m and m.group(1) == title
    assert "lastPrinted" not in text


def _identity_docx_template(path: Path, title_runs=None):
    _attachment4_template(path)
    doc = Document(str(path))
    if title_runs:
        para = doc.tables[0].rows[0].cells[0].paragraphs[0]
        for r in list(para.runs):
            r._element.getparent().remove(r._element)
        for text in title_runs:
            para.add_run(text)
    cp = doc.core_properties
    cp.author, cp.last_modified_by, cp.title = FAKE_IDENTITY[0], FAKE_IDENTITY[1], FAKE_IDENTITY[2]
    cp.comments, cp.keywords, cp.subject, cp.category = FAKE_IDENTITY[5], FAKE_IDENTITY[6], FAKE_IDENTITY[7], FAKE_IDENTITY[8]
    doc.save(str(path))
    # 補一份帶公司／主管欄位的 app.xml（python-docx 範本沒有 app.xml）
    with zipfile.ZipFile(path) as z:
        items = {n: z.read(n) for n in z.namelist()}
    items["docProps/app.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f'<Application>Word</Application><Company>{FAKE_IDENTITY[3]}</Company>'
        f'<Manager>{FAKE_IDENTITY[4]}</Manager></Properties>').encode("utf-8")
    ct = items["[Content_Types].xml"].decode("utf-8")
    if "/docProps/app.xml" not in ct:
        ct = ct.replace("</Types>", '<Override PartName="/docProps/app.xml" ContentType='
                        '"application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>')
        items["[Content_Types].xml"] = ct.encode("utf-8")
    rels = items["_rels/.rels"].decode("utf-8")
    if "docProps/app.xml" not in rels:
        rels = rels.replace("</Relationships>", '<Relationship Id="rIdApp" Type="http://schemas.openxmlformats.org/'
                            'officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
                            "</Relationships>")
        items["_rels/.rels"] = rels.encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in items.items():
            z.writestr(name, data)


def _identity_xlsx_template(path: Path):
    from openpyxl.packaging.custom import StringProperty
    wb = Workbook()
    ws = wb.active
    ws.cell(11, 1).value = "總計："
    ws.cell(15, 1).value = ("說明：\n一、依110年5月31日109學年度第4次某某委員會修正通過，論文審查口試費：審查碩士班學生一名1000元。\n"
                            "二、交通費依規定辦理。")
    ws.merge_cells("A15:M15")
    p = wb.properties
    p.creator, p.lastModifiedBy, p.title = FAKE_IDENTITY[0], FAKE_IDENTITY[1], FAKE_IDENTITY[2]
    p.description, p.keywords, p.subject, p.category = FAKE_IDENTITY[5], FAKE_IDENTITY[6], FAKE_IDENTITY[7], FAKE_IDENTITY[8]
    wb.custom_doc_props.append(StringProperty(name="Owner", value=FAKE_IDENTITY[0]))
    wb.save(path)


def test_attachment4_overwrites_template_core_and_app_properties(tmp_path, monkeypatch):
    monkeypatch.delenv("KOUSHI_DEMO_ROOT", raising=False)
    monkeypatch.delenv("KOUSHI_ATT4_TITLE_NOTE", raising=False)
    template = tmp_path / "att4.docx"
    _identity_docx_template(template)
    out = tmp_path / "out.docx"
    attachment4.generate(_master(), out, template=template)
    _assert_clean(out, "114學年度學位考試一覽表")
    assert "Company" not in _properties_text(out) and "Manager" not in _properties_text(out)


def test_attachment6_overwrites_template_core_and_custom_properties(tmp_path, monkeypatch):
    monkeypatch.delenv("KOUSHI_DEMO_ROOT", raising=False)
    monkeypatch.delenv("KOUSHI_ATT6_NEUTRAL_NOTE", raising=False)
    template = tmp_path / "att6.xlsx"
    _identity_xlsx_template(template)
    summaries = attachment6.generate(_master(), tmp_path / "out", template=template)
    assert summaries
    for s in summaries:
        _assert_clean(Path(s["輸出"]), re.sub(r"\s+", " ", attachment6.title_for("1142")).strip())


def test_title_note_and_regulation_source_follow_mode(tmp_path, monkeypatch):
    """正式模式照範本；示範模式拿掉附件4 班別提醒與附件6 會議出處；環境變數可明確指定。"""
    from openpyxl import load_workbook
    template = tmp_path / "att4.docx"
    _identity_docx_template(template, ["114", "學年度學位考試一覽表          ", "甲碩士班", "/", "乙碩士班"])
    xlsx = tmp_path / "att6.xlsx"
    _identity_xlsx_template(xlsx)

    def run(tag):
        out = tmp_path / f"{tag}.docx"
        attachment4.generate(_master(), out, template=template)
        title = Document(str(out)).tables[0].rows[0].cells[0].text
        attachment6.generate(_master(), tmp_path / tag, template=xlsx)
        note = load_workbook(next((tmp_path / tag).glob("*.xlsx"))).active.cell(15, 1).value
        return title, note, _properties_text(out)

    for key in ("KOUSHI_DEMO_ROOT", "KOUSHI_ATT4_TITLE_NOTE", "KOUSHI_ATT6_NEUTRAL_NOTE"):
        monkeypatch.delenv(key, raising=False)
    title, note, _ = run("prod")
    assert "甲碩士班/乙碩士班" in title and "某某委員會" in note

    monkeypatch.setenv("KOUSHI_DEMO_ROOT", str(tmp_path / "demo"))
    title, note, props = run("demo")
    assert title == "114學年度學位考試一覽表"
    assert "某某委員會" not in note and "論文審查口試費：審查碩士班學生一名1000元" in note and "二、交通費" in note
    assert "<dc:title>114學年度學位考試一覽表</dc:title>" in props

    monkeypatch.setenv("KOUSHI_ATT4_TITLE_NOTE", "碩士班／博士班")
    monkeypatch.setenv("KOUSHI_ATT6_NEUTRAL_NOTE", "0")
    title, note, _ = run("explicit")
    assert title.endswith("碩士班／博士班") and "某某委員會" in note


_REAL_ATT4 = Path(C.TPL_ATT4)
_REAL_ATT6 = Path(C.TPL_ATT6)


@pytest.mark.skipif(not (_REAL_ATT4.is_file() and _REAL_ATT6.is_file()), reason="正式範本不在版控內（resource/template）")
def test_real_templates_do_not_leak_properties(tmp_path, monkeypatch):
    from koushi.document_conversion import docx_template, find_soffice
    monkeypatch.delenv("KOUSHI_DEMO_ROOT", raising=False)
    summaries = attachment6.generate(_master(), tmp_path / "a6", template=_REAL_ATT6)
    for s in summaries:
        _assert_clean(Path(s["輸出"]), re.sub(r"\s+", " ", attachment6.title_for("1142")).strip())
    if find_soffice() is None:
        pytest.skip("找不到 LibreOffice，略過附件4 .doc 範本")
    with docx_template(_REAL_ATT4) as tpl:
        attachment4.generate(_master(), tmp_path / "a4.docx", template=tpl)
    _assert_clean(tmp_path / "a4.docx", "114學年度學位考試一覽表")
