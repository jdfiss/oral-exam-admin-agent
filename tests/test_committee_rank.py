# -*- coding: utf-8 -*-
"""委員職級規定：碩士需助理教授以上、博士需副教授以上；不符只標記給專員，不阻擋。"""
import pytest

from koushi import validation
from koushi.model import Committee, Exam, Master, Member


@pytest.mark.parametrize("degree, title, flagged", [
    ("碩", "助理教授", False),
    ("碩", "教授", False),
    ("碩", "講師", True),
    ("博", "助理教授", True),
    ("博", "副教授", False),
    ("博", "Associate Professor", False),
    ("博", "Assistant Professor", True),
    ("博", "副研究員", False),
    ("碩", "中央研究院院士", False),
    ("碩", "兼任副教授", True),   # 修飾詞需人工判斷
    ("碩", "技術長", True),       # 業界職稱無法自動判斷
    ("碩", "", True),
])
def test_rank_exception_by_title(degree, title, flagged):
    assert bool(validation.rank_exception(degree, Member(職稱=title))) is flagged


def test_portal_qualification_marked_eligible_is_trusted():
    member = Member(職稱="技術長", 委員資格="符合各學位考試委員職級之教師或院士")
    assert validation.rank_exception("博", member) == ""


def test_rank_exception_is_office_owned_and_non_blocking():
    exam = Exam(
        學期="1141", 學號="A", 姓名="學生A", 系所="資訊工程學系博士班", 指導教授="教授甲",
        委員=[Member(committee_id="C1", 服務單位="國立範例大學資訊工程學系", 職稱="助理教授")],
    ).derive()
    master = Master(exams=[exam], committees={"C1": Committee(committee_id="C1", 委員姓名="委員甲")})
    issue = next(i for i in validation.check(master) if i["問題代碼"] == "COMMITTEE_RANK_EXCEPTION")
    assert issue["責任"] == validation.OWNER_OFFICE
    assert issue["阻擋產出"] == "否"
    assert "副教授以上" in issue["說明"]


def test_exception_message_distinguishes_portal_special_qualification():
    special = Member(職稱="客座教授", 委員資格="屬稀少或特殊性考試委員，符合本系(所)資格認定基準")
    message = validation.rank_exception("碩", special)
    assert message.startswith("［Portal 特殊資格］") and "屬稀少或特殊性" in message
    plain = validation.rank_exception("博", Member(職稱="助理教授"))
    assert plain.startswith("［職稱未達或無法判斷］") and "副教授以上" in plain
