# -*- coding: utf-8 -*-
import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from koushi import master as master_workbook
from koushi.generators import attachment4, attachment6
from koushi.model import Committee, Exam, Master, Member


def _master() -> Master:
    committee = Committee(
        committee_id="C0001",
        委員姓名="測試委員",
        服務單位="測試大學資訊工程學系",
    )
    exam = Exam(
        學期="1142",
        學號="SIM001",
        姓名="測試學生",
        系所="資訊工程學系碩士班",
        案件種類="學位考試申請",
        審核狀態="簽核完成",
        指導教授="測試教授",
        口試日期原文="9/1",
        口試方式="視訊",
        有附件2=True,
        有附件3=True,
        委員=[
            Member(
                committee_id="C0001",
                委員順序=1,
                服務單位="測試大學資訊工程學系",
                職稱="教授",
                委員資格="1",
                口試費=1000,
                交通費=0,
            )
        ],
    ).derive()
    return Master(exams=[exam], committees={committee.committee_id: committee})


class MasterSchema5Tests(unittest.TestCase):
    def test_committee_filled_flag_round_trips(self):
        """介面委員已填存讀後要保留，否則重新載入主檔會誤報「委員名單未填」。"""
        master = _master()
        master.exams[0].介面委員已填 = True
        with TemporaryDirectory() as folder:
            output = Path(folder) / "master.xlsx"
            master_workbook.save(master, [], output)
            loaded = master_workbook.load(output)
        self.assertTrue(loaded.exams[0].介面委員已填)

    def test_nullable_exam_fields_round_trip_as_blank(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "master.xlsx"

            master_workbook.save(_master(), [], output)
            loaded = master_workbook.load(output)

        exam = loaded.exams[0]
        self.assertEqual(master_workbook.SCHEMA_VERSION, 5)
        self.assertEqual(exam.論文題目, "")
        self.assertEqual(exam.口試開始時間, "")
        self.assertEqual(exam.口試結束時間, "")
        self.assertEqual(exam.口試地點, "")
        self.assertEqual(exam.資料狀態, "齊全")

    def test_nullable_exam_fields_preserve_values_when_source_is_connected(self):
        master = _master()
        exam = master.exams[0]
        exam.論文題目 = "可追溯的模擬論文題目"
        exam.口試開始時間 = "09:00"
        exam.口試結束時間 = "10:30"
        exam.口試地點 = "工程館 S1-A-101"

        with TemporaryDirectory() as folder:
            output = Path(folder) / "master.xlsx"
            master_workbook.save(master, [], output)
            loaded = master_workbook.load(output)

        reloaded = loaded.exams[0]
        self.assertEqual(reloaded.論文題目, exam.論文題目)
        self.assertEqual(reloaded.口試開始時間, "09:00")
        self.assertEqual(reloaded.口試結束時間, "10:30")
        self.assertEqual(reloaded.口試地點, "工程館 S1-A-101")

    def test_optional_fields_do_not_change_attachment_grouping(self):
        master = _master()
        first = master.exams[0]
        first.論文題目 = "題目甲"
        first.口試開始時間 = "09:00"
        first.口試結束時間 = "10:00"
        first.口試地點 = "會議室甲"
        second = copy.deepcopy(first)
        second.學號 = "SIM002"
        second.姓名 = "另一位學生"
        second.論文題目 = "題目乙"
        second.口試開始時間 = "14:00"
        second.口試結束時間 = "16:00"
        second.口試地點 = "會議室乙"
        second.derive()
        master.exams.append(second)

        attachment4_rows = attachment4.group(master.exams)["1142"]
        attachment6_groups = attachment6.roster_groups(master.exams)

        self.assertEqual(len(attachment4_rows), 1)
        self.assertEqual(attachment4_rows[0]["時間"], "")
        self.assertEqual(len(attachment6_groups), 1)


if __name__ == "__main__":
    unittest.main()
