# -*- coding: utf-8 -*-
"""HTML 回信引用清理的回歸測試（巢狀 blockquote、下方回覆、純文字貼上）。"""
import datetime as dt

from koushi import application
from koushi.engines.confirmation import CONFIRMED
from koushi.mail.inbox import _strip_html_quotes, parse_message

from tests.test_advisor_confirmation import _request, _sent_and_reply, env  # noqa: F401

INSTRUCTION = "內容正確：回覆「確認無誤」即可。"


def _html_mail(html: str, mid="<n1@example.org>") -> bytes:
    return (
        "From: jia@example.org\r\n"
        "Subject: Re: [口試附件4確認 A4-00000000]\r\n"
        f"Message-ID: {mid}\r\n"
        "Date: Fri, 02 Oct 2026 10:00:00 +0800\r\n"
        "Content-Type: text/html; charset=utf-8\r\n\r\n" + html
    ).encode("utf-8")


def test_nested_blockquote_from_resent_round_is_removed_entirely():
    """第二輪重寄的信被回覆時，外層引用裡還包著第一輪的引用；整段都要移除。"""
    html = (
        "<div>日期要改成 11/5，謝謝。</div>"
        "<blockquote><div>第二輪附件4</div>"
        f"<blockquote><div>第一輪附件4</div><div>{INSTRUCTION}</div></blockquote>"
        f"<div>{INSTRUCTION}</div><div>附件4 版本：abcdef0123</div>"
        "</blockquote>"
    )
    mail = parse_message(_html_mail(html))
    assert "日期要改成 11/5" in mail.reply_text
    assert "確認無誤" not in mail.reply_text
    assert "附件4 版本" not in mail.reply_text
    assert INSTRUCTION in mail.body  # 原始 body 保持不動，供稽核與版本核對


def test_nested_gmail_quote_divs_are_removed():
    html = (
        "<div>沒問題但時間要改下午三點</div>"
        '<div class="gmail_quote gmail_quote_container"><div><div>'
        f"{INSTRUCTION}</div></div><blockquote>舊信</blockquote></div>"
    )
    reply = parse_message(_html_mail(html)).reply_text
    assert "下午三點" in reply and "確認無誤" not in reply


def test_reply_below_quote_is_kept_and_quote_dropped():
    """穿插／下方回覆：引用後面的文字要保留，不能只剩上面一半。"""
    html = (
        "<div>確認</div>"
        f"<blockquote><blockquote>{INSTRUCTION}</blockquote>{INSTRUCTION}</blockquote>"
        "<div>另外陳教授改線上</div>"
    )
    reply = parse_message(_html_mail(html)).reply_text
    assert "確認" in reply and "陳教授改線上" in reply
    assert "確認無誤" not in reply


def test_unclosed_quote_is_cut_to_end():
    html = f"<div>好</div><blockquote><blockquote>{INSTRUCTION}</blockquote>"
    reply = _strip_html_quotes(html)
    assert "確認無誤" not in reply and "好" in reply


def test_outlook_header_block_cut_to_end():
    html = f'<div>OK</div><div id="divRplyFwdMsg">寄件者：系辦</div><div>{INSTRUCTION}</div>'
    assert "確認無誤" not in _strip_html_quotes(html)


def test_quote_only_reply_goes_to_human(env):  # noqa: F811
    """清理後沒有任何回覆文字：reply_text 為空、不送模型、不自動確認。"""
    html = f"<blockquote><blockquote>{INSTRUCTION}</blockquote>{INSTRUCTION}</blockquote>"
    assert parse_message(_html_mail(html)).reply_text == ""
    calls = []

    def classifier(mail, request):
        calls.append(1)
        raise AssertionError("不應呼叫模型")

    mail = _sent_and_reply(env, body=html, reply_text=parse_message(_html_mail(html)).reply_text)
    added = application.ingest_advisor_replies(master=env["master"], state_dir=env["state"],
                                               mails=[mail], classifier=classifier, date=dt.date(2026, 10, 2))
    assert calls == [] and added[0].intent is None
    assert _request(env, "教授甲").response != CONFIRMED


def test_plain_text_paste_without_markers_is_a_known_limitation():
    """已知限制：教授把原信直接貼成純文字、沒有任何引用標記時無法分辨，原文會留下。

    這類信仍受其他防線保護：判讀依據須逐字引用、附件4 版本與寄件者核對、白名單與開關。
    """
    raw = (
        "From: jia@example.org\r\nSubject: Re: test\r\nMessage-ID: <p1@example.org>\r\n"
        "Date: Fri, 02 Oct 2026 10:00:00 +0800\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        f"請看一下\n{INSTRUCTION}\n"
    ).encode("utf-8")
    assert INSTRUCTION in parse_message(raw).reply_text
