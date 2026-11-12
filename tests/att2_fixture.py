# -*- coding: utf-8 -*-
"""測試用附件2（Portal 匯出）產生器：案件只從附件2 來，測試要先有附件2。"""

from __future__ import annotations

from pathlib import Path

import openpyxl

HEADERS = [
    "學年度 / 學年學期", "學院", "系所", "學號", "姓名", "題目含特殊字", "案件種類",
    "審核狀態", "委員姓名", "委員服務單位", "委員職稱", "最高學歷學校名稱",
    "最高學歷學位名稱", "委員資格", "備註", "簽核完成時間",
]

DEFAULT_COMMITTEE = {"name": "陳大文", "unit": "國立範例大學資訊工程學系", "title": "教授"}


def case(term="1141", student_id="111900001", name="王小明",
         department="資訊工程學系碩士班", committees=None, status="電子表單簽核完成"):
    return {
        "term": term, "student_id": student_id, "name": name, "department": department,
        "committees": [DEFAULT_COMMITTEE] if committees is None else committees,
        "status": status,
    }


def write_att2(path: Path, cases: list[dict]) -> Path:
    """一案一委員一列；委員為空的案件仍寫一列（委員欄空白），比照 Portal 非必填。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HEADERS)
    for item in cases:
        base = [item["term"], "資電學院", item["department"], item["student_id"], item["name"],
                "否", "學位考試申請", item["status"]]
        members = item["committees"] or [{}]
        for member in members:
            ws.append(base + [
                member.get("name", ""), member.get("unit", ""), member.get("title", ""),
                member.get("school", ""), member.get("degree", ""),
                member.get("qualification", ""), "", "2026-07-14 14:39:48",
            ])
    wb.save(path)
    return path
