# -*- coding: utf-8 -*-
"""信件草稿產生器測試：三種模板類型、追溯資訊、no_recipient 分流。"""

import datetime as _dt

import pytest

from koushi.deadline import Deadline
from koushi.drafts import (
    STATUS_NO_RECIPIENT,
    STATUS_READY,
    TEST_RECIPIENT_DOMAIN,
    TYPE_COLLECTION,
    TYPE_CONFIRMATION,
    TYPE_DEFICIENCY,
    DraftTemplateEngine,
    group_deficiency_by_case,
    placeholder_recipient,
)
from koushi.engines.collection import CollectionItem
from koushi.engines.confirmation import ConfirmationRequest
from koushi.engines.deficiency import DeficiencyItem


def _deadline(days_from_today=5):
    base = _dt.date(2026, 9, 1)
    return Deadline(base_date=base, business_days=days_from_today,
                     computed_date=base + _dt.timedelta(days=days_from_today))


def _def_item(case_key="1142-A", code="NO_COMMITTEE", field="委員名單",
              owner="student", deadline=None):
    return DeficiencyItem(
        case_key=case_key, term="1142", student_id="1115020",
        student_name="學生甲", issue_code=code, field=field,
        description="測試說明", owner=owner,
        blocks_att4=True, blocks_att6=True,
        notified_date=_dt.date(2026, 9, 1) if deadline else None,
        deadline=deadline,
    )


def _confirmation_request(deadline=None):
    return ConfirmationRequest(
        group_key=("1142", "碩", "王教授", "9/10"),
        term="1142", advisor="王教授", degree="碩",
        date_text="9/10", student_names=("學生甲", "學生乙"),
        case_keys=("1142-A", "1142-B"),
        sent_date=_dt.date(2026, 9, 1) if deadline else None,
        deadline=deadline,
    )


def _collection_item(reminder_count=0, last_reminder_date=None, deadline=None):
    return CollectionItem(
        group_key=("1142", "碩", "王教授", "9/10"),
        term="1142", advisor="王教授", degree="碩",
        date_text="9/10", student_names=("學生甲", "學生乙"),
        case_keys=("1142-A", "1142-B"),
        sent_date=_dt.date(2026, 9, 1) if deadline else None,
        deadline=deadline,
        reminder_count=reminder_count,
        last_reminder_date=last_reminder_date,
    )


# ── test_recipient ───────────────────────────────────

class TestPlaceholderRecipient:
    def test_uses_invalid_domain(self):
        assert placeholder_recipient("1142-A").endswith(f"@{TEST_RECIPIENT_DOMAIN}")

    def test_sanitizes_non_alnum(self):
        addr = placeholder_recipient("1142/王教授 9-10")
        local = addr.split("@")[0]
        assert all(ch.isalnum() or ch == "-" for ch in local)

    def test_blank_local_part_falls_back(self):
        assert placeholder_recipient("／／／") == f"case@{TEST_RECIPIENT_DOMAIN}"


# ── group_deficiency_by_case ──────────────────────────

class TestGroupByCase:
    def test_groups_by_case_key(self):
        items = [_def_item(case_key="A"), _def_item(case_key="A", code="X"),
                 _def_item(case_key="B")]
        groups = group_deficiency_by_case(items)
        assert len(groups["A"]) == 2
        assert len(groups["B"]) == 1


# ── deficiency_draft ──────────────────────────────────

class TestDeficiencyDraft:
    def test_ready_when_recipient_given(self):
        engine = DraftTemplateEngine()
        draft = engine.deficiency_draft("1142-A", "學生甲", [_def_item()], to="s@example.edu")
        assert draft.status == STATUS_READY
        assert draft.type == TYPE_DEFICIENCY
        assert draft.to == "s@example.edu"

    def test_no_recipient_when_missing(self):
        engine = DraftTemplateEngine()
        draft = engine.deficiency_draft("1142-A", "學生甲", [_def_item()], to=None)
        assert draft.status == STATUS_NO_RECIPIENT
        assert draft.to is None

    def test_empty_items_raises(self):
        engine = DraftTemplateEngine()
        with pytest.raises(ValueError):
            engine.deficiency_draft("1142-A", "學生甲", [], to="s@x.tw")

    def test_mismatched_case_key_raises(self):
        engine = DraftTemplateEngine()
        items = [_def_item(case_key="A"), _def_item(case_key="B")]
        with pytest.raises(ValueError):
            engine.deficiency_draft("A", "學生甲", items, to="s@x.tw")

    def test_body_lists_each_issue(self):
        engine = DraftTemplateEngine()
        items = [_def_item(code="NO_COMMITTEE"), _def_item(code="NO_EXAM_DATE")]
        draft = engine.deficiency_draft("1142-A", "學生甲", items, to="s@x.tw")
        # 每項都列出，但信中不顯示內部問題代碼（review-2026-09-23 P1-7）
        assert draft.body.count("測試說明") == 2
        assert "NO_COMMITTEE" not in draft.body

    def test_deadline_text_falls_back_without_deadline(self):
        engine = DraftTemplateEngine()
        draft = engine.deficiency_draft("1142-A", "學生甲", [_def_item()], to="s@x.tw")
        assert "尚未設定" in draft.body

    def test_deadline_text_uses_latest_deadline(self):
        engine = DraftTemplateEngine()
        items = [_def_item(deadline=_deadline(3)), _def_item(code="X", deadline=_deadline(7))]
        draft = engine.deficiency_draft("1142-A", "學生甲", items, to="s@x.tw")
        latest = _deadline(7).computed_date.strftime("%Y-%m-%d")
        assert latest in draft.body

    def test_tracking_has_case_and_issue_codes(self):
        engine = DraftTemplateEngine()
        items = [_def_item(code="B"), _def_item(code="A")]
        draft = engine.deficiency_draft("1142-A", "學生甲", items, to="s@x.tw")
        assert draft.tracking["case_key"] == "1142-A"
        assert draft.tracking["issue_codes"] == ["A", "B"]

    def test_idempotency_key_stable_for_same_state(self):
        engine = DraftTemplateEngine()
        items = [_def_item()]
        d1 = engine.deficiency_draft("1142-A", "學生甲", items, to="s@x.tw")
        d2 = engine.deficiency_draft("1142-A", "學生甲", items, to="other@x.tw")
        assert d1.idempotency_key == d2.idempotency_key  # 收件人不影響追蹤鍵

    def test_idempotency_key_changes_with_issue_set(self):
        engine = DraftTemplateEngine()
        d1 = engine.deficiency_draft("1142-A", "學生甲", [_def_item(code="A")], to="s@x.tw")
        d2 = engine.deficiency_draft("1142-A", "學生甲", [_def_item(code="B")], to="s@x.tw")
        assert d1.idempotency_key != d2.idempotency_key


# ── confirmation_draft ────────────────────────────────

class TestConfirmationDraft:
    def test_ready_and_content(self):
        engine = DraftTemplateEngine()
        req = _confirmation_request()
        draft = engine.confirmation_draft(req, to="prof@example.edu")
        assert draft.type == TYPE_CONFIRMATION
        assert draft.status == STATUS_READY
        # 與原附件4 確認信同主旨（含請求代碼），回覆方式一致、不列學號
        from koushi.advisor_confirmation import ref_code
        assert ref_code(req) in draft.subject and draft.subject.startswith("[口試附件4 ")
        assert "王教授" in draft.body and "不需回覆" in draft.body and "「確認無誤」" not in draft.body
        assert "學生甲" in draft.body
        assert "學生乙" in draft.body

    def test_no_recipient(self):
        engine = DraftTemplateEngine()
        draft = engine.confirmation_draft(_confirmation_request(), to=None)
        assert draft.status == STATUS_NO_RECIPIENT

    def test_deadline_fallback(self):
        engine = DraftTemplateEngine()
        draft = engine.confirmation_draft(_confirmation_request(), to="p@x.tw")
        assert "None" not in draft.body

    def test_tracking_uses_request_key(self):
        engine = DraftTemplateEngine()
        req = _confirmation_request()
        draft = engine.confirmation_draft(req, to="p@x.tw")
        assert draft.tracking["request_key"] == list(req.request_key)
        assert draft.tracking["case_keys"] == sorted(req.case_keys)


# ── collection_draft ──────────────────────────────────

class TestCollectionDraft:
    def test_first_reminder_has_no_note(self):
        engine = DraftTemplateEngine()
        draft = engine.collection_draft(_collection_item(reminder_count=0), to="p@x.tw")
        assert "第 1 次提醒" in draft.subject
        assert "已提醒" not in draft.body

    def test_repeat_reminder_shows_history(self):
        engine = DraftTemplateEngine()
        item = _collection_item(reminder_count=2, last_reminder_date=_dt.date(2026, 9, 5))
        draft = engine.collection_draft(item, to="p@x.tw")
        assert "第 3 次提醒" in draft.subject
        assert "已提醒 2 次" in draft.body
        assert "2026-09-05" in draft.body

    def test_no_recipient(self):
        engine = DraftTemplateEngine()
        draft = engine.collection_draft(_collection_item(), to=None)
        assert draft.status == STATUS_NO_RECIPIENT

    def test_tracking_includes_reminder_ord(self):
        engine = DraftTemplateEngine()
        item = _collection_item(reminder_count=1)
        draft = engine.collection_draft(item, to="p@x.tw")
        assert draft.tracking["reminder_ord"] == 2

    def test_idempotency_key_changes_per_reminder_round(self):
        engine = DraftTemplateEngine()
        d1 = engine.collection_draft(_collection_item(reminder_count=0), to="p@x.tw")
        d2 = engine.collection_draft(_collection_item(reminder_count=1), to="p@x.tw")
        assert d1.idempotency_key != d2.idempotency_key


def test_collection_reminder_goes_to_all_committee_members():
    from koushi.mail import recipients
    contacts = {"case_committees": {"1151-1": ["a@x.tw", "b@x.tw"], "1151-2": ["b@x.tw", "c@x.tw"]},
                "case_advisors": {"1151-1": {"王教授": "prof@x.tw"}}}
    assert recipients.resolve_committees(("1151-2", "1151-1"), contacts) == "a@x.tw, b@x.tw, c@x.tw"
    assert recipients.resolve_committees(("1151-9",), contacts) is None
    draft = DraftTemplateEngine().collection_draft(_collection_item(), to="a@x.tw, b@x.tw")
    assert "委員老師您好" in draft.body and "由學生繳回系辦" in draft.body and "教授，您好" not in draft.body
