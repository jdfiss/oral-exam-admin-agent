# -*- coding: utf-8 -*-
"""通訊層測試：收件人解析與草稿結構。不觸發任何真實寄信。"""

import json

import pytest

from koushi import config as C, send_log
from koushi.drafts import DraftTemplateEngine
from koushi.engines.deficiency import DeficiencyItem
from koushi.mail import notify, recipients
from koushi.scheduler import ScheduledReminder


def _issue(code="MISSING_SUBMISSION", blocking=False, owner="student",
           case_key="1142-1115020", name="學生甲"):
    """仿 validation.check() 產出的一筆 issue。"""
    return {
        "案件鍵": case_key, "學期": "1142", "學號": "1115020", "姓名": name,
        "嚴重度": "阻擋" if blocking else "警示",
        "問題代碼": code, "欄位": "附件3", "說明": "測試用說明",
        "阻擋產出": "是" if blocking else "否",
        "阻擋附件4": "是" if blocking else "否",
        "阻擋附件6": "是" if blocking else "否",
        "來源": "檢核", "責任": owner,
    }


def _contacts_file(tmp_path, payload):
    path = tmp_path / "contacts.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ── 通訊錄讀取 ────────────────────────────────────────

class TestLoadContacts:
    def test_missing_file_returns_empty(self, tmp_path):
        """介面層尚未上線時檔案不存在，不該當成錯誤。"""
        contacts = recipients.load_contacts(tmp_path / "nope.json")
        assert contacts == {"students": {}, "advisors": {}, "office": None}

    def test_reads_all_three_sections(self, tmp_path):
        path = _contacts_file(tmp_path, {
            "students": {"1142-1115020": "s@example.edu"},
            "advisors": {"王大明": "prof@example.edu"},
            "office": "office@example.edu",
        })
        contacts = recipients.load_contacts(path)
        assert contacts["students"]["1142-1115020"] == "s@example.edu"
        assert contacts["advisors"]["王大明"] == "prof@example.edu"
        assert contacts["office"] == "office@example.edu"

    def test_partial_sections_fill_defaults(self, tmp_path):
        path = _contacts_file(tmp_path, {"students": {"1142-1115020": "s@example.edu"}})
        contacts = recipients.load_contacts(path)
        assert contacts["advisors"] == {}
        assert contacts["office"] is None

    def test_malformed_json_raises(self, tmp_path):
        path = tmp_path / "contacts.json"
        path.write_text("{ not json", encoding="utf-8")
        with pytest.raises(ValueError, match="通訊錄格式錯誤"):
            recipients.load_contacts(path)

    def test_non_object_root_raises(self, tmp_path):
        path = _contacts_file(tmp_path, ["a", "b"])
        with pytest.raises(ValueError, match="最外層必須是物件"):
            recipients.load_contacts(path)


# ── 收件人解析 ────────────────────────────────────────

class TestResolve:
    def test_student_found(self):
        contacts = {"students": {"1142-1115020": "s@example.edu"}}
        assert recipients.resolve_student("1142-1115020", contacts) == "s@example.edu"

    def test_student_not_found_returns_none(self):
        assert recipients.resolve_student("1142-9999999", {"students": {}}) is None

    def test_no_contacts_returns_none(self):
        """沒有通訊錄時一律回 None，絕不臆造地址。"""
        assert recipients.resolve_student("1142-1115020") is None

    def test_blank_email_treated_as_missing(self):
        contacts = {"students": {"1142-1115020": "   "}}
        assert recipients.resolve_student("1142-1115020", contacts) is None

    def test_email_is_stripped(self):
        contacts = {"students": {"1142-1115020": "  s@example.edu  "}}
        assert recipients.resolve_student("1142-1115020", contacts) == "s@example.edu"

    def test_advisor_and_office(self):
        contacts = {"advisors": {"王大明": "p@example.edu"}, "office": "o@example.edu"}
        assert recipients.resolve_advisor("王大明", contacts) == "p@example.edu"
        assert recipients.resolve_advisor("不存在", contacts) is None
        assert recipients.resolve_office(contacts) == "o@example.edu"

    def test_missing_students_lists_gaps(self):
        contacts = {"students": {"A": "a@example.edu"}}
        assert recipients.missing_students(["A", "B", "C"], contacts) == ["B", "C"]


# ── 草稿結構 ──────────────────────────────────────────

class TestComposeEmail:
    def test_ready_when_recipient_known(self):
        draft = notify.compose_email("學生甲", "1142-1115020", [_issue()],
                                     to="s@example.edu")
        assert draft["status"] == notify.STATUS_READY
        assert draft["to"] == "s@example.edu"
        assert draft["type"] == notify.TYPE_STUDENT_DEFICIENCY
        assert draft["cc"] is None

    def test_no_recipient_when_email_unknown(self):
        """查無 email 仍要產出草稿，但標記為不可寄。"""
        draft = notify.compose_email("學生甲", "1142-1115020", [_issue()], to=None)
        assert draft["status"] == notify.STATUS_NO_RECIPIENT
        assert draft["to"] is None

    def test_issue_codes_deduplicated_and_sorted(self):
        """issue_codes 是 send_log 去重的鍵，必須穩定。"""
        issues = [_issue("NO_EXAM_DATE"), _issue("MISSING_SUBMISSION"),
                  _issue("NO_EXAM_DATE")]
        draft = notify.compose_email("學生甲", "1142-1115020", issues, to="s@x.tw")
        assert draft["metadata"]["issue_codes"] == ["MISSING_SUBMISSION", "NO_EXAM_DATE"]

    def test_counts_split_blocking_and_warning(self):
        issues = [_issue("NO_COMMITTEE", blocking=True), _issue("NO_EXAM_DATE")]
        draft = notify.compose_email("學生甲", "1142-1115020", issues, to="s@x.tw")
        assert draft["metadata"]["blocking_count"] == 1
        assert draft["metadata"]["warning_count"] == 1

    def test_body_lists_both_sections(self):
        issues = [_issue("NO_COMMITTEE", blocking=True), _issue("NO_EXAM_DATE")]
        body = notify.compose_email("學生甲", "1142-1115020", issues, to="s@x.tw")["body"]
        assert "【必須補正】" in body
        assert "【提醒項目】" in body
        assert "NO_COMMITTEE" not in body  # 不顯示內部問題代碼

    def test_subject_carries_name_and_count(self):
        draft = notify.compose_email("學生甲", "1142-1115020",
                                     [_issue(), _issue("NO_EXAM_DATE")], to="s@x.tw")
        assert "學生甲" in draft["subject"]
        assert "1142-1115020" in draft["subject"]
        assert "2 項" in draft["subject"]


# ── 寄送篩選 ──────────────────────────────────────────

class TestSendDrafts:
    def _drafts(self):
        return [
            notify.compose_email("甲", "1142-A", [_issue()], to="a@example.edu"),
            notify.compose_email("乙", "1142-B", [_issue()], to=None),
        ]

    def test_partition(self):
        drafts = self._drafts()
        assert len(notify.sendable(drafts)) == 1
        assert len(notify.unsendable(drafts)) == 1

    def test_skips_no_recipient(self, monkeypatch):
        sent_to = []
        monkeypatch.setattr(
            "koushi.mail.email_sender.send_email",
            lambda to, subject, body, cc=None: sent_to.append(to) or True,
        )
        sent = notify.send_drafts(self._drafts())
        assert sent_to == ["a@example.edu"]
        assert len(sent) == 1

    def test_override_sends_everything(self, monkeypatch):
        """測試模式：連查無收件人的草稿也寄到自己信箱，方便驗證組信。"""
        sent_to = []
        monkeypatch.setattr(
            "koushi.mail.email_sender.send_email",
            lambda to, subject, body, cc=None: sent_to.append(to) or True,
        )
        sent = notify.send_drafts(self._drafts(), override_to="me@example.edu")
        assert sent_to == ["me@example.edu", "me@example.edu"]
        assert len(sent) == 2


# ── send_log 整合 ─────────────────────────────────────

class TestSendDraftsSendLog:
    def _draft(self, code="NO_COMMITTEE"):
        return notify.compose_email("甲", "1142-A", [_issue(code=code)], to="a@example.edu")

    def test_draft_carries_idempotency_key_and_tracking(self):
        draft = self._draft()
        assert draft["idempotency_key"]
        assert draft["tracking"]["case_key"] == "1142-A"

    def test_default_log_path_uses_shared_state_directory(self, tmp_path, monkeypatch):
        """獨立寄件指令也要寫入工作台讀取的固定跨批次狀態目錄。"""
        state_dir = tmp_path / "state"
        monkeypatch.setattr(C, "STATE_DIR", state_dir)

        assert notify.default_send_log_path() == state_dir / "send_log.json"

    def test_shared_log_dedups_across_two_calls(self, monkeypatch):
        """同一份 log 傳進第二次呼叫，同一追蹤鍵不會被寄第二次。"""
        sent_to = []
        monkeypatch.setattr(
            "koushi.mail.email_sender.send_email",
            lambda to, subject, body, cc=None: sent_to.append(to) or True,
        )
        log = send_log.SendEventLog()
        notify.send_drafts([self._draft()], log=log)
        notify.send_drafts([self._draft()], log=log)  # 同案件、同缺件代碼
        assert sent_to == ["a@example.edu"]  # 第二次被 idempotency_key 擋下

    def test_log_records_sent_status(self, monkeypatch):
        monkeypatch.setattr(
            "koushi.mail.email_sender.send_email",
            lambda to, subject, body, cc=None: True,
        )
        log = send_log.SendEventLog()
        draft = self._draft()
        notify.send_drafts([draft], log=log)
        event = log.get(draft["idempotency_key"])
        assert event.status == send_log.SENT

    def test_override_mode_does_not_touch_log(self, monkeypatch):
        """--to me 測試模式不經過 send_log，重複呼叫不會被去重擋下。"""
        sent_to = []
        monkeypatch.setattr(
            "koushi.mail.email_sender.send_email",
            lambda to, subject, body, cc=None: sent_to.append(to) or True,
        )
        draft = self._draft()
        notify.send_drafts([draft], override_to="me@x.tw")
        notify.send_drafts([draft], override_to="me@x.tw")
        assert sent_to == ["me@x.tw", "me@x.tw"]


# ── send_scheduled：實際寄出 ReminderScheduler 排程出的催件草稿 ─────────

class TestSendScheduled:
    def _reminder(self, *, to="a@example.edu", case_key="1142-A"):
        item = DeficiencyItem(
            case_key=case_key, term="1142", student_id="A", student_name="甲",
            issue_code="NO_COMMITTEE", field="委員名單", description="測試",
            owner="student", blocks_att4=True, blocks_att6=True,
        )
        draft = DraftTemplateEngine().deficiency_draft(case_key, "甲", [item], to=to)
        return ScheduledReminder(kind="deficiency", tracking_key=(case_key,), due_date=None, draft=draft)

    def test_sends_ready_drafts_and_skips_no_recipient(self):
        sent_to = []
        transport = lambda *, to, subject, body, cc=None: sent_to.append(to) or True
        ready = self._reminder()
        no_recipient = self._reminder(to=None, case_key="1142-B")
        sent = notify.send_scheduled([ready, no_recipient], transport=transport)
        assert sent_to == ["a@example.edu"]
        assert sent == [ready]

    def test_shared_log_dedups_across_two_calls(self):
        """同一份 log 傳進第二次呼叫，同一追蹤鍵（同案件同缺件代碼）不會被寄第二次。"""
        sent_to = []
        transport = lambda *, to, subject, body, cc=None: sent_to.append(to) or True
        log = send_log.SendEventLog()
        notify.send_scheduled([self._reminder()], log=log, transport=transport)
        notify.send_scheduled([self._reminder()], log=log, transport=transport)
        assert sent_to == ["a@example.edu"]

    def test_failed_transport_does_not_count_as_sent(self):
        log = send_log.SendEventLog()
        sent = notify.send_scheduled([self._reminder()], log=log, transport=lambda **kw: False)
        assert sent == []
        assert log.by_status(send_log.FAILED)


def test_student_letter_states_deadline_and_hides_codes():
    """review-2026-09-23 P1-7：補件信寫明補件截止日，不顯示內部問題代碼。"""
    import datetime as dt
    issue = {"問題代碼": "ADVISOR_SOURCE_UNAVAILABLE", "說明": "尚未在介面填寫指導教授",
             "欄位": "指導教授", "阻擋產出": "是", "責任": "student"}
    body = notify.compose_email("學生甲", "1141-A", [issue], to="a@x.tw",
                                deadline=dt.date(2026, 10, 8))["body"]
    assert "補件截止日：2026-10-08" in body
    assert "ADVISOR_SOURCE_UNAVAILABLE" not in body
    assert "尚未在系統填寫指導教授" in body and "介面" not in body
