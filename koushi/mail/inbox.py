# -*- coding: utf-8 -*-
"""讀取寄件信箱的教授回信（IMAP，與寄信共用 .env 的帳號與應用程式密碼）。

引用清理（LLM前置研究-2026-09-23 第四節第 1 點）：
  寄出的信內文有「內容正確：回覆『確認無誤』即可」。教授回覆常引用原信，整封交判讀
  會誤判。因此先從 MIME／HTML 結構移除引用，再轉純文字；純文字層再處理 > 開頭行等。
  原始 body 永久保留（供稽核與版本核對），另產生 reply_text（只有它能送進模型）。

附件 metadata（第四節第 2 點）：
  InboundMail.attachments 記錄每個真正附件的檔名、MIME、大小、SHA-256。
  Content-Disposition: inline 且有 Content-ID 被 HTML 引用者視為內嵌圖，不算附件。
  附件內容不送模型。
"""

from __future__ import annotations

import datetime as _dt
import email
import hashlib
import imaplib
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from ..advisor_confirmation import AttachmentMeta, InboundMail
from .email_sender import _load_env

DEFAULT_HOST = "imap.gmail.com"


def _header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


# ── 引用清理 ──────────────────────────────────────────────────

_GMAIL_QUOTE_RE = re.compile(r'<div\b[^>]*class\s*=\s*["\'][^"\']*\bgmail_quote\b[^"\']*["\'][^>]*>', re.IGNORECASE)
_OUTLOOK_QUOTE_RE = re.compile(r'<div\b[^>]*id\s*=\s*["\']divRplyFwdMsg["\'][^>]*>', re.IGNORECASE)
_BLOCKQUOTE_RE = re.compile(r'<blockquote\b[^>]*>', re.IGNORECASE)


def _remove_balanced(html: str, opener: re.Pattern, tag: str) -> str:
    """移除 opener 開頭、含巢狀同名標籤的整段元素（依深度找對應的結束標籤）。

    找不到對應的結束標籤（HTML 不完整）時，從開頭標籤一路砍到結尾——寧可少留
    回覆，也不讓引用內容（例如「回覆『確認無誤』即可」）漏進 reply_text。
    引用之後的文字（下方回覆、穿插回覆）保留，避免「上面確認、下面要求修改」
    只剩確認的半封信。
    """
    tag_re = re.compile(rf'<(/?){tag}\b[^>]*>', re.IGNORECASE)
    while True:
        start = opener.search(html)
        if not start:
            return html
        depth, end = 0, None
        for m in tag_re.finditer(html, start.start()):
            depth += -1 if m.group(1) else 1
            if depth == 0:
                end = m.end()
                break
        if end is None:
            return html[:start.start()]
        html = html[:start.start()] + html[end:]


def _strip_html_quotes(html: str) -> str:
    """從 HTML 移除引用容器，回傳剩餘 HTML。

    涵蓋：<blockquote>（可巢狀，例如回覆第二輪重寄的信）、Gmail 的
    class="gmail_quote"、Outlook 的 id="divRplyFwdMsg"。

    - Gmail／通用 blockquote：依巢狀深度移除整個引用元素，保留引用前後的回覆。
    - Outlook divRplyFwdMsg：只是信頭區塊，原信接在它後面而非包在裡面，所以從它
      一路砍到結尾。
    - HTML 不完整找不到結束標籤時，從引用開頭砍到結尾。
    砍完若沒有任何回覆文字，reply_text 為空，該封信不送模型、直接交人工。

    已知限制（不處理）：教授把原信以純文字「貼上」、沒有任何引用標記時，無法分辨
    哪些是引用，原信文字會留在 reply_text。
    """
    html = _OUTLOOK_QUOTE_RE.split(html, maxsplit=1)[0]
    html = _remove_balanced(html, _GMAIL_QUOTE_RE, "div")
    html = _remove_balanced(html, _BLOCKQUOTE_RE, "blockquote")
    return html


def _strip_plain_quotes(text: str) -> str:
    """從純文字移除引用段落。

    處理方式：
      1. '> ' 開頭行（逐行）
      2. 'On ... wrote:' / '於 ... 寫道：' 行及其後全部
      3. '-----Original Message-----' 及其後全部
      4. Outlook 的「寄件者：／寄件日期：／收件者：／主旨：」區塊及其後全部
    """
    lines = text.split('\n')
    result = []
    for i, line in enumerate(lines):
        stripped = line.rstrip()

        # "On ... wrote:" / "於 ... 寫道：" → 切掉這行及以下全部
        if re.match(r'^On\s+.+wrote:\s*$', stripped, re.IGNORECASE):
            break
        if re.match(r'^於\s+.+寫道：\s*$', stripped):
            break

        # "-----Original Message-----"
        if stripped.startswith('-----') and 'original message' in stripped.lower():
            break

        # Outlook 格式：寄件者：/ 寄件日期：/ 收件者：/ 主旨： 連續出現
        if re.match(r'^寄件者[：:]', stripped):
            # 看接下來幾行是否構成 Outlook 引用頭
            remaining = lines[i:]
            header_count = sum(1 for ln in remaining[:5]
                               if re.match(r'^(寄件者|寄件日期|收件者|主旨)[：:]', ln.strip()))
            if header_count >= 2:
                break

        # '> ' 開頭的引用行跳過
        if stripped.startswith('>'):
            continue

        result.append(line)

    return '\n'.join(result)


def _clean_reply_text(raw_reply: str) -> str:
    """最終清理：去頭尾空白、壓縮連續空行。"""
    text = raw_reply.strip()
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text


# ── 附件 metadata ─────────────────────────────────────────────

def _collect_content_ids(msg: Message) -> set[str]:
    """收集 HTML 中引用的 Content-ID（cid:xxx），用來辨識內嵌簽名圖。"""
    cids: set[str] = set()
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() == 'text/html':
            payload = part.get_payload(decode=True) or b""
            html = payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
            # 找 src="cid:xxx" 或 src='cid:xxx'
            cids.update(re.findall(r'cid:([^"\'\s>]+)', html, re.IGNORECASE))
    return cids


def _extract_attachments(msg: Message) -> tuple[AttachmentMeta, ...]:
    """提取真正附件的 metadata；Content-Disposition: inline 且被 HTML 引用的視為內嵌圖，不算。"""
    referenced_cids = _collect_content_ids(msg)
    attachments = []

    for part in (msg.walk() if msg.is_multipart() else [msg]):
        content_type = part.get_content_type()
        disposition = (part.get_content_disposition() or '').lower()
        content_id = (part.get('Content-ID') or '').strip('<> ')

        # 跳過 text/plain 和 text/html 本體
        if content_type in ('text/plain', 'text/html') and disposition != 'attachment':
            continue
        # 跳過 multipart 容器
        if content_type.startswith('multipart/'):
            continue

        # 內嵌簽名圖判定：inline + 有 Content-ID + 被 HTML 引用
        if disposition == 'inline' and content_id and content_id in referenced_cids:
            continue

        payload = part.get_payload(decode=True)
        if payload is None:
            continue

        filename = part.get_filename() or ''
        if filename:
            try:
                filename = str(make_header(decode_header(filename)))
            except Exception:
                pass

        attachments.append(AttachmentMeta(
            filename=filename,
            content_type=content_type,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        ))

    return tuple(attachments)


# ── 本文提取 ──────────────────────────────────────────────────

def _text_body(msg: Message) -> tuple[str, str]:
    """取本文。回傳 (原始全文 body, 去引用 reply_text)。

    純文字優先；只有 HTML 時先去引用容器再轉純文字。
    附件一律不讀入本文。
    """
    parts = msg.walk() if msg.is_multipart() else [msg]
    html = ""
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/plain":
            reply = _clean_reply_text(_strip_plain_quotes(text))
            return text, reply
        html = html or text

    if html:
        # 原始 body：粗略去標籤
        body = re.sub(r"<[^>]+>", " ", html)
        # reply_text：先去引用容器，再去標籤，再去純文字引用
        cleaned_html = _strip_html_quotes(html)
        reply = re.sub(r"<[^>]+>", " ", cleaned_html)
        reply = _clean_reply_text(_strip_plain_quotes(reply))
        return body, reply

    return "", ""


# ── 解析 ──────────────────────────────────────────────────────

def parse_message(raw: bytes) -> InboundMail:
    msg = email.message_from_bytes(raw)
    received = ""
    if msg.get("Date"):
        try:
            parsed = parsedate_to_datetime(msg["Date"])
            received = parsed.astimezone(_dt.timezone.utc).isoformat() if parsed.tzinfo else ""
        except (TypeError, ValueError):
            received = msg["Date"]
    body, reply_text = _text_body(msg)
    attachments = _extract_attachments(msg)
    return InboundMail(
        message_id=(msg.get("Message-ID") or "").strip(),
        from_addr=_header(msg.get("From")),
        subject=_header(msg.get("Subject")),
        received_at=received,
        body=body,
        reply_text=reply_text,
        attachments=attachments,
        in_reply_to=(msg.get("In-Reply-To") or "").strip(),
        references=tuple(re.findall(r"<[^<>]+>", msg.get("References") or "")),
    )


def fetch_replies(*, days: int = 30, host: str | None = None) -> list[InboundMail]:
    """讀收件匣最近 `days` 天的信。連線或登入失敗直接拋錯，不吞掉。"""
    env = _load_env()
    since = (_dt.date.today() - _dt.timedelta(days=days)).strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(host or env.get("MAIL_IMAP_HOST") or DEFAULT_HOST, timeout=30) as client:
        client.login(env["MAIL_SENDER"], env["MAIL_PASSWORD"])
        client.select("INBOX", readonly=True)
        status, data = client.search(None, "SINCE", since)
        if status != "OK":
            raise RuntimeError(f"讀取收件匣失敗：{status}")
        mails = []
        for num in (data[0] or b"").split():
            status, parts = client.fetch(num, "(INTERNALDATE BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            from dataclasses import replace
            parsed = parse_message(parts[0][1])
            internal = re.search(rb'INTERNALDATE "([^"]+)"', parts[0][0])
            if internal:
                trusted = _dt.datetime.strptime(internal.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
                parsed = replace(parsed, received_at=trusted.astimezone(_dt.timezone.utc).isoformat(), received_at_trusted=True)
            mails.append(parsed)
        return mails
