# -*- coding: utf-8 -*-
"""事件時間線測試：只能追加、查詢、序列化。"""

from koushi.audit_log import AuditLog, append_and_save, load, save


class TestAppend:
    def test_sequence_increments(self):
        log = AuditLog()
        e1 = log.append("issue_store", "status_changed")
        e2 = log.append("issue_store", "status_changed")
        assert e1.sequence == 1
        assert e2.sequence == 2

    def test_stores_source_action_tracking_detail(self):
        log = AuditLog()
        event = log.append(
            "send_log", "sent",
            tracking_key=("1142-A", "NO_COMMITTEE", "委員名單"),
            detail={"to": "s@x.tw"},
        )
        assert event.source == "send_log"
        assert event.action == "sent"
        assert event.tracking_key == ("1142-A", "NO_COMMITTEE", "委員名單")
        assert event.detail == {"to": "s@x.tw"}

    def test_len_reflects_event_count(self):
        log = AuditLog()
        log.append("a", "x")
        log.append("b", "y")
        assert len(log) == 2


class TestQueries:
    def test_by_source(self):
        log = AuditLog()
        log.append("issue_store", "status_changed")
        log.append("send_log", "sent")
        assert len(log.by_source("issue_store")) == 1
        assert len(log.by_source("send_log")) == 1

    def test_by_tracking_key(self):
        log = AuditLog()
        log.append("issue_store", "status_changed", tracking_key=("A", "X", "F"))
        log.append("issue_store", "status_changed", tracking_key=("B", "X", "F"))
        matched = log.by_tracking_key(("A", "X", "F"))
        assert len(matched) == 1

    def test_since_excludes_up_to_given_sequence(self):
        log = AuditLog()
        log.append("a", "1")
        log.append("a", "2")
        log.append("a", "3")
        newer = log.since(1)
        assert [e.action for e in newer] == ["2", "3"]


# ── 序列化 ────────────────────────────────────────────

class TestSerialization:
    def test_load_missing_file_returns_empty(self, tmp_path):
        log = load(tmp_path / "nope.json")
        assert len(log) == 0

    def test_save_and_load_round_trip_preserves_order(self, tmp_path):
        path = tmp_path / "audit.json"
        log = AuditLog()
        log.append("issue_store", "status_changed", tracking_key=("A", "X", "F"))
        log.append("send_log", "sent", detail={"to": "s@x.tw"})
        save(log, path)
        restored = load(path)
        assert [e.action for e in restored.all()] == ["status_changed", "sent"]
        assert restored.all()[0].tracking_key == ("A", "X", "F")

    def test_append_and_save_persists_immediately(self, tmp_path):
        path = tmp_path / "audit.json"
        log = AuditLog()
        append_and_save(log, path, "cli", "status_run")
        restored = load(path)
        assert len(restored) == 1
        assert restored.all()[0].action == "status_run"


def test_concurrent_records_keep_every_event(tmp_path):
    """多個請求同時寫稽核：讀→改→寫整段在狀態鎖內，不可掉事件或拋 PermissionError。"""
    import threading
    from koushi.audit_log import record

    path = tmp_path / "audit_log.json"
    errors = []

    def worker(n):
        try:
            for i in range(10):
                record(path, "test", "append", detail={"worker": n, "i": i})
        except Exception as exc:  # noqa: BLE001 - 測試要收集任何錯誤
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    events = load(path).all()
    assert len(events) == 80
    assert len({e.sequence for e in events}) == 80
