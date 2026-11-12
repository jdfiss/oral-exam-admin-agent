# -*- coding: utf-8 -*-
"""上線前自我檢查：逐項列出部署環境還缺什麼，不修改任何資料。

用法（在專案根目錄）：python deploy/preflight.py
結束代碼 0＝必要項目都齊全；1＝有必要項目缺漏。只列出設定「有沒有」，不印出密碼。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from koushi import config as C  # noqa: E402

REQUIRED_ENV = ("KOUSHI_WEB_SECRET_KEY", "KOUSHI_OFFICE_PASSWORD", "KOUSHI_PUBLIC_URL",
                "MAIL_SENDER", "MAIL_PASSWORD", *C.INSTITUTION_ENV)
PACKAGES = ("openpyxl", "docx", "xlrd", "flask", "waitress")


def _dotenv() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


def checks() -> list[tuple[bool, bool, str, str]]:
    """(通過, 必要, 項目, 說明)"""
    from koushi import application, document_conversion, settings as office_settings

    env = {**{k: v for k, v in os.environ.items() if k.startswith(("KOUSHI_", "MAIL_"))}, **_dotenv()}
    out = []
    for name in PACKAGES:
        ok = importlib.util.find_spec(name) is not None
        out.append((ok, True, f"Python 套件 {name}", "" if ok else "執行 pip install -r requirements.txt"))
    for key in REQUIRED_ENV:
        ok = bool(env.get(key))
        out.append((ok, True, f"設定 {key}", "" if ok else "填進專案根目錄 .env（範本見 deploy/.env.example）"))
    url = env.get("KOUSHI_PUBLIC_URL", "")
    out.append((url.startswith("https://"), True, "對外網址使用 HTTPS",
                "" if url.startswith("https://") else "委員專屬連結會帶個資，KOUSHI_PUBLIC_URL 必須是 https://"))
    for label, path in (("附件4 正式範本", C.TPL_ATT4), ("附件6 正式範本", C.TPL_ATT6)):
        ok = path.is_file()
        out.append((ok, True, label, "" if ok else f"放到 {path.relative_to(ROOT)}（向系辦索取空白範本）"))
    soffice = document_conversion.find_soffice()
    out.append((soffice is not None, True, "LibreOffice（附件4 .doc 轉檔）",
                str(soffice) if soffice else "安裝 LibreOffice，或在 .env 設 KOUSHI_SOFFICE"))
    files = application.office_files_status()
    out.append((files.ready, False, "系辦三份建檔資料",
                "已上傳" if files.ready else "上線後由系辦在「匯入資料」頁上傳"))
    settings = office_settings.load(C.STATE_DIR)
    out.append((settings.student_open, False, "本期學期與填寫截止日",
                "已設定" if settings.student_open else "上線後由系辦在「期限設定」頁設定"))
    status = application.load_automation_status(C.STATE_DIR)
    if status is None:
        out.append((False, False, "每日自動化", "尚未執行過；註冊排程後先手動跑一次"))
    else:
        out.append((bool(status.get("ok")), False, "每日自動化",
                    f"最近一次 {status.get('at')}" + ("" if status.get("ok") else f" 失敗：{status.get('error')}")))
    return out


def main() -> int:
    results = checks()
    width = max(len(item) for _, _, item, _ in results)
    for ok, required, item, note in results:
        mark = "OK  " if ok else ("缺  " if required else "待辦")
        print(f"[{mark}] {item.ljust(width)}  {note}")
    missing = [item for ok, required, item, _ in results if required and not ok]
    print()
    print("必要項目都齊全。" if not missing else f"還缺 {len(missing)} 項必要設定：{'、'.join(missing)}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
