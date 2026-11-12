# -*- coding: utf-8 -*-
"""畫面用語整理：中文標籤、評審模式的錯誤頁與停用按鈕、模擬寄件說法、委員表單保留輸入。"""
import re

from koushi import committee_requests, demo, judge
from koushi import mailbox_agent as MA
from koushi.web import labels as L

from tests.test_judge_mode import KEY, _login_judge, jenv  # noqa: F401 — 共用評審模式 fixture


# ── 標籤 ──

def test_term_and_case_labels():
    assert L.term_label("1151") == "115學年度第1學期"
    assert L.term_label("1142") == "114學年度第2學期"
    assert L.term_label("abc") == "abc"
    assert L.case_label("1151-999900001") == "115學年度第1學期・學號 999900001"


def test_issue_owner_and_step_labels():
    assert L.issue_label("COMMITTEE_MATCHED_BY_NAME").startswith("委員以姓名")
    assert L.issue_label("UNKNOWN_CODE") == "UNKNOWN_CODE"
    assert "不找學生" in L.owner_label("post_exam")
    # 模型判讀每次嘗試標第幾次，排在「判讀結果彙整」之前才看得懂
    assert L.step_label("validate", "第 2 次") == "模型判讀（第 2 次）"
    assert L.step_label("analyze") == "判讀結果彙整"
    assert L.decision_label("pending_human") == "待專員決定"
    assert L.decision_label("draft") == "草稿待核准"


def test_readable_output_has_no_codes_or_escapes():
    text = L.readable({"role": "committee", "verified": True, "case_keys": ["1151-1"],
                       "summary": "（離線備援）來信提出疑問。", "category": "question"})
    assert "口試委員" in text and "身分已驗證：是" in text and "提問" in text and "離線備援" in text
    assert "committee" not in text and "\\u" not in text
    assert L.readable({"ok": True}) == "通過檢核"
    assert L.readable([{"id": "a1", "type": "reply_draft", "source": "rule"}]).count("回覆來信") == 1
    assert L.fmt_time("2026-10-10T10:00:00+08:00") == "2026-10-10 10:00"
    assert L.friendly_value("false", "視訊出席") == "否"
    shown = L.friendly_value('[{"association_id": "P1", "name": "甲", "unit": "某校", "title": "教授", '
                             '"email": "a@x.org", "remote": false}]', "committee_list")
    assert shown == "1 位：甲（某校，教授）" and "association_id" not in shown


# ── 評審模式頁面 ──

def _run_until_released(client):
    client.post("/office/demo", data={"action": "advance", "days": "1", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    client.post(f"/office/case/{KEY}/clear", data={"note": "評審放行", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})


def test_demo_page_has_no_real_names_or_env_vars_in_judge_mode(jenv):  # noqa: F811
    client = jenv["client"]
    _login_judge(client)
    page = client.get("/office/demo").get_data(as_text=True)
    for word in ("Sunny", "Eric", "MAIL_SENDER", "OPENAI_API_KEY", "即時服務準備", "不能宣稱"):
        assert word not in page
    assert "模擬寄件匣" in page and "999900001" in page


def test_forbidden_actions_show_chinese_page_and_disabled_controls(jenv):  # noqa: F811
    client = jenv["client"]
    _login_judge(client)
    denied = client.get("/office/overview/export")
    text = denied.get_data(as_text=True)
    assert denied.status_code == 403 and "沒有這項操作的權限" in text and "Forbidden" not in text
    assert "工作總覽" in text  # 有系辦導覽列，可以回到其他頁
    missing = client.get("/office/agent-trace/0123456789abcdef")
    assert missing.status_code == 404 and "找不到頁面" in missing.get_data(as_text=True)
    assert "評審模式不可用" in client.get("/office/overview").get_data(as_text=True)
    assert "評審模式不可用" in client.get("/office/import").get_data(as_text=True)
    assert "評審模式不可用" in client.get("/office/settings").get_data(as_text=True)


def test_case_page_and_overview_show_chinese_labels(jenv):  # noqa: F811
    client = jenv["client"]
    _login_judge(client)
    detail = client.get(f"/office/case/{KEY}").get_data(as_text=True)
    for raw in ("COMMITTEE_MATCHED_BY_NAME</b>", "責任：post_exam", "koushi status", "association_id",
                "委員身分為暫定（P...）"):
        assert raw not in detail
    assert "115學年度第1學期" in detail
    overview = client.get("/office/overview").get_data(as_text=True)
    assert "115學年度第1學期" in overview and ">1151<" not in overview


def test_judge_mode_says_simulated_outbox_not_sent(jenv):  # noqa: F811
    client, cfg = jenv["client"], jenv["cfg"]
    _login_judge(client)
    _run_until_released(client)
    comms = client.get("/office/communications").get_data(as_text=True)
    assert "已放入模擬寄件匣" in comms and "已經成功送到" not in comms
    request = committee_requests.plan(__import__("koushi.application", fromlist=["x"]).load_current_master(
        cfg.master_path), cfg.state_dir)[0]
    token = committee_requests.link_token(cfg.state_dir, request.email, request=request)
    page = client.post(f"/committee/reply/{token}", data={"action": "verify-code", "code": "000000",
                                                         "csrf_token": "t"}).get_data(as_text=True)
    assert "尚未寄送驗證碼" in page and "驗證碼已失效" not in page
    sent = client.post(f"/committee/reply/{token}", data={"action": "send-code", "csrf_token": "t"},
                       follow_redirects=True).get_data(as_text=True)
    assert "模擬寄件匣" in sent and "驗證碼已寄至登記信箱" not in sent


def test_committee_form_keeps_values_on_mismatch_and_lists_received(jenv):  # noqa: F811
    client, cfg = jenv["client"], jenv["cfg"]
    _login_judge(client)
    _run_until_released(client)
    from koushi.application import load_current_master
    request = committee_requests.plan(load_current_master(cfg.master_path), cfg.state_dir)[0]
    token = committee_requests.link_token(cfg.state_dir, request.email, request=request)
    client.post(f"/committee/reply/{token}", data={"action": "send-code", "csrf_token": "t"})
    code = re.search(r"驗證碼是：(\d{6})", judge.outbox(cfg.state_dir)[-1]["body"]).group(1)
    client.post(f"/committee/reply/{token}", data={"action": "verify-code", "code": code, "csrf_token": "t"})
    form = {"身分證字號": "A123456789", "身分證字號_確認": "A123456780", "金融機構": "範例銀行 總行",
            "帳號": "00012345678", "帳號_確認": "00012345678", "戶籍地址": "範例市範例路 1 號", "csrf_token": "t"}
    page = client.post(f"/committee/reply/{token}", data=form).get_data(as_text=True)
    assert 'value="範例銀行 總行"' in page and 'value="範例市範例路 1 號"' in page
    form["身分證字號_確認"] = "A123456789"
    client.post(f"/committee/reply/{token}", data=form)
    committees = client.get("/office/committees").get_data(as_text=True)
    assert "已回填的委員" in committees and request.name in committees.partition("已回填的委員")[2]


def test_mailbox_trace_is_chinese_and_collapsed(jenv):  # noqa: F811
    client, cfg = jenv["client"], jenv["cfg"]
    _login_judge(client)
    _run_until_released(client)
    advisor_mail = next(m for m in judge.outbox(cfg.state_dir) if m["to"] == demo.MAILBOXES["advisor"])
    client.post("/office/judge/outbox/reply", data={"outbox_id": advisor_mail["id"],
                                                    "text": "請問口試當天有投影機嗎？", "csrf_token": "t"})
    client.post("/office/demo", data={"action": "send", "csrf_token": "t"})
    assert MA.load_mails(cfg.state_dir)
    page = client.get("/office/mailbox").get_data(as_text=True)
    run_url = re.search(r'href="(/office/agent-trace/[^"]+)">AI 處理紀錄<', page).group(1)
    run = client.get(run_url.replace("&amp;", "&")).get_data(as_text=True)
    assert "解析來信" in run and "辨識寄件者" in run
    for raw in ("identify_sender", "pii_guard", "plan_actions", "\\u", "T10:00:00+08:00"):
        assert raw not in page
    assert "<details open" not in page
    trace = client.get("/office/agent-trace").get_data(as_text=True)
    assert "來信：" in trace


def test_flow_page_shows_steps_exceptions_and_links(jenv):  # noqa: F811
    client = jenv["client"]
    _login_judge(client)
    page = client.get("/office/flow").get_data(as_text=True)
    assert "流程圖" in page and page.count('class="flow-row"') == 9 and page.count('class="ex"') >= 20
    for href in ("/office/mailbox", "/office/committees", "/office/documents", "/office/import"):
        assert f'href="{href}"' in page
    assert 'href="/office/flow"' in client.get("/office/overview").get_data(as_text=True)  # 側欄有入口
