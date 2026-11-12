"""交付缺陷的正式入口回歸：下載、世代、交易、舊分頁、封存與恢復。"""
import datetime as dt
import io
from dataclasses import replace
import pytest
import openpyxl
from docx import Document
from koushi import application as A, source_state, settings, case_review, master as MW
from koushi.engines.state import EngineState, save_to_dir, load_from_dir
from koushi.engines.confirmation import create_requests
from koushi.drafts import DraftTemplateEngine
from koushi.scheduler import ScheduledReminder
from koushi.web import app as W
from tests.test_web_office_upload import client, _submit, _upload_all, _csrf
from tests.test_application_generate import _clean_exam, _save_master, _clear_all, TEMPLATES


def prepare(client, tmp_path, monkeypatch):
    _submit(client)
    _upload_all(client)
    cfg = W.cfg()
    A.clear_case_review(master_path=cfg.master_path, state_dir=cfg.state_dir,
                        case_key="1141-111900001", actor="專員", note="逐項核對來源異常")
    template = tmp_path / "template.docx"
    doc = Document()
    table = doc.add_table(rows=5, cols=8)
    table.rows[3].cells[0].merge(table.rows[3].cells[-1])
    doc.save(template)
    monkeypatch.setattr(W.C, "TPL_ATT4", template)
    master = A.load_current_master(cfg.master_path)
    req = next(r for r in create_requests(master) if r.advisor == "林指導")
    A.record_confirmation(master_path=cfg.master_path, state_dir=cfg.state_dir,
        token=A.request_token(req.request_key), action="confirmed", actor="專員",
        note="電話確認目前文件內容", expected_version=req.content_version)
    return cfg


@pytest.mark.parametrize("phase", ["pre-exam", "post-exam"])
def test_real_downloads_and_permissions(client, tmp_path, monkeypatch, phase):
    cfg = prepare(client, tmp_path, monkeypatch)
    client.post("/office/documents/generate", data={"phase":phase,"csrf_token":_csrf(client)})
    entry = W._load_doc_status()[phase]
    assert entry["status"] == "built"
    assert entry["attachment6_files"]
    for name in entry["attachment6_files"] + entry.get("attachment4_files", []):
        response = client.get("/office/documents/download/"+phase+"/"+name)
        assert response.status_code == 200
        assert response.data == (cfg.doc_output_root / phase / name).read_bytes()
        if name.endswith(".xlsx"):
            from koushi.generators import attachment6
            ws = openpyxl.load_workbook(io.BytesIO(response.data)).active
            if phase == "pre-exam":
                for field in attachment6._BANK_FIELDS:
                    assert ws.cell(attachment6.DATA_START,attachment6.COL[field]).value is None
            else:
                assert ws.cell(attachment6.DATA_START,attachment6.COL["身分證字號"]).value == "A123456789"
    client.get("/logout")
    assert client.get("/office/documents/download/"+phase+"/"+name).status_code == 302
    client.post("/login/student", data={"student_id":"111900001","csrf_token":_csrf(client)})
    denied = client.get("/office/documents/download/"+phase+"/"+name)
    assert denied.status_code == 302 and not denied.data.startswith(b"PK")


def test_document_batch_failure_keeps_last_manifest(client,tmp_path,monkeypatch):
    cfg=prepare(client,tmp_path,monkeypatch)
    client.post("/office/documents/generate",data={"phase":"post-exam","csrf_token":_csrf(client)})
    previous=W._load_doc_status()["post-exam"]
    folder=cfg.doc_output_root/"post-exam"/"generations"
    before=set(folder.iterdir())
    def fail(master,**kwargs):
        (kwargs["output_dir"]/"partial.xlsx").write_bytes(b"partial")
        raise RuntimeError("second file failed")
    monkeypatch.setattr(A,"generate_post_exam",fail)
    page=client.post("/office/documents/generate",data={"phase":"post-exam","csrf_token":_csrf(client)},follow_redirects=True)
    entry=W._load_doc_status()["post-exam"]
    assert entry["attachment6_files"]==previous["attachment6_files"]
    assert "second file failed" in page.get_data(as_text=True)
    assert set(folder.iterdir())==before
    assert client.get("/office/documents/download/post-exam/"+entry["attachment6_files"][0]).status_code==200


def test_source_change_blocks_generation_and_old_download(client,tmp_path,monkeypatch):
    cfg=prepare(client,tmp_path,monkeypatch)
    client.post("/office/documents/generate",data={"phase":"post-exam","csrf_token":_csrf(client)})
    name=W._load_doc_status()["post-exam"]["attachment6_files"][0]
    cfg.registries.distances.write_text("服務單位,單程公里\n變更單位,50\n",encoding="utf-8")
    assert client.get("/office/documents/download/post-exam/"+name).status_code==409
    with pytest.raises(ValueError,match="過期"):
        A.generate_artifacts(master_path=cfg.master_path,output_dir=tmp_path/"out",templates=cfg.templates,
                             phase="post-exam",state_dir=cfg.state_dir)
    assert client.get("/office/documents").status_code==200


def test_missing_source_manifest_cannot_export(tmp_path):
    p,m=_save_master(tmp_path,[_clean_exam("1141","A","教授",dt.date(2026,11,1))])
    source_state.manifest_path(p).unlink()
    with pytest.raises(ValueError,match="缺來源世代"):
        A.generate_artifacts(master_path=p,output_dir=tmp_path/"out",templates=TEMPLATES,
            phase="post-exam",state_dir=tmp_path/"state")


def test_stale_manual_confirmation_rejected(tmp_path):
    p,m=_save_master(tmp_path,[_clean_exam("1141","A","教授",dt.date(2026,11,1))])
    old=create_requests(m)[0]
    m.exams[0].口試方式="視訊"
    MW.save(m,[],path=p)
    source_state.publish(p,{})
    with pytest.raises(ValueError,match="改版"):
        A.record_confirmation(master_path=p,state_dir=tmp_path/"state",token=A.request_token(old.request_key),
            action="confirmed",expected_version=old.content_version)
    assert not load_from_dir(tmp_path/"state").confirmation


def test_recovered_reminder_does_not_increment_twice(tmp_path):
    _,m=_save_master(tmp_path,[_clean_exam("1141","A","教授",dt.date(2026,11,1))])
    req=create_requests(m)[0]
    save_to_dir(EngineState(deficiency=[],confirmation=[req],collection=[]),tmp_path/"state")
    reminder=ScheduledReminder("confirmation",req.request_key,None,DraftTemplateEngine().confirmation_draft(req))
    A.mark_reminders_sent(tmp_path/"state",[reminder],date=dt.date(2026,11,2))
    A.mark_reminders_sent(tmp_path/"state",[reminder],date=dt.date(2026,11,2))
    A.mark_reminders_sent(tmp_path/"state",[reminder],date=dt.date(2026,11,3))
    assert load_from_dir(tmp_path/"state").confirmation[0].reminder_count==1


def test_archive_is_read_only_and_integrity_checked(client,tmp_path):
    _submit(client)
    cfg=W.cfg()
    source_state.freeze(cfg.state_dir/"archives"/"1141",{"submissions":cfg.submissions_dir},detail={"term":"1141"})
    assert client.get("/office/archives/1141").status_code==200
    path="submissions/1141-111900001.json"
    assert client.get("/office/archives/1141/files/"+path).status_code==200
    (cfg.state_dir/"archives"/"1141"/path).write_bytes(b"tampered")
    assert client.get("/office/archives/1141/files/"+path).status_code==409
    assert client.post("/office/archives/1141/files/"+path,data={"csrf_token":_csrf(client)}).status_code==405


def test_committee_link_identity_change_invalidates_token(tmp_path):
    from koushi import committee_requests as CR
    old=CR.CommitteeRequest("shared@example.org","委員甲",("1141-A",),None,None,False)
    token=CR.link_token(tmp_path,old.email,request=old)
    new=replace(old,name="委員乙")
    assert not CR.token_matches_request(tmp_path,token,new)
    fresh=CR.link_token(tmp_path,new.email,request=new)
    assert fresh!=token and CR.email_for_token(tmp_path,token) is None


def test_committee_same_email_conflict_never_spreads_id(tmp_path):
    from koushi.registry import CommitteeRegistry
    from koushi import ingest
    registry=CommitteeRegistry()
    exams={}
    from koushi.model import Member
    for student,name in [("A","委員甲"),("B","委員乙")]:
        exam=_clean_exam("1141",student,"教授",dt.date(2026,11,1))
        cid=registry.new_provisional_id(exam.案件鍵,name,"外校")
        registry.rows[cid]["Email"]="shared@example.org"
        exam.委員=[Member(committee_id=cid,服務單位="外校",委員姓名=name,Email="shared@example.org")]
        exams[exam.案件鍵]=exam
    issues=ingest.apply_committee_replies(exams,registry,{"shared@example.org":{"身分證字號":"A123456789"}})
    assert len(issues)==2 and all(i["問題代碼"]=="COMMITTEE_EMAIL_CONFLICT" for i in issues)
    assert all(m.committee_id.startswith("P") for e in exams.values() for m in e.委員)
    assert not any(row.get("身分證字號") for row in registry.rows.values())


@pytest.mark.parametrize("override", [
    {"intent":"confirm"},
    {"intent":"confirm","evidence":"ok","has_reservation":"false","modifications":[],"questions":[]},
    {"intent":"confirm","evidence":"","has_reservation":False,"modifications":[],"questions":[]},
])
def test_incomplete_model_output_is_not_confirmation(override):
    import json
    from koushi.classifier import parse_model_output,ClassifierError
    with pytest.raises(ClassifierError):
        parse_model_output(json.dumps(override),model="fake",prompt_ver="test")


def test_full_committee_replacement_does_not_copy_old_personal_data(tmp_path):
    import json
    from koushi import corrections
    from koushi.registry import CommitteeRegistry
    from koushi.model import Committee,Member
    exam=_clean_exam("1141","A","教授",dt.date(2026,11,1))
    exam.委員=[Member(committee_id="C0001",association_id="old",委員姓名="舊委員",服務單位="外校")]
    profiles={"C0001":Committee(committee_id="C0001",委員姓名="舊委員",身分證字號="A123456789")}
    registry=CommitteeRegistry()
    rows=[{"association_id":"old" if i==0 else "","name":"新委員"+str(i),
        "unit":"外校","title":"教授","email":f"c{i}@example.org","remote":False} for i in range(3)]
    change=corrections.Correction(exam.案件鍵,"committee_list",json.dumps(rows,ensure_ascii=False),
        corrections.source_value(exam,profiles,"committee_list"),"專員","更換委員","2026-10-05")
    issues=corrections.apply({exam.案件鍵:exam},profiles,[change],registry=registry)
    assert not issues and len(exam.委員)==3
    assert all(m.committee_id.startswith("P") for m in exam.委員)
    assert all(not registry.rows[m.committee_id].get("身分證字號") for m in exam.委員)


def test_same_name_correction_uses_stable_association(tmp_path):
    from koushi import corrections
    from koushi.model import Committee,Member
    exam=_clean_exam("1141","A","教授",dt.date(2026,11,1))
    exam.委員=[Member(committee_id="C1",association_id="first",委員姓名="同名",服務單位="甲校"),
               Member(committee_id="C2",association_id="second",委員姓名="同名",服務單位="乙校")]
    profiles={"C1":Committee(委員姓名="同名"),"C2":Committee(委員姓名="同名")}
    with pytest.raises(ValueError,match="同名"):
        corrections.source_value(exam,profiles,"服務單位","同名")
    change=corrections.Correction(exam.案件鍵,"服務單位","丙校","乙校","專員","單位異動","2026-10-05",member="second")
    corrections.apply({exam.案件鍵:exam},profiles,[change])
    assert [m.服務單位 for m in exam.委員]==["甲校","丙校"]


def test_old_filtered_generation_cannot_write_tracking(tmp_path):
    p,m=_save_master(tmp_path,[_clean_exam("1141","A","教授",dt.date(2026,11,1))])
    old=A._active_master(source_state.bind(m,p),settings.OfficeSettings(current_term="1141"))
    source_state.publish(p,{})
    with pytest.raises(ValueError,match="舊主檔世代"):
        A.tracking_snapshot(master=old,state_dir=tmp_path/"state",persist=True)
    assert not (tmp_path/"state"/"engines_state.json").exists()


def test_source_issue_evidence_keeps_original_and_flags_new_exception(tmp_path):
    """來源例外改版：保留放行當下的證據；名單沒變就維持放行，只標「放行後有新例外」（決策四-5）。"""
    _,m=_save_master(tmp_path,[_clean_exam("1141","A","教授",dt.date(2026,11,1))])
    m.source_issues=[{"案件鍵":"1141-A","問題代碼":"COMMITTEE_MISMATCH","說明":"甲名單","來源":"Portal"}]
    review=case_review.clear(tmp_path/"state",exam=m.exams[0],master=m,actor="專員",note="核對原名單")
    m.source_issues[0]["說明"]="乙名單"
    saved=case_review.load(tmp_path/"state")["1141-A"]
    assert saved.exception_evidence[0]["說明"]=="甲名單"
    assert not case_review.changed_since_clear(saved,m.exams[0],m)
    assert [i["說明"] for i in case_review.new_exceptions(saved,m.source_issues)]==["乙名單"]


def test_authoritative_committee_id_survives_reupload_and_new_term(tmp_path):
    import json
    from tests.test_committee_requests import _build,FAR
    from tests.att2_fixture import write_att2,case
    from koushi import committee_requests as CR
    from koushi.registry import CommitteeRegistry
    from koushi.workflow import RegistryPaths,build_master
    state=tmp_path/"state"
    _build(tmp_path,state)
    CR.save_reply(state,email="far@example.org",values={"身分證字號":"A123456789",
        "金融機構":"銀行","帳號":"00012345678","戶籍地址":"地址"})
    first=_build(tmp_path,state)
    old=next(c for c in first.committees.values() if c.委員姓名==FAR["name"])
    assert old.committee_id.startswith("C")
    (state/CR.REPLIES_FILE).write_text('{"replies":{}}',encoding="utf-8")
    source=CommitteeRegistry()
    source._register("C0099",{"委員編號":"C0099","委員姓名":FAR["name"],
        "服務單位":FAR["unit"],"身分證字號":"A123456789"})
    source.save(tmp_path/"none.xlsx")
    att2=write_att2(tmp_path/"new-att2.xlsx",[case(term="1142",student_id="B",committees=[FAR])])
    submission=tmp_path/"new.json"
    submission.write_text(json.dumps({"term":"1142","student_id":"B","version":1,
        "exam_date":"2027-01-01","advisors":["新教授"],
        "committees":[{"name":FAR["name"],"unit":FAR["unit"],"email":"changed@example.org"}]}),encoding="utf-8")
    second=build_master(submission_paths=[submission],output_path=tmp_path/"out"/"master.xlsx",
        registries=RegistryPaths(members=tmp_path/"none.xlsx",distances=tmp_path/"none.csv",
            members_output=tmp_path/"out"/"members.xlsx",attachment2=att2,committee_replies=state/CR.REPLIES_FILE))
    new=next(iter(second.master.committees.values()))
    assert new.committee_id==old.committee_id and new.帳號==old.帳號 and new.身分證字號==old.身分證字號
    assert any(i["問題代碼"]=="COMMITTEE_MATCHED_BY_NAME" for i in second.issues)


def test_physical_mode_correction_recomputes_remote_traffic_fee(tmp_path):
    from koushi import corrections,ingest
    from koushi.registry import DistanceTable
    exam=_clean_exam("1141","A","教授",dt.date(2026,11,1))
    exam.口試方式="部分視訊"
    member=exam.委員[0]
    member.服務單位="外校";member.視訊出席=True
    distance=DistanceTable({"外校":10})
    ingest.fill_fees({exam.案件鍵:exam},{},distance)
    assert member.交通費==0
    change=corrections.Correction(exam.案件鍵,"口試方式","實體","部分視訊","專員","改實體","2026-10-05")
    corrections.apply({exam.案件鍵:exam},{},[change])
    ingest.fill_fees({exam.案件鍵:exam},{},distance)
    assert member.交通費==distance.fare("外校")[0]>0
    assert "視訊" not in member.交通費說明


@pytest.mark.parametrize("fail_at",[1,2,3])
def test_student_notification_partial_failure_and_restart(tmp_path,monkeypatch,fail_at):
    import json
    from tests.att2_fixture import write_att2,case
    from koushi.workflow import RegistryPaths
    from koushi import send_log,issue_store,clock
    monkeypatch.setattr(clock,"now",lambda:dt.datetime(2026,10,5,10,tzinfo=dt.timezone(dt.timedelta(hours=8))))
    subs=tmp_path/"submissions";subs.mkdir()
    paths=[]
    for sid in "ABC":
        path=subs/f"1141-{sid}.json"
        path.write_text(json.dumps({"term":"1141","student_id":sid,"version":1,
            "student_email":f"{sid}@example.org","advisors":["教授"],"exam_date":"2026-11-01",
            "committees":[]}),encoding="utf-8")
        paths.append(path)
    att2=write_att2(tmp_path/"att2.xlsx",[case(student_id=sid) for sid in "ABC"])
    state=tmp_path/"state"
    config=settings.OfficeSettings(current_term="1141",fill_deadline=dt.date(2026,10,1),
        att2_first_uploaded=dt.date(2026,10,2),deficiency_days=14,business_days=False,send_hour=9)
    settings.save(config,state)
    kwargs=dict(submission_paths=paths,submissions_dir=subs,master_path=tmp_path/"out"/"master.xlsx",
        state_dir=state,registries=RegistryPaths(members=tmp_path/"none.xlsx",distances=tmp_path/"none.csv",attachment2=att2),send=True)
    deliveries=[];attempts=[]
    def transport(**kw):
        attempts.append(kw["to"])
        if len(attempts)==fail_at:raise RuntimeError("SMTP failure")
        deliveries.append(kw["to"])
        return True
    with pytest.raises(RuntimeError):A.process_round(**kwargs,transport=transport)
    sent=send_log.load(state/"send_log.json").by_status(send_log.SENT)
    assert len(sent)==len(deliveries)==fail_at-1
    for event in sent:
        assert all(r.status==issue_store.REQUEST_SENT for r in issue_store.load(state/"issue_records.json").by_case(event.tracking["case_key"]) if r.owner=="student")
    A.process_round(**kwargs,transport=lambda **kw:deliveries.append(kw["to"]) or True)
    assert len(deliveries)==len(set(deliveries))==3
