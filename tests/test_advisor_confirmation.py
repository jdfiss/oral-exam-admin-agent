# -*- coding: utf-8 -*-
"""附件4 自動寄指導教授與收教授回信（已確認流程決策第六節）。

不碰真實信箱與 LibreOffice：寄送用假 transport、附件4 轉檔換成假範本、收信直接給
InboundMail。全程假資料。
"""
import contextlib
import datetime as dt
import json
from pathlib import Path

import pytest

from koushi import advisor_confirmation as AC
from koushi import application, case_review
from koushi.engines.confirmation import CONFIRMED, MODIFIED
from koushi.engines.state import load_from_dir
from koushi.mail.inbox import parse_message

from tests.test_application_generate import _clean_exam, _save_master

TODAY = dt.date(2026, 10, 1)
_REAL_SENDABLE_KEYS = AC.sendable_keys  # fixture 會換成「依放行」測寄信機制；這裡保留正式版本


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("koushi.clock.now", lambda: dt.datetime(2026, 10, 1, 10, tzinfo=dt.timezone(dt.timedelta(hours=8))))
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
    # 這組測試驗寄信機制（信串、版本、重寄、回信比對），用「已放行」控制誰該收到信；
    # 2026-10-12 的正式寄送關卡（學生資料齊全即寄、不等放行）另見 test_sendable_keys_*。
    monkeypatch.setattr(AC, "sendable_keys", lambda master, state_dir: {
        e.案件鍵 for e in master.exams if e.生效} & case_review.cleared_keys(master, state_dir))
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

    return {"master": master, "master_path": master_path, "state": tmp_path / "state",
            "subs": subs, "sent": sent, "transport": transport}


def _run(env, *, master=None, send=True, date=TODAY):
    return application.process_advisor_confirmations(
        master=master or env["master"], state_dir=env["state"], submissions_dir=env["subs"],
        send=send, transport=env["transport"], date=date, template="unused.doc",
    )


def _request(env, advisor):
    return next(r for r in load_from_dir(env["state"]).confirmation if r.advisor == advisor)


def test_only_cleared_groups_are_sent_to_student_supplied_advisor_email(env):
    case_review.clear(env["state"], exam=env["master"].exams[0], master=env["master"])
    result = _run(env)

    assert [d["to"] for d in result.sent] == ["jia@example.org"]
    mail = env["sent"][0]
    assert "學生A" in mail["body"] and "學生B" not in mail["body"]  # 內文只列自己學生的列
    ref = AC.ref_code(_request(env, "教授甲"))
    assert ref in mail["subject"] and f"確認代碼：{ref}-" in mail["body"]
    assert len(mail["attachments"]) == 1

    req = _request(env, "教授甲")
    assert req.sent_date == TODAY and req.deadline is not None
    assert AC.load_mail_state(env["state"])[ref]["to"] == "jia@example.org"

    # 重跑不重寄；未放行的教授乙仍未寄出
    _run(env)
    assert len(env["sent"]) == 1
    assert _request(env, "教授乙").sent_date is None


def test_preview_builds_drafts_without_sending(env):
    for exam in env["master"].exams:
        case_review.clear(env["state"], exam=exam, master=env["master"])
    result = _run(env, send=False)
    assert len(result.drafts) == 2 and result.sent == [] and env["sent"] == []


def test_changed_attachment4_resets_confirmation_and_resends_new_version(env):
    master = env["master"]
    case_review.clear(env["state"], exam=master.exams[0], master=master)
    _run(env)
    token = application.request_token(_request(env, "教授甲").request_key)
    application.record_confirmation(master_path=env["master_path"], state_dir=env["state"],
                                    token=token, action="confirmed", actor="專員甲")
    assert _request(env, "教授甲").response == CONFIRMED

    master.exams[0].口試方式 = "視訊"  # 教授看到的內容變了
    result = _run(env, master=master, date=TODAY + dt.timedelta(days=1))
    assert result.reset == [AC.ref_code(_request(env, "教授甲"))]
    assert len(env["sent"]) == 2 and "視訊" in env["sent"][1]["body"]
    req = _request(env, "教授甲")
    assert req.response != CONFIRMED and req.sent_date == TODAY + dt.timedelta(days=1)
    assert AC.load_mail_state(env["state"])[AC.ref_code(req)]["rounds"] == 2


def _sent_and_reply(env, *, sender="教授甲 <jia@example.org>", body=None, reply_text=None,
                    subject=None, mid="<m1>", attachments=()):
    master = env["master"]
    case_review.clear(env["state"], exam=master.exams[0], master=master)
    _run(env)
    # 2026-10-12 起附件4 不等放行：資料齊全的教授都會收到，這裡取寄給教授甲的那封
    original = next(m for m in env["sent"] if m["to"] == "jia@example.org")
    env["baseline"] = len(env["sent"])  # 附件4 寄送信件數；之後的斷言只看有沒有「多寄」
    full_body = body if body is not None else "確認無誤，謝謝。\n\n> " + original["body"].replace("\n", "\n> ")
    # reply_text 預設取 body 本身（測試中通常不含引用結構），或明確指定
    if reply_text is None:
        reply_text = body if body is not None else "確認無誤，謝謝。"
    return AC.InboundMail(
        message_id=mid, from_addr=sender,
        subject=subject if subject is not None else "Re: " + original["subject"],
        received_at="2026-10-02T09:00:00+08:00",
        in_reply_to=AC.load_mail_state(env["state"])[AC.ref_code(_request(env, "教授甲"))]["message_id"],
        received_at_trusted=True,
        body=full_body,
        reply_text=reply_text,
        attachments=tuple(attachments),
    )


def test_reply_goes_to_review_queue_and_office_confirms(env):
    mail = _sent_and_reply(env)
    added = application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail])
    assert len(added) == 1 and added[0].checks_passed
    assert _request(env, "教授甲").response != CONFIRMED  # 沒有 LLM 時不自動放行

    # 同一封信再讀一次不重複收
    assert application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail]) == []

    application.handle_advisor_reply(master_path=env["master_path"], state_dir=env["state"],
                                     message_id="<m1>", action="confirmed", actor="專員甲")
    assert _request(env, "教授甲").response == CONFIRMED
    assert AC.load_inbox(env["state"])[0].status == AC.INBOX_APPLIED
    with pytest.raises(ValueError, match="已被處理"):
        application.handle_advisor_reply(master_path=env["master_path"], state_dir=env["state"],
                                         message_id="<m1>", action="confirmed", actor="專員甲")


def test_wrong_sender_and_old_version_fail_checks(env):
    mail = _sent_and_reply(env, sender="someone@else.org")
    item = application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail])[0]
    assert item.checks["寄件者為收件教授"] is False

    old = AC.InboundMail(message_id="<m2>", from_addr="jia@example.org", subject=mail.subject,
                         received_at="", body="確認無誤\n> 附件4 版本：0000000000",
                         reply_text="確認無誤")
    item = application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[old])[0]
    assert item.checks["附件4 為目前版本"] is False


def test_reply_without_code_matches_by_sender_only_when_unique(env):
    mail = _sent_and_reply(env, subject="口試一覽表", body="沒問題")
    item = application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail])[0]
    assert item.token and "依寄件者對應" in item.notes[0]

    stranger = AC.InboundMail(message_id="<m9>", from_addr="x@example.org", subject="hello",
                              received_at="", body="hi", reply_text="hi")
    assert application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[stranger]) == []


def test_classifier_confirm_auto_applies_only_when_checks_pass(env):
    from koushi import settings
    settings.save(settings.OfficeSettings(auto_confirm_enabled=True, auto_confirm_shadow=False,
        auto_confirm_whitelist=("教授甲",)), env["state"])
    mail = _sent_and_reply(env)
    application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail],
                                       classifier=lambda m, r: __import__("koushi.classifier", fromlist=["ClassificationResult"]).ClassificationResult(intent="confirm", evidence=m.reply_text))
    assert _request(env, "教授甲").response == CONFIRMED
    assert AC.load_inbox(env["state"])[0].status == AC.INBOX_APPLIED


def test_classifier_confirm_from_wrong_sender_stays_manual(env):
    mail = _sent_and_reply(env, sender="someone@else.org")
    application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail],
                                       classifier=lambda m, r: (AC.INTENT_CONFIRM, ""))
    assert _request(env, "教授甲").response != CONFIRMED
    assert AC.load_inbox(env["state"])[0].status == AC.INBOX_PENDING


def test_office_marks_modification_and_can_ignore(env):
    mail = _sent_and_reply(env, body="口試日期要改成 11/5")
    application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail])
    application.handle_advisor_reply(master_path=env["master_path"], state_dir=env["state"],
                                     message_id="<m1>", action="modified", actor="專員甲", note="改 11/5")
    req = _request(env, "教授甲")
    assert req.response == MODIFIED and req.notes == "改 11/5"


def test_parse_message_reads_headers_and_plain_body():
    raw = (
        "From: =?utf-8?b?5pWZ5o6I55Sy?= <jia@example.org>\r\n"
        "Subject: =?utf-8?b?UmU6IFvlj6PoqabpmYTku7Y056K66KqNIEE0LTEyMzRhYmNkXQ==?=\r\n"
        "Message-ID: <abc@example.org>\r\n"
        "Date: Fri, 02 Oct 2026 09:00:00 +0800\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "確認無誤"
    ).encode("utf-8")
    mail = parse_message(raw)
    assert mail.message_id == "<abc@example.org>"
    assert "jia@example.org" in mail.from_addr and "A4-1234abcd" in mail.subject
    assert mail.body.strip() == "確認無誤" and mail.received_at == "2026-10-02T01:00:00+00:00"
    assert mail.reply_text.strip() == "確認無誤"
    assert mail.attachments == ()


def test_quote_stripping_plain_text():
    """純文字回信含 > 引用行及 On ... wrote: 段落，reply_text 只保留本文。"""
    raw = (
        "From: jia@example.org\r\n"
        "Subject: Re: test\r\n"
        "Message-ID: <q1@example.org>\r\n"
        "Date: Fri, 02 Oct 2026 10:00:00 +0800\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "日期要改成11/5，謝謝。\n\n"
        "On 2026/10/1, 系辦 <office@example.org> wrote:\n"
        "> 內容正確：回覆「確認無誤」即可。\n"
        "> 附件4 版本：abc1234567\n"
    ).encode("utf-8")
    mail = parse_message(raw)
    assert "確認無誤" in mail.body  # 原文保留引用
    assert "確認無誤" not in mail.reply_text  # reply_text 已去除引用
    assert "日期要改成11/5" in mail.reply_text


def test_quote_stripping_greater_than_lines():
    """> 開頭的引用行被跳過。"""
    raw = (
        "From: jia@example.org\r\n"
        "Subject: Re: test\r\n"
        "Message-ID: <q2@example.org>\r\n"
        "Date: Fri, 02 Oct 2026 10:00:00 +0800\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "沒問題\n"
        "> 這是引用\n"
        "> 另一行引用\n"
    ).encode("utf-8")
    mail = parse_message(raw)
    assert "沒問題" in mail.reply_text
    assert "這是引用" not in mail.reply_text


def test_quote_stripping_outlook_original_message():
    """-----Original Message----- 及其後全部被切掉。"""
    raw = (
        "From: jia@example.org\r\n"
        "Subject: Re: test\r\n"
        "Message-ID: <q3@example.org>\r\n"
        "Date: Fri, 02 Oct 2026 10:00:00 +0800\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "OK，謝謝。\n\n"
        "-----Original Message-----\n"
        "From: office@example.org\n"
        "Sent: ...\n"
        "To: ...\n"
        "Subject: ...\n"
        "確認無誤\n"
    ).encode("utf-8")
    mail = parse_message(raw)
    assert "OK" in mail.reply_text
    assert "Original Message" not in mail.reply_text


def test_quote_stripping_html_gmail():
    """HTML 回信中 Gmail 的 gmail_quote div 被移除。"""
    raw = (
        "From: jia@example.org\r\n"
        "Subject: Re: test\r\n"
        "Message-ID: <q4@example.org>\r\n"
        "Date: Fri, 02 Oct 2026 10:00:00 +0800\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        '<div>好的，沒問題。</div>'
        '<div class="gmail_quote"><blockquote>確認無誤</blockquote></div>'
    ).encode("utf-8")
    mail = parse_message(raw)
    assert "沒問題" in mail.reply_text
    assert "確認無誤" not in mail.reply_text


def test_empty_reply_text_when_only_attachment():
    """只附檔不寫字時 reply_text 為空。"""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    msg = MIMEMultipart()
    msg["From"] = "jia@example.org"
    msg["Subject"] = "Re: test"
    msg["Message-ID"] = "<q5@example.org>"
    msg["Date"] = "Fri, 02 Oct 2026 10:00:00 +0800"

    # 空本文
    msg.attach(MIMEText("", "plain", "utf-8"))

    # 附件
    att = MIMEBase("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")
    att.set_payload(b"fake docx content")
    encoders.encode_base64(att)
    att.add_header("Content-Disposition", "attachment", filename="附件4修正版.docx")
    msg.attach(att)

    mail = parse_message(msg.as_bytes())
    assert mail.reply_text.strip() == ""
    assert len(mail.attachments) == 1
    assert mail.attachments[0].filename == "附件4修正版.docx"
    assert mail.has_real_attachments is True


def test_inline_signature_image_not_counted_as_attachment():
    """內嵌簽名圖（Content-Disposition: inline + Content-ID 被引用）不算真正附件。"""
    raw = (
        "From: jia@example.org\r\n"
        "Subject: Re: test\r\n"
        "Message-ID: <q6@example.org>\r\n"
        "Date: Fri, 02 Oct 2026 10:00:00 +0800\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/related; boundary="boundary123"\r\n\r\n'
        "--boundary123\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n"
        '<div>確認無誤</div><img src="cid:sig001">\r\n'
        "--boundary123\r\n"
        "Content-Type: image/png\r\n"
        "Content-Disposition: inline\r\n"
        "Content-ID: <sig001>\r\n"
        "Content-Transfer-Encoding: base64\r\n\r\n"
        "iVBORw0KGgoAAAANSUhEUg==\r\n"
        "--boundary123--\r\n"
    ).encode("utf-8")
    mail = parse_message(raw)
    assert mail.has_real_attachments is False
    assert "確認無誤" in mail.reply_text


def test_classifier_skipped_when_reply_text_empty(env):
    """reply_text 為空時不呼叫 classifier，直接轉人工。"""
    calls = []

    def tracking_classifier(m, r):
        calls.append(1)
        return (AC.INTENT_CONFIRM, "")

    mail = _sent_and_reply(env, body="", reply_text="")
    application.ingest_advisor_replies(
        master=env["master"], state_dir=env["state"], mails=[mail],
        classifier=tracking_classifier,
    )
    assert len(calls) == 0  # classifier 未被呼叫
    assert _request(env, "教授甲").response != CONFIRMED


def test_attachment_blocks_auto_confirmation(env):
    """有真正附件時即使 classifier 判為 confirm 也不自動放行。"""
    att = AC.AttachmentMeta(filename="修正版.docx", content_type="application/docx", size=1024, sha256="abc")
    mail = _sent_and_reply(env, attachments=(att,))
    application.ingest_advisor_replies(
        master=env["master"], state_dir=env["state"], mails=[mail],
        classifier=lambda m, r: (AC.INTENT_CONFIRM, ""),
    )
    # 有附件 → 不自動放行
    assert _request(env, "教授甲").response != CONFIRMED
    assert AC.load_inbox(env["state"])[0].status == AC.INBOX_PENDING


def test_classification_result_stored_in_inbox_item(env):
    from koushi import settings
    settings.save(settings.OfficeSettings(auto_confirm_enabled=True, auto_confirm_shadow=False,
        auto_confirm_whitelist=("教授甲",)), env["state"])
    """新版 ClassificationResult 完整存入 InboxItem.classification。"""
    from koushi.classifier import ClassificationResult

    def new_classifier(mail, request):
        return ClassificationResult(
            intent="confirm", evidence="確認無誤", model="test-model",
            prompt_version="v1.0", raw_output='{"intent":"confirm"}',
        )

    mail = _sent_and_reply(env)
    application.ingest_advisor_replies(
        master=env["master"], state_dir=env["state"], mails=[mail],
        classifier=new_classifier,
    )
    item = AC.load_inbox(env["state"])[0]
    assert item.classification is not None
    assert item.classification["model"] == "test-model"
    assert item.classification["prompt_version"] == "v1.0"
    assert item.classification["raw_output"] == '{"intent":"confirm"}'
    assert item.intent == "confirm"
    assert _request(env, "教授甲").response == CONFIRMED  # 核對通過，自動放行


def test_classifier_error_caught_per_message(env):
    """classifier 拋錯時逐封捕捉，不中斷整批；該封轉人工。"""
    from koushi.classifier import ClassifierError

    def failing_classifier(mail, request):
        raise ClassifierError("API timeout")

    mail = _sent_and_reply(env)
    added = application.ingest_advisor_replies(
        master=env["master"], state_dir=env["state"], mails=[mail],
        classifier=failing_classifier,
    )
    assert len(added) == 1
    item = AC.load_inbox(env["state"])[0]
    assert item.classifier_error == "API timeout"
    assert item.intent is None  # 未設定意圖
    assert _request(env, "教授甲").response != CONFIRMED  # 不自動放行


def test_has_reservation_blocks_auto_confirmation(env):
    """has_reservation=True 時即使 intent=confirm 也不自動放行。"""
    from koushi.classifier import ClassificationResult

    def reserved_classifier(mail, request):
        return ClassificationResult(
            intent="confirm", evidence="沒問題但日期好像不對",
            has_reservation=True, model="test", prompt_version="v1",
        )

    mail = _sent_and_reply(env, body="沒問題但日期好像不對")
    application.ingest_advisor_replies(
        master=env["master"], state_dir=env["state"], mails=[mail],
        classifier=reserved_classifier,
    )
    assert _request(env, "教授甲").response != CONFIRMED
    item = AC.load_inbox(env["state"])[0]
    assert item.classification["has_reservation"] is True


def test_classifier_cannot_auto_apply_when_feature_disabled(env):
    mail = _sent_and_reply(env)
    application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail],
        classifier=lambda m, r: (AC.INTENT_CONFIRM, "確認無誤"))
    assert _request(env, "教授甲").response != CONFIRMED
    assert AC.load_inbox(env["state"])[0].status == AC.INBOX_PENDING


def test_stale_inbox_reply_cannot_confirm_changed_logistics(env):
    mail = _sent_and_reply(env)
    application.ingest_advisor_replies(master=env["master"], state_dir=env["state"], mails=[mail])
    env["master"].exams[0].口試地點 = "新的模擬地點"
    from koushi import master as master_workbook
    master_workbook.save(env["master"], [], path=env["master_path"])
    with pytest.raises(ValueError, match="目前版本"):
        application.handle_advisor_reply(master_path=env["master_path"], state_dir=env["state"],
            message_id=mail.message_id, action="confirmed", actor="專員甲")
    assert _request(env, "教授甲").response != CONFIRMED


def test_manual_confirmation_is_invalidated_by_location_change(env):
    application.record_confirmation(master_path=env["master_path"], state_dir=env["state"],
        token="1141|教授甲", action="confirmed", actor="專員", note="電話確認")
    env["master"].exams[0].口試地點 = "新的模擬地點"
    _, reset = AC.reset_changed(env["master"], load_from_dir(env["state"]).confirmation, env["state"])
    assert [r.advisor for r in reset] == ["教授甲"]          # 內容變了，舊的確認不沿用、會重寄


def test_partial_smtp_failure_preserves_first_professor_and_retry_does_not_resend(env):
    for exam in env["master"].exams:
        case_review.clear(env["state"], exam=exam, master=env["master"])
    attempts = []
    def fail_second(**mail):
        attempts.append(mail["to"])
        if len(attempts) == 2:
            raise RuntimeError("simulated SMTP failure")
        return True
    with pytest.raises(RuntimeError):
        application.process_advisor_confirmations(master=env["master"],state_dir=env["state"],
            submissions_dir=env["subs"],send=True,transport=fail_second,date=TODAY,template="unused.doc")
    records = AC.load_mail_state(env["state"])
    first = next(r for r in load_from_dir(env["state"]).confirmation
                 if records.get(AC.ref_code(r), {}).get("to") == attempts[0])
    assert first.sent_date == TODAY
    result = _run(env)
    assert [d["to"] for d in result.sent] == [attempts[1]]


# ── 附件4 全學期一份（2026-10-10）──

def _current(env, advisor):
    """依目前主檔重算的請求（尚未寫回狀態檔的也看得到）。"""
    _, confirmation, _ = application._current_engine_lists(env["master"], env["state"])
    return next(r for r in confirmation if r.advisor == advisor)


def _clear(env, *exams):
    for exam in exams or env["master"].exams:
        case_review.clear(env["state"], exam=exam, master=env["master"])


def _confirm(env, advisor):
    token = application.request_token(_request(env, advisor).request_key)
    application.record_confirmation(master_path=env["master_path"], state_dir=env["state"],
                                    token=token, action="confirmed", actor="專員甲")


def _save(env):
    from koushi import master as master_workbook
    master_workbook.save(env["master"], [], path=env["master_path"])


def _add_case(env, sid, advisor, day, emails):
    from koushi.model import Member
    exam = _clean_exam("1141", sid, advisor, day)
    exam.委員 = [Member(committee_id="C-教授甲", 服務單位="國立範例大學資訊工程學系")]
    exam.derive()
    env["master"].exams.append(exam)
    (env["subs"] / f"1141-{sid}.json").write_text(json.dumps({
        "case_key": f"1141-{sid}", "term": "1141", "student_id": sid, "version": 1,
        "student_email": f"{sid}@example.org", "advisor_emails": emails,
    }), encoding="utf-8")
    _save(env)
    return exam


def test_one_shared_term_document_goes_to_every_cleared_advisor(env):
    _clear(env)
    result = _run(env)

    assert sorted(d["to"] for d in result.sent) == ["jia@example.org", "yi@example.org"]
    jia, yi = sorted(env["sent"], key=lambda m: m["to"])
    assert jia["attachments"] == yi["attachments"] and len(jia["attachments"]) == 1
    doc = Path(jia["attachments"][0])
    assert doc.read_text(encoding="utf-8") == "學生A|學生B"     # 同一份：全學期兩位教授的學生
    record = AC.load_documents(env["state"])["1141"]
    assert record["case_keys"] == ["1141-A", "1141-B"] and Path(record["path"]) == doc.resolve()
    # 內文說明是全學期一覽表，只列自己學生的列
    assert "全學期" in jia["subject"] and "每位指導教授收到的是同一份" in jia["body"]
    assert "學生A" in jia["body"] and "學生B" not in jia["body"]
    assert "學生B" in yi["body"] and "學生A" not in yi["body"]
    assert AC.ref_code(_request(env, "教授甲")) != AC.ref_code(_request(env, "教授乙"))


def test_mail_body_lists_own_rows_with_degree_time_place_and_committee(env):
    exam = env["master"].exams[0]
    exam.口試開始時間, exam.口試結束時間, exam.口試地點, exam.口試方式 = "14:00", "16:00", "工程館 301", "視訊"
    _clear(env, exam)
    _run(env)
    body = env["sent"][0]["body"]
    assert "  • 學生A（" in body and "11/1　14:00–16:00　工程館 301（視訊）" in body
    assert "    口試委員：C-教授甲" in body
    assert "不需回覆" in body and "「確認無誤」" not in body and "1141-A" not in body


def test_other_advisors_change_does_not_reset_or_resend_confirmed_advisor(env):
    _clear(env)
    _run(env)
    _confirm(env, "教授甲")
    jia_ref = AC.ref_code(_request(env, "教授甲"))

    env["master"].exams[1].口試地點 = "教授乙學生的新教室"   # 只動到教授乙的列
    result = _run(env, date=TODAY + dt.timedelta(days=1))

    assert _request(env, "教授甲").response == CONFIRMED
    assert result.reset == [AC.ref_code(_request(env, "教授乙"))]
    assert [m["to"] for m in env["sent"][2:]] == ["yi@example.org"]   # 不重寄給教授甲
    assert "教授乙學生的新教室" in env["sent"][2]["body"]
    assert AC.load_mail_state(env["state"])[jia_ref]["rounds"] == 1


def test_own_row_change_resets_and_resends_full_document_only_to_that_advisor(env):
    _clear(env)
    _run(env)
    _confirm(env, "教授乙")
    _confirm(env, "教授甲")

    env["master"].exams[0].口試方式 = "視訊"
    result = _run(env, date=TODAY + dt.timedelta(days=1))

    assert result.reset == [AC.ref_code(_request(env, "教授甲"))]
    resent = env["sent"][2:]
    assert [m["to"] for m in resent] == ["jia@example.org"]
    assert Path(resent[0]["attachments"][0]).read_text(encoding="utf-8") == "學生A|學生B"
    assert _request(env, "教授甲").response != CONFIRMED
    assert _request(env, "教授乙").response == CONFIRMED


def test_newly_released_advisor_gets_current_document_without_resending_others(env):
    _clear(env, env["master"].exams[0])
    _run(env)
    assert Path(env["sent"][0]["attachments"][0]).read_text(encoding="utf-8") == "學生A"

    _clear(env, env["master"].exams[1])
    _run(env, date=TODAY + dt.timedelta(days=1))
    assert [m["to"] for m in env["sent"]] == ["jia@example.org", "yi@example.org"]
    assert Path(env["sent"][1]["attachments"][0]).read_text(encoding="utf-8") == "學生A|學生B"
    assert _request(env, "教授甲").sent_date == TODAY


def test_advisor_is_sent_only_after_all_own_cases_are_released(env):
    extra = _add_case(env, "C", "教授甲", dt.date(2026, 11, 5), {"教授甲": "jia@example.org"})
    _clear(env, env["master"].exams[0])
    _run(env)
    assert env["sent"] == []
    assert _current(env, "教授甲").case_keys == ("1141-A", "1141-C")

    _clear(env, extra)
    _run(env)
    assert [m["to"] for m in env["sent"]] == ["jia@example.org"]
    assert "學生A" in env["sent"][0]["body"] and "學生C" in env["sent"][0]["body"]


def test_co_advised_case_goes_to_both_advisors_and_gates_post_exam_on_both(env):
    _add_case(env, "D", "教授甲、教授乙", dt.date(2026, 11, 3),
              {"教授甲": "jia@example.org", "教授乙": "yi@example.org"})
    _clear(env)
    _run(env)
    assert sorted(m["to"] for m in env["sent"]) == ["jia@example.org", "yi@example.org"]
    assert all("學生D" in m["body"] for m in env["sent"])
    assert env["sent"][0]["attachments"] == env["sent"][1]["attachments"]

    # 2026-10-12 起教授確認不擋口試後產檔：沒有任何「教授尚未確認」的阻擋問題
    assert not any(i.get("問題代碼") == "CONFIRMATION_PENDING"
                   for i in application._document_eligibility_issues(env["master"], env["state"]))


def test_waiting_advisor_document_is_refreshed_for_reminders(env):
    _clear(env)
    _run(env)
    before = AC.load_documents(env["state"])["1141"]

    env["master"].exams[1].口試方式 = "視訊"            # 全學期文件內容變了
    _add_case(env, "E", "教授乙", dt.date(2026, 11, 9), {"教授乙": "yi@example.org"})  # 教授乙暫不寄
    result = _run(env, date=TODAY + dt.timedelta(days=1))

    assert result.sent == [] and len(env["sent"]) == 2
    after = AC.load_documents(env["state"])["1141"]
    assert after["version"] != before["version"]       # 教授甲仍在等回覆：檔案更新成最新版本
    assert AC.find_attachment(env["state"], _current(env, "教授甲")) == Path(after["path"])
    assert AC.find_attachment(env["state"], _current(env, "教授乙")) is None   # 檔案沒有收錄 E


def test_term_document_is_one_official_table_with_every_included_advisor(tmp_path):
    """全學期附件4 用官方範本的同一張表：每位教授的列都在；未放行與別學期的案件不收錄。"""
    from docx import Document
    from koushi.model import Master
    from tests.test_generators_schema4 import _attachment4_template

    template = tmp_path / "att4.docx"
    _attachment4_template(template)
    exams = [_clean_exam("1141", "A", "教授甲", dt.date(2026, 11, 1)),
             _clean_exam("1141", "B", "教授乙", dt.date(2026, 11, 2)),
             _clean_exam("1141", "C", "教授丙", dt.date(2026, 11, 3)),
             _clean_exam("1142", "D", "教授甲", dt.date(2027, 4, 1))]
    master = Master(exams=exams, committees={})
    doc_master = AC.document_master(master, "1141", {"1141-A", "1141-B", "1142-D"})
    assert [e.案件鍵 for e in doc_master.exams] == ["1141-A", "1141-B"]

    out = AC.write_term_document(doc_master, "1141", template=template, output_dir=tmp_path / "out")
    assert out.name == "附件4_114學年度上學期_學位口試一覽表.docx"
    tables = Document(str(out)).tables
    assert len(tables) == 1
    text = "\n".join(cell.text for row in tables[0].rows for cell in row.cells)
    assert "教授甲" in text and "教授乙" in text and "學生A" in text and "學生B" in text
    assert "教授丙" not in text and "學生D" not in text
    assert AC.document_version(doc_master) != AC.document_version(
        AC.document_master(master, "1141", {"1141-A"}))


# ── 2026-10-12 寄送關卡：學生資料齊全即寄，不等專員放行 ──

def test_sendable_keys_sends_complete_cases_without_release(env, monkeypatch):
    monkeypatch.setattr(AC, "sendable_keys", _REAL_SENDABLE_KEYS)
    assert case_review.cleared_keys(env["master"], env["state"]) == set()   # 兩案都未放行
    result = _run(env)
    assert sorted(d["to"] for d in result.sent) == ["jia@example.org", "yi@example.org"]
    assert all("不需回覆" in m["body"] and "「確認無誤」" not in m["body"] for m in env["sent"])


def test_sendable_keys_holds_case_with_blocking_student_issue(env, monkeypatch):
    monkeypatch.setattr(AC, "sendable_keys", _REAL_SENDABLE_KEYS)
    env["master"].exams[1].指導教授 = ""                     # 學生還沒填指導教授 → 檢核「阻擋附件4」
    assert AC.sendable_keys(env["master"], env["state"]) == {"1141-A"}
    result = _run(env)
    assert [d["to"] for d in result.sent] == ["jia@example.org"]
