# -*- coding: utf-8 -*-
"""Phase 3 tests: confirmation engine."""

import datetime as _dt

import pytest

from koushi.engines.confirmation import (
    CONFIRMED,
    MODIFIED,
    OVERDUE,
    PENDING,
    ConfirmationRequest,
    apply_responses,
    create_requests,
    mark_sent,
    reconcile,
    record_response,
    report,
    request_from_dict,
    request_to_dict,
)
from koushi.deadline import Deadline
from koushi.model import Committee, Exam, Master, Member


def _exam(key_suffix="A", advisor="王教授", date_text="9/10"):
    members = [Member(committee_id="C0001", 服務單位="台灣大學", 職稱="教授",
                      委員資格="校外委員", 口試費=1000, 交通費=100)]
    e = Exam(
        學期="1142", 學號=f"111502{key_suffix}", 姓名=f"學生{key_suffix}",
        學院="工學院", 系所="資訊工程學系碩士班", 案件種類="學位考試申請",
        審核狀態="簽核完成", 有附件2=True, 有附件3=True,
        指導教授=advisor, 委員=members,
        口試日期原文=date_text,
    )
    e.derive()
    return e


def _master(exams):
    c = Committee(committee_id="C0001", 委員姓名="張委員", 服務單位="台灣大學",
                  身分證字號="A123456789", 金融機構="台銀", 帳號="12345", 戶籍地址="台北市")
    return Master(exams=exams, committees={"C0001": c})


# ── create_requests ──────────────────────────────────

class TestCreateRequests:
    def test_one_group_one_request(self):
        master = _master([_exam()])
        reqs = create_requests(master)
        assert len(reqs) == 1
        assert reqs[0].advisor == "王教授"
        assert reqs[0].student_names == ("學生A",)
        assert reqs[0].status == "draft"

    def test_co_advised_case_counts_for_every_advisor(self):
        """附件4 全學期一份（2026-10-10）：每學期每位指導教授一筆，共同指導的案件算在每位名下。"""
        master = _master([
            _exam(key_suffix="A", advisor="王教授"),
            _exam(key_suffix="B", advisor="王教授、李教授"),
        ])
        reqs = create_requests(master)
        assert len(reqs) == 2
        wang = next(r for r in reqs if r.advisor == "王教授")
        li = next(r for r in reqs if r.advisor == "李教授")
        assert wang.request_key == ("1142", "王教授")
        assert set(wang.case_keys) == {e.案件鍵 for e in master.exams}
        assert li.case_keys == (master.exams[1].案件鍵,)

    def test_same_group_merged(self):
        e1 = _exam(key_suffix="A", advisor="王教授", date_text="9/10")
        e2 = _exam(key_suffix="B", advisor="王教授", date_text="9/10")
        master = _master([e1, e2])
        reqs = create_requests(master)
        assert len(reqs) == 1
        assert len(reqs[0].student_names) == 2
        assert len(reqs[0].case_keys) == 2

    def test_different_advisor_split(self):
        e1 = _exam(key_suffix="A", advisor="王教授")
        e2 = _exam(key_suffix="B", advisor="李教授")
        master = _master([e1, e2])
        reqs = create_requests(master)
        assert len(reqs) == 2

    def test_different_dates_same_advisor_one_request(self):
        """同一位教授不同日期的學生合成一筆確認（附件4 全學期一份）。"""
        e1 = _exam(key_suffix="A", date_text="9/15")
        e2 = _exam(key_suffix="B", date_text="9/10")
        master = _master([e1, e2])
        reqs = create_requests(master)
        assert len(reqs) == 1
        assert reqs[0].date_text == "9/10、9/15"          # 依口試日期排序
        assert reqs[0].case_keys == (e2.案件鍵, e1.案件鍵)

    def test_term_filter(self):
        e1 = _exam(key_suffix="A")
        master = _master([e1])
        reqs = create_requests(master, term="1141")
        assert len(reqs) == 0


# ── mark_sent ────────────────────────────────────────

class TestMarkSent:
    def test_sets_date_and_deadline(self):
        master = _master([_exam()])
        reqs = create_requests(master)
        today = _dt.date.today()
        sent = mark_sent(reqs, date=today)
        assert sent[0].sent_date == today
        assert sent[0].deadline is not None
        assert sent[0].deadline.business_days == 3
        assert sent[0].status == PENDING

    def test_skips_already_sent(self):
        master = _master([_exam()])
        reqs = create_requests(master)
        sent1 = mark_sent(reqs, date=_dt.date(2026, 9, 1))
        sent2 = mark_sent(sent1, date=_dt.date(2026, 9, 4))
        assert sent2[0].sent_date == _dt.date(2026, 9, 1)


# ── record_response ──────────────────────────────────

class TestRecordResponse:
    def test_confirm(self):
        master = _master([_exam()])
        req = mark_sent(create_requests(master), date=_dt.date(2026, 9, 1))[0]
        updated = record_response(req, CONFIRMED, date=_dt.date(2026, 9, 3))
        assert updated.status == CONFIRMED
        assert updated.response_date == _dt.date(2026, 9, 3)

    def test_modify(self):
        master = _master([_exam()])
        req = mark_sent(create_requests(master), date=_dt.date(2026, 9, 1))[0]
        updated = record_response(req, MODIFIED, notes="帳號有誤")
        assert updated.status == MODIFIED
        assert updated.notes == "帳號有誤"

    def test_invalid_response_raises(self):
        master = _master([_exam()])
        req = create_requests(master)[0]
        with pytest.raises(ValueError, match="無效回覆"):
            record_response(req, "invalid")


# ── apply_responses ──────────────────────────────────

class TestApplyResponses:
    def test_batch_apply(self):
        e1 = _exam(key_suffix="A", advisor="王教授")
        e2 = _exam(key_suffix="B", advisor="李教授")
        master = _master([e1, e2])
        reqs = mark_sent(create_requests(master), date=_dt.date(2099, 1, 1))
        responses = {reqs[0].request_key: CONFIRMED}
        updated = apply_responses(reqs, responses, date=_dt.date(2099, 1, 2))
        assert updated[0].status == CONFIRMED
        assert updated[1].status == PENDING

    def test_unmatched_key_unchanged(self):
        master = _master([_exam()])
        reqs = mark_sent(create_requests(master), date=_dt.date(2099, 1, 1))
        updated = apply_responses(reqs, {("x", "y", "z", "w"): CONFIRMED})
        assert updated[0].status == PENDING


# ── status logic ─────────────────────────────────────

class TestStatus:
    def test_draft(self):
        master = _master([_exam()])
        req = create_requests(master)[0]
        assert req.status == "draft"

    def test_pending(self):
        dl = Deadline(base_date=_dt.date(2099, 1, 1), business_days=3,
                      computed_date=_dt.date(2099, 1, 6))
        req = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            sent_date=_dt.date(2099, 1, 1), deadline=dl,
        )
        assert req.status == PENDING

    def test_no_overdue_after_deadline(self):
        """2026-10-12 起教授不用回信：過了寄出時記下的期限仍是「已寄出」，不會變逾期。"""
        dl = Deadline(base_date=_dt.date(2020, 1, 1), business_days=3,
                      computed_date=_dt.date(2020, 1, 6))
        req = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            sent_date=_dt.date(2020, 1, 1), deadline=dl,
        )
        assert req.status == PENDING

    def test_confirmed_overrides_overdue(self):
        dl = Deadline(base_date=_dt.date(2020, 1, 1), business_days=3,
                      computed_date=_dt.date(2020, 1, 6))
        req = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            sent_date=_dt.date(2020, 1, 1), deadline=dl,
            response=CONFIRMED, response_date=_dt.date(2020, 1, 10),
        )
        assert req.status == CONFIRMED


# ── report ───────────────────────────────────────────

class TestReport:
    def test_all_confirmed(self):
        req = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            response=CONFIRMED,
        )
        r = report([req])
        assert r.all_confirmed is True
        assert len(r.confirmed) == 1

    def test_mixed_statuses(self):
        confirmed = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("A",), case_keys=("1142-A",),
            response=CONFIRMED,
        )
        dl = Deadline(base_date=_dt.date(2099, 1, 1), business_days=3,
                      computed_date=_dt.date(2099, 1, 6))
        pending = ConfirmationRequest(
            group_key=("1142", "碩", "李教授", "9/15", ""),
            term="1142", advisor="李教授", degree="碩", date_text="9/15",
            student_names=("B",), case_keys=("1142-B",),
            sent_date=_dt.date(2099, 1, 1), deadline=dl,
        )
        r = report([confirmed, pending])
        assert r.all_confirmed is False
        assert len(r.confirmed) == 1
        assert len(r.pending) == 1


# ── reconcile ────────────────────────────────────────

class TestReconcile:
    def test_legacy_per_session_request_carries_over_when_cases_match(self):
        """舊版（每場次一筆）存下的已確認請求，案件相同時換算成新鍵沿用；不同就重設。"""
        master = _master([_exam(key_suffix="A")])
        legacy = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""), term="1142", advisor="王教授", degree="碩",
            date_text="9/10", student_names=("學生A",), case_keys=(master.exams[0].案件鍵,),
            response=CONFIRMED,
        )
        [kept] = reconcile(master, [legacy])
        assert kept.response == CONFIRMED and kept.group_key == ("1142", "王教授")

        master.exams.append(_exam(key_suffix="B"))
        [fresh] = reconcile(master, [legacy])
        assert fresh.response == PENDING and fresh.status == "draft"

    def test_preserves_sent_state(self):
        master = _master([_exam(key_suffix="A")])
        reqs = mark_sent(create_requests(master), date=_dt.date(2026, 9, 1))
        reconciled = reconcile(master, reqs)
        assert len(reconciled) == 1
        assert reconciled[0].sent_date == _dt.date(2026, 9, 1)

    def test_adds_new_group(self):
        e1 = _exam(key_suffix="A", advisor="王教授")
        master1 = _master([e1])
        reqs = mark_sent(create_requests(master1), date=_dt.date(2026, 9, 1))

        e2 = _exam(key_suffix="B", advisor="李教授")
        master2 = _master([e1, e2])
        reconciled = reconcile(master2, reqs)
        assert len(reconciled) == 2
        wang = next(r for r in reconciled if r.advisor == "王教授")
        li = next(r for r in reconciled if r.advisor == "李教授")
        assert wang.sent_date == _dt.date(2026, 9, 1)
        assert li.status == "draft"

    def test_drops_removed_group(self):
        e1 = _exam(key_suffix="A", advisor="王教授")
        e2 = _exam(key_suffix="B", advisor="李教授")
        master_full = _master([e1, e2])
        reqs = create_requests(master_full)

        master_partial = _master([e1])
        reconciled = reconcile(master_partial, reqs)
        assert len(reconciled) == 1
        assert reconciled[0].advisor == "王教授"

    def test_case_keys_changed_invalidates_tracking_state(self):
        """換委員／新增學生代表教授看到的內容跟現在不一樣，舊確認結果不能沿用。

        這是盤點文件點名的缺口：原本只更新 student_names／case_keys 顯示
        欄位，response／sent_date／deadline 卻原封不動沿用，導致教授確認
        過的清冊事後偷加一個他從沒看過資料的學生，系統仍顯示「已確認」。
        """
        e1 = _exam(key_suffix="A", advisor="王教授", date_text="9/10")
        master1 = _master([e1])
        reqs = mark_sent(create_requests(master1), date=_dt.date(2026, 9, 1))
        confirmed = [record_response(reqs[0], CONFIRMED, date=_dt.date(2026, 9, 2))]
        assert confirmed[0].status == CONFIRMED

        e2 = _exam(key_suffix="B", advisor="王教授", date_text="9/10")
        master2 = _master([e1, e2])
        reconciled = reconcile(master2, confirmed)

        assert len(reconciled) == 1
        assert len(reconciled[0].student_names) == 2
        assert reconciled[0].status == "draft"
        assert reconciled[0].response == PENDING
        assert reconciled[0].sent_date is None
        assert reconciled[0].deadline is None
        assert reconciled[0].response_date is None

    def test_case_keys_unchanged_preserves_tracking_state(self):
        """內容沒變的話，重新整理主檔（例如只是重跑 build）不該把確認結果洗掉。"""
        e1 = _exam(key_suffix="A", advisor="王教授", date_text="9/10")
        master = _master([e1])
        reqs = mark_sent(create_requests(master), date=_dt.date(2026, 9, 1))
        confirmed = [record_response(reqs[0], CONFIRMED, date=_dt.date(2026, 9, 2))]

        reconciled = reconcile(master, confirmed)
        assert reconciled[0].status == CONFIRMED
        assert reconciled[0].sent_date == _dt.date(2026, 9, 1)


# ── serialization ────────────────────────────────────

class TestSerialization:
    def test_round_trip_draft(self):
        req = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("學生A", "學生B"), case_keys=("1142-A", "1142-B"),
        )
        restored = request_from_dict(request_to_dict(req))
        assert restored.group_key == req.group_key
        assert restored.student_names == req.student_names
        assert restored.sent_date is None

    def test_round_trip_full(self):
        dl = Deadline(base_date=_dt.date(2026, 9, 1), business_days=3,
                      computed_date=_dt.date(2026, 9, 4))
        req = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("學生A",), case_keys=("1142-A",),
            response=CONFIRMED, sent_date=_dt.date(2026, 9, 1),
            deadline=dl, response_date=_dt.date(2026, 9, 3), notes="ok",
        )
        restored = request_from_dict(request_to_dict(req))
        assert restored.response == CONFIRMED
        assert restored.sent_date == _dt.date(2026, 9, 1)
        assert restored.deadline.computed_date == _dt.date(2026, 9, 4)
        assert restored.response_date == _dt.date(2026, 9, 3)
        assert restored.notes == "ok"
