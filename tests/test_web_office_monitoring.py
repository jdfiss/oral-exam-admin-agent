from tests.web_fixture import post_submission
# -*- coding: utf-8 -*-
"""系辦工作台的監管功能：通訊記錄頁（send_log）、案件詳情的缺件進度時間軸
（issue_store）。全程使用假資料，不碰任何真實學生資料。
"""
import datetime as _dt

import pytest

from koushi import issue_store, send_log
from koushi.web import app as webapp
from koushi.workflow import RegistryPaths

from tests.period_fixture import open_filling_period
from tests.att2_fixture import case as att2_case, write_att2


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_cfg = webapp.WebConfig(
        submissions_dir=tmp_path / "submissions",
        master_path=tmp_path / "output" / "master.xlsx",
        state_dir=tmp_path / "state",
        registries=RegistryPaths(
            attachment2=write_att2(tmp_path / "office" / "att2.xlsx", [att2_case(term="1131"), att2_case(term="1141"), att2_case(term="1142"), att2_case(term="1141", student_id="111900002", name="李小華")]),
            members=tmp_path / "office" / "missing_members.xlsx",
            distances=tmp_path / "office" / "missing_distances.csv",
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
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
        if not token:
            import secrets
            token = secrets.token_urlsafe(32)
            sess["_csrf_token"] = token
        return token


def _login_office(client):
    client.post("/login/office", data={"password": webapp.cfg().office_password, "csrf_token": _csrf(client)})


def _submit(client, term="1141", sid="111900001"):
    client.post("/login/student", data={"student_id": sid, "csrf_token": _csrf(client)})
    post_submission(client, data={
        "term": term, "student_name": "王小明", "student_email": "s@example.org",
        "exam_date": "2026-06-01", "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        "committee_0_name": "陳大文", "committee_0_unit": "資工系",
        "committee_0_email": "dawen@example.org",
        "committee_1_name": "校內委員乙", "committee_1_unit": "國立範例大學資訊工程學系",
        "committee_1_email": "b@example.org",
        "committee_2_name": "校內委員丙", "committee_2_unit": "國立範例大學資訊工程學系",
        "committee_2_email": "c@example.org",
        "csrf_token": _csrf(client),
    })
    client.get("/logout")


def test_communications_page_empty_state(client):
    _login_office(client)
    resp = client.get("/office/communications")
    assert resp.status_code == 200
    assert "尚無寄送記錄" in resp.get_data(as_text=True)


def test_communications_page_lists_send_log_events(client):
    log = send_log.SendEventLog()
    log.record(
        idempotency_key="k1", type="deficiency", to="s@example.org",
        subject="[口試資料補正通知] 王小明", tracking={"case_key": "1141-111900001"},
    )
    log.record_review("k1", approved=True, reviewer="office")
    log.record_sent("k1", success=True)

    log.record(
        idempotency_key="k2", type="collection", to="prof@example.org",
        subject="[附件6催繳] 陳大文", tracking={"case_key": "1141-111900002"},
    )
    log.record_review("k2", approved=True, reviewer="office")
    log.record_sent("k2", success=False, note="SMTP 連線逾時")

    send_log.save(log, webapp.cfg().state_dir / "send_log.json")

    _login_office(client)
    resp = client.get("/office/communications")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "缺件補件通知" in text
    assert "已寄出" in text
    assert "寄送失敗" in text
    assert "SMTP 連線逾時" in text


def test_communications_requires_office_login(client):
    resp = client.get("/office/communications", follow_redirects=True)
    assert "請先登入" in resp.get_data(as_text=True)


def test_case_detail_no_longer_shows_issue_timeline(client):
    _submit(client)
    case_key = "1141-111900001"

    record = issue_store.IssueRecord(
        case_key=case_key, issue_code="MISSING_ID_PHOTO", field="id_photo",
        term="1141", student_id="111900001", student_name="王小明",
        description="缺少身分證影本", owner="student",
        status=issue_store.DETECTED, detected_at=_dt.date(2026, 6, 1),
        history=({"from": None, "to": issue_store.DETECTED, "at": "2026-06-01", "note": ""},),
    )
    record = issue_store.transition(record, issue_store.DRAFT_READY, date=_dt.date(2026, 6, 2))
    record = issue_store.transition(record, issue_store.REQUEST_SENT, date=_dt.date(2026, 6, 3))
    store = issue_store.IssueRecordStore(records={record.tracking_key: record})
    issue_store.save(store, webapp.cfg().state_dir / "issue_records.json")

    _login_office(client)
    resp = client.get(f"/office/case/{case_key}")
    text = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "待補資料進度" not in text  # 缺件進度改看待辦清單與文件中心，案件詳情不重複列


# ── 匯出 Excel／月曆視圖 ─────────────────────────────────────

def _read_xlsx(resp):
    import io
    from openpyxl import load_workbook
    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return load_workbook(io.BytesIO(resp.data)).active


def test_overview_export_contains_all_cases(client):
    _submit(client)
    _login_office(client)
    ws = _read_xlsx(client.get("/office/overview/export"))
    rows = [[c.value for c in r] for r in ws.iter_rows()]
    assert rows[0][:5] == ["學生姓名", "學號", "學期", "口試日期", "填寫狀態"]
    body = [r for r in rows[1:] if r[1] == "111900001"]
    assert body and body[0][3] == "2026-06-01"


def test_communications_export_keeps_formula_like_text_as_text(client):
    log = send_log.SendEventLog()
    log.record(
        idempotency_key="k1", type="deficiency", to="s@example.org",
        subject='=HYPERLINK("http://evil.example","點我")', tracking={"case_key": "1141-111900001"},
    )
    send_log.save(log, webapp.cfg().state_dir / "send_log.json")
    _login_office(client)
    ws = _read_xlsx(client.get("/office/communications/export"))
    assert ws["A2"].value == "缺件補件通知"
    assert ws["C2"].data_type == "s"
    assert ws["C2"].value.startswith("=HYPERLINK")
    assert ws["D2"].value == "草稿中"


def test_exports_require_office_login(client):
    for path in ("/office/overview/export", "/office/communications/export"):
        resp = client.get(path, follow_redirects=True)
        assert "請先登入" in resp.get_data(as_text=True)


def test_calendar_view_places_case_on_exam_date(client):
    _submit(client)
    _login_office(client)
    text = client.get("/office/overview?view=calendar&month=2026-06").get_data(as_text=True)
    assert "2026 年 6 月" in text
    assert 'aria-label="查看 111900001 案件（2026-06-01）"' in text
    assert 'id="list-view" hidden' in text
    # 別的月份不會出現這筆
    other = client.get("/office/overview?view=calendar&month=2026-07").get_data(as_text=True)
    assert "（2026-06-01）" not in other


def test_calendar_counts_cases_without_exam_date(client):
    import json
    _submit(client)
    path = webapp.cfg().submissions_dir / "1141-111900001.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["exam_date"] = ""
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _login_office(client)
    text = client.get("/office/overview?view=calendar&month=2026-06").get_data(as_text=True)
    assert "1 筆案件尚未填口試日期" in text


def test_calendar_bad_month_falls_back_to_current_month(client):
    _login_office(client)
    resp = client.get("/office/overview?view=calendar&month=2026-13")
    assert resp.status_code == 200
    today = _dt.date.today()
    assert f"{today.year} 年 {today.month} 月" in resp.get_data(as_text=True)


def test_overview_defaults_to_list_view(client):
    _login_office(client)
    text = client.get("/office/overview").get_data(as_text=True)
    assert 'id="calendar-view" hidden' in text
    assert "匯出 Excel" in text


def test_calendar_fragment_for_month_navigation(client):
    _submit(client)
    _login_office(client)
    text = client.get("/office/overview/calendar?month=2026-06").get_data(as_text=True)
    assert 'data-month="2026-06"' in text
    assert "（2026-06-01）" in text
    assert "<html" not in text.lower()  # 只回月曆區塊，不是整頁
    assert 'data-cal-month="2026-07"' in text


def test_calendar_fragment_requires_office_login(client):
    resp = client.get("/office/overview/calendar?month=2026-06", follow_redirects=True)
    assert "請先登入" in resp.get_data(as_text=True)
