# -*- coding: utf-8 -*-
"""匿名審查防護：版控內不得出現可辨識場域、師生或團隊的字串。

比賽初賽為匿名審查（競賽辦法），簡報、影片與繳交的程式碼都不得出現
學校、系所、指導教師或可辨識隊伍的資訊；版控也不得放含真實名單的 Office／PDF 檔。

禁用詞以 base64 存放，避免這支測試本身就把真實校名、人名寫進版控。新增禁用詞：
    python -c "import base64; print(base64.b64encode('詞'.encode()).decode())"
不想進版控的詞（例如其他真實人名）可放在專案根目錄的 .anonymity-denylist.txt（一行一個，
已在 .gitignore），或環境變數 KOUSHI_ANONYMITY_DENYLIST（逗號分隔）。

注意：這只檢查目前的版控內容。git 歷史裡早期 commit 仍有真實資料，清除與否由團隊決定。
"""
from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_ENCODED_DENYLIST = (
    '5Lit5aSu5aSn5a24', '5Lit5aSn6LOH5bel', '5Lit5aSu566h6Zmi', 'bmN1LmVkdS50dw==',
    'bmN1MjAyNg==', '5bel56iL5LqU6aSo', '5Lit5aOi', '6Zmz6Z2c5YSA', '6Z2c5YSA', '5by15ZiJ5oOg',
    'Y2hpYS1odWk=', '5a6l5r6E', 'ZWxlcGhhbnRA', 'NDIyNzE1MQ==', 'YTkyMTIxM2Y=',
    'ZXJpYzExMzQwMzIwMQ==', '546L5bCJ5Lu7', '5ZOI5YWD5rOw', '5piM5bu35a6H', '6JWt5a2Q55Gc',
)

# 刻意含有禁用詞、且不構成識別的檔案
EXEMPT_FILES = {
    "tests/test_anonymity.py",
    "tests/test_show_demo.py",      # 自帶「展示輸出不得出現真實校名」的檢查清單
    "koushi/affiliation_agent.py",  # 全台大學縮寫對照表（各校並列），不指向特定場域
}

# 版控不得出現的資料檔（真實名單多半是這些格式）
FORBIDDEN_SUFFIXES = {".xls", ".xlsx", ".xlsm", ".doc", ".docx", ".csv", ".pdf"}

# 繳交用簡報：PDF 由同目錄的 .pptx 轉出（不含名單），內容由 test_slides_have_no_identifying_strings 檢查 .pptx
SLIDES_PPTX = "docs/繳件/簡報/口試行政Agent_提案簡報.pptx"
DELIVERABLE_FILES = {"docs/繳件/簡報/口試行政Agent_提案簡報.pdf"}


def _denylist() -> list[str]:
    terms = [base64.b64decode(t).decode("utf-8") for t in _ENCODED_DENYLIST]
    terms += [t.strip() for t in os.environ.get("KOUSHI_ANONYMITY_DENYLIST", "").split(",") if t.strip()]
    local = ROOT / ".anonymity-denylist.txt"
    if local.is_file():
        terms += [line.strip() for line in local.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.startswith("#")]
    return [t.lower() for t in terms]


def _tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("不在 git 工作目錄內，無法列出版控檔案")
    return [p for p in out.decode("utf-8").split("\0") if p]


def test_no_identifying_strings_in_tracked_files():
    terms = _denylist()
    hits = []
    for rel in _tracked_files():
        if rel in EXEMPT_FILES:
            continue
        path = ROOT / rel
        if not path.is_file():
            continue      # 已暫存刪除但仍在索引中
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue      # 圖片等二進位檔
        text = data.decode("utf-8", errors="ignore").lower()
        for lineno, line in enumerate(text.splitlines(), 1):
            for term in terms:
                if term in line:
                    # 不印出禁用詞本身，免得測試報告又把它帶出去
                    hits.append(f"{rel}:{lineno}（第 {terms.index(term) + 1} 個禁用詞）")
    assert not hits, "版控內出現可辨識場域或師生的字串：\n" + "\n".join(hits)


def test_no_data_files_tracked():
    tracked = [p for p in _tracked_files()
               if Path(p).suffix.lower() in FORBIDDEN_SUFFIXES and p not in DELIVERABLE_FILES]
    assert not tracked, "Office／CSV／PDF 檔不得進版控（可能含真實名單）：" + "、".join(tracked)


def test_slides_have_no_identifying_strings():
    """簡報是二進位檔，上面的逐行掃描會略過；改讀 .pptx 內每頁投影片與講者備註的 XML。"""
    import zipfile
    path = ROOT / SLIDES_PPTX
    if SLIDES_PPTX not in _tracked_files() or not path.is_file():
        pytest.skip("簡報未進版控")
    terms = _denylist()
    hits = []
    with zipfile.ZipFile(path) as deck:
        for name in deck.namelist():
            if not name.endswith(".xml"):
                continue
            text = deck.read(name).decode("utf-8", errors="ignore").lower()
            hits += [f"{name}（第 {terms.index(t) + 1} 個禁用詞）" for t in terms if t in text]
    assert not hits, "簡報出現可辨識場域或師生的字串：\n" + "\n".join(hits)
