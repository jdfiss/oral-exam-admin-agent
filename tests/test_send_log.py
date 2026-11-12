# -*- coding: utf-8 -*-
"""寄送記錄測試：draft → reviewed → sent 狀態鏈、去重、序列化。"""

import pytest

from koushi.drafts import DraftTemplateEngine
from koushi.engines.deficiency import DeficiencyItem
from koushi.send_log import (
    APPROVED,
    DRAFT,
    FAILED,
    REJECTED,
    SENT,
    SendEventLog,
    event_from_dict,
    event_to_dict,
    load,
    save,
)


def _draft(to="s@example.edu"):
    item = DeficiencyItem(
        case_key="1142-A", term="1142", student_id="1115020", student_name="學生甲",
        issue_code="NO_COMMITTEE", field="委員名單", description="測試", owner="student",
        blocks_att4=True, blocks_att6=True,
    )
    return DraftTemplateEngine().deficiency_draft("1142-A", "學生甲", [item], to=to)


# ── record_draft ──────────────────────────────────────

class TestRecordDraft:
    def test_creates_draft_event(self):
        log = SendEventLog()
        event = log.record_draft(_draft())
        assert event.status == DRAFT
        assert event.type == "deficiency"

    def test_duplicate_key_returns_existing_not_new(self):
        log = SendEventLog()
        draft = _draft()
        e1 = log.record_draft(draft)
        e2 = log.record_draft(draft)
        assert e1 == e2
        assert len(log.all()) == 1

    def test_different_recipient_same_idempotency_key_is_still_one_event(self):
        log = SendEventLog()
        log.record_draft(_draft(to="a@x.tw"))
        log.record_draft(_draft(to="b@x.tw"))  # 追蹤鍵相同，收件人不影響去重
        assert len(log.all()) == 1


# ── record_review ─────────────────────────────────────

class TestRecordReview:
    def test_approve(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        event = log.record_review(draft.idempotency_key, approved=True, reviewer="系辦")
        assert event.status == APPROVED
        assert event.reviewed_by == "系辦"

    def test_reject(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        event = log.record_review(draft.idempotency_key, approved=False, note="收件人有誤")
        assert event.status == REJECTED

    def test_cannot_review_twice(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        with pytest.raises(ValueError):
            log.record_review(draft.idempotency_key, approved=True)

    def test_unknown_key_raises(self):
        log = SendEventLog()
        with pytest.raises(KeyError):
            log.record_review("nope", approved=True)


# ── record_sent ───────────────────────────────────────

class TestRecordSent:
    def test_success_from_approved(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        event = log.record_sent(draft.idempotency_key, success=True)
        assert event.status == SENT
        assert event.sent_at is not None

    def test_failure_from_approved(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        event = log.record_sent(draft.idempotency_key, success=False, note="SMTP 逾時")
        assert event.status == FAILED
        assert event.note == "SMTP 逾時"

    def test_cannot_send_without_approval(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        with pytest.raises(ValueError):
            log.record_sent(draft.idempotency_key, success=True)

    def test_retry_after_failure(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        log.record_sent(draft.idempotency_key, success=False)
        event = log.retry_after_failure(draft.idempotency_key)
        assert event.status == APPROVED
        assert event.sent_at is None


# ── dispatch ──────────────────────────────────────────

class TestDispatch:
    def test_success_marks_sent(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        calls = []
        event = log.dispatch(draft.idempotency_key, lambda: calls.append(1) or True)
        assert event.status == SENT
        assert calls == [1]

    def test_send_fn_returning_false_marks_failed(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        event = log.dispatch(draft.idempotency_key, lambda: False)
        assert event.status == FAILED

    def test_send_fn_returning_none_counts_as_success(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        event = log.dispatch(draft.idempotency_key, lambda: None)
        assert event.status == SENT

    def test_exception_marks_failed_and_reraises(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)

        def boom():
            raise RuntimeError("smtp down")

        with pytest.raises(RuntimeError):
            log.dispatch(draft.idempotency_key, boom)
        assert log.get(draft.idempotency_key).status == FAILED

    def test_cannot_dispatch_without_approval(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        with pytest.raises(ValueError):
            log.dispatch(draft.idempotency_key, lambda: True)


# ── is_duplicate ──────────────────────────────────────

class TestIsDuplicate:
    def test_false_before_sent(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        assert log.is_duplicate(draft.idempotency_key) is False

    def test_true_after_sent(self):
        log = SendEventLog()
        draft = _draft()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        log.record_sent(draft.idempotency_key, success=True)
        assert log.is_duplicate(draft.idempotency_key) is True

    def test_false_for_unknown_key(self):
        log = SendEventLog()
        assert log.is_duplicate("nope") is False


# ── 序列化 ────────────────────────────────────────────

class TestSerialization:
    def test_round_trip_preserves_fields(self):
        draft = _draft()
        original = SendEventLog()
        original.record_draft(draft)
        original.record_review(draft.idempotency_key, approved=True, reviewer="系辦")
        restored = event_from_dict(event_to_dict(original.get(draft.idempotency_key)))
        assert restored == original.get(draft.idempotency_key)

    def test_load_missing_file_returns_empty(self, tmp_path):
        log = load(tmp_path / "nope.json")
        assert log.all() == []

    def test_save_and_load_round_trip(self, tmp_path):
        path = tmp_path / "send_log.json"
        draft = _draft()
        log = SendEventLog()
        log.record_draft(draft)
        log.record_review(draft.idempotency_key, approved=True)
        log.record_sent(draft.idempotency_key, success=True)
        save(log, path)
        restored = load(path)
        assert restored.get(draft.idempotency_key).status == SENT
