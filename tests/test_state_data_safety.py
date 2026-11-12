"""O11：持久狀態損壞時停止操作、不退回空狀態重寄；Web 顯示 503 恢復頁。"""
import pytest

from koushi import advisor_confirmation as AC, application, case_review, settings, util as U
from koushi.engines import state as engine_state
from koushi.web import app as webapp
from tests.test_advisor_confirmation import env, TODAY  # noqa: F401  (fixture)
from tests.test_web_office_upload import client, _login_office  # noqa: F401  (fixture)

LOADERS = [
    ("settings.json", settings.load),
    (AC.MAIL_FILE, AC.load_mail_state),
    (AC.INBOX_FILE, AC.load_inbox),
    ("engines_state.json", engine_state.load_from_dir),
]


@pytest.mark.parametrize("name,load", LOADERS)
def test_missing_state_is_empty_but_corrupt_or_wrong_shape_stops(tmp_path, name, load):
    load(tmp_path)  # 不存在：回傳預設值，不拋錯
    for content in ("{not json", "[]"):
        (tmp_path / name).write_text(content, encoding="utf-8")
        with pytest.raises(U.StateDataError):
            load(tmp_path)


def test_corrupt_mail_state_sends_nothing(env):
    for exam in env["master"].exams:
        case_review.clear(env["state"], exam=exam, master=env["master"])
    (env["state"] / AC.MAIL_FILE).write_text("{broken", encoding="utf-8")
    sent = []
    with pytest.raises(U.StateDataError):
        application.process_advisor_confirmations(
            master=env["master"], state_dir=env["state"], submissions_dir=env["subs"], send=True,
            transport=lambda **mail: sent.append(mail) or True, date=TODAY, template="unused.doc")
    assert sent == []


def test_web_shows_recovery_page_for_corrupt_settings(client):
    _login_office(client)
    (webapp.cfg().state_dir / "settings.json").write_text("{broken", encoding="utf-8")
    response = client.get("/office/settings")
    assert response.status_code == 503
    assert "本次操作已停止" in response.get_data(as_text=True)


def test_rejected_submission_redisplays_what_the_student_typed(client):
    """G13：驗證失敗不能讓學生重打；回 422 並帶回剛輸入的內容，也不存檔。"""
    from tests.test_web_office_upload import _csrf
    from tests.web_fixture import post_submission
    client.post("/login/student", data={"student_id": "111900001", "csrf_token": _csrf(client)})
    response = post_submission(client, data={
        "term": "1141", "student_email": "s@example.org", "exam_date": "2026-06-01",
        "exam_location": "工程館 A204", "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        "committee_0_name": "唯一委員", "committee_0_unit": "國立範例大學資訊工程學系",
        "committee_0_email": "only@example.org", "csrf_token": _csrf(client)})
    page = response.get_data(as_text=True)
    assert response.status_code == 422
    assert "工程館 A204" in page and "唯一委員" in page
    assert not list(webapp.cfg().submissions_dir.glob("*.json"))


def test_sensitive_pages_are_not_cached_and_uploads_are_capped(client):
    """O04／O05：個資頁面不留快取、不送 Referer；超大上傳直接拒絕。"""
    _login_office(client)
    response = client.get("/office/overview")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    too_big = client.post("/office/upload-registries", data=b"x" * (21 * 1024 * 1024),
                          content_type="application/octet-stream")
    assert too_big.status_code == 413


def test_production_session_cookie_is_https_only():
    from dataclasses import replace
    current = webapp.cfg()
    try:
        webapp.configure_app(webapp.app, replace(current, production=True))
        assert webapp.app.config["SESSION_COOKIE_SECURE"] is True
    finally:
        webapp.configure_app(webapp.app, current)
    assert webapp.app.config["SESSION_COOKIE_SECURE"] is current.production


def test_production_overview_warns_when_daily_automation_is_stale(client, monkeypatch):
    """O09：正式環境排程超過一天沒更新狀態時，總覽顯示告警。"""
    import datetime as dt
    from dataclasses import replace
    from koushi import application, settings as office_settings
    office_settings.save(office_settings.OfficeSettings(current_term="1141"), webapp.cfg().state_dir)
    _login_office(client)
    webapp.configure_app(webapp.app, replace(webapp.cfg(), production=True))
    try:
        # 沒有任何執行紀錄
        assert "尚無每日自動化執行紀錄" in client.get("/office/overview").get_data(as_text=True)
        # 兩天前跑過一次（即使成功），視為排程可能沒跑
        two_days_ago = (dt.datetime.now() - dt.timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        monkeypatch.setattr(application.clock, "now", lambda: dt.datetime.now())
        (webapp.cfg().state_dir / "automation_status.json").write_text(
            f'{{"at": "{two_days_ago}", "ok": true, "phase": "", "error": "", "notes": []}}', encoding="utf-8")
        page = client.get("/office/overview").get_data(as_text=True)
        assert "已超過" in page and "排程可能沒有執行" in page
    finally:
        webapp.configure_app(webapp.app, replace(webapp.cfg(), production=False))


def test_demo_overview_does_not_warn_about_stale_automation(client):
    """示範模式每日流程是手動觸發，不應出現排程過時告警（production=False）。"""
    import datetime as dt
    from koushi import settings as office_settings
    office_settings.save(office_settings.OfficeSettings(current_term="1141"), webapp.cfg().state_dir)
    _login_office(client)
    old = (dt.datetime.now() - dt.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
    (webapp.cfg().state_dir / "automation_status.json").write_text(
        f'{{"at": "{old}", "ok": true, "phase": "", "error": "", "notes": []}}', encoding="utf-8")
    assert "排程可能沒有執行" not in client.get("/office/overview").get_data(as_text=True)
