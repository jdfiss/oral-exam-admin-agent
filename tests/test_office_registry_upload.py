# -*- coding: utf-8 -*-
"""系辦上傳三份必要檔案（附件2 Portal 匯出、交通費歷史資料、口試委員歷史資料）
與「上傳後自動建檔」的驗收。

2026-09-23：指導教授改由學生填寫，不再上傳師生對照表；附件2 是案件來源，
三份都備齊後才自動重建主檔。這裡只做「三檔都存在即視為備齊」的粗粒度判定，
不逐案核對內容是否涵蓋。
"""
import json

import openpyxl
import pytest

from koushi import config as C
from koushi.application import (
    load_auto_build_status, office_files_status, save_office_registry_file,
    auto_build_if_ready,
)
from koushi.workflow import RegistryPaths

from tests.att2_fixture import case, write_att2


def _registries(tmp_path):
    return RegistryPaths(
        attachment2=tmp_path / "office" / "附件2.xlsx",
        members=tmp_path / "office" / "委員個資.xlsx",
        distances=tmp_path / "office" / "距離表.csv",
        members_output=tmp_path / "output" / "data" / "委員個資.xlsx",
    )


def _make_distance_csv(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("服務單位,單程公里\n國立清華大學資訊工程學系,30\n", encoding="utf-8-sig")
    return path


def _make_att2(path, *student_ids):
    return write_att2(path, [case(student_id=sid) for sid in (student_ids or ("111900001",))])


def _make_member_xlsx(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["委員編號", "委員姓名", "英文姓名", "服務單位", "最高學歷學校", "最高學位",
               "身分證字號", "金融機構", "帳號", "戶籍地址", "Email", "最後更新", "來源"])
    ws.append(["C0001", "展示委員C", "", "國立範例大學資訊工程學系", "", "",
               "A123456789", "", "", "", "", "2026-09-17", "系辦上傳"])
    wb.save(path)
    return path


def _write_submission(tmp_path, name="a.json", **overrides):
    data = {
        "term": "1141",
        "student_id": "111900001",
        "thesis_title": "測試論文題目",
        "exam_date": "2026-06-01",
        "exam_mode": "",
        "advisors": ["展示委員C"],
        "committees": [
            {"name": "陳大文", "unit": "國立範例大學資訊工程學系", "title": "教授",
             "email": "dawen@example.org"},
        ],
    }
    data.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


# ── office_files_status ──────────────────────────────────

def test_office_files_status_false_until_all_three_present(tmp_path):
    registries = _registries(tmp_path)
    assert not office_files_status(registries).ready

    _make_distance_csv(registries.distances)
    assert not office_files_status(registries).ready

    _make_att2(registries.attachment2)
    assert not office_files_status(registries).ready

    _make_member_xlsx(registries.members)
    status = office_files_status(registries)
    assert status.distances and status.attachment2 and status.members
    assert status.ready


# ── save_office_registry_file ────────────────────────────

def test_save_office_registry_file_rejects_wrong_extension(tmp_path):
    registries = _registries(tmp_path)
    bad = tmp_path / "wrong.txt"
    bad.write_text("not a csv", encoding="utf-8")
    with pytest.raises(ValueError, match="格式錯誤"):
        save_office_registry_file("distances", bad, registries=registries)
    assert not registries.distances.exists()


def test_save_office_registry_file_rejects_empty_content(tmp_path):
    registries = _registries(tmp_path)
    empty_csv = tmp_path / "empty.csv"
    empty_csv.write_text("服務單位,單程公里\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match="沒有解析出任何資料列"):
        save_office_registry_file("distances", empty_csv, registries=registries)
    assert not registries.distances.exists()


def test_save_office_registry_file_atomically_replaces_target(tmp_path):
    registries = _registries(tmp_path)
    source = tmp_path / "upload.csv"
    source.write_text("服務單位,單程公里\n國立清華大學資訊工程學系,30\n", encoding="utf-8-sig")

    saved = save_office_registry_file("distances", source, registries=registries)
    assert saved == registries.distances
    assert registries.distances.is_file()
    # 來源檔本身不受影響、也沒有留下暫存檔
    assert source.is_file()
    assert list(registries.distances.parent.glob(".*")) == []


def test_save_attachment2_requires_portal_headers(tmp_path):
    registries = _registries(tmp_path)
    wrong = tmp_path / "wrong.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["學號", "姓名"])
    wb.active.append(["111900001", "王小明"])
    wb.save(wrong)
    with pytest.raises(ValueError, match="無法讀取"):
        save_office_registry_file("attachment2", wrong, registries=registries)
    assert not registries.attachment2.exists()

    saved = save_office_registry_file("attachment2", _make_att2(tmp_path / "ok.xlsx"),
                                      registries=registries)
    assert saved == registries.attachment2


def test_auto_build_only_creates_cases_from_attachment2(tmp_path):
    registries = _registries(tmp_path)
    _make_distance_csv(registries.distances)
    _make_att2(registries.attachment2, "111900001")
    _make_member_xlsx(registries.members)

    submissions_dir = tmp_path / "submissions"
    submissions_dir.mkdir()
    _write_submission(submissions_dir, name="a.json", student_id="111900001")
    _write_submission(submissions_dir, name="b.json", student_id="111900777")  # 附件2 沒有

    result = auto_build_if_ready(
        submissions_dir=submissions_dir, master_path=tmp_path / "output" / "master.xlsx",
        registries=registries,
    )
    assert result.status == "built"
    assert [e.案件鍵 for e in result.build.master.exams] == ["1141-111900001"]
    assert any(i["問題代碼"] == "SUBMISSION_WITHOUT_PORTAL_CASE" for i in result.build.issues)


# ── auto_build_if_ready ──────────────────────────────────

def test_auto_build_skips_when_office_files_missing(tmp_path):
    registries = _registries(tmp_path)
    submissions_dir = tmp_path / "submissions"
    submissions_dir.mkdir()
    _write_submission(submissions_dir)

    result = auto_build_if_ready(
        submissions_dir=submissions_dir,
        master_path=tmp_path / "output" / "master.xlsx",
        registries=registries,
    )
    assert result.status == "skipped"
    assert result.build is None
    assert not (tmp_path / "output" / "master.xlsx").exists()


def test_auto_build_runs_once_all_three_files_present(tmp_path):
    registries = _registries(tmp_path)
    _make_distance_csv(registries.distances)
    _make_att2(registries.attachment2)
    _make_member_xlsx(registries.members)

    submissions_dir = tmp_path / "submissions"
    submissions_dir.mkdir()
    _write_submission(submissions_dir)

    master_path = tmp_path / "output" / "master.xlsx"
    status_path = tmp_path / "output" / "auto_build_status.json"
    result = auto_build_if_ready(
        submissions_dir=submissions_dir, master_path=master_path,
        registries=registries, status_path=status_path,
    )
    assert result.status == "built"
    assert master_path.is_file()
    assert len(result.build.master.exams) == 1

    persisted = load_auto_build_status(status_path)
    assert persisted["status"] == "built"
    assert persisted["case_count"] == 1


def test_auto_build_failure_preserves_previous_master(tmp_path):
    registries = _registries(tmp_path)
    _make_distance_csv(registries.distances)
    _make_att2(registries.attachment2)
    _make_member_xlsx(registries.members)

    submissions_dir = tmp_path / "submissions"
    submissions_dir.mkdir()
    master_path = tmp_path / "output" / "master.xlsx"
    status_path = tmp_path / "output" / "auto_build_status.json"

    _write_submission(submissions_dir, name="a.json", student_id="111900001")
    first = auto_build_if_ready(
        submissions_dir=submissions_dir, master_path=master_path,
        registries=registries, status_path=status_path,
    )
    assert first.status == "built"
    first_mtime = master_path.stat().st_mtime

    # 兩筆表單解出同一案件鍵，屬於 FATAL_INGEST_CODES，build 會拋例外。
    _write_submission(submissions_dir, name="b.json", student_id="111900001")
    second = auto_build_if_ready(
        submissions_dir=submissions_dir, master_path=master_path,
        registries=registries, status_path=status_path,
    )
    assert second.status == "failed"
    assert second.error
    assert master_path.stat().st_mtime == first_mtime  # 舊主檔沒有被覆蓋或清空

    persisted = load_auto_build_status(status_path)
    assert persisted["status"] == "failed"
    assert persisted["error"]


def test_current_master_path_is_stable_and_contacts_live_in_state_dir():
    """review-2026-09-23 P0-5：網頁與每日自動化共用同一個固定主檔；通訊錄不隨日期換檔。"""
    from koushi.mail import recipients
    assert C.MASTER == C.CURRENT_MASTER
    assert "current" in C.MASTER.parts
    assert recipients.contacts_path().parent == C.STATE_DIR
