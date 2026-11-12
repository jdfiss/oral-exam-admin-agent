# -*- coding: utf-8 -*-
"""Phase 1 tests: validation refinement, two-phase generation, CLI --phase."""

from koushi.model import Committee, Exam, Master, Member
from koushi import validation


def _exam(key_suffix="A", advisor="王教授", has_att3=True, committee_ids=None):
    """快速建一筆測試案件。"""
    members = []
    for cid in (committee_ids or ["C0001"]):
        members.append(Member(committee_id=cid, 服務單位="台灣大學", 職稱="教授",
                              委員資格="校外委員", 口試費=1000))
    e = Exam(
        學期="1142", 學號=f"111502{key_suffix}", 姓名=f"學生{key_suffix}",
        學院="工學院", 系所="資訊工程學系碩士班", 案件種類="學位考試申請",
        審核狀態="簽核完成", 有附件2=True, 有附件3=has_att3, 介面委員已填=has_att3,
        指導教授=advisor, 委員=members,
    )
    e.derive()
    return e


def _committee(cid="C0001", bank_complete=True):
    return Committee(
        committee_id=cid, 委員姓名="張委員", 服務單位="台灣大學",
        身分證字號="A123456789" if bank_complete else "",
        金融機構="台銀" if bank_complete else "",
        帳號="12345" if bank_complete else "",
        戶籍地址="台北市" if bank_complete else "",
        Email="zhang@example.org",
    )


def _master(exams, committees):
    return Master(exams=exams, committees=committees)


# ── 1-1 validation 新欄位 ────────────────────────────

class TestValidationBlockFields:
    def test_fatal_issue_blocks_both(self):
        exam = _exam(advisor="")
        master = _master([exam], {"C0001": _committee()})
        issues = validation.check(master)
        advisor_issue = next(i for i in issues if i["問題代碼"] == "ADVISOR_SOURCE_UNAVAILABLE")
        assert advisor_issue["阻擋附件4"] == "是"
        assert advisor_issue["阻擋附件6"] == "是"
        assert advisor_issue["阻擋產出"] == "是"

    def test_missing_bank_blocks_only_att56(self):
        exam = _exam()
        master = _master([exam], {"C0001": _committee(bank_complete=False)})
        issues = validation.check(master)
        bank_issue = next(i for i in issues if i["問題代碼"] == "MISSING_BANK_INFO")
        assert bank_issue["阻擋附件4"] == "否"
        assert bank_issue["阻擋附件6"] == "是"
        assert bank_issue["阻擋產出"] == "是"

    def test_no_distance_blocks_only_att56(self):
        exam = _exam()
        for m in exam.委員:
            m.交通費 = None
        master = _master([exam], {"C0001": _committee()})
        issues = validation.check(master)
        dist_issue = next(i for i in issues if i["問題代碼"] == "NO_DISTANCE")
        assert dist_issue["阻擋附件4"] == "否"
        assert dist_issue["阻擋附件6"] == "是"

    def test_warning_blocks_neither(self):
        exam = _exam(has_att3=False)
        master = _master([exam], {"C0001": _committee()})
        issues = validation.check(master)
        att3_issue = next(i for i in issues if i["問題代碼"] == "MISSING_SUBMISSION")
        assert att3_issue["阻擋附件4"] == "否"
        assert att3_issue["阻擋附件6"] == "否"


class TestBlockedKeyFilters:
    def test_attachment4_blocked_excludes_fatal_only(self):
        e1 = _exam(key_suffix="A", advisor="")
        e2 = _exam(key_suffix="B")
        master = _master([e1, e2], {"C0001": _committee(bank_complete=False)})
        issues = validation.check(master)
        att4_blocked = validation.attachment4_blocked_keys(issues)
        att56_blocked = validation.attachment6_blocked_keys(issues)
        assert e1.案件鍵 in att4_blocked
        assert e2.案件鍵 not in att4_blocked
        assert e2.案件鍵 in att56_blocked

    def test_clean_case_not_blocked(self):
        exam = _exam()
        for m in exam.委員:
            m.交通費 = 100
        master = _master([exam], {"C0001": _committee()})
        issues = validation.check(master)
        assert exam.案件鍵 not in validation.attachment4_blocked_keys(issues)
        assert exam.案件鍵 not in validation.attachment6_blocked_keys(issues)


class TestIssueResponsibility:
    def test_issues_have_responsibility_field(self):
        exam = _exam(advisor="", has_att3=False)
        master = _master([exam], {"C0001": _committee(bank_complete=False)})
        issues = validation.check(master)
        for issue in issues:
            assert "責任" in issue
