# -*- coding: utf-8 -*-
"""情境測試用的合成環境：附件2、學生送出、每日自動化、放行，全走真正的 koushi 模組。

源自 2026-10-09 流程規則稽核的情境腳本；時間以 `koushi.clock.now` 控制，寄信只收進
`Env.sent`，附件4 轉檔與產檔以假的實作取代（不需要 LibreOffice）。
"""

from __future__ import annotations

import contextlib
import datetime as dt
from dataclasses import replace
from pathlib import Path

import openpyxl

from koushi import application as A, advisor_confirmation as AC, clock, ingest
from koushi import settings as S, case_review, committee_requests, corrections
from koushi.workflow import RegistryPaths
from tests.att2_fixture import case as att2_case, write_att2

T = "1141"
C_HOME_KNOWN = ("陳大文", "國立範例大學資訊工程學系", "教授", "dawen@example.org")
C_EXT = ("林小芳", "國立清華大學資訊工程學系", "副教授", "fang@example.org")
C_HOME_NEW = ("張志明", "國立範例大學資訊工程學系", "助理教授", "zhiming@example.org")
C_EXT2 = ("吳外校", "國立交通大學資訊工程學系", "教授", "wu@example.org")
DEFAULT_COMMITTEES = [C_HOME_KNOWN, C_EXT, C_HOME_NEW]


class Env:
    def __init__(self, tmp: Path, monkeypatch, start: dt.date):
        self.tmp = tmp
        self.sub = tmp / "submissions"
        self.master = tmp / "current" / "master.xlsx"
        self.state = tmp / "state"
        self.state.mkdir(parents=True)
        self.att2 = tmp / "office" / "att2.xlsx"
        self.members = tmp / "office" / "members.xlsx"
        self.distances = tmp / "office" / "distances.csv"
        self.regs = RegistryPaths(attachment2=self.att2, members=self.members, distances=self.distances,
                                  committee_replies=self.state / committee_requests.REPLIES_FILE,
                                  corrections=self.state / corrections.FILENAME)
        self.sent: list[dict] = []
        self.now = dt.datetime.combine(start, dt.time(10, 0))
        monkeypatch.setattr(clock, "now", lambda: self.now)

        @contextlib.contextmanager
        def fake_template(path, **kwargs):
            yield path

        def fake_write(master, term, *, template, output_dir):
            output_dir.mkdir(parents=True, exist_ok=True)
            out = output_dir / f"附件4_{term}.docx"
            out.write_text("att4", encoding="utf-8")
            return out

        monkeypatch.setattr("koushi.document_conversion.docx_template", fake_template)
        monkeypatch.setattr(AC, "write_term_document", fake_write)
        self.write_distances({"國立清華大學資訊工程學系": 30})
        self.write_members([("C0001", "陳大文", "國立範例大學資訊工程學系", "A123456789", "", "", "")])

    def set_day(self, day: dt.date, hour: int = 10):
        self.now = dt.datetime.combine(day, dt.time(hour, 0))

    @property
    def today(self) -> dt.date:
        return self.now.date()

    def write_distances(self, rows: dict):
        self.distances.parent.mkdir(parents=True, exist_ok=True)
        text = "服務單位,單程公里\n" + "".join(f"{k},{v}\n" for k, v in rows.items())
        self.distances.write_text(text, encoding="utf-8-sig")

    def write_members(self, rows):
        self.members.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["委員編號", "委員姓名", "英文姓名", "服務單位", "最高學歷學校", "最高學位",
                   "身分證字號", "金融機構", "帳號", "戶籍地址", "Email", "最後更新", "來源"])
        for cid, name, unit, idno, bank, acct, addr in rows:
            ws.append([cid, name, "", unit, "", "", idno, bank, acct, addr, "", "2026-09-17", "系辦上傳"])
        wb.save(self.members)

    def upload_att2(self, cases, day=None):
        rows = []
        for c in cases:
            c = dict(c)
            comms = c.pop("committees", DEFAULT_COMMITTEES)
            c["committees"] = [x if isinstance(x, dict) else {"name": x[0], "unit": x[1], "title": x[2]}
                               for x in comms]
            rows.append(att2_case(**c))
        write_att2(self.att2, rows)
        A.record_att2_upload(self.state, self.att2, day=day or self.today)

    def settings(self, **changes):
        S.save(replace(S.load(self.state), **changes), self.state)
        return S.load(self.state)

    def form(self, sid, *, email="s@example.org", exam_date=None, committees=None, mode="實體",
             advisors=(("林指導", "lin@example.org"),), location="工程館 A204", term=T, remote=()):
        exam_date = exam_date or (self.today + dt.timedelta(days=40))
        data = {"term": term, "student_email": email, "exam_date": exam_date.isoformat(),
                "exam_start_time": "10:00", "exam_end_time": "12:00", "exam_location": location,
                "exam_mode": mode, "thesis_title": "論文"}
        for i, (n, e) in enumerate(advisors):
            data[f"advisor_{i}"] = n
            data[f"advisor_{i}_email"] = e
        for i, (n, u, t, e) in enumerate(committees or DEFAULT_COMMITTEES):
            data.update({f"committee_{i}_name": n, f"committee_{i}_unit": u,
                         f"committee_{i}_title": t, f"committee_{i}_email": e})
            if n in remote:
                data[f"committee_{i}_remote"] = "1"
        return data

    def submit(self, sid, **kw):
        data = A.submit_application(student_id=sid, fields=self.form(sid, **kw), submissions_dir=self.sub)
        A.mark_case_resubmitted(f"{T}-{sid}", self.state)
        return data

    def automate(self, day=None, *, send=True, hour=10):
        if day:
            self.set_day(day, hour)
        return A.run_daily_automation(
            submission_paths=ingest.student_submission_paths(self.sub), master_path=self.master,
            state_dir=self.state, registries=self.regs, submissions_dir=self.sub, send=send,
            transport=lambda **kw: self.sent.append(kw) or True, date=self.today, now=self.now)

    def build(self):
        return A.build_current_master(submission_paths=ingest.student_submission_paths(self.sub),
                                      master_path=self.master, registries=self.regs, submissions_dir=self.sub)

    def m(self):
        return A.load_current_master(self.master)

    def clear(self, key, actor="專員甲"):
        return A.clear_case_review(master_path=self.master, state_dir=self.state, case_key=key, actor=actor)

    def cleared(self) -> set[str]:
        return case_review.cleared_keys(self.m(), self.state)

    def mails_to(self, addr, since=0):
        return [m for m in self.sent[since:] if m["to"] == addr]
