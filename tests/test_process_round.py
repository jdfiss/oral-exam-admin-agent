from tests.web_fixture import post_submission
"""學生重填與通知輪次的公開介面驗收；不使用真實資料或 SMTP。"""

import json

import pytest

from koushi.engines.state import load_from_dir
from koushi import cli, config as C, issue_store, send_log
from koushi.application import process_round, tracking_snapshot
from koushi.web import app as webapp
from koushi.workflow import RegistryPaths

from tests.att2_fixture import case as att2_case, write_att2


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


@pytest.fixture
def case(tmp_path, monkeypatch):
    submissions = tmp_path / "submissions"
    registries = RegistryPaths(
        attachment2=write_att2(tmp_path / "office" / "att2.xlsx", [att2_case()]),
        members=tmp_path / "missing_members.xlsx",
        distances=tmp_path / "missing_distances.csv",
        members_output=tmp_path / "output" / "data" / "members.xlsx",
    )
    test_cfg = webapp.WebConfig(
        submissions_dir=submissions,
        master_path=tmp_path / "output" / "master.xlsx",
        state_dir=tmp_path / "state",
        registries=registries,
        doc_output_root=tmp_path / "doc_output",
        office_password="test-password",
    )
    monkeypatch.setitem(webapp.app.config, "_web", test_cfg)
    monkeypatch.setitem(webapp.app.config, "TESTING", True)
    _open_filling_period(test_cfg.state_dir)
    with webapp.app.test_client() as client:
        client.post("/login/student", data={"student_id": "111900001", "csrf_token": _csrf(client)})
        yield client, submissions, registries, tmp_path


def _submit(client, *, exam_date="2026-10-01", student_email="student@example.org", matched=False,
            **overrides):
    """matched=False：委員名單與附件2 對不上 → 附件2 的委員沒有 E-mail → 學生責任缺件。

    送出時的欄位檢查（review-2026-09-23 P1-15）已擋掉空白口試日期與指導教授，
    送出後仍可能發生的學生缺件就是名單對不上。
    """
    data = {
        "term": "1141",
        "student_name": "王小明",
        "student_email": student_email,
        "college": "工學院",
        "department": "資訊工程學系碩士班",
        "exam_date": exam_date,
        "advisor_0": "林指導", "advisor_0_email": "lin@example.org",
        "committee_0_name": "陳大文" if matched else "附件2沒有的委員",
        "committee_0_unit": "國立範例大學資訊工程學系",
        "committee_0_email": "dawen@example.org",
        "committee_1_name": "校內委員乙", "committee_1_unit": "國立範例大學資訊工程學系",
        "committee_1_email": "b@example.org",
        "committee_2_name": "校內委員丙", "committee_2_unit": "國立範例大學資訊工程學系",
        "committee_2_email": "c@example.org",
        "csrf_token": _csrf(client),
    }
    data.update(overrides)
    return post_submission(client, data=data, follow_redirects=True)


def _open_filling_period(state_dir):
    """系辦設好本期學期與填寫截止日，學生端才開放（2026-10-01 Q1）。"""
    import datetime as _dt
    from koushi import settings as office_settings
    office_settings.save(
        office_settings.OfficeSettings(current_term="1141",
                                       fill_deadline=_dt.date.today() + _dt.timedelta(days=30)),
        state_dir,
    )


def _start_deficiency_period(state_dir):
    """填寫截止日與附件2 上傳都在昨天：今天是正式檢核開始日，系統才會寄補件信。"""
    import datetime as _dt
    from koushi import settings as office_settings
    yesterday = _dt.date.today() - _dt.timedelta(days=1)
    office_settings.save(
        office_settings.OfficeSettings(fill_deadline=yesterday, att2_first_uploaded=yesterday,
                                       current_term="1141", send_hour=0),
        state_dir,
    )


def _process(submissions, registries, root, **kwargs):
    if kwargs.get("send"):
        _start_deficiency_period(root / "state")
    return process_round(
        submission_paths=list(submissions.glob("*.json")),
        master_path=root / "output" / "master.xlsx",
        state_dir=root / "state",
        registries=registries,
        **kwargs,
    )


def test_submission_and_resubmission_are_visible_to_master_without_review(case):
    client, submissions, registries, root = case
    _submit(client, exam_date="2026-09-30", matched=True)
    record = json.loads((submissions / "1141-111900001.json").read_text(encoding="utf-8"))
    assert "status" not in record  # 送出即有效

    first = _process(submissions, registries, root)
    assert first.build.master.exams[0].審核狀態 == "電子表單簽核完成"  # 來自附件2
    assert first.build.master.exams[0].口試日期.isoformat() == "2026-09-30"
    assert first.sent == []
    assert not first.log_path.exists()

    _submit(client, exam_date="2026-10-01", matched=True)
    latest = json.loads((submissions / "1141-111900001.json").read_text(encoding="utf-8"))
    assert latest["version"] == 2
    second = _process(submissions, registries, root)
    assert second.build.master.exams[0].口試日期.isoformat() == "2026-10-01"
    assert second.drafts == []


def test_sends_each_issue_once_and_only_new_issues_after_resubmission(case):
    client, submissions, registries, root = case
    _submit(client)
    deliveries = []

    def fake_transport(*, to, subject, body, cc):
        deliveries.append((to, body))
        return True

    first = _process(submissions, registries, root, send=True, transport=fake_transport)
    assert len(first.sent) == 1
    assert first.sent[0]["to"] == "student@example.org"
    assert "修改後按「送出」即完成補件" in deliveries[0][1]
    assert send_log.load(first.log_path).by_status(send_log.SENT)

    repeated = _process(submissions, registries, root, send=True, transport=fake_transport)
    assert repeated.sent == []
    assert len(deliveries) == 1

    # 重送新版本但問題沒變：同一個問題不重複寄（2026-10-01 Q7），之後交給截止前催件
    _submit(client, student_email="updated@example.org")
    new_version = _process(submissions, registries, root, send=True, transport=fake_transport)
    assert new_version.sent == []
    assert len(deliveries) == 1

    # 冒出新問題（附件2 多了一位學生表單沒有的委員）：當天先不寄（決策三-3「隔天每日檢核
    # 時問題仍在才寄」），隔天只寄新問題，寄到最新信箱
    import datetime as _dt
    from tests.att2_fixture import DEFAULT_COMMITTEE
    write_att2(registries.attachment2, [att2_case(committees=[
        DEFAULT_COMMITTEE, {"name": "李新委員", "unit": "國立範例大學資訊工程學系", "title": "教授"}])])
    same_day = _process(submissions, registries, root, send=True, transport=fake_transport)
    assert same_day.sent == []
    assert len(deliveries) == 1
    newer = _process(submissions, registries, root, send=True, transport=fake_transport,
                     date=_dt.date.today() + _dt.timedelta(days=1))
    assert len(newer.sent) == 1
    assert deliveries[1][0] == "updated@example.org"
    assert "李新委員" in deliveries[1][1]
    assert "陳大文" not in deliveries[1][1]
    assert len(send_log.load(first.log_path).by_status(send_log.SENT)) == 2


def test_resubmission_that_fixes_issue_sends_nothing_new(case):
    """學生重送即生效：修正後的版本下一輪重檢沒有問題，就不再寄補件信。"""
    client, submissions, registries, root = case
    _submit(client)  # 委員名單對不上 → 學生責任缺件
    baseline = _process(submissions, registries, root, send=True, transport=lambda **kw: True)
    assert len(baseline.sent) == 1

    _submit(client, matched=True)  # 修正委員名單
    again = _process(
        submissions, registries, root, send=True,
        transport=lambda **kw: pytest.fail("已修正的版本不應再收到補件信"),
    )
    assert again.drafts == []
    assert again.sent == []


def test_failed_delivery_is_not_counted_or_lost(case):
    client, submissions, registries, root = case
    _submit(client)
    result = _process(submissions, registries, root, send=True,
                      transport=lambda **kwargs: False)
    assert result.sent == []
    assert len(send_log.load(result.log_path).by_status(send_log.FAILED)) == 1
    retry = _process(submissions, registries, root, send=True,
                     transport=lambda **kwargs: True)
    assert len(retry.sent) == 1
    assert len(send_log.load(result.log_path).by_status(send_log.SENT)) == 1


def test_missing_recipient_never_calls_transport(case):
    client, submissions, registries, root = case
    _submit(client, student_email="")
    result = _process(submissions, registries, root, send=True,
                      transport=lambda **kwargs: pytest.fail("不應寄到未知地址"))
    assert len(result.drafts) == 1
    assert result.drafts[0]["status"] == "no_recipient"
    assert result.sent == []
    assert send_log.load(result.log_path).all() == []


def test_issue_record_advances_through_draft_ready_to_request_sent(case):
    """process_round 自己先對帳建立記錄，產生草稿即推進 DRAFT_READY；實際寄出成功後推進
    REQUEST_SENT。只預覽時不可把缺件標成已通知（首次通知前不需先跑 `koushi status`）。
    """
    client, submissions, registries, root = case
    # 指導教授留白 → ADVISOR_SOURCE_UNAVAILABLE（阻擋、學生責任）；
    # NO_EXAM_DATE 不阻擋產出，不會進 deficiency_scan()，不能拿來測這條鏈路。
    _submit(client)

    _process(submissions, registries, root)

    records = issue_store.load(root / "state" / "issue_records.json")
    seeded = [r for r in records.all() if r.owner == "student"]
    assert seeded and seeded[0].status == issue_store.DRAFT_READY
    state = load_from_dir(root / "state")
    assert all(i.notified_date is None for i in state.deficiency if i.owner == "student")

    sent_round = _process(submissions, registries, root, send=True, transport=lambda **kw: True)
    assert len(sent_round.sent) == 1

    records = issue_store.load(root / "state" / "issue_records.json")
    advanced = [r for r in records.all() if r.owner == "student"]
    assert advanced and advanced[0].status == issue_store.REQUEST_SENT
    state = load_from_dir(root / "state")
    assert all(i.notified_date for i in state.deficiency if i.owner == "student")


def test_issue_record_resolves_once_deficiency_fixed(case):
    client, submissions, registries, root = case
    _submit(client)
    preview = _process(submissions, registries, root)
    tracking_snapshot(master=preview.build.master, state_dir=root / "state", persist=True)

    _submit(client, matched=True)  # 修正委員名單
    fixed = _process(submissions, registries, root)
    tracking_snapshot(master=fixed.build.master, state_dir=root / "state", persist=True)

    records = issue_store.load(root / "state" / "issue_records.json")
    matching = [r for r in records.all() if r.owner == "student"]
    assert matching and matching[0].status == issue_store.RESOLVED


def test_cli_process_previews_without_sending(case, monkeypatch, capsys):
    client, submissions, registries, root = case
    _submit(client)
    monkeypatch.setattr(C, "PORTAL_ATT2", registries.attachment2)
    monkeypatch.setattr(C, "REG_MEMBER", registries.members)
    monkeypatch.setattr(C, "DISTANCE_CSV", registries.distances)
    master_path = root / "output" / "cli-master.xlsx"
    assert cli.main([
        "process", "--submissions-dir", str(submissions),
        "--master", str(master_path), "--state-dir", str(root / "state"),
    ]) == 0
    output = capsys.readouterr().out
    assert "預覽模式" in output
    assert "student@example.org" in output
    assert master_path.is_file()
    assert not (root / "state" / "send_log.json").exists()


def test_cli_automate_runs_build_status_remind_in_sequence(case, monkeypatch, capsys):
    """`automate` 依序跑完 process（建檔＋首次通知）→ status（三引擎對帳、
    落 issue_records.json）→ remind；--send 才會真的寄信／回寫節流狀態。
    """
    client, submissions, registries, root = case
    # 指導教授留白 → ADVISOR_SOURCE_UNAVAILABLE（阻擋、學生責任），process 這步會產生一封草稿。
    _submit(client)
    monkeypatch.setattr(C, "PORTAL_ATT2", registries.attachment2)
    monkeypatch.setattr(C, "REG_MEMBER", registries.members)
    monkeypatch.setattr(C, "DISTANCE_CSV", registries.distances)
    _start_deficiency_period(root / "state")
    master_path = root / "output" / "cli-master.xlsx"

    assert cli.main([
        "automate", "--submissions-dir", str(submissions), "--master", str(master_path),
        "--state-dir", str(root / "state"),
    ]) == 0
    output = capsys.readouterr().out
    assert "已建立主檔" in output
    assert "缺件通知：1 封草稿" in output
    # 學生責任缺件只預覽、沒寄出 → 仍是新增；委員本人責任不寄信 → 直接起算
    assert "缺件追蹤：新增 1／已通知 1" in output
    assert "目前時段：補件期間" in output
    assert "預覽模式" in output

    records = issue_store.load(root / "state" / "issue_records.json")
    seeded = [r for r in records.all() if r.owner == "student"]
    assert seeded and seeded[0].status == issue_store.DRAFT_READY  # 草稿已產生、尚未寄出


def test_cli_automate_send_dispatches_first_notification(case, monkeypatch, capsys):
    client, submissions, registries, root = case
    _submit(client)
    monkeypatch.setattr(C, "PORTAL_ATT2", registries.attachment2)
    monkeypatch.setattr(C, "REG_MEMBER", registries.members)
    monkeypatch.setattr(C, "DISTANCE_CSV", registries.distances)
    monkeypatch.setattr("koushi.cli._fetch_replies", lambda: [])
    monkeypatch.setattr("koushi.mail.email_sender.send_email",
                        lambda to, subject, body, cc=None, attachments=None: True)
    _start_deficiency_period(root / "state")
    master_path = root / "output" / "cli-master.xlsx"

    assert cli.main([
        "automate", "--send", "--submissions-dir", str(submissions), "--master", str(master_path),
        "--state-dir", str(root / "state"),
    ]) == 0
    output = capsys.readouterr().out
    assert "缺件通知：1 封草稿；已寄出 1 封" in output
    assert "預覽模式" not in output
    assert (root / "state" / "send_log.json").exists()


def test_cli_send_flag_rejects_unsupported_commands():
    with pytest.raises(ValueError, match="process／remind"):
        cli.main(["build", "--send"])


def test_cli_remind_send_runs_end_to_end_with_nothing_scheduled(case, monkeypatch, capsys):
    """沒有需要催辦的項目時，`remind --send` 要能跑完整條路徑不出錯
    （真正「有東西可寄」的節流／收件人解析已在 test_application_tracking.py
    ／test_mail.py／test_scheduler.py 分層驗證過，這裡只驗證 CLI 接線本身）。
    """
    client, submissions, registries, root = case
    _submit(client, matched=True)  # 資料完整、無學生缺件
    monkeypatch.setattr(C, "PORTAL_ATT2", registries.attachment2)
    monkeypatch.setattr(C, "REG_MEMBER", registries.members)
    monkeypatch.setattr(C, "DISTANCE_CSV", registries.distances)
    master_path = root / "output" / "cli-master.xlsx"

    assert cli.main([
        "build", "--submissions-dir", str(submissions), "--master", str(master_path),
    ]) == 0
    assert cli.main([
        "remind", "--send", "--master", str(master_path), "--state-dir", str(root / "state"),
        "--submissions-dir", str(submissions),
    ]) == 0
    output = capsys.readouterr().out
    assert "目前沒有需要催辦或提醒的項目" in output


def test_cli_status_prints_issue_lifecycle_counts(case, monkeypatch, capsys):
    client, submissions, registries, root = case
    _submit(client)  # 委員名單對不上 → 觸發學生責任缺件
    monkeypatch.setattr(C, "PORTAL_ATT2", registries.attachment2)
    monkeypatch.setattr(C, "REG_MEMBER", registries.members)
    monkeypatch.setattr(C, "DISTANCE_CSV", registries.distances)
    master_path = root / "output" / "cli-master.xlsx"

    assert cli.main([
        "build", "--submissions-dir", str(submissions), "--master", str(master_path),
    ]) == 0
    assert cli.main([
        "status", "--master", str(master_path), "--state-dir", str(root / "state"),
    ]) == 0
    output = capsys.readouterr().out
    assert "缺件生命週期記錄" in output
    assert "偵測：2" in output  # 缺指導教授（學生）＋ 委員暫定身分（待委員本人提供）


def test_cli_build_uses_submissions_without_implicit_attachment2(case, monkeypatch):
    client, submissions, registries, root = case
    _submit(client)
    old_attachment = root / "invalid-historical-att2.xlsx"
    old_attachment.write_text("這不是 Excel", encoding="utf-8")
    monkeypatch.setattr(C, "SRC_ATT2", old_attachment)
    monkeypatch.setattr(C, "PORTAL_ATT2", registries.attachment2)
    monkeypatch.setattr(C, "REG_MEMBER", registries.members)
    monkeypatch.setattr(C, "DISTANCE_CSV", registries.distances)
    master_path = root / "output" / "submission-master.xlsx"

    assert cli.main([
        "build", "--submissions-dir", str(submissions), "--master", str(master_path),
    ]) == 0
    assert master_path.is_file()
