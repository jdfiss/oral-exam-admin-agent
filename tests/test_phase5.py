# -*- coding: utf-8 -*-
"""Phase 5 tests: state persistence and CLI status command."""

import datetime as _dt
import json
import shutil
import uuid
from pathlib import Path

import pytest

from koushi.deadline import Deadline
from koushi.engines.state import EngineState, load, save
from koushi.engines.deficiency import DeficiencyItem
from koushi.engines.confirmation import ConfirmationRequest, CONFIRMED, PENDING
from koushi.engines.collection import CollectionItem

_SCRATCH = Path(__file__).resolve().parent.parent / "outputs" / "_test_scratch"


@pytest.fixture()
def scratch():
    d = _SCRATCH / uuid.uuid4().hex[:8]
    d.mkdir(parents=True, exist_ok=True)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _deficiency_item(case_key="1142-A"):
    return DeficiencyItem(
        case_key=case_key, term="1142", student_id="111502A",
        student_name="學生A", issue_code="NO_COMMITTEE", field="委員名單",
        description="test", owner="student",
        blocks_att4=True, blocks_att6=True,
    )


def _confirmation_request(advisor="王教授"):
    return ConfirmationRequest(
        group_key=("1142", "碩", advisor, "9/10", ""),
        term="1142", advisor=advisor, degree="碩", date_text="9/10",
        student_names=("學生A",), case_keys=("1142-A",),
        response=PENDING, sent_date=_dt.date(2026, 9, 1),
        deadline=Deadline(
            base_date=_dt.date(2026, 9, 1), business_days=3,
            computed_date=_dt.date(2026, 9, 4),
        ),
    )


def _collection_item(advisor="王教授"):
    return CollectionItem(
        group_key=("1142", "碩", advisor, "9/10", ""),
        term="1142", advisor=advisor, degree="碩", date_text="9/10",
        student_names=("學生A",), case_keys=("1142-A",),
    )


class TestStatePersistence:
    def test_empty_state_when_no_file(self, scratch):
        state = load(scratch / "nonexistent.json")
        assert state.deficiency == []
        assert state.confirmation == []
        assert state.collection == []

    def test_round_trip(self, scratch):
        state = EngineState(
            deficiency=[_deficiency_item()],
            confirmation=[_confirmation_request()],
            collection=[_collection_item()],
        )
        path = scratch / "state.json"
        save(state, path)
        assert path.exists()

        restored = load(path)
        assert len(restored.deficiency) == 1
        assert restored.deficiency[0].case_key == "1142-A"
        assert len(restored.confirmation) == 1
        assert restored.confirmation[0].advisor == "王教授"
        assert len(restored.collection) == 1
        assert restored.collection[0].advisor == "王教授"

    def test_valid_json(self, scratch):
        state = EngineState(
            deficiency=[_deficiency_item()],
            confirmation=[],
            collection=[],
        )
        path = scratch / "state.json"
        save(state, path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "deficiency" in raw
        assert "confirmation" in raw
        assert "collection" in raw

    def test_creates_parent_dir(self, scratch):
        path = scratch / "sub" / "dir" / "state.json"
        state = EngineState(deficiency=[], confirmation=[], collection=[])
        save(state, path)
        assert path.exists()

    def test_round_trip_with_notified_deficiency(self, scratch):
        dl = Deadline(
            base_date=_dt.date(2026, 9, 1), business_days=5,
            computed_date=_dt.date(2026, 9, 8),
        )
        item = DeficiencyItem(
            case_key="1142-A", term="1142", student_id="111502A",
            student_name="學生A", issue_code="NO_COMMITTEE", field="委員名單",
            description="test", owner="student",
            blocks_att4=True, blocks_att6=True,
            notified_date=_dt.date(2026, 9, 1), deadline=dl,
        )
        state = EngineState(deficiency=[item], confirmation=[], collection=[])
        path = scratch / "state.json"
        save(state, path)
        restored = load(path)
        assert restored.deficiency[0].notified_date == _dt.date(2026, 9, 1)
        assert restored.deficiency[0].deadline.computed_date == _dt.date(2026, 9, 8)

    def test_round_trip_confirmed_request(self, scratch):
        req = ConfirmationRequest(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("學生A",), case_keys=("1142-A",),
            response=CONFIRMED, sent_date=_dt.date(2026, 9, 1),
            deadline=Deadline(
                base_date=_dt.date(2026, 9, 1), business_days=3,
                computed_date=_dt.date(2026, 9, 4),
            ),
            response_date=_dt.date(2026, 9, 3), notes="ok",
        )
        state = EngineState(deficiency=[], confirmation=[req], collection=[])
        path = scratch / "state.json"
        save(state, path)
        restored = load(path)
        assert restored.confirmation[0].response == CONFIRMED
        assert restored.confirmation[0].response_date == _dt.date(2026, 9, 3)

    def test_round_trip_collected_item(self, scratch):
        item = CollectionItem(
            group_key=("1142", "碩", "王教授", "9/10", ""),
            term="1142", advisor="王教授", degree="碩", date_text="9/10",
            student_names=("學生A",), case_keys=("1142-A",),
            sent_date=_dt.date(2026, 9, 1),
            deadline=Deadline(
                base_date=_dt.date(2026, 9, 1), business_days=5,
                computed_date=_dt.date(2026, 9, 8),
            ),
            collected_date=_dt.date(2026, 9, 5),
            reminder_count=1, last_reminder_date=_dt.date(2026, 9, 4),
        )
        state = EngineState(deficiency=[], confirmation=[], collection=[item])
        path = scratch / "state.json"
        save(state, path)
        restored = load(path)
        assert restored.collection[0].collected_date == _dt.date(2026, 9, 5)
        assert restored.collection[0].reminder_count == 1
