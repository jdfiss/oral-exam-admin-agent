# -*- coding: utf-8 -*-
"""產出文件的中繼資料（docProps）一律覆寫，不沿用範本。

附件4／附件6 是拿系辦既有檔案當範本產生的，範本的 core.xml 帶著原作者、最後修改者
（含個人姓名與帳號）與多年前的標題；不覆寫的話，每份產出的文件都會把這些身分資訊
一起寄出去（比賽匿名審查與個資都不允許）。這裡把作者類欄位換成系辦署名
（config.OFFICE_SIGNATURE，可為空）、標題換成本次產生的標題，其餘描述性欄位清空。
"""
from __future__ import annotations

import datetime as _dt
import re

from .. import clock
from .. import config as C

_APP_IDENTITY_TAGS = ("Company", "Manager", "HyperlinkBase")
_EMPTY_CUSTOM = (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                 b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
                 b'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>')


def signature() -> str:
    return str(getattr(C, "OFFICE_SIGNATURE", "") or "").strip()


def _now() -> _dt.datetime:
    """UTC、不帶時區（docx／xlsx 寫檔時都會標成 Z）。"""
    return clock.now().astimezone(_dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def _clean_title(title) -> str:
    return re.sub(r"\s+", " ", str(title or "")).strip()


def scrub_app_xml(blob: bytes) -> bytes:
    """清掉 app.xml 裡的公司、主管等欄位（保留應用程式名稱與版本）。"""
    text = blob.decode("utf-8")
    for tag in _APP_IDENTITY_TAGS:
        text = re.sub(rf"<{tag}>.*?</{tag}>|<{tag}\s*/>", "", text, flags=re.S)
    return text.encode("utf-8")


def scrub_docx(document, title) -> None:
    """python-docx Document：覆寫 core.xml，清掉 app.xml 的公司欄位與 custom.xml 的自訂屬性。"""
    from docx.oxml.ns import qn

    sig = signature()
    cp = document.core_properties
    cp.author = sig
    cp.last_modified_by = sig
    cp.title = _clean_title(title)
    for attr in ("subject", "comments", "keywords", "category", "content_status", "identifier", "version"):
        setattr(cp, attr, "")
    cp.revision = 1
    cp.modified = _now()
    element = cp._element
    for child in list(element):
        if child.tag == qn("cp:lastPrinted"):
            element.remove(child)
    for part in document.part.package.iter_parts():
        name = str(part.partname)
        if name == "/docProps/app.xml" and hasattr(part, "_blob"):
            part._blob = scrub_app_xml(part.blob)
        elif name == "/docProps/custom.xml" and hasattr(part, "_blob"):
            part._blob = _EMPTY_CUSTOM


def scrub_xlsx(workbook, title) -> None:
    """openpyxl Workbook：覆寫核心屬性並清掉自訂屬性（openpyxl 存檔時 app.xml 會重寫，不帶公司欄位）。"""
    sig = signature()
    props = workbook.properties
    props.creator = sig
    props.lastModifiedBy = sig
    props.title = _clean_title(title)
    for attr in ("subject", "description", "keywords", "category", "identifier", "contentStatus", "version"):
        if hasattr(props, attr):
            setattr(props, attr, None)
    props.lastPrinted = None
    props.modified = _now()
    if hasattr(workbook, "custom_doc_props"):
        from openpyxl.packaging.custom import CustomPropertyList
        workbook.custom_doc_props = CustomPropertyList()
