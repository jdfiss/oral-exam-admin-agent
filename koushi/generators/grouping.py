# -*- coding: utf-8 -*-
"""附件6 的清冊分組與委員彙總規則（附件5 已取消）。"""

from __future__ import annotations

from collections import OrderedDict

from .. import config as C
from .. import util as U


def roster_key(exam):
    """同學期、學位別、指導教授、日期為同一清冊組；不使用口試時間。

    指導教授或日期缺少時，不可把多個不完整案件誤併，退回案件鍵隔離。
    """
    fallback = "" if exam.指導教授 and exam.日期字串 else exam.案件鍵
    return (
        str(exam.學期),
        exam.學位別,
        exam.指導教授,
        exam.日期字串,
        fallback,
    )


def roster_groups(exams):
    """回傳有序的「清冊鍵 -> 案件列表」。"""
    active = [exam for exam in exams if exam.生效 and exam.委員]
    active.sort(key=lambda exam: (*roster_key(exam), exam.案件鍵))
    groups = OrderedDict()
    for exam in active:
        groups.setdefault(roster_key(exam), []).append(exam)
    return groups


def key_text(key) -> str:
    return "|".join(str(part) for part in key)


def _merge_value(current, incoming, *, field, committee_id):
    if U.blank(incoming):
        return current
    if U.blank(current):
        return incoming
    if current != incoming:
        raise ValueError(
            f"同一清冊組的委員欄位衝突：{committee_id} {field}={current!r}/{incoming!r}"
        )
    return current


def aggregate_members(master, exams):
    """依委員強識別彙總同一清冊組，交通費只計一次。"""
    aggregated = {}
    order = []
    for exam in exams:
        for member in exam.委員:
            committee = master.committee_of(member)
            if committee is None:
                raise ValueError(f"清冊找不到委員編號：{member.committee_id}")
            committee_id = member.committee_id
            rate = member.口試費 if member.口試費 is not None else exam.費率
            if C.ADVISOR_EXAM_FEE_EXEMPT and member.是否指導教授:
                rate = 0
            if committee_id not in aggregated:
                aggregated[committee_id] = {
                    "committee_id": committee_id,
                    "委員姓名": member.委員姓名 or committee.委員姓名,
                    "服務單位": U.display_unit(member.服務單位 or committee.服務單位),
                    "職稱": member.職稱,
                    "委員資格": member.委員資格,
                    "身分證字號": committee.身分證字號,
                    "口試費": rate,
                    "交通費": member.交通費,
                    "金融機構": committee.金融機構,
                    "帳號": committee.帳號,
                    "戶籍地址": committee.戶籍地址,
                    "學生": [],
                    "學號": [],
                    "案件鍵": [],
                    "備註": [],
                    "順序": len(order),
                }
                order.append(committee_id)
            row = aggregated[committee_id]
            for field, incoming in (
                ("服務單位", U.display_unit(member.服務單位 or committee.服務單位)),
                ("職稱", member.職稱),
                ("委員資格", member.委員資格),
                ("口試費", rate),
                ("交通費", member.交通費),
            ):
                row[field] = _merge_value(
                    row[field], incoming, field=field, committee_id=committee_id
                )
            row["學生"].append(exam.姓名)
            row["學號"].append(str(exam.學號))
            row["案件鍵"].append(exam.案件鍵)
            for note in (member.交通費說明, member.委員備註):
                if note and note not in row["備註"]:
                    row["備註"].append(note)

    rows = []
    for committee_id in order:
        row = aggregated[committee_id]
        row["口試篇數"] = len(row["案件鍵"])
        row["學生"] = "    ".join(row["學生"])
        row["學號"] = " ".join(row["學號"])
        row["案件鍵"] = " ".join(row["案件鍵"])
        row["備註"] = "；".join(row["備註"])
        cap = C.FEE_PAPER_CAP.get(exams[0].學位別) if exams else None
        paid_papers = min(row["口試篇數"], cap) if cap else row["口試篇數"]
        if paid_papers < row["口試篇數"]:
            row["備註"] = "；".join(filter(None, [row["備註"], f"口試費以 {cap} 名為上限"]))
        row["支領篇數"] = paid_papers
        row["合計"] = paid_papers * (row["口試費"] or 0) + (row["交通費"] or 0)
        rows.append(row)
    rows.sort(key=lambda row: (-row["口試篇數"], row["順序"]))
    return rows
