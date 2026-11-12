# -*- coding: utf-8 -*-
"""機構識別可設定：預設匿名、正式環境必填、畫面與附件6 標題取自設定。"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from koushi import config as C
from koushi.generators import attachment6
from koushi.web import app as webapp
from koushi.workflow import RegistryPaths

ROOT = Path(__file__).resolve().parent.parent
_PROBE = (
    "from koushi import config as C, util as U;"
    "print(C.UNIV_NAME, C.DEPT_NAME, C.OFFICE_SIGNATURE, C.HOME_DEPT_LABEL,"
    " '|'.join(C.HOME_DEPT_KEYS), '|'.join(C.HOME_UNIV_KEYS),"
    " U.is_home_dept('示範大學示範學系'), U.is_home_univ('示範大學數學系'),"
    " U.is_home_univ('國立範例大學資訊工程學系'))"
)


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("KOUSHI_") or k == "KOUSHI_SOFFICE"}
    env.update(extra, PYTHONIOENCODING="utf-8")
    return env


def test_defaults_are_anonymous_and_self_consistent():
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=_clean_env(),
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout.split()
    assert out == ["示範大學", "示範學系", "示範學系系辦", "本校示範學系",
                   "示範大學示範學系", "示範大學", "True", "True", "False"]


def test_env_overrides_keys_with_comma_list():
    env = _clean_env(KOUSHI_UNIV_NAME="國立範例大學", KOUSHI_DEPT_NAME="資訊工程學系",
                     KOUSHI_HOME_DEPT_KEYS="範例大學資訊工程, 範例大學資工",
                     KOUSHI_HOME_UNIV_KEYS="範例大學")
    out = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout.split()
    assert out == ["國立範例大學", "資訊工程學系", "資訊工程學系系辦", "本校資訊工程學系",
                   "範例大學資訊工程|範例大學資工", "範例大學", "False", "False", "True"]


def test_production_refuses_to_start_without_institution(tmp_path):
    env = _clean_env(KOUSHI_ENV="production", KOUSHI_DATA_ROOT=str(tmp_path / "data"),
                     KOUSHI_OUTPUT_ROOT=str(tmp_path / "out"))
    result = subprocess.run([sys.executable, "-c", "import koushi.config"], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0
    assert "正式環境必須設定機構識別" in result.stderr
    assert "KOUSHI_HOME_DEPT_KEYS" in result.stderr


def test_attachment6_title_uses_configured_university(monkeypatch):
    monkeypatch.setattr(C, "UNIV_NAME", "某某大學")
    assert attachment6.title_for("1142") == "某某大學   一一四學年度 第  二 學期 口試費印領清冊"


def test_login_page_shows_configured_names(tmp_path, monkeypatch):
    cfg = webapp.WebConfig(
        submissions_dir=tmp_path / "submissions", master_path=tmp_path / "master.xlsx",
        state_dir=tmp_path / "state",
        registries=RegistryPaths(attachment2=tmp_path / "att2.xlsx", members=tmp_path / "m.xlsx",
                                 distances=tmp_path / "d.csv"),
        doc_output_root=tmp_path / "docs", office_password="test-password",
    )
    monkeypatch.setitem(webapp.app.config, "_web", cfg)
    monkeypatch.setitem(webapp.app.config, "TESTING", True)
    monkeypatch.setattr(C, "UNIV_NAME", "某某大學")
    monkeypatch.setattr(C, "DEPT_NAME", "某某學系")
    with webapp.app.test_client() as client:
        page = client.get("/").get_data(as_text=True)
    assert "某某大學 某某學系" in page
    assert "範例大學" not in page


@pytest.mark.parametrize("extra, expected", [
    ({}, "示範"),
    ({"KOUSHI_UNIV_NAME": "國立範例大學"}, "範例"),
    ({"KOUSHI_UNIV_NAME": "國立範例大學", "KOUSHI_UNIV_SHORT_NAME": "範大"}, "範大"),
])
def test_univ_short_name_for_mileage_note(extra, expected):
    probe = "from koushi import config as C; print(C.UNIV_SHORT_NAME)"
    out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=_clean_env(**extra),
                         capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    assert out == expected
