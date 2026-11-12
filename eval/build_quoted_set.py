# -*- coding: utf-8 -*-
"""產生 eval/quoted_set.json（合成資料；留存作為資料來源紀錄，重跑會覆寫）。"""
import json
from pathlib import Path

ORIG_LINES = [
    "示範教授 老師您好，",
    "",
    "附上 115學年度第1學期 您指導學生的學位口試一覽表（附件4），內容如下，敬請核對：",
    "  • 115學年度第1學期　2026-12-20　碩士班：示範學生甲、示範學生乙　實體",
    "",
    "請於 2026-10-20 前直接回覆本信（請保留主旨中的代碼 A4-0a1b2c3d）：",
    "  - 內容正確：回覆「確認無誤」即可。",
    "  - 需要修改：請寫明要修改的地方。",
    "",
    "附件4 版本：0123456789",
    "— 示範學系系辦",
]
ORIG = "\n".join(ORIG_LINES)
ORIG_HTML = "".join(f"<div>{ln or '<br>'}</div>" for ln in ORIG_LINES)
GT = "\n".join("> " + ln if ln else ">" for ln in ORIG_LINES)


def plain(style, reply):
    if style == "gmail_en":
        return f"{reply}\n\nOn Thu, Oct 8, 2026 at 10:02 AM 示範學系系辦 <office@example.edu> wrote:\n\n{GT}\n"
    if style == "gmail_zh":
        return f"{reply}\n\n於 2026年10月8日 週四 上午10:02寫道：\n\n{GT}\n"
    if style == "outlook_original":
        return f"{reply}\n\n-----Original Message-----\nFrom: office@example.edu\nSent: Thursday, October 8, 2026 10:02 AM\nSubject: [口試附件4確認 A4-0a1b2c3d]\n\n{ORIG}\n"
    if style == "outlook_zh_header":
        return f"{reply}\n\n寄件者： 示範學系系辦 <office@example.edu>\n寄件日期： 2026年10月8日 上午 10:02\n收件者： 示範教授\n主旨： [口試附件4確認 A4-0a1b2c3d]\n\n{ORIG}\n"
    if style == "gt_only":
        return f"{reply}\n\n{GT}\n"
    if style == "bottom_post":
        return f"On Thu, Oct 8, 2026 at 10:02 AM 示範學系系辦 <office@example.edu> wrote:\n\n{GT}\n\n{reply}\n"
    if style == "inline_interleaved":
        # 教授在引用行之間插話
        return ("> 請於 2026-10-20 前直接回覆本信（請保留主旨中的代碼 A4-0a1b2c3d）：\n"
                "> - 內容正確：回覆「確認無誤」即可。\n"
                f"\n{reply}\n\n"
                "> 附件4 版本：0123456789\n")
    if style == "no_marker_paste":
        # 沒有任何引用標記，直接把原信貼在下面（清理層抓不到）
        return f"{reply}\n\n{ORIG}\n"
    raise ValueError(style)


def html(style, reply):
    r = "".join(f"<div>{ln}</div>" for ln in reply.split("\n"))
    if style == "html_gmail_quote":
        return (f"<div dir=\"ltr\">{r}</div><br><div class=\"gmail_quote\"><div dir=\"ltr\" class=\"gmail_attr\">"
                f"於 2026年10月8日 週四 上午10:02寫道：<br></div><blockquote class=\"gmail_quote\">{ORIG_HTML}</blockquote></div>")
    if style == "html_blockquote":
        return f"<div>{r}</div><blockquote type=\"cite\">{ORIG_HTML}</blockquote>"
    if style == "html_outlook":
        return (f"<div>{r}</div><hr><div id=\"divRplyFwdMsg\"><b>寄件者：</b> 示範學系系辦<br><b>寄件日期：</b> 2026年10月8日 上午 10:02<br>"
                f"<b>主旨：</b> [口試附件4確認 A4-0a1b2c3d]</div><div>{ORIG_HTML}</div>")
    if style == "html_nested_blockquote":
        # 第二輪重寄：系辦的信本身引用了上一輪，教授回覆時形成巢狀 blockquote
        return (f"<div>{r}</div><blockquote type=\"cite\"><div>（第二輪）附件4 已更新，請再次核對。</div>"
                f"<blockquote type=\"cite\"><div>（第一輪原信）</div></blockquote>{ORIG_HTML}</blockquote>")
    raise ValueError(style)


M = lambda row, field, value: {"row": row, "field": field, "new_value": value}
CASES = [
    # id, style, reply, intent, reservation, mods, category
    ("Q01", "gmail_en", "日期要改成 12/27，其他沒問題", "modify", False, [M("", "日期", "12/27")], "引用+修改"),
    ("Q02", "gmail_zh", "請問口試可以改成線上嗎？", "question", False, [], "引用+疑問"),
    ("Q03", "outlook_original", "本人 10/25 前出國開會，屆時再回覆。（自動回覆）", "auto_reply", False, [], "引用+自動回覆"),
    ("Q04", "outlook_zh_header", "地點改為工程館 A521", "modify", False, [M("", "地點", "工程館 A521")], "引用+修改"),
    ("Q05", "html_gmail_quote", "示範學生乙那組改成線上口試", "modify", False, [M("示範學生乙", "方式", "線上")], "HTML引用+修改"),
    ("Q06", "html_blockquote", "這個時間我有課，能不能改到下午？", "question", False, [], "HTML引用+疑問"),
    ("Q07", "html_outlook", "確認無誤，謝謝", "confirm", False, [], "HTML引用+確認"),
    ("Q08", "gmail_en", "好的，沒問題", "confirm", False, [], "引用+確認"),
    ("Q09", "gt_only", "委員名單少了一位，請補上示範委員丙", "modify", False, [M("", "委員", "示範委員丙")], "引用+修改"),
    ("Q10", "gmail_zh", "沒問題，但日期好像不對？我記得是 12/25 不是 12/20", "confirm", True, [], "引用+有保留的確認"),
    ("Q11", "html_gmail_quote", "老師目前出國開會，我是老師的助理，收到後會轉達老師", "auto_reply", False, [], "HTML引用+助理代回"),
    ("Q12", "outlook_original", "請問下週系務會議改到幾點？", "unrelated", False, [], "引用+無關"),
    ("Q13", "inline_interleaved", "這邊日期不對，應該是 12/25", "modify", False, [M("", "日期", "12/25")], "行間插話"),
    ("Q14", "bottom_post", "日期要改成 1/5", "modify", False, [M("", "日期", "1/5")], "底部回覆（清理層已知限制）"),
    ("Q16", "html_nested_blockquote", "請問可以改成線上口試嗎？", "question", False, [], "巢狀引用（第二輪重寄）"),
    ("Q15", "no_marker_paste", "時間改下午三點", "modify", False, [M("", "時間", "下午三點")], "無標記貼上原信（清理層已知限制）"),
]

out = {
    "_meta": {
        "synthetic": True,
        "description": "合成資料：教授回覆位於引用原信之上（或之中），原信含「確認無誤」提示語。"
                       "所有姓名、信箱、代碼、版本皆為虛構。用於比較「原始 body」與「inbox.py 去引用後 reply_text」兩種送模型方式。",
        "built": "2026-10-08，依 advisor_confirmation.draft() 的實際寄件內文格式手工撰寫回覆、以程式套上 12 種引用格式（含 3 種清理層已知抓不到的格式）",
        "quoted_trigger": "確認無誤",
    },
    "cases": [],
}
for cid, style, reply, intent, res, mods, cat in sorted(CASES):
    is_html = style.startswith("html_")
    out["cases"].append({
        "id": cid,
        "category": cat,
        "quote_style": style,
        "content_type": "text/html" if is_html else "text/plain",
        "professor_reply": reply,
        "raw_body": html(style, reply) if is_html else plain(style, reply),
        "expected_intent": intent,
        "expected_has_reservation": res,
        "expected_modifications": mods,
        "source": "synthetic",
    })
(Path(__file__).resolve().parent / "quoted_set.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(len(out["cases"]))
