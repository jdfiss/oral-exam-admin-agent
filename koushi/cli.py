"""口試流程的唯一命令列入口。"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import config as C
from . import ingest
from . import issue_store
from .validation import summary
from .workflow import RegistryPaths, TemplatePaths
from .application import (
    build_current_master, check_current_master, generate_artifacts,
    load_current_master, process_round, tracking_snapshot, plan_reminders,
    mark_reminders_sent, send_reminder_plan, run_daily_automation, record_automation_status,
)
from . import send_log
from . import settings as office_settings
from . import committee_requests
from . import corrections
from .mail import notify


def _fetch_replies():
    from .mail.inbox import fetch_replies
    return fetch_replies()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="koushi", description="口試資料處理、追蹤與文件產製")
    parser.add_argument("command", choices=[
        "build", "process", "check", "doctor", "generate", "status", "remind", "automate", "mailbox",
    ])
    parser.add_argument("--attachment2", "--att2", dest="attachment2", default=None,
                        help="附件2（Portal 匯出）；未指定時使用系辦上傳的附件2")
    parser.add_argument("--submission", action="append", default=[])
    parser.add_argument("--submissions-dir", dest="submissions_dir", default=str(C.SRC_SUBMISSIONS_DIR))
    parser.add_argument("--master", default=str(C.MASTER))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--state-dir", default=str(C.STATE_DIR),
                        help="跨批次追蹤與寄送記錄位置（process/status/remind 使用）")
    parser.add_argument("--send", action="store_true",
                        help="process／remind／automate 實際寄信；預設只預覽")
    parser.add_argument("--term", default=None)
    parser.add_argument("--phase", choices=["pre-exam", "post-exam"], default=None,
                        help="generate 必填階段：pre-exam（口試前）或 post-exam（口試後）")
    return parser


def _registries(master_path: Path, state_dir: str | Path) -> RegistryPaths:
    return RegistryPaths(
        attachment2=C.PORTAL_ATT2,
        committee_replies=Path(state_dir) / committee_requests.REPLIES_FILE,
        corrections=Path(state_dir) / corrections.FILENAME,
        members=C.REG_MEMBER,
        distances=C.DISTANCE_CSV,
        members_output=master_path.parent / "data" / "委員個資.xlsx",
    )


def _print_issues(issues: list[dict[str, object]]) -> None:
    counts = summary(issues)
    if not counts:
        print("資料檢核：無異常")
        return
    print("資料檢核：")
    for code, count in counts.items():
        blocking = any(
            item.get("問題代碼") == code and item.get("阻擋產出") == "是"
            for item in issues
        )
        print(f"  {code}: {count}{'（阻擋）' if blocking else ''}")


def _print_mailbox(records) -> None:
    from . import mailbox_agent as MA
    if not records:
        print("信箱 agent：沒有新信")
        return
    print(f"═══ 信箱 agent：新處理 {len(records)} 封 ═══")
    for r in records:
        policies = "、".join(f"{a['label']}（{MA.POLICY_LABELS[a['policy']]}）" for a in r["actions"])
        print(f"  [{MA.MAIL_STATUS_LABELS[r['status']]}] {MA.ROLE_LABELS[r['sender']['role']]}"
              f"／{MA.CATEGORY_LABELS[r['analysis']['category']]}：{r['analysis']['summary']}")
        print(f"    建議：{policies}")


def _print_proposals(proposals) -> None:
    from collections import Counter
    from .ai_proposals import AGENTS
    if not proposals:
        print("AI 建議：本輪沒有新建議")
        return
    by_agent = Counter(p["agent"] for p in proposals)
    errors = sum(1 for p in proposals if p.get("error"))
    detail = "、".join(f"{AGENTS[a]} {n}" for a, n in by_agent.items())
    print(f"AI 建議：新增 {len(proposals)} 筆（{detail}）；全部待專員在「AI 建議」頁決定"
          + (f"；其中 {errors} 筆模型失敗，已轉專員" if errors else ""))


def main(argv: list[str] | None = None) -> int:
    from .runtime import load_local_env, make_classifier, make_mailbox_llm
    load_local_env()
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "generate" and args.phase is None:
        parser.error("generate 必須指定 --phase pre-exam 或 --phase post-exam")
    master_path = Path(args.master)
    command = "check" if args.command == "doctor" else args.command
    if args.send and command not in ("process", "remind", "automate", "mailbox"):
        raise ValueError("--send 僅能搭配 process／remind／automate／mailbox 使用")

    if command == "build":
        submissions = [Path(path) for path in args.submission]
        submissions.extend(ingest.student_submission_paths(args.submissions_dir))
        result = build_current_master(
            attachment2_path=Path(args.attachment2) if args.attachment2 else None,
            submission_paths=submissions,
            master_path=master_path,
            registries=_registries(master_path, args.state_dir),
            submissions_dir=args.submissions_dir,
        )
        print(f"已建立主檔：{result.output_path}")
        print(f"案件：{len(result.master.exams)}；委員：{sum(len(e.委員) for e in result.master.exams)}")
        _print_issues(result.issues)

    if command == "process":
        submissions = [Path(path) for path in args.submission]
        submissions.extend(ingest.student_submission_paths(args.submissions_dir))
        result = process_round(
            attachment2_path=Path(args.attachment2) if args.attachment2 else None,
            submission_paths=submissions,
            master_path=master_path,
            state_dir=args.state_dir,
            registries=_registries(master_path, args.state_dir),
            send=args.send,
        )
        print(f"已建立主檔：{result.build.output_path}")
        _print_issues(result.build.issues)
        notify.preview_drafts(result.drafts)
        print(f"通知：{len(result.drafts)} 封草稿；已寄出 {len(result.sent)} 封；"
              f"查無收件人 {len(notify.unsendable(result.drafts))} 封")
        if args.send:
            print(f"寄送記錄：{result.log_path}")
        else:
            print("預覽模式；明確加 --send 才會呼叫 SMTP。")

    if command == "check":
        _print_issues(check_current_master(master_path))

    if command == "mailbox":
        # 讀業務信箱：先跑既有附件4 回信流程，再由信箱 agent 分析每一封信。
        # 預覽模式（無 --send）也會分析並記錄，但不寄自動收件通知。
        from .application import ingest_advisor_replies, run_mailbox_agent
        master = load_current_master(master_path)
        mails = _fetch_replies()
        ingest_advisor_replies(master=master, state_dir=args.state_dir, mails=mails,
                               classifier=make_classifier(args.state_dir))
        _print_mailbox(run_mailbox_agent(master=master, state_dir=args.state_dir, mails=mails,
                                         llm=make_mailbox_llm(args.state_dir), send=args.send))

    if command == "generate":
        output_dir = Path(args.output_dir) if args.output_dir else master_path.parent
        templates = TemplatePaths(
            attachment4=C.TPL_ATT4,
            attachment6=C.TPL_ATT6,
        )
        state_dir = Path(args.state_dir)  # 放行／教授確認狀態一律從 --state-dir 讀，與產出位置無關
        result = generate_artifacts(
            master_path=master_path, templates=templates, output_dir=output_dir,
            term=args.term, phase=args.phase, state_dir=state_dir,
        )
        if args.phase == "pre-exam":
            print(f"已建立附件4：{result.attachment4}")
            print(f"已建立預填附件6：{len(result.attachment6_files)} 份")
        elif args.phase == "post-exam":
            print(f"已建立附件6：{len(result.attachment6_files)} 份")
        _print_issues(result.issues)

    if command == "status":
        master = load_current_master(master_path)
        state_dir = Path(args.output_dir) if args.output_dir else Path(args.state_dir)
        tracking = tracking_snapshot(
            master=master, state_dir=state_dir, term=args.term, persist=True,
        )
        d_report, c_report, l_report = (
            tracking.deficiency, tracking.confirmation, tracking.collection,
        )

        print("═══ 缺件追蹤 ═══")
        print(f"  新增：{len(d_report.new)}  已通知：{len(d_report.notified)}  "
              f"逾期：{len(d_report.overdue)}  已解決：{len(d_report.resolved_keys)}")
        for item in d_report.overdue:
            print(f"  ⚠ {item.case_key} {item.issue_code} ({item.owner})")

        print("═══ 教授確認 ═══")
        print(f"  草稿：{len(c_report.draft)}  待確認：{len(c_report.pending)}  "
              f"已確認：{len(c_report.confirmed)}  需修改：{len(c_report.modified)}  "
              f"逾期：{len(c_report.overdue)}")

        print("═══ 附件6催繳 ═══")
        print(f"  草稿：{len(l_report.draft)}  待收：{len(l_report.outstanding)}  "
              f"已收：{len(l_report.collected)}  逾期：{len(l_report.overdue)}")

        records = issue_store.load(state_dir / "issue_records.json")
        print("═══ 缺件生命週期記錄 ═══")
        print(f"  偵測：{len(records.by_status(issue_store.DETECTED))}  "
              f"草稿就緒：{len(records.by_status(issue_store.DRAFT_READY))}  "
              f"已寄出：{len(records.by_status(issue_store.REQUEST_SENT))}  "
              f"已解決：{len(records.by_status(issue_store.RESOLVED))}")

        if l_report.all_collected:
            print("  全部到齊")

    if command == "remind":
        master = load_current_master(master_path)
        state_dir = Path(args.output_dir) if args.output_dir else Path(args.state_dir)
        plan = plan_reminders(master=master, state_dir=state_dir, term=args.term,
                              submissions_dir=args.submissions_dir,
                              resolve_recipients=args.send)
        scheduled, todos = plan.scheduled, plan.internal_todos

        if not scheduled and not todos:
            print("目前沒有需要催辦或提醒的項目。")

        if scheduled:
            if args.send:
                log_path = state_dir / "send_log.json"
                sent = send_reminder_plan(state_dir=state_dir, reminders=scheduled, master=master)
                print(f"═══ 排定的提醒（{len(scheduled)} 筆；已寄出 {len(sent)} 封）═══")
                for s in scheduled:
                    status = "已寄出" if s in sent else (
                        "查無收件人" if s.draft.to is None else "已跳過（先前寄過或已核准中）")
                    print(f"  [{s.kind}] {s.draft.subject}（{status}）")
                    print(f"    收件人：{s.draft.to}　到期：{s.due_date or '未知'}")
                print(f"寄送記錄：{log_path}")
            else:
                print(f"═══ 排定的提醒（{len(scheduled)} 筆；收件人為佔位地址，尚未實際寄送）═══")
                for s in scheduled:
                    print(f"  [{s.kind}] {s.draft.subject}")
                    print(f"    收件人：{s.draft.to}　到期：{s.due_date or '未知'}")

        if todos:
            print(f"═══ 系辦內部待辦（{len(todos)} 筆；責任在系辦，不寄信）═══")
            for item in todos:
                print(f"  {item.case_key} [{item.issue_code}] {item.field}：{item.description}")

    if command == "automate":
        submissions = [Path(path) for path in args.submission]
        submissions.extend(ingest.student_submission_paths(args.submissions_dir))
        try:
            result = run_daily_automation(
                submission_paths=submissions,
                master_path=master_path,
                state_dir=args.state_dir,
                attachment2_path=Path(args.attachment2) if args.attachment2 else None,
                registries=_registries(master_path, args.state_dir),
                submissions_dir=args.submissions_dir,
                send=args.send,
                # 正式寄送時一併讀信箱裡的教授回信；預覽模式不連線。
                fetch_replies=_fetch_replies if args.send else None,
                classifier=make_classifier(args.state_dir) if args.send else None,
                llm=make_mailbox_llm(args.state_dir) if args.send else None,
            )
        except Exception as exc:
            record_automation_status(args.state_dir, ok=False, error=str(exc))
            raise
        record_automation_status(args.state_dir, ok=True, phase=result.phase, notes=result.notes)
        print(f"目前時段：{office_settings.PHASE_LABELS[result.phase]}")
        for note in result.notes:
            print(f"  ・{note}")
        print(f"已建立主檔：{result.process.build.output_path}")
        _print_issues(result.process.build.issues)
        print(f"缺件通知：{len(result.process.drafts)} 封草稿；已寄出 {len(result.process.sent)} 封")

        if result.tracking is not None:
            d_report = result.tracking.deficiency
            print(f"缺件追蹤：新增 {len(d_report.new)}／已通知 {len(d_report.notified)}／"
                  f"逾期 {len(d_report.overdue)}／已解決 {len(d_report.resolved_keys)}")
        print(f"催件排程：{len(result.reminders.scheduled)} 筆；已寄出 {len(result.reminders_sent)} 封；"
              f"系辦內部待辦 {len(result.reminders.internal_todos)} 筆")
        print(f"委員個資索取：{len(result.committee_drafts)} 封草稿；已寄出 {len(result.committee_sent)} 封")
        if result.advisor is not None:
            print(f"附件4 寄指導教授：{len(result.advisor.drafts)} 封草稿；已寄出 {len(result.advisor.sent)} 封；"
                  f"內容變動重寄 {len(result.advisor.reset)} 筆")
        print(f"教授回信：新收 {len(result.replies)} 封（待系辦於文件中心判讀）")
        _print_mailbox(result.mailbox)
        _print_proposals(result.proposals)
        if not args.send:
            print("預覽模式；明確加 --send 才會寄出缺件通知與催件。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
