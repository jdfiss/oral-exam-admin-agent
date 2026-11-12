# -*- coding: utf-8 -*-
"""步驟 4：白名單自動放行 — evaluate / execute / shadow mode / compare-and-set。

測試 evaluate_auto_confirms() 與 execute_auto_confirms()，
不碰真實信箱、LLM 或 LibreOffice。
"""
import contextlib
import datetime as dt
import json

import pytest

from koushi import advisor_confirmation as AC
from koushi import application, case_review
from koushi.classifier import ClassificationResult
from koushi.engines.confirmation import CONFIRMED, MODIFIED, PENDING, ConfirmationRequest
from koushi.engines.state import load_from_dir
from koushi.settings import OfficeSettings

from tests.test_application_generate import _clean_exam, _save_master

TODAY = dt.date(2026, 10, 1)


# ── Fixture ──────────────────────────────────────────────────────

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("koushi.clock.now", lambda: dt.datetime(2026, 10, 1, 10, tzinfo=dt.timezone(dt.timedelta(hours=8))))
    """與 test_advisor_confirmation.py 相同的環境，但兩位教授都已放行＋寄出。"""
    @contextlib.contextmanager
    def fake_template(path, **kwargs):
        yield path

    def fake_write(master, term, *, template, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / f"附件4_{term}.docx"
        out.write_text("|".join(e.姓名 for e in master.exams), encoding="utf-8")
        return out

    monkeypatch.setattr("koushi.document_conversion.docx_template", fake_template)
    monkeypatch.setattr(AC, "write_term_document", fake_write)
    exams = [
        _clean_exam("1141", "A", "教授甲", dt.date(2026, 11, 1)),
        _clean_exam("1141", "B", "教授乙", dt.date(2026, 11, 2)),
    ]
    master_path, master = _save_master(tmp_path, exams)
    subs = tmp_path / "subs"
    subs.mkdir()
    for key, sid, advisor, email in (("1141-A", "A", "教授甲", "jia@example.org"),
                                     ("1141-B", "B", "教授乙", "yi@example.org")):
        (subs / f"{key}.json").write_text(json.dumps({
            "case_key": key, "term": "1141", "student_id": sid, "version": 1,
            "student_email": f"{sid}@example.org", "advisor_emails": {advisor: email},
        }), encoding="utf-8")
    sent = []

    def transport(**kwargs):
        sent.append(kwargs)
        return True

    state = tmp_path / "state"
    return {"master": master, "master_path": master_path, "state": state,
            "subs": subs, "sent": sent, "transport": transport}


def _send_and_get_request(env, advisor="教授甲"):
    """放行＋寄出確認信，回傳對應的 ConfirmationRequest。"""
    master = env["master"]
    exam = next(e for e in master.exams if e.指導教授 == advisor)
    case_review.clear(env["state"], exam=exam, master=master)
    application.process_advisor_confirmations(
        master=master, state_dir=env["state"], submissions_dir=env["subs"],
        send=True, transport=env["transport"], date=TODAY, template="unused.doc",
    )
    _, confirmation, _ = application._current_engine_lists(master, env["state"])
    return next(r for r in confirmation if r.advisor == advisor)


def _make_auto_item(env, advisor="教授甲", body="確認無誤，謝謝。"):
    """模擬一封已通過 classifier + checks 的自動放行候選信（InboxItem）。

    回傳 (item, requests) 以便測試 evaluate。
    """
    request = _send_and_get_request(env, advisor)
    ref = AC.ref_code(request)
    _, confirmation, _ = application._current_engine_lists(env["master"], env["state"])

    mail = AC.InboundMail(
        message_id=f"<auto-{advisor}>", from_addr=f"{'jia' if advisor == '教授甲' else 'yi'}@example.org",
        subject=f"Re: {ref}", received_at="2026-10-02T09:00:00+08:00",
        in_reply_to=AC.load_mail_state(env["state"])[ref]["message_id"], received_at_trusted=True,
        body=body, reply_text=body,
    )

    def confirm_classifier(m, r):
        return ClassificationResult(
            intent="confirm", evidence=body, model="test", prompt_version="v1.1",
        )

    added, auto = AC.ingest_replies(
        [mail], master=env["master"], requests=confirmation, state_dir=env["state"],
        token_of=lambda req: application.request_token(req.request_key),
        classifier=confirm_classifier,
    )
    assert len(auto) == 1, f"Expected 1 auto candidate, got {len(auto)}"
    return auto[0], confirmation


# ── Feature flag 測試 ────────────────────────────────────────────

def test_auto_confirm_disabled_rejects_all(env):
    """auto_confirm_enabled=False → 全部拒絕。"""
    item, requests = _make_auto_item(env)
    settings = OfficeSettings(auto_confirm_enabled=False, auto_confirm_whitelist=("教授甲",))
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    assert len(decisions) == 1
    assert decisions[0].approved is False
    assert decisions[0].reason == AC.REJECT_DISABLED


def test_auto_confirm_enabled_with_whitelist_approves(env):
    """auto_confirm_enabled=True + 白名單 + shadow=False → 放行。"""
    item, requests = _make_auto_item(env)
    settings = OfficeSettings(
        auto_confirm_enabled=True, auto_confirm_shadow=False,
        auto_confirm_whitelist=("教授甲",),
    )
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    assert len(decisions) == 1
    assert decisions[0].approved is True
    assert decisions[0].would_approve is True


# ── 白名單測試 ──────────────────────────────────────────────────

def test_advisor_not_in_whitelist_rejected(env):
    """教授不在白名單 → 拒絕。"""
    item, requests = _make_auto_item(env)
    settings = OfficeSettings(
        auto_confirm_enabled=True, auto_confirm_shadow=False,
        auto_confirm_whitelist=("教授丙",),  # 甲不在白名單
    )
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    assert decisions[0].approved is False
    assert decisions[0].reason == AC.REJECT_NOT_WHITELISTED


def test_empty_whitelist_rejects_all(env):
    """白名單為空 → 全部拒絕。"""
    item, requests = _make_auto_item(env)
    settings = OfficeSettings(
        auto_confirm_enabled=True, auto_confirm_shadow=False,
        auto_confirm_whitelist=(),
    )
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    assert decisions[0].approved is False
    assert decisions[0].reason == AC.REJECT_NOT_WHITELISTED


# ── Shadow mode 測試 ────────────────────────────────────────────

def test_shadow_mode_suggests_but_does_not_approve(env):
    """shadow=True → approved=False 但 shadow=True, would_approve=True。"""
    item, requests = _make_auto_item(env)
    settings = OfficeSettings(
        auto_confirm_enabled=True, auto_confirm_shadow=True,
        auto_confirm_whitelist=("教授甲",),
    )
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    assert decisions[0].approved is False
    assert decisions[0].shadow is True
    assert decisions[0].would_approve is True
    assert decisions[0].reason == AC.REJECT_SHADOW


# ── Compare-and-Set 測試 ────────────────────────────────────────

def test_version_changed_after_ingest_rejects(env):
    """ingest 後附件4 被修改 → compare-and-set 失敗 → 拒絕。"""
    item, requests = _make_auto_item(env)

    # 模擬附件4 內容變動：改口試方式
    env["master"].exams[0].口試方式 = "視訊"

    settings = OfficeSettings(
        auto_confirm_enabled=True, auto_confirm_shadow=False,
        auto_confirm_whitelist=("教授甲",),
    )
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    assert decisions[0].approved is False
    assert decisions[0].reason == AC.REJECT_VERSION_CHANGED


def test_other_advisors_change_does_not_break_compare_and_set(env):
    """附件4 全學期一份，但 CAS 版本只算這位教授自己的列：別位教授的案件改了，照樣放行。"""
    item, requests = _make_auto_item(env)
    env["master"].exams[1].口試地點 = "教授乙學生換教室"       # 教授乙的學生，不在教授甲的列
    env["master"].exams[1].口試方式 = "視訊"
    settings = OfficeSettings(auto_confirm_enabled=True, auto_confirm_shadow=False,
                              auto_confirm_whitelist=("教授甲",))
    from koushi import settings as office_settings
    office_settings.save(settings, env["state"])
    decisions = AC.evaluate_auto_confirms([item], master=env["master"], requests=requests,
                                          state_dir=env["state"], settings=settings)
    assert decisions[0].approved is True
    updated, applied = AC.execute_auto_confirms(decisions, requests=requests, state_dir=env["state"],
                                                date=TODAY, master=env["master"])
    assert len(applied) == 1
    assert next(r for r in updated if r.advisor == "教授甲").response == CONFIRMED


# ── execute 測試 ─────────────────────────────────────────────────

def test_execute_applies_approved_decisions(env):
    """approved=True 的決策被執行：request 變成 CONFIRMED，inbox item 被標記為 applied。"""
    item, requests = _make_auto_item(env)
    settings = OfficeSettings(
        auto_confirm_enabled=True, auto_confirm_shadow=False,
        auto_confirm_whitelist=("教授甲",),
    )
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    updated_requests, applied = AC.execute_auto_confirms(
        decisions, requests=requests, state_dir=env["state"], date=TODAY,
    )
    assert len(applied) == 1
    # 確認 request 已更新
    confirmed = next(r for r in updated_requests if r.advisor == "教授甲")
    assert confirmed.response == CONFIRMED
    assert confirmed.notes == "LLM 自動放行：" + item.message_id
    # inbox item 被標記
    inbox = AC.load_inbox(env["state"])
    handled = next(i for i in inbox if i.message_id == item.message_id)
    assert handled.status == AC.INBOX_APPLIED
    assert handled.handled_by == "auto_confirm"


def test_execute_skips_non_approved(env):
    """approved=False 的決策不執行，request 保持 PENDING。"""
    item, requests = _make_auto_item(env)
    settings = OfficeSettings(auto_confirm_enabled=False, auto_confirm_whitelist=("教授甲",))
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    updated_requests, applied = AC.execute_auto_confirms(
        decisions, requests=requests, state_dir=env["state"], date=TODAY,
    )
    assert len(applied) == 0
    pending = next(r for r in updated_requests if r.advisor == "教授甲")
    assert pending.response == PENDING


def test_execute_writes_audit_log(env):
    """所有決策（含拒絕、shadow）都寫入稽核日誌。"""
    item, requests = _make_auto_item(env)
    settings = OfficeSettings(
        auto_confirm_enabled=True, auto_confirm_shadow=True,
        auto_confirm_whitelist=("教授甲",),
    )
    decisions = AC.evaluate_auto_confirms(
        [item], master=env["master"], requests=requests,
        state_dir=env["state"], settings=settings,
    )
    AC.execute_auto_confirms(
        decisions, requests=requests, state_dir=env["state"], date=TODAY,
    )
    from koushi import audit_log
    log = audit_log.load(env["state"] / "audit_log.json")
    auto_entries = [e for e in log.all() if e.source == "auto_confirm"]
    assert len(auto_entries) >= 1
    entry = auto_entries[-1]
    assert entry.detail["shadow"] is True
    assert entry.detail["approved"] is False


# ── 設定序列化測試 ──────────────────────────────────────────────

def test_settings_roundtrip_with_auto_confirm(tmp_path):
    """自動放行設定可正確存取。"""
    from koushi import settings as S
    original = OfficeSettings(
        auto_confirm_enabled=True,
        auto_confirm_shadow=False,
        auto_confirm_whitelist=("教授甲", "教授乙"),
    )
    S.save(original, tmp_path)
    loaded = S.load(tmp_path)
    assert loaded.auto_confirm_enabled is True
    assert loaded.auto_confirm_shadow is False
    assert loaded.auto_confirm_whitelist == ("教授甲", "教授乙")


def test_settings_from_form_auto_confirm():
    """表單可設定自動放行欄位。"""
    from koushi import settings as S
    form = {
        "auto_confirm_enabled": "1",
        "auto_confirm_shadow": "0",
        "auto_confirm_whitelist": "教授甲，教授乙、教授丙",
    }
    settings = S.from_form(form)
    assert settings.auto_confirm_enabled is True
    assert settings.auto_confirm_shadow is False
    assert settings.auto_confirm_whitelist == ("教授甲", "教授乙", "教授丙")


def test_settings_default_auto_confirm_disabled():
    """預設 auto_confirm 關閉。"""
    settings = OfficeSettings()
    assert settings.auto_confirm_enabled is False
    assert settings.auto_confirm_shadow is True
    assert settings.auto_confirm_whitelist == ()


@pytest.mark.parametrize("enabled,shadow,whitelist,expected", [
    (False,False,("教授甲",),False), (True,True,("教授甲",),False),
    (True,False,("教授乙",),False), (True,False,("教授甲",),True),
])
def test_application_auto_confirm_policy_end_to_end(env,enabled,shadow,whitelist,expected):
    from dataclasses import replace
    from koushi.settings import save
    item,_=_make_auto_item(env)
    save(OfficeSettings(auto_confirm_enabled=enabled,auto_confirm_shadow=shadow,
        auto_confirm_whitelist=whitelist),env["state"])
    mail=replace(AC.mail_from_item(item),message_id="policy-entry-mail")
    classifier=lambda m,r: ClassificationResult(intent="confirm",evidence=m.reply_text,
        model="fake",prompt_version="test")
    application.ingest_advisor_replies(master=env["master"],state_dir=env["state"],
        mails=[mail],classifier=classifier)
    request=next(r for r in load_from_dir(env["state"]).confirmation if r.advisor=="教授甲")
    assert (request.response==CONFIRMED)==expected
    inbox=next(i for i in AC.load_inbox(env["state"]) if i.message_id==mail.message_id)
    assert (inbox.status==AC.INBOX_APPLIED)==expected


def test_application_rejects_evidence_not_in_original_reply(env):
    from dataclasses import replace
    from koushi.settings import save
    item,_=_make_auto_item(env)
    save(OfficeSettings(auto_confirm_enabled=True,auto_confirm_shadow=False,
        auto_confirm_whitelist=("教授甲",)),env["state"])
    mail=replace(AC.mail_from_item(item),message_id="unrelated-evidence-mail")
    classifier=lambda m,r: ClassificationResult(intent="confirm",evidence="原回覆不存在的文字",
        model="fake",prompt_version="test")
    added=application.ingest_advisor_replies(master=env["master"],state_dir=env["state"],
        mails=[mail],classifier=classifier)
    assert added[0].classifier_error and added[0].status==AC.INBOX_PENDING
    assert next(r for r in load_from_dir(env["state"]).confirmation if r.advisor=="教授甲").response!=CONFIRMED
