from tests.web_fixture import post_submission, post_tracking
# -*- coding: utf-8 -*-
"""全流程演練（LLM 除外）：系辦設定 → 學生填寫／暫存 → 系辦上傳 → 補件信 → 補件 →
專員放行 → 附件4 寄教授、向委員索取資料 → 委員線上回填 → 教授回信確認 →
口試後附件6 → 登錄回收。

依 docs/已確認流程決策.md 的端到端流程，一個案例串完所有關卡；任何一關斷掉這裡會失敗。
全程假資料、暫存資料夾、假寄送器。附件4／6 實際寫檔需要 LibreOffice 與正式範本，
這裡只替換「寫出檔案」那一段，納入／排除案件的關卡邏輯照實際程式跑。
"""
import contextlib
import datetime as dt
import io
import re
import tempfile
from pathlib import Path

import openpyxl
import pytest

from koushi import advisor_confirmation as AC
from koushi import agent_trace, application, committee_requests, ingest, send_log
from koushi import settings as office_settings
from koushi import workflow
from koushi.web import app as webapp
from koushi.workflow import RegistryPaths, TemplatePaths

from tests.att2_fixture import case as att2_case, write_att2

TODAY = dt.date.today()
TERM = "1141"
KEY1, KEY2 = f"{TERM}-111900001", f"{TERM}-111900002"
EXAM_DATE = (TODAY + dt.timedelta(days=40)).isoformat()
# 陳大文：本系、舊口委資料庫有身分證 → 不必索取
# 林小芳：外校 → 索取完整撥帳資料
# 張志明：本系、資料庫查無此人 → 只索取身分證字號（否則暫定身分會擋附件6）
COMMITTEES = [
    ("陳大文", "國立範例大學資訊工程學系", "教授", "dawen@example.org"),
    ("林小芳", "國立清華大學資訊工程學系", "副教授", "fang@example.org"),
    ("張志明", "國立範例大學資訊工程學系", "助理教授", "zhiming@example.org"),
]
ADVISORS = {"111900001": ("林指導", "lin@example.org"), "111900002": ("黃指導", "huang@example.org")}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = webapp.WebConfig(
        submissions_dir=tmp_path / "submissions", master_path=tmp_path / "current" / "master.xlsx",
        state_dir=tmp_path / "state",
        registries=RegistryPaths(attachment2=tmp_path / "office" / "att2.xlsx",
                                 members=tmp_path / "office" / "members.xlsx",
                                 distances=tmp_path / "office" / "distances.csv"),
        doc_output_root=tmp_path / "docs", office_password="pw",
    )
    monkeypatch.setitem(webapp.app.config, "_web", cfg)
    monkeypatch.setitem(webapp.app.config, "TESTING", True)
    monkeypatch.setenv("KOUSHI_PUBLIC_URL", "http://localhost:5000")

    @contextlib.contextmanager
    def fake_template(path, **kwargs):
        yield path

    def fake_write(master, term, *, template, output_dir):
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / f"附件4_{term}.docx"
        out.write_text("附件4替身", encoding="utf-8")
        return out

    monkeypatch.setattr("koushi.document_conversion.docx_template", fake_template)
    monkeypatch.setattr(AC, "write_term_document", fake_write)
    sent = []
    with webapp.app.test_client() as client:
        yield {"cfg": cfg, "client": client, "sent": sent, "tmp": tmp_path, "monkeypatch": monkeypatch}


# ── 小工具 ─────────────────────────────────────────────────

def _csrf(client):
    with client.session_transaction() as sess:
        if not sess.get("_csrf_token"):
            import secrets
            sess["_csrf_token"] = secrets.token_urlsafe(32)
        return sess["_csrf_token"]


def _login(client, *, office=False, student_id=None):
    client.get("/logout")
    if office:
        client.post("/login/office", data={"password": "pw", "csrf_token": _csrf(client)})
    else:
        client.post("/login/student", data={"student_id": student_id, "csrf_token": _csrf(client)})


def _student_form(client, sid, name, email, action="submit"):
    advisor, advisor_email = ADVISORS[sid]
    data = {
        "term": TERM, "student_name": name, "student_email": email, "exam_date": EXAM_DATE,
        "exam_start_time": "10:00", "exam_end_time": "12:00", "exam_location": "工程館 A204",
        "exam_mode": "實體", "advisor_0": advisor, "advisor_0_email": advisor_email,
        "thesis_title": "測試論文", "action": action, "csrf_token": _csrf(client),
    }
    for i, (n, u, t, e) in enumerate(COMMITTEES):
        data.update({f"committee_{i}_name": n, f"committee_{i}_unit": u,
                     f"committee_{i}_title": t, f"committee_{i}_email": e})
    return data


def _office_files():
    committees = [{"name": n, "unit": u, "title": t} for n, u, t, _ in COMMITTEES]
    with tempfile.TemporaryDirectory() as d:
        att2 = io.BytesIO(write_att2(Path(d) / "a.xlsx", [
            att2_case(term=TERM, student_id="111900001", name="王小明", committees=committees),
            att2_case(term=TERM, student_id="111900002", name="李小華", committees=committees),
        ]).read_bytes())
    distances = io.BytesIO("服務單位,單程公里\n國立清華大學資訊工程學系,30\n".encode("utf-8-sig"))
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["委員編號", "委員姓名", "英文姓名", "服務單位", "最高學歷學校", "最高學位",
               "身分證字號", "金融機構", "帳號", "戶籍地址", "Email", "最後更新", "來源"])
    ws.append(["C0001", "陳大文", "", "國立範例大學資訊工程學系", "", "", "A123456789",
               "", "", "", "", "2026-09-17", "系辦上傳"])
    members = io.BytesIO()
    wb.save(members)
    members.seek(0)
    return att2, distances, members


def _save_settings(cfg, **changes):
    """改設定檔模擬「時間經過」；正常操作走期限設定頁（見演練第 2 步）。"""
    current = office_settings.load(cfg.state_dir)
    office_settings.save(office_settings.OfficeSettings(**{**current.__dict__, **changes}), cfg.state_dir)


def _automate(env, date=TODAY, fetch_replies=None, llm=None):
    """等同 `python -m koushi automate --send`，路徑指向暫存資料夾、寄送用假寄送器。"""
    cfg = env["cfg"]
    return application.run_daily_automation(
        submission_paths=ingest.student_submission_paths(cfg.submissions_dir),
        master_path=cfg.master_path, state_dir=cfg.state_dir, registries=cfg.build_registries,
        submissions_dir=cfg.submissions_dir, send=True,
        transport=lambda **kw: env["sent"].append(kw) or True,
        date=date, now=dt.datetime.combine(date, dt.time(10, 0)), fetch_replies=fetch_replies, llm=llm,
    )


def _review_rows(cfg):
    master = application.load_current_master(cfg.master_path)
    return {r.case_key: r for r in application.review_queue(master, cfg.state_dir)}


def _tw_id(seed):
    letters = dict(zip("ABCDEFGHJKLMNPQRSTUVXYWZIO", range(10, 36)))
    head = letters["B"]
    body = f"1{seed}234567"
    digits = [head // 10, head % 10, *map(int, body)]
    check = (10 - sum(d * w for d, w in zip(digits, (1, 9, 8, 7, 6, 5, 4, 3, 2, 1))) % 10) % 10
    return f"B{body}{check}"


# ── 全流程 ─────────────────────────────────────────────────

def test_full_workflow_walkthrough(env):
    cfg, client, sent = env["cfg"], env["client"], env["sent"]

    # 1. 系辦還沒設定學期前，學生端不開放
    _login(client, student_id="111900001")
    assert "尚未開放" in client.get("/dashboard").get_data(as_text=True)
    post_submission(client, data=_student_form(client, "111900001", "王小明", "s1@example.org"))
    assert not list(cfg.submissions_dir.glob("*.json"))

    # 2. 系辦在期限設定頁設定學期、填寫截止日、學生信箱後備格式
    _login(client, office=True)
    client.post("/office/settings", data={
        "current_term": TERM, "fill_deadline": (TODAY + dt.timedelta(days=14)).isoformat(),
        "deficiency_days": "7", "deficiency_reminder_lead_days": "2", "send_hour": "0",
        "business_days": "0", "confirmation_days": "5", "confirmation_reminder_interval_days": "2",
        "committee_exception_lead_days": "3", "student_email_template": "{學號}@cc.example.edu",
        "holidays": "", "csrf_token": _csrf(client)})
    assert office_settings.load(cfg.state_dir).phase_on(TODAY) == office_settings.PHASE_FILLING

    # 3. 王小明一次填齊送出；李小華只暫存
    _login(client, student_id="111900001")
    post_submission(client, data=_student_form(client, "111900001", "王小明", "s1@example.org"))
    assert (cfg.submissions_dir / f"{KEY1}.json").is_file()
    _login(client, student_id="111900002")
    post_submission(client, data=_student_form(client, "111900002", "李小華", "", action="draft"))
    assert not (cfg.submissions_dir / f"{KEY2}.json").is_file()
    assert application.load_draft(cfg.submissions_dir, KEY2) is not None

    # 4. 系辦提早上傳三份檔案 → 自動建主檔、記錄附件2 首次上傳日
    _login(client, office=True)
    att2, distances, members = _office_files()
    client.post("/office/upload-registries", data={
        "attachment2": (att2, "att2.xlsx"), "distances": (distances, "d.csv"),
        "members": (members, "m.xlsx"), "csrf_token": _csrf(client)}, content_type="multipart/form-data")
    assert application.load_auto_build_status(webapp._status_path())["status"] == "built"
    assert office_settings.load(cfg.state_dir).att2_first_uploaded == TODAY
    assert "未填寫" in client.get("/office/overview").get_data(as_text=True)

    # 5. 填寫截止前的每日自動化：只建預覽，不寄信
    _automate(env)
    assert sent == []

    # 6. 時間快轉到補件期間：沒收到補件通知的王小明被鎖
    two_days_ago = TODAY - dt.timedelta(days=2)
    _save_settings(cfg, fill_deadline=two_days_ago, att2_first_uploaded=two_days_ago)
    assert office_settings.load(cfg.state_dir).phase_on(TODAY) == office_settings.PHASE_DEFICIENCY
    assert application.submission_block_reason(KEY1, cfg.state_dir)

    # 2026-10-12 起附件4 不等放行：資料齊全的案件，附件4 隨每日流程就寄給指導教授參考
    def att4_to():
        return sorted(m["to"] for m in sent if "口試附件4" in m["subject"])

    def others():
        return [m for m in sent if "口試附件4" not in m["subject"]]

    # 7. 補件信只寄李小華（後備信箱），信中提醒有暫存；8. 同日重跑不重寄
    _automate(env)
    assert [m["to"] for m in others()] == ["111900002@cc.example.edu"]
    assert "暫存" in others()[0]["body"]
    sent_att4 = att4_to()
    _automate(env)
    assert len(others()) == 1 and att4_to() == sent_att4

    # 9. 李小華補件 → 隔天自動化：學生責任缺件已解決，不再寄信給她
    _login(client, student_id="111900002")
    assert not application.submission_block_reason(KEY2, cfg.state_dir)
    post_submission(client, data=_student_form(client, "111900002", "李小華", "s2@example.org"))
    result = _automate(env, date=TODAY + dt.timedelta(days=1))
    assert not [i for i in result.tracking.deficiency.by_case.get(KEY2, []) if i.owner == "student"]
    assert len(others()) == 1

    # 10. 放行前不索取委員資料；兩案都在人工關卡（附件4 資料齊全就已寄出，不等放行）
    assert all(r.review is None for r in _review_rows(cfg).values())

    # 11. 專員放行：沒填處理人被擋，填了才放行
    _login(client, office=True)
    client.post(f"/office/case/{KEY1}/clear", data={"handler": "", "csrf_token": _csrf(client)})
    assert _review_rows(cfg)[KEY1].review is None
    for key in (KEY1, KEY2):
        client.post(f"/office/case/{key}/clear", data={"handler": "專員甲", "note": "資格已確認",
                                                        "csrf_token": _csrf(client)})
    assert all(r.review is not None for r in _review_rows(cfg).values())

    # 12. 放行後自動化：外校委員要撥帳資料、本系新委員只要身分證；兩位指導教授都已收到附件4
    before = len(sent)
    _automate(env, date=TODAY + dt.timedelta(days=1))
    new = {m["to"]: m for m in sent[before:] if "口試附件4" not in m["subject"]}
    assert sorted(new) == ["fang@example.org", "zhiming@example.org"]
    assert att4_to() == ["huang@example.org", "lin@example.org"]
    assert "撥帳" in new["fang@example.org"]["subject"]
    assert "身分證字號" in new["zhiming@example.org"]["subject"]

    # 13. 委員從專屬連結回填；兩次輸入不一致會被擋；送出後不回顯個資
    client.get("/logout")
    for i, to in enumerate(("fang@example.org", "zhiming@example.org")):
        token = re.search(r"/committee/reply/([\w-]+)", new[to]["body"]).group(1)
        from koushi import committee_verification as CV
        codes = []
        CV.send_code(cfg.state_dir, token=token, email=to, secret=webapp.app.secret_key,
                     transport=lambda **kw: codes.append(kw) or True)
        code = re.search(r"驗證碼是：(\d{6})", codes[-1]["body"]).group(1)
        client.post(f"/committee/reply/{token}", data={"action":"verify-code", "code":code,
                                                       "csrf_token":_csrf(client)})
        id_no = _tw_id(i)
        form = {"身分證字號": id_no, "身分證字號_確認": id_no, "csrf_token": _csrf(client)}
        if to == "fang@example.org":
            form.update({"金融機構": "測試銀行", "帳號": "1234567890", "帳號_確認": "1234567890",
                         "戶籍地址": "範例市示範區測試路1號"})
            client.post(f"/committee/reply/{token}", data={**form, "身分證字號_確認": "A000000000"})
            assert to not in committee_requests.load_replies(cfg.state_dir)
        page = client.post(f"/committee/reply/{token}", data=form).get_data(as_text=True)
        assert id_no not in page
    master = application.load_current_master(cfg.master_path)
    assert not application._committees_missing_banking(master, [KEY1, KEY2])
    assert not any(m.committee_id.startswith("P") for e in master.exams for m in e.委員)

    # 14. 教授回信「確認無誤」→ 進待判讀（核對通過）→ 系辦判讀確認（教授不必回信；回了照常處理）
    att4_mail = {m["to"]: m for m in sent if "口試附件4" in m["subject"]}
    replies = [AC.InboundMail(message_id=f"<r{i}@example.org>", from_addr=to, subject="Re: " + att4_mail[to]["subject"],
                              received_at=f"{TODAY + dt.timedelta(days=2)}T11:00:00+08:00", body="確認無誤，謝謝。",
                              in_reply_to=AC.load_mail_state(cfg.state_dir)[next(
                                  ref for ref, record in AC.load_mail_state(cfg.state_dir).items() if record["to"] == to)]["message_id"],
                              received_at_trusted=True)
               for i, to in enumerate(("lin@example.org", "huang@example.org"))]
    from tests.test_mailbox_agent import FakeLLM, analysis
    llm = FakeLLM(analysis("confirm", "教授確認附件4 無誤", ["確認無誤"]))
    result = _automate(env, date=TODAY + dt.timedelta(days=2), fetch_replies=lambda: replies, llm=llm)
    assert len(result.replies) == 2 and all(r.checks_passed for r in result.replies)
    # 信箱 agent 接在同一輪每日流程後面，兩封信都分析並留下軌跡；確認仍由系辦判讀
    assert len(result.mailbox) == 2
    steps = {t["step"] for t in agent_trace.load_trace(cfg.state_dir) if t["agent"] == "mailbox"}
    assert {"identify_sender", "link_case", "analyze", "done"} <= steps, steps
    _login(client, office=True)
    for r in replies:
        client.post("/office/advisor-reply", data={"message_id": r.message_id, "action": "confirmed",
                                                    "handler": "專員甲", "csrf_token": _csrf(client)})

    # 15. 口試後產附件6：兩案都納入（只替換寫檔，關卡邏輯照跑）
    included = []
    env["monkeypatch"].setattr(workflow.attachment6_generator, "generate",
                               lambda m, out, *, template, term=None: included.extend(e.案件鍵 for e in m.exams) or [])
    env["monkeypatch"].setattr(workflow, "_validate_templates", lambda *a, **k: None)
    application.generate_artifacts(
        master_path=cfg.master_path, output_dir=env["tmp"] / "out", phase="post-exam", state_dir=cfg.state_dir,
        templates=TemplatePaths(attachment4=Path("x.doc"), attachment6=Path("x.xlsx")))
    assert sorted(included) == [KEY1, KEY2]

    # 16. 系辦登錄附件6 已交付、已收回（合格）
    docs = client.get("/office/documents").get_data(as_text=True)
    tokens = re.findall(r'action="/office/collection".*?name="token" value="([^"]+)"', docs, re.S)
    assert tokens
    for token in tokens:
        for action in ("sent", "collected"):
            page = post_tracking(client, "/office/collection", data={"token": token, "action": action, "handler": "專員甲",
                                                            "csrf_token": _csrf(client)}, follow_redirects=True)
            assert 'class="flash error"' not in page.get_data(as_text=True)

    # 17. 收尾：各頁與匯出正常，寄送全有紀錄、無失敗
    for path in ("/office/overview", "/office/overview?view=calendar", "/office/communications",
                 "/office/documents", "/office/committees", "/office/overview/export",
                 "/office/communications/export"):
        assert client.get(path).status_code == 200, path
    events = send_log.load(cfg.state_dir / "send_log.json").all()
    assert len(events) == len(sent) and not [e for e in events if e.status == send_log.FAILED]
