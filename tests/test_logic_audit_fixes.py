# -*- coding: utf-8 -*-
"""2026-10-09 流程規則稽核發現的邏輯問題回歸測試（對照 docs/已確認流程決策.md）。

情境改寫自稽核腳本：附件2、學生送出、每日自動化、放行都走真正的模組（tests/scenario_env.py）。
"""

import datetime as dt
import json
from pathlib import Path

import openpyxl
import pytest

from koushi import application as A, audit_log, case_review, committee_requests as CR, config as C
from koushi import send_log, validation, workflow
from koushi.registry import DistanceTable
from koushi.workflow import TemplatePaths
from tests.scenario_env import C_EXT, C_EXT2, C_HOME_KNOWN, C_HOME_NEW, DEFAULT_COMMITTEES, Env, T

D = dt.date(2026, 11, 4)  # 填寫截止日
K1, K2 = f"{T}-111900001", f"{T}-111900002"
EXAM = D + dt.timedelta(days=40)


def _env(tmp_path, monkeypatch, cases=None, **settings):
    e = Env(tmp_path, monkeypatch, D - dt.timedelta(days=5))
    e.settings(**{"current_term": T, "fill_deadline": D, "deficiency_days": 7, "business_days": False,
                  "send_hour": 9, **settings})
    e.upload_att2(cases or [dict(student_id="111900001", name="甲")], day=D - dt.timedelta(days=1))
    return e


def _released(tmp_path, monkeypatch, cases=None, keys=(K1,)):
    """所有案件都送出、正式檢核開始、指定案件放行。"""
    e = _env(tmp_path, monkeypatch, cases)
    for c in cases or [dict(student_id="111900001")]:
        e.submit(c["student_id"], email=f"{c['student_id'][-1]}@example.org", exam_date=EXAM)
    e.automate(D + dt.timedelta(days=1))
    for key in keys:
        e.clear(key)
    return e


# ── 1. 名單沒變、只多了其他例外：維持放行，只標「放行後有新例外」（決策四-5）──

def test_new_source_exception_keeps_release_and_is_flagged(tmp_path, monkeypatch):
    e = _released(tmp_path, monkeypatch)
    # 附件2 重傳多了一列重複委員：名單（姓名＋單位＋職稱）沒變，多一個來源例外
    e.upload_att2([dict(student_id="111900001", name="甲",
                        committees=[C_HOME_KNOWN, C_EXT, C_HOME_NEW, C_EXT])])
    e.build()
    assert "DUPLICATE_ATTACHMENT_2_COMMITTEE" in {i["問題代碼"] for i in e.m().source_issues}
    assert K1 in e.cleared()
    row = next(r for r in A.review_queue(e.m(), e.state) if r.case_key == K1)
    assert not row.pending and not row.changed_since_clear
    assert [i["問題代碼"] for i in row.new_exceptions] == ["DUPLICATE_ATTACHMENT_2_COMMITTEE"]
    assert not [i for i in A._unreviewed_case_issues(e.m(), e.state) if i["案件鍵"] == K1]


def test_committee_change_still_revokes_release(tmp_path, monkeypatch):
    e = _released(tmp_path, monkeypatch)
    e.upload_att2([dict(student_id="111900001", name="甲", committees=[C_HOME_KNOWN, C_EXT2, C_HOME_NEW])])
    e.build()
    assert K1 not in e.cleared()
    pending = [i for i in A._unreviewed_case_issues(e.m(), e.state) if i["案件鍵"] == K1]
    assert pending and "委員名單有變動" in pending[0]["說明"]


# ── 2. 案件仍有學生責任問題時放行照樣生效（決策四-5）──

def test_release_takes_effect_even_with_student_responsibility_issue(tmp_path, monkeypatch):
    e = _env(tmp_path, monkeypatch, [dict(student_id="111900001", name="甲"),
                                     dict(student_id="111900002", name="乙")])
    e.submit("111900002", email="b@example.org")  # 甲從未送出：MISSING_SUBMISSION
    e.automate(D + dt.timedelta(days=1))
    e.clear(K1)
    e.clear(K2)
    assert {i["問題代碼"] for i in e.m().source_issues if i.get("案件鍵") == K1} >= {"MISSING_SUBMISSION"}
    assert e.cleared() == {K1, K2}
    assert not [i for i in A._unreviewed_case_issues(e.m(), e.state) if i["案件鍵"] == K1]
    # 放行不略過學生資料缺漏：從未送出的案件仍不會寄附件4（沒有指導教授）
    n = len(e.sent)
    e.automate(D + dt.timedelta(days=2))
    assert all(K1 not in m["body"] for m in e.sent[n:] if "附件4" in m["subject"])


# ── 7. 放行須填處理人（API／CLI 與網頁一致）──

@pytest.mark.parametrize("actor", ["", "   "])
def test_clear_requires_actor(tmp_path, monkeypatch, actor):
    e = _env(tmp_path, monkeypatch)
    e.submit("111900001", email="a@example.org", exam_date=EXAM)
    e.automate(D + dt.timedelta(days=1))
    with pytest.raises(ValueError, match="處理人"):
        e.clear(K1, actor=actor)
    assert case_review.load(e.state).get(K1) is None
    with pytest.raises(ValueError, match="處理人"):
        case_review.clear(e.state, exam=e.m().exams[0], master=e.m(), actor="")


# ── 3. 逾期未完成：專員以修正層補齊資料後放行（決策三-5）──

def _staff_corrections(include_committees=True):
    corr = {
        "口試日期": EXAM.isoformat(), "口試開始時間": "10:00", "口試結束時間": "12:00", "口試地點": "A204",
        "口試方式": "實體", "指導教授": "林指導",
        "advisor_emails": json.dumps({"林指導": "lin@example.org"}, ensure_ascii=False),
        "student_email": "a@example.org",
    }
    if include_committees:
        corr["committee_list"] = json.dumps(
            [{"name": n, "unit": u, "title": t, "email": em, "remote": False} for n, u, t, em in DEFAULT_COMMITTEES],
            ensure_ascii=False)
    return corr


def _overdue_env(tmp_path, monkeypatch):
    e = _env(tmp_path, monkeypatch, deficiency_days=3, student_email_template="{學號}@cc.example.edu")
    e.automate(D + dt.timedelta(days=1))
    e.automate(D + dt.timedelta(days=5))  # 補件截止已過
    codes = [i["問題代碼"] for r in A.review_queue(e.m(), e.state, today=D + dt.timedelta(days=5))
             if r.case_key == K1 for i in r.exceptions]
    assert "STUDENT_OVERDUE_INCOMPLETE" in codes
    return e


def _student_issues(e):
    return [i["問題代碼"] for i in validation.check(e.m()) if i["案件鍵"] == K1 and i["責任"] == "student"]


def test_staff_filled_overdue_case_can_be_released_and_sent(tmp_path, monkeypatch):
    e = _overdue_env(tmp_path, monkeypatch)
    for field, value in _staff_corrections().items():
        A.save_case_correction(master_path=e.master, state_dir=e.state, case_key=K1, field=field, value=value,
                               actor="專員甲", reason="逾期代補")
    e.build()
    assert _student_issues(e) == []
    assert "MISSING_SUBMISSION" not in {i["問題代碼"] for i in e.m().source_issues if i.get("案件鍵") == K1}
    assert A._document_eligibility_issues(e.m(), e.state) == []
    e.clear(K1)
    n = len(e.sent)
    e.automate(D + dt.timedelta(days=6))
    assert e.mails_to("lin@example.org", n)


def test_partial_staff_fill_keeps_missing_submission(tmp_path, monkeypatch):
    e = _overdue_env(tmp_path, monkeypatch)
    for field, value in _staff_corrections(include_committees=False).items():
        A.save_case_correction(master_path=e.master, state_dir=e.state, case_key=K1, field=field, value=value,
                               actor="專員甲", reason="逾期代補")
    e.build()  # 委員名單（含 E-mail）沒補：仍視為未填寫
    assert "MISSING_SUBMISSION" in _student_issues(e)


# ── 4. 口試前交付版附件6：委員未交個資不擋，口試現場補齊（決策五-4、七-1）──

@pytest.fixture
def capture_att6(monkeypatch):
    calls = []
    real = workflow.attachment6_generator.generate

    def fake_att4(master, out, *, template, term=None):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("x")

    def spy(master, out, *, template, term=None, prefill=False):
        result = real(master, out, template=C.TPL_ATT6, term=term, prefill=prefill)
        calls.append({"prefill": prefill, "cases": [x.案件鍵 for x in master.exams],
                      "files": [r["輸出"] for r in result]})
        return result

    monkeypatch.setattr(workflow.attachment4_generator, "generate", fake_att4)
    monkeypatch.setattr(workflow.attachment6_generator, "generate", spy)
    monkeypatch.setattr(workflow, "_validate_templates", lambda *a, **k: None)
    return calls


@pytest.mark.skipif(not C.TPL_ATT6.is_file(), reason="附件6 範本不在版控內（resource/template）")
def test_pre_exam_att6_includes_case_with_missing_committee_data(tmp_path, monkeypatch, capture_att6):
    e = _released(tmp_path, monkeypatch)
    e.automate(D + dt.timedelta(days=2))
    issues = validation.check(e.m())
    assert {"MISSING_BANK_INFO", "UNVERIFIED_COMMITTEE_IDENTITY"} <= {i["問題代碼"] for i in issues if i["案件鍵"] == K1}
    templates = TemplatePaths(attachment4=Path("x.docx"), attachment6=C.TPL_ATT6)
    result = A.generate_artifacts(master_path=e.master, output_dir=tmp_path / "out", templates=templates,
                                  phase="pre-exam", state_dir=e.state)
    pre = capture_att6[-1]
    assert pre["prefill"] and pre["cases"] == [K1] and pre["files"]
    manifest = json.loads(Path(result.manifest).read_text(encoding="utf-8"))
    assert manifest["included_attachment6"] == [K1]
    text = " ".join(str(c.value) for row in openpyxl.load_workbook(pre["files"][0]).active.iter_rows()
                    for c in row if c.value)
    assert "A123456789" not in text  # 交付版個資四欄留白


def test_prefill_block_only_ignores_pii_issues():
    issues = [
        {"案件鍵": "A", "問題代碼": "MISSING_BANK_INFO", "阻擋附件6": "是"},
        {"案件鍵": "B", "問題代碼": "UNVERIFIED_COMMITTEE_IDENTITY", "阻擋附件6": "是"},
        {"案件鍵": "C", "問題代碼": "NO_DISTANCE", "阻擋附件6": "是"},
    ]
    assert validation.attachment6_prefill_blocked_keys(issues) == {"C"}
    assert validation.attachment6_blocked_keys(issues) == {"A", "B", "C"}  # 口試後完整版照擋


# ── 5. 委員專屬連結：有效到最晚口試日，案件組變動自動寄「連結更新」（決策五-5）──

def test_link_valid_until_latest_exam_plus_grace(tmp_path, monkeypatch):
    monkeypatch.setenv(A.PUBLIC_URL_ENV, "https://koushi.example.edu.tw")
    e = _released(tmp_path, monkeypatch)
    e.automate(D + dt.timedelta(days=2))
    token = CR._load_links(e.state)["fang@example.org"]
    real = CR.time.time
    monkeypatch.setattr(CR.time, "time", lambda: real() + 8 * 86400)
    assert CR.email_for_token(e.state, token) == "fang@example.org"  # 不再固定 7 天失效
    after = dt.datetime.combine(EXAM + dt.timedelta(days=CR.LINK_GRACE_DAYS + 1), dt.time()).timestamp()
    monkeypatch.setattr(CR.time, "time", lambda: after)
    assert CR.email_for_token(e.state, token) is None


def test_binding_change_sends_link_update_once(tmp_path, monkeypatch):
    monkeypatch.setenv(A.PUBLIC_URL_ENV, "https://koushi.example.edu.tw")
    e = _released(tmp_path, monkeypatch, cases=[dict(student_id="111900001", name="甲"),
                                                  dict(student_id="111900002", name="乙")], keys=(K1,))
    e.automate(D + dt.timedelta(days=2))
    first = e.mails_to("fang@example.org")
    assert [m["subject"] for m in first] == ["[口試委員撥帳資料] 敬請提供口試費撥款資料"]
    old_token = CR._load_links(e.state)["fang@example.org"]

    e.clear(K2)  # 委員加入第二案：綁定改變、舊連結失效
    n = len(e.sent)
    e.automate(D + dt.timedelta(days=3))
    update = e.mails_to("fang@example.org", n)
    assert [m["subject"] for m in update] == ["[口試委員資料] 填寫連結更新通知"]
    assert "並非催促" in update[0]["body"] and K2 in update[0]["body"]
    req = next(r for r in CR.plan(e.m(), e.state) if r.key == "fang@example.org")
    new_token = CR._load_links(e.state)["fang@example.org"]
    assert new_token != old_token and new_token in update[0]["body"]
    assert CR.token_matches_request(e.state, new_token, req)
    assert CR.email_for_token(e.state, old_token) is None
    events = audit_log.load(e.state / "audit_log.json").by_source("committee_request")
    assert [ev for ev in events if ev.action == "link_updated"]
    sent_types = [ev.type for ev in send_log.load(e.state / "send_log.json").by_status(send_log.SENT)]
    assert CR.TYPE_COMMITTEE_LINK_UPDATE in sent_types

    # 冪等：同日重跑、隔天再跑都不再寄（委員不催件）
    n = len(e.sent)
    e.automate(D + dt.timedelta(days=3), hour=17)
    e.automate(D + dt.timedelta(days=4))
    assert not e.mails_to("fang@example.org", n)
    assert CR._load_links(e.state)["fang@example.org"] == new_token


def test_no_link_update_without_public_url(tmp_path, monkeypatch):
    monkeypatch.delenv(A.PUBLIC_URL_ENV, raising=False)
    e = _released(tmp_path, monkeypatch, cases=[dict(student_id="111900001", name="甲"),
                                                  dict(student_id="111900002", name="乙")], keys=(K1,))
    e.automate(D + dt.timedelta(days=2))
    e.clear(K2)
    n = len(e.sent)
    e.automate(D + dt.timedelta(days=3))
    assert not e.mails_to("fang@example.org", n)  # 沒有連結，委員本來就是回信


# ── 6. 距離表模糊比對歧義：只列該委員例外，不中止整批建檔 ──

def test_distance_ambiguity_is_per_member_exception(tmp_path, monkeypatch):
    comms = [C_HOME_KNOWN, ("周陽交", "國立陽明交通大學資訊工程學系", "教授", "chou@example.org"), C_HOME_NEW]
    e = _env(tmp_path, monkeypatch, [dict(student_id="111900001", name="甲", committees=comms),
                                     dict(student_id="111900002", name="乙")])
    e.write_distances({"交通大學": 80, "陽明交通大學": 80, "國立清華大學資訊工程學系": 30})
    e.submit("111900001", email="a@example.org", committees=comms)
    e.submit("111900002", email="b@example.org")
    e.build()  # 不拋例外
    issues = [i for i in validation.check(e.m()) if i["問題代碼"] == "NO_DISTANCE"]
    assert [(i["案件鍵"], i["欄位"]) for i in issues] == [(K1, "委員：周陽交")]
    assert "歧義" in issues[0]["說明"] and "「交通大學」" in issues[0]["說明"] and "「陽明交通大學」" in issues[0]["說明"]
    assert issues[0]["阻擋附件6"] == "是" and issues[0]["責任"] == "office"
    fang = next(m for m in e.m().by_key()[K2].委員 if m.服務單位 == C_EXT[1])
    assert fang.交通費 == 180  # 其他案件照常計算
    e.automate(D + dt.timedelta(days=1))  # 每日自動化也不中止


def test_distance_table_fare_reports_candidates():
    table = DistanceTable({"交通大學": 80, "陽明交通大學": 80})
    fee, note = table.fare("國立陽明交通大學資訊工程學系")
    assert fee is None and "交通大學" in note and "陽明交通大學" in note
    with pytest.raises(ValueError):
        table.km_for("國立陽明交通大學資訊工程學系")


# ── 8. 補件期間冒出的新問題：隔天每日檢核仍在才寄（決策三-3）──

def test_new_issue_during_deficiency_is_mailed_next_day(tmp_path, monkeypatch):
    e = _env(tmp_path, monkeypatch, [dict(student_id="111900002", name="乙")],
             deficiency_reminder_lead_days=2, student_email_template="{學號}@cc.example.edu")
    e.automate(D + dt.timedelta(days=1))
    assert len(e.sent) == 1  # 首次補件信：開始日當天照寄

    e.set_day(D + dt.timedelta(days=2), 11)
    e.submit("111900002", email="b@example.org", committees=[C_HOME_KNOWN, C_EXT, C_EXT2])
    build = e.build()  # 比照網頁送出後自動建檔
    A.sync_deficiency_deadlines(master=build.master, state_dir=e.state, settings=e.settings(),
                                notified_case_keys=A.notified_student_cases(e.state))
    assert A.student_case_status(K2, e.state).pending  # 學生頁立即顯示

    n = len(e.sent)
    e.automate(D + dt.timedelta(days=2), hour=15)  # 同日重跑：不寄補件信
    # （學生補齊後附件4 會照 2026-10-12 規則寄給指導教授參考，那不是補件信）
    assert [m for m in e.sent[n:] if "口試附件4" not in m["subject"]] == []
    e.automate(D + dt.timedelta(days=3))
    mails = e.mails_to("b@example.org", n)
    assert len(mails) == 1 and "補正" in mails[0]["subject"]
    n = len(e.sent)
    e.automate(D + dt.timedelta(days=4))
    assert not [m for m in e.sent[n:] if "補正通知" in m["subject"]]  # 同一問題不重寄


def test_first_seen_is_pruned_and_restarts_on_recurrence(tmp_path):
    issue = {"案件鍵": K1, "問題代碼": "NO_EXAM_DATE", "欄位": "口試日期", "責任": "student"}
    first = A.record_issue_first_seen(tmp_path, [issue], date=D, prune=True)
    assert first[K1]["NO_EXAM_DATE|口試日期"] == D.isoformat()
    assert A.record_issue_first_seen(tmp_path, [issue], date=D + dt.timedelta(days=1))[K1][
        "NO_EXAM_DATE|口試日期"] == D.isoformat()
    assert A.record_issue_first_seen(tmp_path, [], date=D + dt.timedelta(days=2), prune=True) == {}
    again = A.record_issue_first_seen(tmp_path, [issue], date=D + dt.timedelta(days=3), prune=True)
    assert again[K1]["NO_EXAM_DATE|口試日期"] == (D + dt.timedelta(days=3)).isoformat()
