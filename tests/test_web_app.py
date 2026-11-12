from tests.web_fixture import post_submission
# -*- coding: utf-8 -*-
"""學生補件介面（方案C：原地覆蓋 + 快照歸檔）的端到端驗證。

沒有 Portal、沒有附件2：這個介面是學生唯一入口，案件由學生自己在介面
上新增（選學期），不是預先存在主檔裡等學生來填。案件鍵的學號永遠取
session、不信任表單／路徑參數；同一案件重複送出要覆寫原檔並把舊版
搬進 versions/ 留底，不能累積成多筆同案記錄。

全程使用假資料，不碰任何真實學生資料。
"""
import datetime as dt
import json

import pytest

from koushi import settings as office_settings
from koushi.web import app as webapp
from koushi.workflow import RegistryPaths

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
    _open_term()
    with webapp.app.test_client() as c:
        yield c


def _open_term(term="1141", **overrides):
    """系辦設好本期學期與填寫截止日，學生端才開放（2026-10-01 Q1）；預設在填寫期間。"""
    values = {"current_term": term, "fill_deadline": dt.date.today() + dt.timedelta(days=30)}
    values.update(overrides)
    settings = office_settings.OfficeSettings(**values)
    office_settings.save(settings, webapp.cfg().state_dir)
    return settings


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


def _login(client, sid="111900001"):
    client.post("/login/student", data={"student_id": sid, "csrf_token": _csrf(client)},
                follow_redirects=True)


def _submit(client, term="1141", **overrides):
    data = {
        "term": term,
        "student_email": "s111900001@cc.example.edu",
        "exam_date": "2026-06-01",
        "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        "committee_0_name": "陳大文",
        "committee_0_unit": "資工系",
        "committee_0_email": "dawen@example.org",
        "committee_1_name": "校內委員乙", "committee_1_unit": "國立範例大學資訊工程學系",
        "committee_1_email": "b@example.org",
        "committee_2_name": "校內委員丙", "committee_2_unit": "國立範例大學資訊工程學系",
        "committee_2_email": "c@example.org",
        "csrf_token": _csrf(client),
    }
    data.update(overrides)
    return post_submission(client, data=data, follow_redirects=True)


def test_post_without_csrf_token_is_rejected(client):
    resp = client.post("/login/student", data={"student_id": "111900001"})
    assert resp.status_code == 400
    # 沒通過 CSRF 檢查，session 不會建立
    dashboard = client.get("/dashboard", follow_redirects=True)
    assert "請先登入" in dashboard.get_data(as_text=True)


def test_post_with_wrong_csrf_token_is_rejected(client):
    _csrf(client)  # 先建立一個合法 token 在 session 裡
    resp = client.post("/login/student", data={"student_id": "111900001", "csrf_token": "wrong"})
    assert resp.status_code == 400


def test_login_rejects_malformed_student_id(client):
    resp = client.post("/login/student", data={"student_id": "../etc", "csrf_token": _csrf(client)},
                       follow_redirects=True)
    assert "請輸入正確格式的學號" in resp.get_data(as_text=True)
    # 登入被拒後不應建立 session，儀表板應被導回登入頁
    dashboard = client.get("/dashboard", follow_redirects=True)
    assert "請先登入" in dashboard.get_data(as_text=True)


def test_dashboard_starts_empty_with_new_case_form(client):
    _login(client)
    text = client.get("/dashboard").get_data(as_text=True)
    assert "尚無申請紀錄" in text
    assert "本期受理學期" in text and 'value="1141"' in text


@pytest.mark.parametrize("settings", [
    {"current_term": "", "fill_deadline": None},
    {"current_term": "1141", "fill_deadline": None},
    {"current_term": "", "fill_deadline": dt.date(2026, 12, 1)},
])
def test_student_side_closed_until_term_and_fill_deadline_set(client, settings):
    """2026-10-01 Q1：本期學期與填寫截止日都設好之前，學生端不開放、不能自填學期。"""
    office_settings.save(office_settings.OfficeSettings(**settings), webapp.cfg().state_dir)
    _login(client)
    text = client.get("/dashboard").get_data(as_text=True)
    assert "本學期尚未開放填寫" in text
    assert 'name="term"' not in text
    assert "本學期尚未開放填寫" in client.get("/form?term=1141", follow_redirects=True).get_data(as_text=True)
    resp = _submit(client, term="1141")
    assert "本學期尚未開放填寫" in resp.get_data(as_text=True)
    assert not list(webapp.cfg().submissions_dir.glob("*.json"))


def test_form_rejects_malformed_term(client):
    _login(client)
    resp = client.get("/form?term=../outside", follow_redirects=True)
    assert "本期受理的學期是 114學年度第1學期" in resp.get_data(as_text=True)


def test_submit_rejects_malformed_term(client):
    _login(client)
    resp = _submit(client, term="..\\outside")
    assert "本期受理的學期是 114學年度第1學期" in resp.get_data(as_text=True)
    assert not list(webapp.cfg().submissions_dir.glob("*.json"))


def test_submit_creates_case_owned_by_session_student(client):
    _login(client, sid="111900001")
    _submit(client, term="1141")
    path = webapp.cfg().submissions_dir / "1141-111900001.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["student_id"] == "111900001"
    assert data["term"] == "1141"
    assert data["version"] == 1
    assert data["advisors"] == ["林指導"]
    assert "status" not in data  # 送出即有效，不經系辦審核


def test_submit_ignores_client_supplied_student_id(client):
    """就算表單夾帶 student_id，案件鍵與存檔內容還是以 session 的學號為準。"""
    _login(client, sid="111900001")
    _submit(client, term="1141", student_id="999999999")
    assert (webapp.cfg().submissions_dir / "1141-111900001.json").is_file()
    assert not (webapp.cfg().submissions_dir / "1141-999999999.json").exists()


def test_resubmit_overwrites_in_place_and_archives_previous_version(client):
    _login(client)

    _submit(client, term="1141", exam_date="2026-06-01")
    path = webapp.cfg().submissions_dir / "1141-111900001.json"
    first = json.loads(path.read_text(encoding="utf-8"))
    assert first["version"] == 1

    _submit(client, term="1141", exam_date="2026-06-02")
    second = json.loads(path.read_text(encoding="utf-8"))
    assert second["version"] == 2
    assert second["exam_date"] == "2026-06-02"

    # 只有一份 current 記錄，沒有累積出第二個案件檔案
    current_files = list(webapp.cfg().submissions_dir.glob("*.json"))
    assert [f.name for f in current_files] == ["1141-111900001.json"]

    versions = list((webapp.cfg().submissions_dir / "versions").glob("*.json"))
    assert len(versions) == 1
    archived = json.loads(versions[0].read_text(encoding="utf-8"))
    assert archived["version"] == 1
    assert archived["exam_date"] == "2026-06-01"


def test_submit_does_not_store_portal_fields_or_committee_banking(client):
    """表單不收 Portal 已有欄位（姓名、學院、系所），委員撥帳個資不經學生。"""
    _login(client)
    _submit(client, term="1141", student_name="王小明", college="工學院",
            department="資訊工程學系碩士班", committee_0_id_number="A123456789",
            committee_0_account="123")
    data = json.loads((webapp.cfg().submissions_dir / "1141-111900001.json").read_text(encoding="utf-8"))
    assert not {"student_name", "college", "department"} & set(data)
    assert data["committees"][0] == {"name": "陳大文", "unit": "資工系", "title": "",
                                     "email": "dawen@example.org", "remote": False}


def test_form_has_no_banking_or_portal_fields(client):
    _login(client)
    text = client.get("/form?term=1141").get_data(as_text=True)
    for field in ("student_name", "college", "department",
                  "_id_number", "_bank", "_account", "_address"):
        assert field not in text
    assert 'name="advisor_0"' in text


def test_view_rejects_malformed_case_key(client):
    _login(client)
    resp = client.get("/view/..%5Coutside", follow_redirects=True)
    assert "找不到該筆申請" in resp.get_data(as_text=True)


def test_student_cannot_view_other_students_case(client):
    _login(client, sid="111900001")
    _submit(client, term="1141")

    client.get("/logout")
    _login(client, sid="999999999")
    resp = client.get("/view/1141-111900001", follow_redirects=True)
    assert "無法檢視他人的申請" in resp.get_data(as_text=True)


def test_office_sees_submission_without_review_step(client):
    _login(client)
    _submit(client, term="1141")

    client.get("/logout")
    client.post("/login/office", data={"password": webapp.cfg().office_password, "csrf_token": _csrf(client)},
               follow_redirects=True)

    office_text = client.get("/dashboard", follow_redirects=True).get_data(as_text=True)
    assert "1141" in office_text  # 學期欄位有顯示
    assert "待審核" not in office_text

    view_text = client.get("/view/1141-111900001").get_data(as_text=True)
    assert "第 1 版" in view_text
    assert 'action="/review' not in view_text

    resp = client.post("/review/1141-111900001",
                       data={"action": "approve", "version": "1", "csrf_token": _csrf(client)})
    assert resp.status_code == 404  # 審核路由已移除


def test_submit_blocked_once_deficiency_deadline_passed(client):
    """補件截止日為硬截止：截止後學生不得再自行送出。"""
    _login(client)
    _submit(client, term="1141", exam_date="2026-06-01")

    long_ago = dt.date.today() - dt.timedelta(days=60)
    _open_term(fill_deadline=long_ago, att2_first_uploaded=long_ago, deficiency_days=5)

    resp = _submit(client, term="1141", exam_date="2026-07-01")
    assert "補件截止日已過" in resp.get_data(as_text=True)
    data = json.loads((webapp.cfg().submissions_dir / "1141-111900001.json").read_text(encoding="utf-8"))
    assert data["exam_date"] == "2026-06-01"  # 沒有被逾期後的重送覆蓋
    assert data["version"] == 1


def test_student_with_two_terms_gets_two_independent_cases(client):
    """學號相同但學期不同（延畢／重考）要各自獨立，不能互相覆蓋。"""
    _open_term("1131")
    _login(client)
    _submit(client, term="1131", exam_date="2025-06-01")
    _open_term("1141")
    _submit(client, term="1141", exam_date="2026-06-01")

    a = json.loads((webapp.cfg().submissions_dir / "1131-111900001.json").read_text(encoding="utf-8"))
    b = json.loads((webapp.cfg().submissions_dir / "1141-111900001.json").read_text(encoding="utf-8"))
    assert a["exam_date"] == "2025-06-01"
    assert b["exam_date"] == "2026-06-01"
    assert not list((webapp.cfg().submissions_dir / "versions").glob("*.json"))


def _login_office(client):
    client.post("/login/office", data={"password": webapp.cfg().office_password, "csrf_token": _csrf(client)})


def test_office_settings_page_saves_and_shows_computed_deadline(client):
    (webapp.cfg().state_dir / "settings.json").unlink()
    _login_office(client)
    text = client.get("/office/settings").get_data(as_text=True)
    assert "期限與催件設定" in text and "（未設定）" in text

    resp = client.post("/office/settings", data={
        "fill_deadline": "2026-10-01", "deficiency_days": "5", "business_days": "1",
        "current_term": "1141", "csrf_token": _csrf(client),
    }, follow_redirects=True)
    text = resp.get_data(as_text=True)
    assert "已儲存期限與催件設定" in text
    assert "（待系所審核名單）" in text  # 附件2 還沒上傳，補件截止日還算不出來

    office_settings.record_att2_upload(webapp.cfg().state_dir, day=dt.date(2026, 10, 1))
    text = client.get("/office/settings").get_data(as_text=True)
    assert "2026-10-02" in text  # 正式檢核開始日＝填寫截止日隔天（週五）
    assert "2026-10-09" in text  # 補件截止日＝正式檢核開始日＋5 個工作日


def test_office_settings_manual_deadline_override(client):
    """2026-10-01 Q6：補件截止日可手動覆寫，留空回到公式。"""
    _open_term(fill_deadline=dt.date(2026, 10, 1), att2_first_uploaded=dt.date(2026, 10, 1))
    _login_office(client)
    client.post("/office/settings", data={
        "fill_deadline": "2026-10-01", "current_term": "1141",
        "deficiency_deadline_override": "2026-10-20", "csrf_token": _csrf(client),
    })
    assert office_settings.load(webapp.cfg().state_dir).deficiency_deadline == dt.date(2026, 10, 20)
    client.post("/office/settings", data={
        "fill_deadline": "2026-10-01", "current_term": "1141", "csrf_token": _csrf(client),
    })
    assert office_settings.load(webapp.cfg().state_dir).deficiency_deadline == dt.date(2026, 10, 9)


def test_switching_term_requires_confirmation_and_lists_open_cases(client):
    """2026-10-01 Q16：切換本期學期前先列出舊學期未結案件，確認後才切換。"""
    _open_term(att2_first_uploaded=dt.date.today())
    _login_office(client)
    form = {"fill_deadline": "2027-03-01", "current_term": "1142", "csrf_token": _csrf(client)}
    text = client.post("/office/settings", data=form).get_data(as_text=True)
    assert "確認切換本期學期" in text and "confirm_term_switch" in text
    assert office_settings.load(webapp.cfg().state_dir).current_term == "1141"  # 尚未切換

    resp = client.post("/office/settings", data={**form, "confirm_term_switch": "1"},
                       follow_redirects=True)
    assert "已切換本期學期" in resp.get_data(as_text=True)
    switched = office_settings.load(webapp.cfg().state_dir)
    assert switched.current_term == "1142"
    assert switched.att2_first_uploaded is None  # 新學期要重新上傳附件2


def test_office_settings_page_rejects_bad_input_without_saving(client):
    _login_office(client)
    before = (webapp.cfg().state_dir / "settings.json").read_text(encoding="utf-8")
    resp = client.post("/office/settings", data={
        "fill_deadline": "2026-10-01", "deficiency_days": "-3", "csrf_token": _csrf(client),
    }, follow_redirects=True)
    assert "0 以上" in resp.get_data(as_text=True)
    assert (webapp.cfg().state_dir / "settings.json").read_text(encoding="utf-8") == before


def test_settings_page_requires_office_login(client):
    _login(client)
    resp = client.get("/office/settings", follow_redirects=True)
    assert "期限與催件設定" not in resp.get_data(as_text=True)


def test_submit_blocked_after_fill_deadline_without_deficiency(client):
    _open_term(fill_deadline=dt.date.today() - dt.timedelta(days=1))
    _login(client)
    resp = _submit(client, term="1141")
    assert "填寫截止日已過" in resp.get_data(as_text=True)
    assert not (webapp.cfg().submissions_dir / "1141-111900001.json").exists()


def test_current_term_is_fixed_for_students(client):
    """review-2026-09-23 P1-9：系辦設定本期學期後，學生不能用其他學期送出。"""
    _login(client)
    text = client.get("/dashboard").get_data(as_text=True)
    assert "本期受理學期" in text and 'value="1141"' in text
    resp = _submit(client, term="1131")
    assert "本期受理的學期是 114學年度第1學期" in resp.get_data(as_text=True)
    assert not (webapp.cfg().submissions_dir / "1131-111900001.json").exists()
    _submit(client, term="1141")
    assert (webapp.cfg().submissions_dir / "1141-111900001.json").exists()


def test_student_dashboard_shows_pending_items_and_deadline(client):
    """review-2026-09-23 P2-24：學生看得到自己的待補項目與補件截止日（不顯示問題代碼）。"""
    from koushi.engines.deficiency import DeficiencyItem
    from koushi.engines.state import EngineState, save_to_dir

    yesterday = dt.date.today() - dt.timedelta(days=1)
    settings = _open_term(fill_deadline=yesterday, att2_first_uploaded=yesterday, deficiency_days=5)
    item = DeficiencyItem(
        case_key="1141-111900001", term="1141", student_id="111900001", student_name="王小明",
        issue_code="MISSING_COMMITTEE_LIST", field="委員名單", description="尚未填寫口試委員名單",
        owner="student", blocks_att4=False, blocks_att6=True, notified_date=dt.date.today(),
    )
    office_item = DeficiencyItem(
        case_key="1141-111900001", term="1141", student_id="111900001", student_name="王小明",
        issue_code="COMMITTEE_RANK_EXCEPTION", field="委員：某人", description="系辦內部例外",
        owner="office", blocks_att4=False, blocks_att6=False,
    )
    save_to_dir(EngineState(deficiency=[item, office_item], confirmation=[], collection=[]),
                webapp.cfg().state_dir)

    _login(client)  # 學生還沒在本系統填寫過，本期案件仍要顯示待補項目
    text = client.get("/dashboard").get_data(as_text=True)
    assert "補件期間" in text
    assert "委員名單：尚未填寫口試委員名單" in text
    assert str(settings.deficiency_deadline) in text
    assert "MISSING_COMMITTEE_LIST" not in text
    assert "系辦內部例外" not in text


def test_student_submit_writes_audit_without_content(client):
    _login(client)
    _submit(client, term="1141")
    audit = (webapp.cfg().state_dir / "audit_log.json").read_text(encoding="utf-8")
    assert '"submission"' in audit and "1141-111900001" in audit
    assert "dawen@example.org" not in audit


def test_build_lock_timeout_keeps_submission_and_reports_busy(client, monkeypatch):
    """review-2026-09-23 P2-19：建檔鎖逾時不讓送出失敗，資料已存檔。"""
    import contextlib
    from koushi import application, util as U

    @contextlib.contextmanager
    def busy_lock(*args, **kwargs):
        raise U.BuildLockTimeout("busy")
        yield

    monkeypatch.setattr(application, "office_files_status",
                        lambda registries=None: type("S", (), {"ready": True})())
    monkeypatch.setattr(U, "build_lock", busy_lock)
    _login(client)
    resp = _submit(client, term="1141")
    assert resp.status_code == 200
    assert (webapp.cfg().submissions_dir / "1141-111900001.json").exists()
    assert not (webapp.cfg().master_path.parent / "auto_build_status.json").exists()
