# -*- coding: utf-8 -*-
"""案件只從附件2（Portal 匯出）來；學生介面只補附件 3 資料（2026-09-23 定案）。

`ingest.read_student_submissions` 只把介面填寫併入附件2 已有的案件：
口試日期時地、指導教授、委員名單與 E-mail。學期／姓名／系所與委員職稱等
以附件2 為準；委員撥帳個資不經學生。找不到案件、委員名單對不上都列為
例外交給專員，不中斷整批建檔。學生送出即有效，不經系辦審核。
"""
import json

from koushi import config as C
from koushi import ingest, validation
from koushi.model import Master
from koushi.registry import CommitteeRegistry

from tests.att2_fixture import case, write_att2


def _write_submission(tmp_path, name="a.json", **overrides):
    data = {
        "term": "1141",
        "student_id": "111900001",
        "thesis_title": "測試論文題目",
        "exam_date": "2026-06-01",
        "exam_mode": "",
        "advisors": ["林指導"],
        "committees": [
            {"name": "陳大文", "unit": "國立範例大學資訊工程學系", "title": "教授",
             "email": "dawen@example.org"},
        ],
    }
    data.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _load(tmp_path, cases, submissions):
    registry = CommitteeRegistry()
    exams, issues = ingest.read_att2(registry, write_att2(tmp_path / "att2.xlsx", cases))
    issues += ingest.read_student_submissions(exams, registry, submissions)
    return exams, issues, registry


def _codes(issues):
    return {i["問題代碼"] for i in issues}


class TestCaseSource:
    def test_submission_merges_into_portal_case(self, tmp_path):
        exams, issues, _ = _load(tmp_path, [case()], [_write_submission(tmp_path)])

        exam = exams["1141-111900001"]
        assert exam.姓名 == "王小明"            # 來自附件2
        assert exam.系所 == "資訊工程學系碩士班"
        assert exam.論文題目 == "測試論文題目"   # 來自介面
        assert exam.指導教授 == "林指導"
        assert exam.有附件2 and exam.有附件3
        assert "MISSING_SUBMISSION" not in _codes(issues)

    def test_submission_without_portal_case_is_exception_not_case(self, tmp_path):
        exams, issues, _ = _load(
            tmp_path, [case(student_id="111900999")], [_write_submission(tmp_path)],
        )
        assert "1141-111900001" not in exams
        exception = next(i for i in issues if i["問題代碼"] == "SUBMISSION_WITHOUT_PORTAL_CASE")
        assert exception["責任"] == validation.OWNER_OFFICE
        assert exception["阻擋產出"] == "否"

    def test_portal_case_without_submission_flags_missing_submission(self, tmp_path):
        exams, issues, _ = _load(tmp_path, [case()], [])
        assert "1141-111900001" in exams
        assert "MISSING_SUBMISSION" in _codes(issues)

    def test_student_cannot_override_portal_identity_fields(self, tmp_path):
        submission = _write_submission(
            tmp_path, student_name="別人", department="別的系", college="別的學院",
        )
        exams, _issues, _ = _load(tmp_path, [case()], [submission])
        exam = exams["1141-111900001"]
        assert (exam.姓名, exam.系所) == ("王小明", "資訊工程學系碩士班")

    def test_two_terms_for_same_student_are_independent_cases(self, tmp_path):
        exams, _issues, _ = _load(
            tmp_path,
            [case(term="1131"), case(term="1141")],
            [_write_submission(tmp_path, name="a.json", term="1131", exam_date="2025-06-01"),
             _write_submission(tmp_path, name="b.json", term="1141", exam_date="2026-06-01")],
        )
        assert exams["1131-111900001"].口試日期.year == 2025
        assert exams["1141-111900001"].口試日期.year == 2026

    def test_duplicate_submissions_for_same_case_flagged(self, tmp_path):
        _exams, issues, _ = _load(
            tmp_path, [case()],
            [_write_submission(tmp_path, name="a.json"),
             _write_submission(tmp_path, name="b.json", exam_date="2026-07-01")],
        )
        assert "DUPLICATE_SUBMISSION" in _codes(issues)


class TestAdvisors:
    def test_co_advisors_joined_and_marked_on_members(self, tmp_path):
        submission = _write_submission(tmp_path, advisors=["林指導", "陳大文"])
        exams, _issues, registry = _load(tmp_path, [case()], [submission])
        exam = exams["1141-111900001"]
        assert exam.指導教授們 == ["林指導", "陳大文"]
        committees = ingest.materialize_committees(registry, exams)
        ingest.fill_advisors(exams, committees)
        assert exam.委員[0].是否指導教授 is True

    def test_missing_advisor_is_student_deficiency(self, tmp_path):
        exams, _issues, registry = _load(
            tmp_path, [case()], [_write_submission(tmp_path, advisors=[])],
        )
        master = Master(exams=list(exams.values()),
                        committees=ingest.materialize_committees(registry, exams))
        issue = next(i for i in validation.check(master)
                     if i["問題代碼"] == "ADVISOR_SOURCE_UNAVAILABLE")
        assert issue["責任"] == validation.OWNER_STUDENT


class TestCommittees:
    def test_matching_committee_gets_email_only_no_banking(self, tmp_path):
        submission = _write_submission(tmp_path, committees=[{
            "name": "陳大文", "unit": "國立範例大學資訊工程學系", "title": "教授",
            "email": "dawen@example.org",
            # 舊版表單欄位：學生不再提供撥帳個資，即使送來也不寫入
            "id_number": "A123456789", "bank": "某銀行", "account": "123",
        }])
        exams, issues, registry = _load(tmp_path, [case()], [submission])
        member = exams["1141-111900001"].委員[0]
        profile = registry.get(member.committee_id)
        assert profile["Email"] == "dawen@example.org"
        assert not profile.get("身分證字號")
        assert not profile.get("帳號")
        assert member.committee_id.startswith("P")
        assert "COMMITTEE_MISMATCH" not in _codes(issues)

    def test_portal_without_committee_uses_submission_list(self, tmp_path):
        exams, issues, registry = _load(
            tmp_path, [case(committees=[])], [_write_submission(tmp_path)],
        )
        exam = exams["1141-111900001"]
        assert len(exam.委員) == 1
        assert exam.委員[0].資料來源 == "介面"
        assert exam.委員[0].職稱 == "教授"
        assert registry.get(exam.委員[0].committee_id)["委員姓名"] == "陳大文"
        assert not _codes(issues) & {"COMMITTEE_MISMATCH", "ATTACHMENT_2_MISSING_COMMITTEE_IDENTITY"}

    def test_submission_committee_list_is_required(self, tmp_path):
        """委員名單與 E-mail 必填（資工系為準）：學生沒填時是學生缺件，附件2 名單照用。"""
        exams, issues, registry = _load(
            tmp_path, [case()], [_write_submission(tmp_path, committees=[])],
        )
        assert len(exams["1141-111900001"].委員) == 1
        assert "COMMITTEE_MISMATCH" not in _codes(issues)
        master = Master(exams=list(exams.values()),
                        committees=ingest.materialize_committees(registry, exams))
        checks = validation.check(master)
        missing = next(i for i in checks if i["問題代碼"] == "MISSING_COMMITTEE_LIST")
        assert missing["責任"] == validation.OWNER_STUDENT
        assert missing["阻擋附件4"] == "否" and missing["阻擋附件6"] == "是"
        # 名單整份沒填時不再逐位重複報缺 E-mail
        assert "MISSING_COMMITTEE_EMAIL" not in _codes(checks)

    def test_mismatched_lists_are_exception_and_portal_wins(self, tmp_path):
        submission = _write_submission(tmp_path, committees=[
            {"name": "陳大文", "unit": "國立範例大學資訊工程學系", "email": "a@example.org"},
            {"name": "多出來", "unit": "某大學", "email": "b@example.org"},
        ])
        portal = case(committees=[
            {"name": "陳大文", "unit": "國立範例大學資訊工程學系", "title": "教授"},
            {"name": "只在附件2", "unit": "另一大學", "title": "副教授"},
        ])
        exams, issues, _ = _load(tmp_path, [portal], [submission])
        assert len(exams["1141-111900001"].委員) == 2
        mismatch = next(i for i in issues if i["問題代碼"] == "COMMITTEE_MISMATCH")
        assert "多出來" in mismatch["說明"] and "只在附件2" in mismatch["說明"]
        assert mismatch["責任"] == validation.OWNER_OFFICE
        assert mismatch["阻擋產出"] == "否"

    def test_no_committee_anywhere_is_student_deficiency(self, tmp_path):
        exams, _issues, registry = _load(
            tmp_path, [case(committees=[])], [_write_submission(tmp_path, committees=[])],
        )
        master = Master(exams=list(exams.values()),
                        committees=ingest.materialize_committees(registry, exams))
        issue = next(i for i in validation.check(master) if i["問題代碼"] == "NO_COMMITTEE")
        assert issue["責任"] == validation.OWNER_STUDENT

    def test_missing_committee_email_is_student_deficiency(self, tmp_path):
        submission = _write_submission(tmp_path, committees=[
            {"name": "陳大文", "unit": "國立範例大學資訊工程學系", "email": ""},
        ])
        exams, _issues, registry = _load(tmp_path, [case()], [submission])
        master = Master(exams=list(exams.values()),
                        committees=ingest.materialize_committees(registry, exams))
        issue = next(i for i in validation.check(master)
                     if i["問題代碼"] == "MISSING_COMMITTEE_EMAIL")
        assert issue["責任"] == validation.OWNER_STUDENT

    def test_provisional_identity_does_not_block_attachment4_or_mail_student(self, tmp_path):
        exams, _issues, registry = _load(tmp_path, [case()], [_write_submission(tmp_path)])
        master = Master(exams=list(exams.values()),
                        committees=ingest.materialize_committees(registry, exams))
        issue = next(i for i in validation.check(master)
                     if i["問題代碼"] == "UNVERIFIED_COMMITTEE_IDENTITY")
        assert issue["阻擋附件4"] == "否"
        assert issue["阻擋附件6"] == "是"
        assert issue["責任"] != validation.OWNER_STUDENT


class TestFees:
    def test_video_exam_has_zero_travel_fee(self, tmp_path):
        portal = case(committees=[{"name": "外校委員", "unit": "遠方大學", "title": "教授"}])
        submission = _write_submission(tmp_path, exam_mode="視訊", committees=[])
        exams, _issues, registry = _load(tmp_path, [portal], [submission])
        committees = ingest.materialize_committees(registry, exams)
        unknown = ingest.fill_fees(exams, committees, distance_table=_NoDistances())
        member = exams["1141-111900001"].委員[0]
        assert member.交通費 == 0
        assert unknown == []


class _NoDistances:
    def fare(self, unit):
        return None, ""


class TestCommitteeNameMatching:
    """review-2026-09-23 P0-2：同案以姓名比對，服務單位寫法不同也要對得上。"""

    def test_unit_written_differently_still_matches_and_attaches_email(self, tmp_path):
        submission = _write_submission(tmp_path, committees=[
            {"name": "陳大文教授", "unit": "範例大學資工系", "email": "dawen@example.org"},
        ])
        exams, issues, registry = _load(tmp_path, [case()], [submission])
        member = exams["1141-111900001"].委員[0]
        assert registry.get(member.committee_id)["Email"] == "dawen@example.org"
        assert "COMMITTEE_MISMATCH" not in _codes(issues)

    def test_same_name_twice_in_case_uses_unit_to_disambiguate(self, tmp_path):
        portal = case(committees=[
            {"name": "王明", "unit": "甲大學", "title": "教授"},
            {"name": "王明", "unit": "乙大學", "title": "教授"},
        ])
        submission = _write_submission(tmp_path, committees=[
            {"name": "王明", "unit": "乙大學", "email": "b@example.org"},
            {"name": "王明", "unit": "甲大學", "email": "a@example.org"},
        ])
        exams, issues, registry = _load(tmp_path, [portal], [submission])
        emails = {m.服務單位: registry.get(m.committee_id)["Email"] for m in exams["1141-111900001"].委員}
        assert emails == {"甲大學": "a@example.org", "乙大學": "b@example.org"}
        assert "COMMITTEE_MISMATCH" not in _codes(issues)


def test_remote_committee_gets_no_travel_fee(tmp_path):
    """部分視訊：勾選視訊出席的委員交通費為 0（review-2026-09-23 P1-17）。"""
    portal = case(committees=[
        {"name": "遠端委員", "unit": "遠方大學", "title": "教授"},
        {"name": "現場委員", "unit": "遠方大學", "title": "教授"},
    ])
    submission = _write_submission(tmp_path, exam_mode="部分視訊", committees=[
        {"name": "遠端委員", "unit": "遠方大學", "email": "r@example.org", "remote": True},
        {"name": "現場委員", "unit": "遠方大學", "email": "s@example.org"},
    ])
    exams, _issues, _ = _load(tmp_path, [portal], [submission])
    members = exams["1141-111900001"].委員
    assert members[0].交通費 == 0 and members[0].交通費說明 == "視訊出席"
    assert members[1].交通費 is None  # 仍依距離表計算
