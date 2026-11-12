from tests.web_fixture import post_submission, post_tracking, post_committee_reply
# -*- coding: utf-8 -*-
"""系辦上傳建檔資料（附件2／交通費／口試委員）的 Web 路由，以及上傳後自動建檔的端到端驗證。

學生送出即有效（2026-09-23 取消系辦審核），測試不再經過審核步驟。

只測 HTTP 層的串接（登入權限、檔案上傳、flash 訊息、儀表板狀態顯示）；
`save_office_registry_file`／`auto_build_if_ready` 本身的規則見
test_office_registry_upload.py。
"""
import io
import tempfile
from pathlib import Path

import openpyxl
import pytest

from koushi.web import app as webapp
from koushi.workflow import RegistryPaths

from tests.period_fixture import open_filling_period
from tests.att2_fixture import case, write_att2


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_cfg = webapp.WebConfig(
        submissions_dir=tmp_path / "submissions",
        master_path=tmp_path / "output" / "master.xlsx",
        state_dir=tmp_path / "state",
        registries=RegistryPaths(
            attachment2=tmp_path / "office" / "附件2.xlsx",
            members=tmp_path / "office" / "委員個資.xlsx",
            distances=tmp_path / "office" / "距離表.csv",
        ),
        doc_output_root=tmp_path / "doc_output",
        office_password="test-password",
    )
    monkeypatch.setitem(webapp.app.config, "_web", test_cfg)
    monkeypatch.setitem(webapp.app.config, "TESTING", True)
    open_filling_period(test_cfg.state_dir)
    with webapp.app.test_client() as c:
        yield c


def _csrf(client):
    """所有 POST 都要求表單帶 session 裡的 csrf_token；測試直接讀寫
    session（等同於先 GET 過某個頁面拿到 token），不必真的解析 HTML。"""
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
        if not token:
            import secrets
            token = secrets.token_urlsafe(32)
            sess["_csrf_token"] = token
        return token


def _login_office(client):
    client.post("/login/office", data={"password": webapp.cfg().office_password, "csrf_token": _csrf(client)})


def _submit(client):
    client.post("/login/student", data={"student_id": "111900001", "csrf_token": _csrf(client)})
    resp = post_submission(client, data={
        "term": "1141", "student_email": "s@example.org", "exam_date": "2026-06-01",
        "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        "committee_0_name": "陳大文", "committee_0_unit": "國立範例大學資訊工程學系",
        "committee_0_email": "dawen@example.org",
        "committee_1_name": "校內委員乙", "committee_1_unit": "國立範例大學資訊工程學系",
        "committee_1_email": "b@example.org",
        "committee_2_name": "校內委員丙", "committee_2_unit": "國立範例大學資訊工程學系",
        "committee_2_email": "c@example.org",
        "csrf_token": _csrf(client),
    }, follow_redirects=True)
    client.get("/logout")
    _login_office(client)
    return resp


from tests.period_fixture import start_deficiency_period as _start_deficiency_period


def _valid_files():
    distance = io.BytesIO("服務單位,單程公里\n國立清華大學資訊工程學系,30\n".encode("utf-8-sig"))

    with tempfile.TemporaryDirectory() as tmp:
        att2_path = write_att2(Path(tmp) / "att2.xlsx", [
            case(),
            # 服務單位不在距離表 → NO_DISTANCE（系辦責任）
            case(student_id="111900002", name="李小華",
                 committees=[{"name": "外校委員", "unit": "遠方大學", "title": "教授"}]),
        ])
        att2_buf = io.BytesIO(att2_path.read_bytes())

    member_wb = openpyxl.Workbook()
    ws2 = member_wb.active
    ws2.append(["委員編號", "委員姓名", "英文姓名", "服務單位", "最高學歷學校", "最高學位",
                "身分證字號", "金融機構", "帳號", "戶籍地址", "Email", "最後更新", "來源"])
    ws2.append(["C0001", "陳大文", "", "國立範例大學資訊工程學系", "", "",
                "A123456789", "", "", "", "", "2026-09-17", "系辦上傳"])
    member_buf = io.BytesIO()
    member_wb.save(member_buf)
    member_buf.seek(0)

    return distance, att2_buf, member_buf


def test_upload_requires_office_login(client):
    distance, att2, member = _valid_files()
    resp = client.post("/office/upload-registries", data={
        "distances": (distance, "d.csv"),
        "csrf_token": _csrf(client),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert "請先登入" in resp.get_data(as_text=True)


def test_upload_rejects_bad_file_without_touching_target(client):
    _login_office(client)
    bad = io.BytesIO(b"not a csv")
    resp = client.post("/office/upload-registries", data={
        "distances": (bad, "d.txt"),
        "csrf_token": _csrf(client),
    }, content_type="multipart/form-data", follow_redirects=True)
    text = resp.get_data(as_text=True)
    assert "格式錯誤" in text
    assert not webapp.cfg().registries.distances.exists()


def test_dashboard_shows_waiting_status_after_submit_without_files(client, tmp_path):
    resp = _submit(client)
    text = resp.get_data(as_text=True)
    assert "已送出" in text
    # 建檔資料狀態已搬到「匯入資料」頁，不再顯示於 /dashboard（工作總覽）。
    import_page = client.get("/office/import").get_data(as_text=True)
    assert "尚未提供" in import_page
    assert "等待中" in import_page
    assert not webapp.cfg().master_path.exists()


def test_upload_all_three_files_triggers_auto_build(client, tmp_path):
    _submit(client)
    distance, att2, member = _valid_files()
    resp = client.post("/office/upload-registries", data={
        "distances": (distance, "d.csv"),
        "attachment2": (att2, "a.xlsx"),
        "members": (member, "m.xlsx"),
        "csrf_token": _csrf(client),
    }, content_type="multipart/form-data", follow_redirects=True)
    text = resp.get_data(as_text=True)
    assert "已更新" in text
    assert "案件資料已自動更新" in text
    assert webapp.cfg().master_path.is_file()

    # 自動建檔結果同樣顯示在「匯入資料」頁。
    import_page = client.get("/office/import").get_data(as_text=True)
    assert "成功" in import_page
    assert "2 案" in import_page  # 案件數以附件2 為準，不是以表單數


def test_successful_auto_build_starts_deficiency_deadline_clock(client, tmp_path):
    """建檔成功後應自動同步缺件追蹤，讓新缺件的補件期限開始倒數。"""
    from koushi.engines.state import load_from_dir

    _submit(client)
    _start_deficiency_period(webapp.cfg().state_dir)  # 填寫期間已結束，上傳附件2 後正式檢核
    distance, att2, member = _valid_files()
    client.post("/office/upload-registries", data={
        "distances": (distance, "d.csv"),
        "attachment2": (att2, "a.xlsx"),
        "members": (member, "m.xlsx"),
        "csrf_token": _csrf(client),
    }, content_type="multipart/form-data", follow_redirects=True)

    state = load_from_dir(webapp.cfg().state_dir)
    # 111900001 表單資料完整，不會有學生責任缺件；委員暫定身分屬於向委員
    # 本人索取的缺件，不算學生的。另外確認狀態檔已建立。
    assert (webapp.cfg().state_dir / "engines_state.json").is_file()
    assert state.deficiency
    assert not [d for d in state.deficiency
                if d.owner == "student" and d.case_key == "1141-111900001"]


def test_upload_ignores_directory_parts_in_browser_filename(client, tmp_path, monkeypatch):
    """瀏覽器送來的檔名含 ../ 時只取最後一段，暫存檔不可跳出暫存資料夾。"""
    import tempfile
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(sandbox))
    _login_office(client)
    distance, _att2, _member = _valid_files()
    client.post("/office/upload-registries", data={
        "distances": (distance, "../../escaped.csv"),
        "csrf_token": _csrf(client),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert webapp.cfg().registries.distances.is_file()
    assert not (tmp_path / "escaped.csv").exists()
    assert not (sandbox / "escaped.csv").exists()


def test_partial_upload_only_updates_selected_files(client):
    _login_office(client)
    distance, att2, member = _valid_files()
    client.post("/office/upload-registries", data={
        "distances": (distance, "d.csv"),
        "csrf_token": _csrf(client),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert webapp.cfg().registries.distances.is_file()
    assert not webapp.cfg().registries.attachment2.exists()
    assert not webapp.cfg().registries.members.exists()


def test_dashboard_shows_no_tracking_panel_before_first_build(client):
    _login_office(client)
    text = client.get("/office/documents").get_data(as_text=True)
    assert "追蹤現況" not in text


def test_dashboard_shows_tracking_and_todos_after_build(client, tmp_path):
    """一筆乾淨案件＋一筆刻意不填指導教授、委員服務單位不在距離表的案件，
    後者同時產生 student 責任（缺指導教授）與 office 責任（NO_DISTANCE）
    的真實缺件，藉此驗證儀表板兩個待辦區塊都會顯示。
    """
    import datetime as dt
    from dataclasses import replace
    from koushi.deadline import Deadline
    from koushi.engines.state import load_from_dir, save_to_dir

    _submit(client)
    _start_deficiency_period(webapp.cfg().state_dir)  # 填寫期間已結束，上傳附件2 後正式檢核

    client.post("/login/student", data={"student_id": "111900002", "csrf_token": _csrf(client)})
    post_submission(client, data={  # 不填指導教授 → 學生責任缺件
        "term": "1141", "student_email": "s2@example.org", "exam_date": "2026-06-02",
        "committee_0_name": "外校委員", "committee_0_unit": "遠方大學",
        "committee_0_email": "far@example.org",
        "committee_1_name": "校內委員乙", "committee_1_unit": "國立範例大學資訊工程學系",
        "committee_1_email": "b@example.org",
        "committee_2_name": "校內委員丙", "committee_2_unit": "國立範例大學資訊工程學系",
        "committee_2_email": "c@example.org",
        "csrf_token": _csrf(client),
    }, follow_redirects=True)
    client.get("/logout")
    _login_office(client)

    distance, att2, member = _valid_files()
    client.post("/office/upload-registries", data={
        "distances": (distance, "d.csv"),
        "attachment2": (att2, "a.xlsx"),
        "members": (member, "m.xlsx"),
        "csrf_token": _csrf(client),
    }, content_type="multipart/form-data", follow_redirects=True)

    # sync_deficiency_deadlines 已把這批缺件的 notified_date 設成今天；
    # 手動把截止日撥到過去，模擬「已經過了補件期限」，不改變缺件本身。
    state = load_from_dir(webapp.cfg().state_dir)
    assert state.deficiency  # 確認真的偵測到缺件，不是空跑一次測試
    past = Deadline(base_date=dt.date(2020, 1, 1), business_days=5, computed_date=dt.date(2020, 1, 8))
    state.deficiency = [replace(item, deadline=past) for item in state.deficiency]
    save_to_dir(state, webapp.cfg().state_dir)

    text = client.get("/office/documents").get_data(as_text=True)
    assert "追蹤現況" in text
    assert "系辦內部待辦" in text and "NO_DISTANCE" in text
    assert "學生補件已逾期" in text and "1141-111900002" in text


def _upload_all(client):
    distance, att2, member = _valid_files()
    client.post("/office/upload-registries", data={
        "distances": (distance, "d.csv"), "attachment2": (att2, "a.xlsx"),
        "members": (member, "m.xlsx"), "csrf_token": _csrf(client),
    }, content_type="multipart/form-data", follow_redirects=True)


def test_review_queue_and_clear_case_end_to_end(client):
    """人工關卡：建檔後每案列為待放行；按「已處理」後改為已放行並留下處理人與備註。"""
    _submit(client)
    _upload_all(client)

    overview = client.get("/office/overview").get_data(as_text=True)
    assert "委員資格審核" in overview and "等你審核" in overview

    # 111900002 只有附件2、學生沒填：抽屜仍要能打開
    detail = client.get("/office/case/1141-111900002").get_data(as_text=True)
    assert "學生尚未在本系統填寫補充資料" in detail
    assert "審核完成，可以寄學位口試一覽表" in detail

    resp = client.post("/office/case/1141-111900001/clear",
                       data={"note": "委員資格已確認", "handler": "專員甲", "csrf_token": _csrf(client)},
                       follow_redirects=True)
    assert "審核完成 王小明（111900001）" in resp.get_data(as_text=True)

    from koushi import case_review
    reviews = case_review.load(webapp.cfg().state_dir)
    assert reviews["1141-111900001"].note == "委員資格已確認"
    assert reviews["1141-111900001"].cleared_by == "專員甲"
    assert "1141-111900002" not in reviews
    detail = client.get("/office/case/1141-111900001").get_data(as_text=True)
    assert "重新審核完成" in detail and "委員資格已確認" in detail


def test_clear_unknown_case_is_rejected(client):
    _submit(client)
    _upload_all(client)
    resp = client.post("/office/case/1141-999999999/clear",
                       data={"handler": "專員甲", "csrf_token": _csrf(client)}, follow_redirects=True)
    assert "找不到案件" in resp.get_data(as_text=True)


def test_committee_request_page_and_manual_reply(client):
    """放行後，撥帳不齊的校外委員出現在「委員個資」頁；系辦登錄回覆後標為已回填。"""
    client.post("/login/student", data={"student_id": "111900002", "csrf_token": _csrf(client)})
    post_submission(client, data={
        "term": "1141", "student_email": "s2@example.org", "exam_date": "2026-12-01",
        "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        "committee_0_name": "外校委員", "committee_0_unit": "遠方大學",
        "committee_0_email": "far@example.org",
        "committee_1_name": "校內委員乙", "committee_1_unit": "國立範例大學資訊工程學系",
        "committee_1_email": "b@example.org",
        "committee_2_name": "校內委員丙", "committee_2_unit": "國立範例大學資訊工程學系",
        "committee_2_email": "c@example.org",
        "csrf_token": _csrf(client),
    })
    client.get("/logout")
    _login_office(client)
    _upload_all(client)

    assert "目前沒有需要索取資料的委員" in client.get("/office/committees").get_data(as_text=True)
    client.post("/office/case/1141-111900002/clear", data={"handler": "專員甲", "csrf_token": _csrf(client)})
    page = client.get("/office/committees").get_data(as_text=True)
    assert "外校委員" in page and "待寄出" in page

    resp = post_committee_reply(client, data={
        "email": "far@example.org", "身分證字號": "A123456789", "金融機構": "某銀行",
        "帳號": "0001-2345-678", "戶籍地址": "某地", "handler": "專員甲", "csrf_token": _csrf(client),
        "身分證字號_確認": "a123456789", "帳號_確認": "00012345678",
    }, follow_redirects=True)
    text = resp.get_data(as_text=True)
    assert "已登錄 far@example.org" in text
    # 回填後重建主檔：撥帳齊全的委員不再列入待取得清單，頁面也不顯示個資
    assert "A123456789" not in text
    # 不再列在待取得清單，改列在「已回填的委員」（只有姓名與時間，沒有個資）
    pending_part, _, received_part = text.partition("已回填的委員")
    assert "外校委員" not in pending_part and "外校委員" in received_part


def test_pre_exam_generation_records_attachment4_and_prefilled_attachment6(client, monkeypatch, tmp_path):
    """口試前產檔結果只有一份附件4＋預填附件6；先前讀不存在的 attachment4_files 會在最後一步出錯。"""
    from koushi.web import app as web_module
    from koushi.workflow import PreExamResult

    _submit(client)
    _upload_all(client)
    out = web_module._doc_output_dir("pre-exam")
    out.mkdir(parents=True, exist_ok=True)
    (out / "附件4-學位口試一覽.docx").write_bytes(b"docx")
    (out / "附件6-預填-1.xlsx").write_bytes(b"xlsx")
    monkeypatch.setattr(web_module, "generate_artifacts", lambda **kw: PreExamResult(
        attachment4=out / "附件4-學位口試一覽.docx",
        attachment6_files=[out / "附件6-預填-1.xlsx"], issues=[]))

    resp = client.post("/office/documents/generate", data={"phase": "pre-exam", "csrf_token": _csrf(client)},
                       follow_redirects=True)
    text = resp.get_data(as_text=True)
    assert "已產生學位口試一覽表與 1 份預填學生口試費清冊" in text
    assert client.get("/office/documents/download/pre-exam/附件4-學位口試一覽.docx").status_code == 200


def test_clear_requires_handler_name(client):
    _submit(client)
    _upload_all(client)
    resp = client.post("/office/case/1141-111900001/clear",
                       data={"csrf_token": _csrf(client)}, follow_redirects=True)
    assert "請填寫處理人姓名" in resp.get_data(as_text=True)
    from koushi import case_review
    assert "1141-111900001" not in case_review.load(webapp.cfg().state_dir)


def test_attachment4_log_and_collection_recording(client):
    """文件中心：附件4 只列寄送紀錄（教授確認已取消，不再手動登錄）；附件6 回收照常登錄。"""
    from koushi.application import request_token
    from koushi.engines.state import load_from_dir

    _submit(client)
    _upload_all(client)
    page = client.get("/office/documents").get_data(as_text=True)
    assert "學位口試一覽表 寄送紀錄" in page and "學生口試費清冊 回收登錄" in page and "林指導" in page
    assert 'value="confirmed"' not in page

    key = ("1141", "碩", "林指導", "6/1")   # 附件6 回收仍以清冊（場次）為單位
    token = request_token(key)
    resp = post_tracking(client, "/office/collection", data={
        "token": token, "action": "needs_correction", "note": "缺簽名", "handler": "專員甲",
        "csrf_token": _csrf(client),
    }, follow_redirects=True)
    assert "已登錄學生口試費清冊 回收狀態" in resp.get_data(as_text=True)
    item = next(i for i in load_from_dir(webapp.cfg().state_dir).collection if i.request_key == key)
    assert item.qualified is False and item.correction_note == "缺簽名"

    audit = (webapp.cfg().state_dir / "audit_log.json").read_text(encoding="utf-8")
    assert "專員甲" in audit


def test_overview_lists_portal_cases_not_filled_and_unmatched_fillings(client):
    """review-2026-09-23 P1-6／P1-8：附件2 上傳後，總覽列出未填寫的案件與附件2 查無的填寫。"""
    _submit(client)  # 111900001 已填
    client.get("/logout")
    client.post("/login/student", data={"student_id": "111900999", "csrf_token": _csrf(client)})
    post_submission(client, data={  # 附件2 沒有這位學生
        "term": "1141", "student_email": "x@example.org", "exam_date": "2026-11-01", "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        **{f"committee_{i}_{k}": v for i in range(3) for k, v in
           (("name", f"委員{i}"), ("unit", "某大學"), ("email", f"c{i}@example.org"))},
        "csrf_token": _csrf(client),
    })
    client.get("/logout")
    _login_office(client)
    _upload_all(client)
    text = client.get("/office/overview").get_data(as_text=True)
    assert "李小華" in text and "未填寫" in text and "無信箱" in text   # 111900002 在附件2、沒填
    assert "系所審核名單 查無" in text and "111900999" in text
    assert "項自我檢查提醒" in text  # 111900001 的口試日期 2026-06-01 已經過去


def test_overview_warns_when_attachment2_missing_after_deadline(client):
    import datetime as dt
    from koushi import settings as office_settings
    _login_office(client)
    office_settings.save(office_settings.OfficeSettings(
        fill_deadline=dt.date.today() - dt.timedelta(days=2)), webapp.cfg().state_dir)
    text = client.get("/office/overview").get_data(as_text=True)
    assert "尚未上傳系所審核名單" in text


def test_overview_shows_failed_automation(client):
    from koushi.application import record_automation_status
    _login_office(client)
    record_automation_status(webapp.cfg().state_dir, ok=False, error="填寫截止日已過但尚未上傳附件2")
    text = client.get("/office/overview").get_data(as_text=True)
    assert "每日自動化最近一次" in text and "執行失敗" in text


def test_committee_fills_banking_data_through_private_link(client):
    """委員從專屬連結自行填寫；錯誤時不存，成功後頁面不回顯個資，連結變成已完成。"""
    client.post("/login/student", data={"student_id": "111900002", "csrf_token": _csrf(client)})
    post_submission(client, data={
        "term": "1141", "student_email": "s2@example.org", "exam_date": "2026-12-01",
        "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        "committee_0_name": "外校委員", "committee_0_unit": "遠方大學",
        "committee_0_email": "far@example.org",
        "committee_1_name": "校內委員乙", "committee_1_unit": "國立範例大學資訊工程學系",
        "committee_1_email": "b@example.org",
        "committee_2_name": "校內委員丙", "committee_2_unit": "國立範例大學資訊工程學系",
        "committee_2_email": "c@example.org",
        "csrf_token": _csrf(client),
    })
    client.get("/logout")
    _login_office(client)
    _upload_all(client)
    client.post("/office/case/1141-111900002/clear", data={"handler": "專員甲", "csrf_token": _csrf(client)})
    page = client.get("/office/committees").get_data(as_text=True)
    assert "委員線上填寫連結" in page
    client.get("/logout")

    from koushi import committee_requests
    token = committee_requests.link_token(webapp.cfg().state_dir, "far@example.org")
    assert client.get("/committee/reply/not-a-real-token-at-all-000").status_code == 404
    form = client.get(f"/committee/reply/{token}").get_data(as_text=True)
    assert "外校委員 老師您好" in form

    rejected = client.post(f"/committee/reply/{token}", data={"csrf_token": _csrf(client)}).get_data(as_text=True)
    assert "請先完成信箱驗證" in rejected
    from koushi import committee_verification as CV
    captured = []
    CV.send_code(webapp.cfg().state_dir, token=token, email="far@example.org",
        secret=webapp.app.secret_key, transport=lambda **kw: captured.append(kw) or True)
    code = __import__("re").search(r"驗證碼是：(\d{6})", captured[-1]["body"]).group(1)
    client.post(f"/committee/reply/{token}", data={"action":"verify-code", "code":code, "csrf_token":_csrf(client)})

    bad = client.post(f"/committee/reply/{token}", data={
        "身分證字號": "A123456788", "金融機構": "某銀行", "帳號": "00012345678", "戶籍地址": "某地",
        "身分證字號_確認": "A123456788", "帳號_確認": "00012345678", "csrf_token": _csrf(client),
    }).get_data(as_text=True)
    assert "檢查碼" in bad
    assert "far@example.org" not in committee_requests.load_replies(webapp.cfg().state_dir)

    ok = client.post(f"/committee/reply/{token}", data={
        "身分證字號": "A123456789", "金融機構": "某銀行", "帳號": "00012345678", "戶籍地址": "某地",
        "身分證字號_確認": "A123456789", "帳號_確認": "00012345678", "csrf_token": _csrf(client),
    }).get_data(as_text=True)
    assert "已收到您的撥款資料" in ok and "A123456789" not in ok
    reply = committee_requests.load_replies(webapp.cfg().state_dir)["far@example.org"]
    assert reply["source"] == "委員線上填寫"
    # 回填後重建主檔，委員撥帳齊全，已不在索取清單：連結顯示無效（不再收資料）
    assert client.get(f"/committee/reply/{token}").status_code == 404


def test_attachment2_upload_records_first_upload_date_only_once(client):
    """2026-10-01 Q4：從上傳頁上傳附件2 會記錄本期第一次上傳日；重傳不改。"""
    import datetime as dt
    from koushi import settings as office_settings
    _login_office(client)
    _upload_all(client)
    first = office_settings.load(webapp.cfg().state_dir).att2_first_uploaded
    assert first == dt.date.today()
    office_settings.save(office_settings.OfficeSettings(
        current_term="1141", fill_deadline=dt.date.today(), att2_first_uploaded=dt.date(2026, 1, 1)),
        webapp.cfg().state_dir)
    _upload_all(client)
    assert office_settings.load(webapp.cfg().state_dir).att2_first_uploaded == dt.date(2026, 1, 1)


def _master():
    from koushi.application import load_current_master
    return load_current_master(webapp.cfg().master_path)


def test_office_correction_survives_reupload_and_resubmission(client):
    """2026-10-01 Q10／Q11：專員修正存成另一層，重傳附件2、學生重送都不會蓋掉。"""
    _submit(client)
    _upload_all(client)
    resp = client.post("/office/case/1141-111900001/correction", data={
        "field": "口試地點", "value": "工程館 A204", "handler": "專員甲", "reason": "教授來信更正",
        "action": "set", "csrf_token": _csrf(client),
    }, follow_redirects=True)
    assert "已儲存修正" in resp.get_data(as_text=True)
    assert _master().by_key()["1141-111900001"].口試地點 == "工程館 A204"

    _upload_all(client)  # 系辦重傳附件2
    _submit(client)      # 學生重送（表單沒有這個地點）
    assert _master().by_key()["1141-111900001"].口試地點 == "工程館 A204"
    detail = client.get("/office/case/1141-111900001").get_data(as_text=True)
    assert "原為「" in detail and "教授來信更正" in detail

    # 案件詳情的表單用 target＝「委員姓名|欄位」（案件層欄位的委員姓名為空）
    client.post("/office/case/1141-111900001/correction", data={
        "target": "|口試地點", "handler": "專員甲", "action": "remove", "csrf_token": _csrf(client),
    })
    assert _master().by_key()["1141-111900001"].口試地點 != "工程館 A204"


def test_correction_requires_handler_and_reason(client):
    _submit(client)
    _upload_all(client)
    resp = client.post("/office/case/1141-111900001/correction", data={
        "field": "口試地點", "value": "X", "handler": "專員甲", "reason": "", "action": "set",
        "csrf_token": _csrf(client),
    }, follow_redirects=True)
    assert "請填寫修正原因" in resp.get_data(as_text=True)


def test_committee_change_after_clear_revokes_release(client):
    """2026-10-01 Q9：放行後委員名單（含職稱）變動，放行失效、回到待放行，不寄附件4／不索取個資。"""
    from koushi import case_review
    from koushi.application import _unreviewed_case_issues
    _submit(client)
    _upload_all(client)
    client.post("/office/case/1141-111900001/clear",
                data={"handler": "專員甲", "csrf_token": _csrf(client)})
    assert "1141-111900001" in case_review.cleared_keys(_master(), webapp.cfg().state_dir)

    client.post("/office/case/1141-111900001/correction", data={
        "field": "職稱", "member": "陳大文", "value": "副教授", "handler": "專員甲",
        "reason": "教授來信：職稱有誤", "action": "set", "csrf_token": _csrf(client),
    })
    master = _master()
    assert "1141-111900001" not in case_review.cleared_keys(master, webapp.cfg().state_dir)
    overview = client.get("/office/overview").get_data(as_text=True)
    assert "委員名單有變動・等你重新審核" in overview
    pending = [i for i in _unreviewed_case_issues(master, webapp.cfg().state_dir)
               if i["案件鍵"] == "1141-111900001"]
    assert pending and "重新審核" in pending[0]["說明"]

    client.post("/office/case/1141-111900001/clear",
                data={"handler": "專員甲", "csrf_token": _csrf(client)})
    assert "1141-111900001" in case_review.cleared_keys(_master(), webapp.cfg().state_dir)


def test_locked_student_requests_change_and_office_unlocks(client):
    """2026-10-01 Q8：沒收到補件信的學生資料鎖定，申請異動後由專員開放修改；系統不會自動開放。"""
    import datetime as dt
    from koushi import case_access
    _submit(client)
    _upload_all(client)
    _start_deficiency_period(webapp.cfg().state_dir)
    client.get("/logout")
    client.post("/login/student", data={"student_id": "111900002", "csrf_token": _csrf(client)})
    dashboard = client.get("/dashboard").get_data(as_text=True)
    assert "申請異動" in dashboard
    resp = client.post("/change-request", data={"reason": "口試改期到 12/3", "csrf_token": _csrf(client)},
                       follow_redirects=True)
    assert "已送出異動申請" in resp.get_data(as_text=True)
    assert not case_access.is_unlocked(webapp.cfg().state_dir, "1141-111900002", dt.date.today())

    client.get("/logout")
    _login_office(client)
    assert "學生異動申請" in client.get("/office/overview").get_data(as_text=True)
    detail = client.get("/office/case/1141-111900002").get_data(as_text=True)
    assert "口試改期到 12/3" in detail and "待處理" in detail
    until = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    resp = client.post("/office/case/1141-111900002/unlock", data={
        "until": until, "handler": "專員甲", "reason": "口試改期", "csrf_token": _csrf(client),
    }, follow_redirects=True)
    assert f"修改到 {until}" in resp.get_data(as_text=True)
    assert case_access.is_unlocked(webapp.cfg().state_dir, "1141-111900002", dt.date.today())
    assert not case_access.requests(webapp.cfg().state_dir, pending_only=True)


def test_collected_requires_committee_banking_registered(client):
    """2026-10-01 Q15：委員在附件6 上手寫的個資沒登錄完，該份附件6 不能標「已收回（合格）」。"""
    from koushi.application import request_token
    from koushi.engines.state import load_from_dir
    client.post("/login/student", data={"student_id": "111900002", "csrf_token": _csrf(client)})
    post_submission(client, data={
        "term": "1141", "student_email": "s2@example.org", "exam_date": "2026-12-01",
        "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        "committee_0_name": "外校委員", "committee_0_unit": "遠方大學",
        "committee_0_email": "far@example.org",
        "committee_1_name": "校內委員乙", "committee_1_unit": "國立範例大學資訊工程學系",
        "committee_1_email": "b@example.org",
        "committee_2_name": "校內委員丙", "committee_2_unit": "國立範例大學資訊工程學系",
        "committee_2_email": "c@example.org",
        "csrf_token": _csrf(client),
    })
    client.get("/logout")
    _login_office(client)
    _upload_all(client)
    client.post("/office/case/1141-111900002/clear", data={"handler": "專員甲", "csrf_token": _csrf(client)})
    token = request_token(("1141", "碩", "林指導", "12/1"))
    for action in ("sent", "confirmed"):
        post_tracking(client, "/office/confirmation", data={
            "token": token, "action": action, "handler": "專員甲", "csrf_token": _csrf(client)})

    resp = post_tracking(client, "/office/collection", data={
        "token": token, "action": "collected", "handler": "專員甲", "csrf_token": _csrf(client),
    }, follow_redirects=True)
    assert "外校委員" in resp.get_data(as_text=True) and "尚未登錄" in resp.get_data(as_text=True)

    post_committee_reply(client, data={
        "email": "far@example.org", "身分證字號": "A123456789", "金融機構": "某銀行",
        "帳號": "0001-2345-678", "戶籍地址": "某地", "handler": "專員甲", "csrf_token": _csrf(client),
        "身分證字號_確認": "a123456789", "帳號_確認": "00012345678",
    })
    resp = post_tracking(client, "/office/collection", data={
        "token": token, "action": "collected", "handler": "專員甲", "csrf_token": _csrf(client),
    }, follow_redirects=True)
    assert "已登錄學生口試費清冊 回收狀態" in resp.get_data(as_text=True)
    key = ("1141", "碩", "林指導", "12/1")
    item = next(i for i in load_from_dir(webapp.cfg().state_dir).collection if i.request_key == key)
    assert item.qualified is True


def test_student_can_save_draft_without_validation_and_resume(client):
    """2026-10-01 Q2：暫存不檢查、不算送出，回來接著填；送出成功後暫存清掉。"""
    import json
    client.post("/login/student", data={"student_id": "111900001", "csrf_token": _csrf(client)})
    resp = post_submission(client, data={
        "term": "1141", "student_email": "", "exam_location": "工程館 A204",
        "advisor_0": "林指導", "action": "draft", "csrf_token": _csrf(client),
    }, follow_redirects=True)
    text = resp.get_data(as_text=True)
    assert "已暫存，尚未送出" in text and "有暫存但尚未送出" in text
    assert not (webapp.cfg().submissions_dir / "1141-111900001.json").exists()
    form = client.get("/form?term=1141").get_data(as_text=True)
    assert "上次暫存的內容" in form and "工程館 A204" in form

    _submit(client)  # 正式送出（會登出學生、登入系辦）
    assert (webapp.cfg().submissions_dir / "1141-111900001.json").exists()
    assert not (webapp.cfg().submissions_dir / "drafts" / "1141-111900001.json").exists()
