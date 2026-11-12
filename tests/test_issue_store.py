# -*- coding: utf-8 -*-
"""缺件紀錄庫測試：狀態機轉移、對帳、序列化。"""

import datetime as _dt

import pytest

from koushi.deadline import Deadline
from koushi.engines.deficiency import DeficiencyItem
from koushi.issue_store import (
    DETECTED,
    DRAFT_READY,
    REQUEST_SENT,
    RESOLVED,
    RESUBMITTED,
    REVALIDATED,
    IssueRecordStore,
    RevalidationVerdict,
    automated_reviewer,
    from_deficiency_item,
    load,
    reconcile,
    revalidate,
    record_from_dict,
    record_to_dict,
    save,
    transition,
)


def _item(case_key="1142-A", code="NO_COMMITTEE"):
    return DeficiencyItem(
        case_key=case_key, term="1142", student_id="1115020", student_name="學生甲",
        issue_code=code, field="委員名單", description="測試", owner="student",
        blocks_att4=True, blocks_att6=True,
    )


def _deadline():
    base = _dt.date(2026, 9, 1)
    return Deadline(base_date=base, business_days=5, computed_date=base + _dt.timedelta(days=5))


# ── from_deficiency_item ──────────────────────────────

class TestFromDeficiencyItem:
    def test_starts_detected_with_history(self):
        record = from_deficiency_item(_item(), date=_dt.date(2026, 9, 1))
        assert record.status == DETECTED
        assert record.detected_at == _dt.date(2026, 9, 1)
        assert len(record.history) == 1
        assert record.history[0]["to"] == DETECTED

    def test_tracking_key_matches_deficiency_item(self):
        item = _item()
        record = from_deficiency_item(item)
        assert record.tracking_key == item.tracking_key


# ── transition ────────────────────────────────────────

class TestTransition:
    def test_valid_chain(self):
        r = from_deficiency_item(_item())
        r = transition(r, DRAFT_READY, date=_dt.date(2026, 9, 2))
        assert r.status == DRAFT_READY
        assert r.draft_ready_at == _dt.date(2026, 9, 2)
        r = transition(r, REQUEST_SENT, date=_dt.date(2026, 9, 3), deadline=_deadline())
        assert r.status == REQUEST_SENT
        assert r.deadline is not None
        r = transition(r, RESUBMITTED, date=_dt.date(2026, 9, 5))
        r = transition(r, REVALIDATED, date=_dt.date(2026, 9, 6))
        r = transition(r, RESOLVED, date=_dt.date(2026, 9, 7))
        assert r.status == RESOLVED
        assert len(r.history) == 6  # detected + 5 次轉移

    def test_invalid_transition_raises(self):
        r = from_deficiency_item(_item())
        with pytest.raises(ValueError):
            transition(r, REQUEST_SENT)  # 不能跳過 draft_ready

    def test_revalidated_can_return_to_request_sent(self):
        r = from_deficiency_item(_item())
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        r = transition(r, RESUBMITTED)
        r = transition(r, REVALIDATED)
        r = transition(r, REQUEST_SENT, note="複驗仍未過")
        assert r.status == REQUEST_SENT

    def test_resolved_can_reopen_to_detected(self):
        r = from_deficiency_item(_item())
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        r = transition(r, RESOLVED)
        r = transition(r, DETECTED, note="復發")
        assert r.status == DETECTED
        assert r.resolved_at is None

    def test_resolved_records_version_when_given(self):
        r = from_deficiency_item(_item())
        r = transition(r, RESOLVED, resolved_version="batch-2026-09-07")
        assert r.resolved_version == "batch-2026-09-07"

    def test_terminal_states_have_no_outgoing_except_resolved(self):
        r = from_deficiency_item(_item())
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        r = transition(r, RESUBMITTED)
        with pytest.raises(ValueError):
            transition(r, DRAFT_READY)  # resubmitted 不能跳回 draft_ready


# ── IssueRecordStore ──────────────────────────────────

class TestStore:
    def test_by_status_owner_case(self):
        a = from_deficiency_item(_item(case_key="A"))
        b = transition(from_deficiency_item(_item(case_key="B")), DRAFT_READY)
        store = IssueRecordStore(records={a.tracking_key: a, b.tracking_key: b})
        assert store.by_status(DETECTED) == [a]
        assert store.by_status(DRAFT_READY) == [b]
        assert store.by_owner("student") == [a, b]
        assert store.by_case("A") == [a]

    def test_active_excludes_resolved(self):
        a = from_deficiency_item(_item(case_key="A"))
        b = transition(from_deficiency_item(_item(case_key="B")), RESOLVED)
        store = IssueRecordStore(records={a.tracking_key: a, b.tracking_key: b})
        assert store.active() == [a]


# ── reconcile ─────────────────────────────────────────

class TestReconcile:
    def test_new_item_creates_detected_record(self):
        store = reconcile(IssueRecordStore.empty(), [_item()])
        assert len(store.all()) == 1
        assert store.all()[0].status == DETECTED

    def test_missing_item_resolves(self):
        previous = reconcile(IssueRecordStore.empty(), [_item(case_key="A")])
        result = reconcile(previous, [])  # A 這次沒再偵測到
        assert result.get(("A", "NO_COMMITTEE", "委員名單")).status == RESOLVED

    def test_still_present_item_unchanged(self):
        previous = reconcile(IssueRecordStore.empty(), [_item(case_key="A")])
        advanced = transition(previous.get(("A", "NO_COMMITTEE", "委員名單")), DRAFT_READY)
        previous.put(advanced)
        result = reconcile(previous, [_item(case_key="A")])
        assert result.get(("A", "NO_COMMITTEE", "委員名單")).status == DRAFT_READY

    def test_resolved_item_reappearing_reopens(self):
        previous = reconcile(IssueRecordStore.empty(), [_item(case_key="A")])
        resolved = transition(previous.get(("A", "NO_COMMITTEE", "委員名單")), RESOLVED)
        previous.put(resolved)
        result = reconcile(previous, [_item(case_key="A")])
        assert result.get(("A", "NO_COMMITTEE", "委員名單")).status == DETECTED


# ── 序列化 ────────────────────────────────────────────

class TestSerialization:
    def test_round_trip_preserves_fields(self):
        r = from_deficiency_item(_item())
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT, deadline=_deadline())
        restored = record_from_dict(record_to_dict(r))
        assert restored == r

    def test_load_missing_file_returns_empty(self, tmp_path):
        store = load(tmp_path / "nope.json")
        assert store.all() == []

    def test_save_and_load_round_trip(self, tmp_path):
        path = tmp_path / "issues.json"
        store = reconcile(IssueRecordStore.empty(), [_item(case_key="A"), _item(case_key="B")])
        save(store, path)
        restored = load(path)
        assert {r.tracking_key for r in restored.all()} == {r.tracking_key for r in store.all()}


# ── reconcile 跳過 resubmitted ───────────────────────

class TestReconcileSkipsResubmitted:
    def test_reconcile_does_not_resolve_resubmitted(self):
        """resubmitted 的記錄即使本次掃描消失也不由 reconcile 直接解決。"""
        store = IssueRecordStore.empty()
        r = from_deficiency_item(_item(case_key="A"))
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        r = transition(r, RESUBMITTED)
        store.put(r)
        result = reconcile(store, [])  # A 不在本次掃描
        assert result.get(("A", "NO_COMMITTEE", "委員名單")).status == RESUBMITTED

    def test_reconcile_still_resolves_request_sent(self):
        """request_sent（學生尚未重送）的記錄消失時仍直接 resolved。"""
        store = IssueRecordStore.empty()
        r = from_deficiency_item(_item(case_key="A"))
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        store.put(r)
        result = reconcile(store, [])
        assert result.get(("A", "NO_COMMITTEE", "委員名單")).status == RESOLVED


# ── revalidate ───────────────────────────────────────

class TestRevalidate:
    def _resubmitted_store(self, case_key="A"):
        r = from_deficiency_item(_item(case_key=case_key))
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        r = transition(r, RESUBMITTED)
        store = IssueRecordStore.empty()
        store.put(r)
        return store

    def test_issue_gone_resolves(self):
        store = self._resubmitted_store()
        result = revalidate(store, set())  # 問題消失
        rec = result.get(("A", "NO_COMMITTEE", "委員名單"))
        assert rec.status == RESOLVED
        assert rec.revalidated_at is not None

    def test_issue_still_present_returns_to_request_sent(self):
        key = ("A", "NO_COMMITTEE", "委員名單")
        store = self._resubmitted_store()
        result = revalidate(store, {key})  # 問題仍在
        rec = result.get(key)
        assert rec.status == REQUEST_SENT
        assert rec.revalidated_at is not None

    def test_skips_non_resubmitted(self):
        store = IssueRecordStore.empty()
        r = from_deficiency_item(_item(case_key="A"))
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        store.put(r)
        result = revalidate(store, set())
        assert result.get(("A", "NO_COMMITTEE", "委員名單")).status == REQUEST_SENT

    def test_custom_reviewer(self):
        """自訂 reviewer 可以覆寫自動檢核的結論。"""
        key = ("A", "NO_COMMITTEE", "委員名單")
        store = self._resubmitted_store()

        def always_fail(record, still_detected):
            return RevalidationVerdict(passed=False, note="LLM 判定內容不足")

        result = revalidate(store, set(), reviewer=always_fail)
        rec = result.get(key)
        assert rec.status == REQUEST_SENT
        assert any("LLM 判定內容不足" in h["note"] for h in rec.history)

    def test_history_records_both_steps(self):
        store = self._resubmitted_store()
        result = revalidate(store, set())
        rec = result.get(("A", "NO_COMMITTEE", "委員名單"))
        statuses = [h["to"] for h in rec.history]
        assert REVALIDATED in statuses
        assert RESOLVED in statuses

    def test_automated_reviewer_pass(self):
        r = from_deficiency_item(_item())
        v = automated_reviewer(r, still_detected=False)
        assert v.passed is True

    def test_automated_reviewer_fail(self):
        r = from_deficiency_item(_item())
        v = automated_reviewer(r, still_detected=True)
        assert v.passed is False


# ── mark_case_resubmitted ────────────────────────────

class TestMarkCaseResubmitted:
    def test_marks_request_sent_issues(self, tmp_path):
        from koushi.application import mark_case_resubmitted

        path = tmp_path / "issue_records.json"
        r = from_deficiency_item(_item(case_key="1142-A"))
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        store = IssueRecordStore.empty()
        store.put(r)
        save(store, path)

        mark_case_resubmitted("1142-A", tmp_path)

        result = load(path)
        assert result.get(("1142-A", "NO_COMMITTEE", "委員名單")).status == RESUBMITTED

    def test_ignores_other_statuses(self, tmp_path):
        from koushi.application import mark_case_resubmitted

        path = tmp_path / "issue_records.json"
        r = from_deficiency_item(_item(case_key="1142-A"))
        r = transition(r, DRAFT_READY)
        store = IssueRecordStore.empty()
        store.put(r)
        save(store, path)

        mark_case_resubmitted("1142-A", tmp_path)

        result = load(path)
        assert result.get(("1142-A", "NO_COMMITTEE", "委員名單")).status == DRAFT_READY

    def test_ignores_other_cases(self, tmp_path):
        from koushi.application import mark_case_resubmitted

        path = tmp_path / "issue_records.json"
        r = from_deficiency_item(_item(case_key="1142-B"))
        r = transition(r, DRAFT_READY)
        r = transition(r, REQUEST_SENT)
        store = IssueRecordStore.empty()
        store.put(r)
        save(store, path)

        mark_case_resubmitted("1142-A", tmp_path)

        result = load(path)
        assert result.get(("1142-B", "NO_COMMITTEE", "委員名單")).status == REQUEST_SENT

    def test_noop_when_no_records(self, tmp_path):
        from koushi.application import mark_case_resubmitted

        mark_case_resubmitted("1142-A", tmp_path)
        assert not (tmp_path / "issue_records.json").exists()
