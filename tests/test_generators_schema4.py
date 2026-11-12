from __future__ import annotations

import copy
from io import BytesIO
import unittest
from subprocess import CompletedProcess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as SpreadsheetImage
from PIL import Image as PillowImage

from koushi import document_conversion, validation
from koushi.generators import attachment4, attachment6
from koushi.model import Committee, Exam, Master, Member
from koushi.workflow import TemplatePaths, generate_456


def _master() -> Master:
    committee = Committee(
        committee_id="C0001",
        委員姓名="測試委員",
        服務單位="測試大學資訊工程學系",
        身分證字號="TEST-ID-1",
        金融機構="測試銀行分行",
        帳號="TEST-ACCOUNT",
        戶籍地址="測試地址",
    )
    exam = Exam(
        學期="1142",
        學號="SIM001",
        姓名="測試學生",
        系所="資訊工程學系碩士班",
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
                交通費=120,
                交通費說明="測試交通費",
            )
        ],
    ).derive()
    return Master(exams=[exam], committees={committee.committee_id: committee})


def _master_with_two_students_same_advisor_and_date() -> Master:
    master = _master()
    second = Exam(
        學期="1142",
        學號="SIM002",
        姓名="另一位學生",
        系所="資訊工程學系碩士班",
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
                交通費=120,
                交通費說明="測試交通費",
            )
        ],
    ).derive()
    master.exams.append(second)
    return master


def _attachment4_template(path: Path) -> None:
    document = Document()
    table = document.add_table(rows=7, cols=8)
    table.cell(0, 0).merge(table.cell(0, 7)).text = "114學年度學位考試一覽表"
    table.cell(1, 1).merge(table.cell(1, 3)).text = "口試學生"
    for column, value in enumerate(
        ("指導教授", "博士班", "碩士一般", "碩士專班", "口試日期", "時間", "方式", "備註")
    ):
        table.cell(2, column).text = value
    for row, fill, label in (
        (3, "FFCC99", "舊學期列"),
        (5, "F6C5AC", "主資料學期列"),
    ):
        cell = table.cell(row, 0).merge(table.cell(row, 7))
        cell.text = label
        shading = OxmlElement("w:shd")
        shading.set(qn("w:fill"), fill)
        cell._tc.get_or_add_tcPr().append(shading)

    # LibreOffice 轉換舊版 .doc 時會輸出邏輯方向 start/end；部分 DOCX
    # 預覽器只支援傳統 left/right。測試範本刻意重現正式範本的這個狀態。
    for row in table.rows:
        for tc in row._tr.tc_lst:
            borders = OxmlElement("w:tcBorders")
            for side in ("top", "start", "bottom", "end"):
                border = OxmlElement(f"w:{side}")
                border.set(qn("w:val"), "single")
                border.set(qn("w:sz"), "4")
                border.set(qn("w:space"), "0")
                border.set(qn("w:color"), "auto")
                borders.append(border)
            tc.get_or_add_tcPr().append(borders)

    title_alignment = OxmlElement("w:jc")
    title_alignment.set(qn("w:val"), "end")
    table.rows[0].cells[0].paragraphs[0]._p.get_or_add_pPr().append(title_alignment)
    document.save(path)


class Schema4GeneratorContractTests(unittest.TestCase):
    def test_committee_count_does_not_block_generation(self):
        master = _master()
        exam = master.exams[0]
        for number in range(2, 5):
            committee_id = f"C{number:04d}"
            master.committees[committee_id] = Committee(
                committee_id=committee_id,
                委員姓名=f"測試委員{number}",
                服務單位="測試大學資訊工程學系",
            )
            exam.委員.append(
                Member(
                    committee_id=committee_id,
                    委員順序=number,
                    服務單位="測試大學資訊工程學系",
                    口試費=1000,
                    交通費=0,
                )
            )

        issue_codes = {item["問題代碼"] for item in validation.check(master)}

        self.assertNotIn("FEE_CAP_EXCEEDED", issue_codes)

    def test_attachment4_intentionally_leaves_template_time_column_blank(self):
        grouped = attachment4.group(_master().exams)

        row = grouped["1142"][0]
        self.assertEqual(row["指導教授"], "測試教授")
        self.assertEqual(row["碩士一般"], ["測試學生"])
        self.assertEqual(row["時間"], "")
        self.assertEqual(row["方式"], "視訊")

    def test_attachment4_combines_same_advisor_date_into_one_template_row(self):
        grouped = attachment4.group(
            _master_with_two_students_same_advisor_and_date().exams
        )

        rows = grouped["1142"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["碩士一般"], ["測試學生", "另一位學生"])

    def test_attachment4_uses_template_conventions_for_physical_exam_and_advisor_note(self):
        master = _master()
        master.exams[0].口試方式 = "實體"
        master.exams[0].委員[0].委員備註 = "指導教授"

        row = attachment4.group(master.exams)["1142"][0]

        self.assertEqual(row["方式"], "")
        self.assertEqual(row["備註"], "")

    def test_attachment4_uses_main_template_semester_band(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "attachment4.docx"
            output = root / "output.docx"
            _attachment4_template(template)

            attachment4.generate(_master(), output, template=template)

            table = Document(output).tables[0]
            shading = table.rows[3].cells[0]._tc.tcPr.find(qn("w:shd"))
            self.assertEqual(shading.get(qn("w:fill")), "F6C5AC")

    def test_attachment4_title_year_follows_exam_term_and_keeps_formatting(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "attachment4.docx"
            output = root / "output.docx"
            _attachment4_template(template)
            # 正式範本轉檔後年度數字是獨立的 run：「114」「學年度學位考試一覽表」
            document = Document(template)
            paragraph = document.tables[0].rows[0].cells[0].paragraphs[0]
            paragraph.runs[0].text = "114"
            paragraph.add_run("學年度學位考試一覽表  軟工碩士班").bold = True
            document.save(template)
            master = _master()
            master.exams[0].學期 = "1151"

            attachment4.generate(master, output, template=template)

            title = Document(output).tables[0].rows[0].cells[0].paragraphs[0]
            self.assertEqual(title.text, "115學年度學位考試一覽表  軟工碩士班")
            self.assertTrue(title.runs[1].bold)

    def test_attachment4_normalizes_logical_borders_and_alignment_for_viewers(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "attachment4.docx"
            output = root / "output.docx"
            _attachment4_template(template)

            attachment4.generate(_master(), output, template=template)

            document = Document(output)
            xml_root = document.element
            self.assertEqual(xml_root.xpath(".//w:start"), [])
            self.assertEqual(xml_root.xpath(".//w:end"), [])
            self.assertGreater(len(xml_root.xpath(".//w:left")), 0)
            self.assertGreater(len(xml_root.xpath(".//w:right")), 0)
            for alignment in xml_root.xpath(".//w:jc"):
                self.assertNotIn(alignment.get(qn("w:val")), {"start", "end"})
            title_alignment = document.tables[0].rows[0].cells[0].paragraphs[0]
            self.assertEqual(
                title_alignment._p.pPr.find(qn("w:jc")).get(qn("w:val")),
                "right",
            )

    def test_attachment4_writes_table_level_grid_borders_for_google_docs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "attachment4.docx"
            output = root / "output.docx"
            _attachment4_template(template)

            attachment4.generate(_master(), output, template=template)

            table = Document(output).tables[0]
            borders = table._tbl.tblPr.find(qn("w:tblBorders"))
            self.assertIsNotNone(borders)
            for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
                border = borders.find(qn(f"w:{side}"))
                self.assertIsNotNone(border, side)
                self.assertEqual(border.get(qn("w:val")), "single")
                self.assertEqual(border.get(qn("w:sz")), "4")

    def test_attachment4_vertically_merges_adjacent_rows_for_same_advisor(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "attachment4.docx"
            output = root / "output.docx"
            _attachment4_template(template)
            master = _master_with_two_students_same_advisor_and_date()
            later_exam = copy.deepcopy(master.exams[0])
            later_exam.學號 = "SIM003"
            later_exam.姓名 = "第三位學生"
            later_exam.口試日期原文 = "9/2"
            later_exam.derive()
            master.exams.append(later_exam)

            attachment4.generate(master, output, template=template)

            table = Document(output).tables[0]
            first_merge = table.rows[4]._tr.tc_lst[0].tcPr.find(qn("w:vMerge"))
            second_merge = table.rows[5]._tr.tc_lst[0].tcPr.find(qn("w:vMerge"))
            self.assertEqual(first_merge.get(qn("w:val")), "restart")
            self.assertIsNone(second_merge.get(qn("w:val")))

    def test_attachment4_vertically_merges_adjacent_identical_dates(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "attachment4.docx"
            output = root / "output.docx"
            _attachment4_template(template)
            master = _master()
            second_exam = copy.deepcopy(master.exams[0])
            second_exam.學號 = "SIM002"
            second_exam.姓名 = "另一位學生"
            second_exam.指導教授 = "另一位教授"
            second_exam.derive()
            master.exams.append(second_exam)

            attachment4.generate(master, output, template=template)

            table = Document(output).tables[0]
            first_merge = table.rows[4]._tr.tc_lst[4].tcPr.find(qn("w:vMerge"))
            second_merge = table.rows[5]._tr.tc_lst[4].tcPr.find(qn("w:vMerge"))
            self.assertEqual(first_merge.get(qn("w:val")), "restart")
            self.assertIsNone(second_merge.get(qn("w:val")))

    def test_attachment6_single_case_group_resolves_committee(self):
        master = _master()
        groups = attachment6.roster_groups(master.exams)

        self.assertEqual(len(groups), 1)
        rows = attachment6.aggregate_group(master, next(iter(groups.values())))
        self.assertEqual(rows[0]["姓名"], "測試委員")
        self.assertEqual(rows[0]["帳號"], "TEST-ACCOUNT")
        self.assertEqual(rows[0]["合計"], 1120)

    def test_attachment6_groups_same_advisor_and_date_without_time(self):
        master = _master_with_two_students_same_advisor_and_date()

        groups = attachment6.roster_groups(master.exams)

        self.assertEqual(len(groups), 1)
        exams = next(iter(groups.values()))
        self.assertEqual([exam.案件鍵 for exam in exams], ["1142-SIM001", "1142-SIM002"])
        rows = attachment6.aggregate_group(master, exams)
        self.assertEqual(rows[0]["口試篇數"], 2)
        self.assertEqual(rows[0]["合計"], 2120)

    def test_attachment6_keeps_template_data_total_and_note_rows(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "attachment6.xlsx"
            output = root / "output.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.cell(11, 1).value = "總計："
            sheet.cell(15, 1).value = "說明："
            sheet.merge_cells("A15:M15")
            workbook.save(template)

            master = _master()
            rows = attachment6.aggregate_group(master, master.exams)
            attachment6.generate_one(rows, "1142", output, template=template)

            generated = load_workbook(output, data_only=False).active
            self.assertEqual(generated.cell(11, 1).value, "總計：")
            self.assertEqual(generated.cell(15, 1).value, "說明：")
            self.assertEqual(generated.cell(5, 1).value, "測試委員")
            self.assertTrue(all(generated.cell(row, 1).value is None for row in range(6, 11)))

    def test_attachment6_expands_only_when_more_than_six_members(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            template = root / "attachment6.xlsx"
            output = root / "output.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.cell(11, 1).value = "總計："
            sheet.cell(15, 1).value = "說明："
            sheet.merge_cells("A15:M15")
            workbook.save(template)
            rows = [
                {
                    "姓名": f"測試委員{number}",
                    "口試篇數": 1,
                    "口試費": 1000,
                    "交通費": 0,
                    "合計": 1000,
                }
                for number in range(1, 8)
            ]

            attachment6.generate_one(rows, "1142", output, template=template)

            generated = load_workbook(output, data_only=False).active
            self.assertEqual(generated.cell(12, 1).value, "總計：")
            self.assertEqual(generated.cell(16, 1).value, "說明：")
            self.assertEqual(generated.cell(11, 1).value, "測試委員7")
            self.assertTrue(generated.sheet_properties.pageSetUpPr.fitToPage)
            self.assertEqual(generated.page_setup.fitToWidth, 1)
            self.assertEqual(generated.page_setup.fitToHeight, 1)

    def test_generate_preflights_all_templates_without_partial_outputs(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "outputs"
            templates = TemplatePaths(
                attachment4=root / "附件4.doc",
                attachment6=root / "附件6.csv",
            )
            templates.attachment4.write_bytes(b"legacy Word template")

            with self.assertRaisesRegex(FileNotFoundError, "未產生任何檔案") as caught:
                generate_456(_master(), templates=templates, output_dir=output)

            self.assertNotIn("附件4=", str(caught.exception))
            self.assertFalse(output.exists())

    def test_legacy_doc_template_is_converted_in_an_isolated_temporary_folder(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "附件4-學位口試一覽.doc"
            source.write_bytes(b"legacy Word template")

            def fake_run(args, **_kwargs):
                if "--outdir" in args:
                    output_dir = Path(args[args.index("--outdir") + 1])
                    (output_dir / "input.docx").write_bytes(b"converted docx")
                return CompletedProcess(args=args, returncode=0, stdout="converted", stderr="")

            with patch.object(document_conversion.subprocess, "run", side_effect=fake_run):
                with document_conversion.docx_template(
                    source,
                    soffice=root / "soffice.com",
                ) as converted:
                    self.assertEqual(converted.suffix, ".docx")
                    self.assertEqual(converted.read_bytes(), b"converted docx")
                    self.assertNotEqual(converted.parent, source.parent)

                self.assertFalse(converted.exists())


if __name__ == "__main__":
    unittest.main()
