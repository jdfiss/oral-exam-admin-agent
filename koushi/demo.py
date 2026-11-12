"""Five repeatable demo scenarios（含「AI 協助判讀」）. Each reset selects a new isolated run."""
from __future__ import annotations
import argparse
import datetime as dt
import json
import os
import secrets
from pathlib import Path
from . import clock, config as C, util as U

SCENARIOS = {"normal": "正常完成", "deficiency": "學生缺件", "modify": "教授要求修改", "committee": "委員未回填",
             "ai": "AI 協助判讀"}
DEFAULT_MAILBOXES = {"student": "student@example.org", "advisor": "advisor@example.org", "committee": "committee@example.org"}
MAILBOXES = DEFAULT_MAILBOXES.copy()


def load_mailboxes(root):
    path = Path(root) / "mailboxes.json"
    values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else DEFAULT_MAILBOXES
    if set(values) != set(DEFAULT_MAILBOXES) or any("@" not in str(v) for v in values.values()):
        raise ValueError("展示信箱設定必須包含 student、advisor、committee")
    MAILBOXES.clear()
    MAILBOXES.update({key: str(value).strip().lower() for key, value in values.items()})
    COMMITTEES[-1]["email"] = MAILBOXES["committee"]


def fallback_template():
    local, domain = MAILBOXES["student"].split("@", 1)
    return local + "+{學號}@" + domain
TERM = "1151"
SID = "999900001"
HOME_UNIT = C.UNIV_NAME + C.DEPT_NAME          # 由設定組出，展示不寫死真實校系
EXTERNAL_UNIT = "國立範例科技大學電子工程學系"   # 虛構校外單位
DEPARTMENT = C.DEPT_NAME + "碩士班"
COMMITTEES = [
    {"name": "模擬校內甲", "unit": HOME_UNIT, "title": "教授", "email": "known-a@example.org"},
    {"name": "模擬校內乙", "unit": HOME_UNIT, "title": "教授", "email": "known-b@example.org"},
    {"name": "模擬校外委員", "unit": EXTERNAL_UNIT, "title": "教授", "email": MAILBOXES["committee"]},
]
# 「AI 協助判讀」情境：校外委員的服務單位寫成簡稱（距離表只有全名）、職稱是規則判斷不了的「客座教授」，
# 每日流程會自然產生服務單位對應與職稱判讀建議；專員核准對應後，交通費才依距離表計算
AI_SCENARIO_UNIT = "範例科大電子系"
AI_SCENARIO_TITLE = "客座教授"


def committees_for(scenario):
    if scenario != "ai":
        return COMMITTEES
    return [*COMMITTEES[:2], dict(COMMITTEES[2], unit=AI_SCENARIO_UNIT, title=AI_SCENARIO_TITLE)]


def active(root):
    root = Path(root).resolve()
    meta = json.loads((root / "active.json").read_text(encoding="utf-8"))
    run = (root / "runs" / meta["run_id"]).resolve()
    if run.parent != root / "runs" or not (run / "demo.json").is_file():
        raise ValueError("展示資料路徑無效")
    return run, meta


def initialize(root, scenario="normal"):
    if scenario not in SCENARIOS:
        raise ValueError("未知的展示情境")
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    load_mailboxes(root)
    marker = root / "demo-root.json"
    if not marker.is_file() and any(root.iterdir()):
        raise ValueError("展示資料夾已有其他資料，拒絕覆寫")
    if marker.is_file() and json.loads(marker.read_text(encoding="utf-8")).get("purpose") != "koushi-demo-v1":
        raise ValueError("展示資料夾標記不正確")
    U.atomic_write_json({"purpose": "koushi-demo-v1"}, marker)
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    run = root / "runs" / run_id
    run.mkdir(parents=True)
    meta = {"run_id": run_id, "scenario": scenario, "label": SCENARIOS[scenario]}
    U.atomic_write_json(meta, run / "demo.json")
    # Business time is synthetic; SMTP, IMAP, OTP and link expiry use real time.
    start = dt.datetime.combine(dt.date.today(), dt.time(10))
    U.atomic_write_json({"now": start.isoformat()}, root / "clock.json")
    seed(run, start.date(), scenario)
    U.atomic_write_json(meta, root / "active.json")
    return run


def seed(run, day, scenario):
    import openpyxl
    from . import application, settings
    data = run / "data"; data.mkdir()
    subs = run / "submissions"; subs.mkdir()
    committees = committees_for(scenario)
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["學年度 / 學年學期", "學院", "系所", "學號", "姓名", "題目含特殊字", "案件種類", "審核狀態", "委員姓名", "委員服務單位", "委員職稱", "最高學歷學校名稱", "最高學歷學位名稱", "委員資格", "備註", "簽核完成時間"])
    for m in committees:
        ws.append([TERM, "示範學院", DEPARTMENT, SID, "模擬學生", "否", "學位考試申請", "電子表單簽核完成", m["name"], m["unit"], m["title"], "", "", "", "", day.isoformat() + " 09:00:00"])
    wb.save(data / "附件2-Portal匯出.xlsx")
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(["委員編號", "委員姓名", "英文姓名", "服務單位", "最高學歷學校", "最高學位", "身分證字號", "金融機構", "帳號", "戶籍地址", "Email", "最後更新", "來源"])
    for i, m in enumerate(COMMITTEES[:2], 1):
        ws.append([f"C{i:04d}", m["name"], "", m["unit"], "", "", f"DEMO-KNOWN-{i}", "", "", "", m["email"], day.isoformat(), "虛構展示資料"])
    wb.save(data / "委員個資.xlsx")
    (data / "距離表.csv").write_text(f"服務單位,單程公里\n{EXTERNAL_UNIT},30\n", encoding="utf-8-sig")
    fields = {"term": TERM, "student_email": MAILBOXES["student"],
              "exam_date": (day + dt.timedelta(days=30)).isoformat(),
              "exam_start_time": "10:00", "exam_end_time": "12:00", "exam_location": "模擬會議室",
              "exam_mode": "實體", "advisor_0": "模擬指導教授", "advisor_0_email": MAILBOXES["advisor"], "thesis_title": "虛構展示論文"}
    for i, member in enumerate(committees):
        for key in ("name", "unit", "title", "email"):
            fields[f"committee_{i}_{key}"] = member[key]
    if scenario == "deficiency":
        application.save_draft(student_id=SID, fields=fields, submissions_dir=subs)
    else:
        application.submit_application(student_id=SID, fields=fields, submissions_dir=subs)
    settings.save(settings.OfficeSettings(current_term=TERM, fill_deadline=day,
        att2_first_uploaded=day, deficiency_days=7, business_days=False, send_hour=0,
        student_email_template=fallback_template(), auto_confirm_enabled=True,
        auto_confirm_shadow=False, auto_confirm_whitelist=["模擬指導教授"]), run / "state")


def template_paths(root):
    """Convert the official legacy template once, without editing its format."""
    import hashlib
    import shutil
    from .document_conversion import docx_template
    from .workflow import TemplatePaths
    source = C.TPL_ATT4
    if source.suffix.lower() == ".docx":
        return TemplatePaths(attachment4=source, attachment6=C.TPL_ATT6)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
    target = Path(root) / "templates" / f"attachment4-{digest}.docx"
    with U.build_lock(target):
        if not target.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            with docx_template(source) as converted:
                shutil.copy2(converted, target)
    return TemplatePaths(attachment4=target, attachment6=C.TPL_ATT6)


def web_config(root):
    from . import judge
    from .web.app import WebConfig
    from .workflow import RegistryPaths
    load_mailboxes(root)
    judge_password = None
    if judge.enabled():
        judge.ensure_allowed(root)
        judge_password = os.environ.get(judge.PASSWORD_ENV) or None
        if not judge_password:
            raise RuntimeError(f"評審模式須設定 {judge.PASSWORD_ENV}")
        if judge_password == os.environ.get("KOUSHI_OFFICE_PASSWORD"):
            raise RuntimeError("評審密碼不可與系辦密碼相同")
    run, _ = active(root)
    return WebConfig(submissions_dir=run / "submissions", master_path=run / "current" / "口試主檔.xlsx",
        state_dir=run / "state", registries=RegistryPaths(members=run / "data" / "委員個資.xlsx",
        distances=run / "data" / "距離表.csv", attachment2=run / "data" / "附件2-Portal匯出.xlsx"),
        doc_output_root=run / "documents", office_password=os.environ["KOUSHI_OFFICE_PASSWORD"], demo_root=Path(root).resolve(),
        judge_password=judge_password)


def transport_for(config):
    """展示寄信：評審模式只進模擬寄件匣；一般展示寄到三個指定測試信箱。"""
    if config.judge_mode:
        from .judge import capture_transport
        return capture_transport(config.state_dir)
    return mail_transport(config.demo_root)


def mail_transport(root):
    from .mail.email_sender import send_email, sender_address
    def send(**kwargs):
        if sender_address().lower() != MAILBOXES["student"]:
            raise ValueError("展示寄件人必須是登記的學生／業務信箱，請檢查 MAIL_SENDER")
        # Map our generated missing-submission alias back to the registered mailbox.
        target = kwargs.get("to", "")
        local, domain = MAILBOXES["student"].split("@", 1)
        if target.startswith(local + "+") and target.endswith("@" + domain):
            kwargs["to"] = MAILBOXES["student"]
        permitted = set(MAILBOXES.values())
        if kwargs.get("to") not in permitted or (kwargs.get("cc") and kwargs["cc"] not in permitted):
            raise ValueError("展示模式僅能寄給三個已指定的測試信箱")
        _, meta = active(root)
        kwargs["subject"] = f"[模擬 {meta['run_id']}] " + kwargs["subject"]
        kwargs["body"] = "本信為假資料流程展示，不是真實口試通知。\n\n" + kwargs["body"]
        return send_email(**kwargs)
    return send


def run_automation(config, send=False):
    from . import ai_switch, application, ingest
    from .runtime import make_classifier, make_mailbox_llm
    from .mail.inbox import fetch_replies
    classifier = make_classifier(config.state_dir) if send else None
    llm = make_mailbox_llm(config.state_dir) if send else None
    from .llm import offline_selected
    # 評審模式沒有模型也能跑（全部轉人工）；AI 緊急停止與離線備援時本來就不該要求真實模型
    if send and classifier is None and not config.judge_mode and not ai_switch.is_disabled(config.state_dir)             and not offline_selected():
        raise ValueError("請先在本機 .env 設定 OPENAI_API_KEY 或 GEMINI_API_KEY，再執行即時展示")
    def replies():
        if config.judge_mode:
            from .judge import take_replies
            return take_replies(config.state_dir)
        from .advisor_confirmation import _addr
        _, meta = active(config.demo_root)
        return [mail for mail in fetch_replies()
                if meta["run_id"] in mail.subject and _addr(mail.from_addr) == MAILBOXES["advisor"]]
    result = application.run_daily_automation(
        submission_paths=ingest.student_submission_paths(config.submissions_dir),
        master_path=config.master_path, state_dir=config.state_dir, registries=config.build_registries,
        submissions_dir=config.submissions_dir, send=send,
        transport=transport_for(config) if send else None,
        fetch_replies=replies if send else None, classifier=classifier, llm=llm,
        date=clock.today(), now=clock.now(), templates=config.templates)
    application.record_automation_status(config.state_dir, ok=True, phase=result.phase, notes=result.notes)
    return result


def main():
    from .runtime import load_local_env
    load_local_env()
    parser = argparse.ArgumentParser(description="15 分鐘口試流程展示")
    parser.add_argument("command", choices=["init", "serve", "automate", "judge"])
    parser.add_argument("--scenario", choices=list(SCENARIOS), default="normal")
    parser.add_argument("--root", type=Path, default=None,
                        help="展示資料夾；預設 outputs/demo，評審模式預設 outputs/judge")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--public-url", default="",
                        help="對外網址（例如 Cloudflare Tunnel 的 https 網址）；委員專屬連結與信中連結用這個，伺服器仍只聽本機")
    parser.add_argument("--offline", action="store_true",
                        help="強制使用離線備援模型（不連網）；評審模式沒有金鑰時自動使用")
    args = parser.parse_args()
    judge_mode = args.command == "judge"
    args.root = args.root or C.OUTPUT_ROOT / ("judge" if judge_mode else "demo")
    if judge_mode:
        # 評審模式：先檢查不是正式環境、不是正式資料夾，再碰任何檔案
        from . import judge
        judge.ensure_allowed(args.root)
        os.environ[judge.MODE_ENV] = "1"
        judge.block_real_smtp()
        model_label = judge.configure_llm(force_offline=args.offline)
    elif args.offline:
        os.environ["KOUSHI_LLM_PROVIDER"] = "offline"
    os.environ["KOUSHI_DEMO_ROOT"] = str(args.root.resolve())
    # Keep runtime conversion files in the isolated demo area.
    import tempfile
    temp = args.root / "temp"
    # initialize() owns root creation and checks its marker first.

    if args.command == "init" or not (args.root / "active.json").is_file():
        initialize(args.root, args.scenario)
    temp.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temp.resolve())
    access = args.root / "local-access.json"
    secrets_file = json.loads(access.read_text(encoding="utf-8")) if access.is_file() else {}
    defaults = {"KOUSHI_WEB_SECRET_KEY": secrets.token_hex(32), "KOUSHI_OFFICE_PASSWORD": secrets.token_urlsafe(12)}
    if judge_mode:
        defaults["KOUSHI_JUDGE_PASSWORD"] = secrets.token_urlsafe(12)  # 與系辦密碼不同的評審專用密碼
    if any(key not in secrets_file for key in defaults):
        U.atomic_write_json({**defaults, **secrets_file}, access)
    for key, value in json.loads(access.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)
    public_url = args.public_url.strip().rstrip("/") or f"http://127.0.0.1:{args.port}"
    os.environ["KOUSHI_PUBLIC_URL"] = public_url
    from .web.app import app, configure_app, _try_auto_build
    configure_app(app, web_config(args.root))
    _try_auto_build()
    if args.command == "init":
        print(f"已建立 {SCENARIOS[args.scenario]}；展示資料位於 {args.root}")
    elif args.command == "automate":
        print(run_automation(web_config(args.root), args.send).phase)
    else:
        from waitress import serve
        # 先把官方舊式 .doc 附件4 範本轉好（LibreOffice 第一次約 30 秒）；不然第一次按「執行本日流程」要等很久
        print("準備附件4 範本中（第一次啟動需轉檔，約 30 秒）…", flush=True)
        try:
            template_paths(args.root)
        except Exception as exc:  # noqa: BLE001 — 轉檔失敗不擋啟動，產附件4 時會再顯示原因
            print(f"附件4 範本轉檔失敗（{exc}）；產附件4 時會再嘗試並顯示原因。", flush=True)
        if judge_mode:
            print(f"評審模式網址：{public_url}；評審密碼（KOUSHI_JUDGE_PASSWORD）位於 {access}。"
                  f"示範資料、不會寄出任何信件。模型：{model_label}", flush=True)
        else:
            print(f"展示網址：http://127.0.0.1:{args.port}；系辦密碼設定位於 {access}", flush=True)
        serve(app, host="127.0.0.1", port=args.port, threads=1)


if __name__ == "__main__":
    main()
