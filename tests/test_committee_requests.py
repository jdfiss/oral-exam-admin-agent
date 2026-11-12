# -*- coding: utf-8 -*-
"""向委員本人索取撥帳個資：只對已放行案件、撥帳不齊的校外委員；以 E-mail 合併；
回填（系辦手動登錄）在建檔時疊加進委員資料，不經學生。

本系委員免撥帳，但舊口委資料庫查無此人（暫定身分）時只索取身分證字號，否則暫定
身分會一直擋附件6（2026-10-01）。"""
import datetime as dt
import json

import pytest

from koushi import case_review, committee_requests
from koushi.application import process_committee_requests, review_queue
from koushi.workflow import RegistryPaths, build_master

from tests.att2_fixture import case, write_att2

FAR = {"name": "外校委員", "unit": "遠方大學資訊系", "title": "教授"}
HOME = {"name": "校內委員", "unit": "國立範例大學資訊工程學系", "title": "教授"}


def _submission(path, student_id, exam_date, committees):
    path.write_text(json.dumps({
        "term": "1141", "student_id": student_id, "version": 1, "exam_date": exam_date,
        "advisors": ["林指導"],
        "committees": [{"name": c["name"], "unit": c["unit"], "title": c["title"], "email": c["email"]}
                       for c in committees],
    }, ensure_ascii=False), encoding="utf-8")
    return path


def _build(tmp_path, state_dir):
    far = {**FAR, "email": "far@example.org"}
    home = {**HOME, "email": "home@example.org"}
    subs = [
        _submission(tmp_path / "a.json", "111900001", "2026-11-10", [far, home]),
        _submission(tmp_path / "b.json", "111900002", "2026-11-20", [far]),
    ]
    registries = RegistryPaths(
        members=tmp_path / "none.xlsx", distances=tmp_path / "none.csv",
        members_output=tmp_path / "out" / "members.xlsx",
        attachment2=write_att2(tmp_path / "att2.xlsx", [
            case(student_id="111900001", committees=[FAR, HOME]),
            case(student_id="111900002", name="李小華", committees=[FAR]),
        ]),
        committee_replies=state_dir / committee_requests.REPLIES_FILE,
    )
    return build_master(submission_paths=subs, output_path=tmp_path / "out" / "master.xlsx",
                        registries=registries).master


def _clear(state_dir, master, *case_keys):
    for exam in master.exams:
        if exam.案件鍵 in case_keys:
            case_review.clear(state_dir, exam=exam, master=master)


def test_only_cleared_cases_and_external_incomplete_committees(tmp_path):
    state = tmp_path / "state"
    master = _build(tmp_path, state)
    assert committee_requests.plan(master, state) == []  # 還沒放行

    _clear(state, master, "1141-111900001")
    requests = {r.email: r for r in committee_requests.plan(master, state)}
    assert sorted(requests) == ["far@example.org", "home@example.org"]
    assert requests["far@example.org"].case_keys == ("1141-111900001",)
    assert not requests["far@example.org"].id_only
    # 本系委員免撥帳，但資料庫查無此人：只要身分證字號
    assert requests["home@example.org"].id_only
    assert requests["home@example.org"].fields == ("身分證字號",)


def test_same_committee_across_cases_is_one_request(tmp_path):
    state = tmp_path / "state"
    master = _build(tmp_path, state)
    _clear(state, master, "1141-111900001", "1141-111900002")
    request = next(r for r in committee_requests.plan(master, state) if r.email == "far@example.org")
    assert request.case_keys == ("1141-111900001", "1141-111900002")
    assert request.earliest_exam == dt.date(2026, 11, 10)


def test_sends_once_and_records_sent_date(tmp_path):
    state = tmp_path / "state"
    master = _build(tmp_path, state)
    _clear(state, master, "1141-111900001")
    deliveries = []
    drafts, sent = process_committee_requests(
        master=master, state_dir=state, send=True, date=dt.date(2026, 10, 5),
        transport=lambda **kw: deliveries.append(kw) or True,
    )
    by_to = {d["to"]: d for d in deliveries}
    assert len(sent) == 2 and sorted(by_to) == ["far@example.org", "home@example.org"]
    assert "帳號" in by_to["far@example.org"]["body"]
    assert "身分證字號" in by_to["home@example.org"]["subject"]
    assert "不需提供撥款資料" in by_to["home@example.org"]["body"]
    assert all(r.sent_date == dt.date(2026, 10, 5) for r in committee_requests.plan(master, state))

    drafts, sent = process_committee_requests(
        master=master, state_dir=state, send=True, date=dt.date(2026, 10, 6),
        transport=lambda **kw: pytest.fail("已寄過的委員不應重寄"),
    )
    assert drafts == [] and sent == []


def test_reply_is_applied_on_rebuild_and_stops_requests(tmp_path):
    state = tmp_path / "state"
    master = _build(tmp_path, state)
    _clear(state, master, "1141-111900001", "1141-111900002")
    committee_requests.save_reply(state, email="FAR@example.org", values={
        "身分證字號": "A123456789", "金融機構": "某銀行某分行", "帳號": "00012345678", "戶籍地址": "某地",
    })
    committee_requests.save_reply(state, email="home@example.org", values={"身分證字號": "B123456780"},
                                  fields=committee_requests.ID_ONLY_FIELDS)

    rebuilt = _build(tmp_path, state)
    far_ids = {m.committee_id for e in rebuilt.exams for m in e.委員
               if rebuilt.committees[m.committee_id].委員姓名 == "外校委員"}
    assert len(far_ids) == 1 and next(iter(far_ids)).startswith("C")  # 跨案件合併為正式身分
    assert rebuilt.committees[next(iter(far_ids))].撥帳齊全
    assert committee_requests.plan(rebuilt, state) == []
    home_ids = {m.committee_id for e in rebuilt.exams for m in e.委員
                if rebuilt.committees[m.committee_id].委員姓名 == "校內委員"}
    assert all(cid.startswith("C") for cid in home_ids)  # 本系委員身分不再暫定，不擋附件6

    audit = (state / "audit_log.json").read_text(encoding="utf-8")
    assert "A123456789" not in audit  # 稽核不寫個資


def test_id_only_reply_needs_only_valid_id(tmp_path):
    fields = committee_requests.ID_ONLY_FIELDS
    with pytest.raises(ValueError, match="檢查碼"):
        committee_requests.save_reply(tmp_path, email="h@example.org", values={"身分證字號": "A123456780"},
                                      fields=fields)
    committee_requests.save_reply(tmp_path, email="h@example.org", values={"身分證字號": "a123456789"},
                                  confirm={"身分證字號": "A123456789"}, fields=fields)
    assert committee_requests.load_replies(tmp_path)["h@example.org"]["身分證字號"] == "A123456789"


def test_incomplete_reply_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="不完整"):
        committee_requests.save_reply(tmp_path, email="x@example.org", values={"身分證字號": "A1"})


def test_missing_before_exam_becomes_review_exception(tmp_path):
    state = tmp_path / "state"
    master = _build(tmp_path, state)
    _clear(state, master, "1141-111900001")

    def codes(today):
        row = next(r for r in review_queue(master, state, today=today) if r.case_key == "1141-111900001")
        return [i["問題代碼"] for i in row.exceptions]

    assert "COMMITTEE_DATA_MISSING_BEFORE_EXAM" not in codes(dt.date(2026, 11, 1))
    assert "COMMITTEE_DATA_MISSING_BEFORE_EXAM" in codes(dt.date(2026, 11, 8))


def _member_registry(path, rows):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["委員編號", "委員姓名", "英文姓名", "服務單位", "最高學歷學校", "最高學位",
               "身分證字號", "金融機構", "帳號", "戶籍地址", "Email", "最後更新", "來源"])
    for cid, name, unit, id_no in rows:
        ws.append([cid, name, "", unit, "", "", id_no, "某銀行", "000999", "某地", "", "2026-01-01", "系辦上傳"])
    wb.save(path)
    return path


def _build_with_registry(tmp_path, state_dir, registry_rows):
    far = {**FAR, "email": "far@example.org"}
    registries = RegistryPaths(
        members=_member_registry(tmp_path / "members.xlsx", registry_rows),
        distances=tmp_path / "none.csv", members_output=tmp_path / "out" / "members.xlsx",
        attachment2=write_att2(tmp_path / "att2.xlsx", [case(student_id="111900001", committees=[FAR])]),
        committee_replies=state_dir / committee_requests.REPLIES_FILE,
    )
    subs = [_submission(tmp_path / "a.json", "111900001", "2026-11-10", [far])]
    return build_master(submission_paths=subs, output_path=tmp_path / "out" / "master.xlsx",
                        registries=registries)


def test_unique_name_and_unit_match_reuses_registry_and_skips_request(tmp_path):
    state = tmp_path / "state"
    result = _build_with_registry(tmp_path, state, [("C0007", "外校委員", "遠方大學資訊系", "B223456789")])
    member = result.master.exams[0].委員[0]
    assert member.committee_id == "C0007"
    assert result.master.committees["C0007"].撥帳齊全
    matched = [i for i in result.issues if i["問題代碼"] == "COMMITTEE_MATCHED_BY_NAME"]
    assert matched and matched[0]["責任"] == "office" and "C0007" in matched[0]["說明"]

    _clear(state, result.master, "1141-111900001")
    assert committee_requests.plan(result.master, state) == []  # 資料已完整，不寄信


def test_ambiguous_name_and_unit_match_is_not_reused(tmp_path):
    state = tmp_path / "state"
    result = _build_with_registry(tmp_path, state, [
        ("C0007", "外校委員", "遠方大學資訊系", "B223456789"),
        ("C0008", "外校委員", "遠方大學資訊系", "C123456789"),  # 同名同單位的另一人
    ])
    assert result.master.exams[0].委員[0].committee_id.startswith("P")
    assert not any(i["問題代碼"] == "COMMITTEE_MATCHED_BY_NAME" for i in result.issues)


@pytest.mark.parametrize("value, ok", [
    ("A123456789", True), ("a123456789", True), ("A123456788", False), ("A323456789", False),
    ("F100000006", True), ("A800000014", True), ("AC01234567", True), ("A12345678", False),
])
def test_valid_taiwan_id(value, ok):
    assert committee_requests.valid_taiwan_id(value) is ok


def _reply(**overrides):
    values = {"身分證字號": "A123456789", "金融機構": "某銀行", "帳號": "0001-2345 678", "戶籍地址": "某地"}
    values.update(overrides)
    return values


def test_validate_reply_normalizes_account_and_uppercases_id():
    cleaned = committee_requests.validate_reply(
        _reply(身分證字號="a123456789"), confirm={"身分證字號": "A123456789", "帳號": "00012345678"})
    assert cleaned["身分證字號"] == "A123456789" and cleaned["帳號"] == "00012345678"


@pytest.mark.parametrize("values, confirm, message", [
    (_reply(身分證字號="A123456788"), None, "檢查碼"),
    (_reply(帳號="12345"), None, "位數字"),
    (_reply(帳號="1234abcd5678"), None, "位數字"),
    (_reply(), {"身分證字號": "A123456789", "帳號": "00012345679"}, "帳號不一致"),
    (_reply(), {"身分證字號": "B123456789", "帳號": "00012345678"}, "身分證字號不一致"),
])
def test_validate_reply_rejects_bad_input(values, confirm, message):
    with pytest.raises(ValueError, match=message):
        committee_requests.validate_reply(values, confirm=confirm)


def test_foreign_id_skips_checksum(tmp_path):
    committee_requests.save_reply(tmp_path, email="x@example.org", foreign_id=True,
                                  values=_reply(身分證字號="P12345678"))
    assert committee_requests.load_replies(tmp_path)["x@example.org"]["身分證字號"] == "P12345678"


def test_link_token_is_stable_per_committee_and_resolves_back(tmp_path):
    token = committee_requests.link_token(tmp_path, "Far@Example.org")
    assert committee_requests.link_token(tmp_path, "far@example.org") == token
    assert committee_requests.email_for_token(tmp_path, token) == "far@example.org"
    assert committee_requests.email_for_token(tmp_path, token[:-1] + "x") is None


def test_request_mail_uses_link_only_when_public_url_is_set(tmp_path, monkeypatch):
    from koushi import application
    state = tmp_path / "state"
    master = _build(tmp_path, state)
    _clear(state, master, "1141-111900001", "1141-111900002")

    monkeypatch.delenv(application.PUBLIC_URL_ENV, raising=False)
    drafts, _ = application.process_committee_requests(master=master, state_dir=state)
    assert "回覆本信" in drafts[0]["body"] and "/committee/reply/" not in drafts[0]["body"]

    monkeypatch.setenv(application.PUBLIC_URL_ENV, "https://koushi.example.edu.tw/")
    drafts, _ = application.process_committee_requests(master=master, state_dir=state)
    token = committee_requests.link_token(state, drafts[0]["to"])
    assert f"https://koushi.example.edu.tw/committee/reply/{token}" in drafts[0]["body"]


def test_online_reply_records_source(tmp_path):
    committee_requests.save_reply(tmp_path, email="x@example.org", values=_reply(),
                                  actor="委員本人", source=committee_requests.SOURCE_ONLINE)
    assert committee_requests.load_replies(tmp_path)["x@example.org"]["source"] == "委員線上填寫"
