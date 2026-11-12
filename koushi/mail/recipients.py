# -*- coding: utf-8 -*-
"""收件人解析 —— 把案件／指導教授對應到 email。

學生與指導教授 email 取自學生填寫（案件層級）；系辦通訊錄只作後備。
查不到一律回 None，由呼叫端標記 no_recipient 保留給系辦人工處理，
絕不臆造收件地址。

通訊錄格式（outputs/contacts.json）：

    {
      "students": {"1142-1115020": "s1115020@cc.example.edu"},
      "advisors": {"王大明": "dmwang@dept.example.edu"},
      "office": "office@example.edu"
    }

outputs/ 已在 .gitignore 內；通訊錄含個資，不得移出該目錄。
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import config as C

CONTACTS_FILENAME = "contacts.json"

_EMPTY: dict = {"students": {}, "advisors": {}, "office": None}


def contacts_path(output_dir: str | Path | None = None) -> Path:
    """通訊錄路徑。預設放在跨批次的狀態資料夾，不隨日期換檔（系辦維護一次即可）。"""
    return Path(output_dir or C.STATE_DIR) / CONTACTS_FILENAME


def load_contacts(path: str | Path | None = None) -> dict:
    """讀取通訊錄。檔案不存在時回空通訊錄，不視為錯誤。

    介面層尚未上線前，這個檔案通常不存在；此時所有草稿都會是
    no_recipient，預覽仍可正常運作。
    """
    target = Path(path) if path else contacts_path()
    if not target.is_file():
        return dict(_EMPTY)

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"通訊錄格式錯誤，未寄出任何信件：{target}（{exc}）") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"通訊錄最外層必須是物件：{target}")

    return {
        "students": dict(raw.get("students") or {}),
        "advisors": dict(raw.get("advisors") or {}),
        "office": raw.get("office") or None,
    }


def _clean(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def resolve_student(case_key: str, contacts: dict | None = None) -> str | None:
    """以案件鍵查學生 email。查不到回 None。"""
    contacts = _EMPTY if contacts is None else contacts
    return _clean(contacts.get("students", {}).get(str(case_key)))


def resolve_advisor(
    advisor_name: str, contacts: dict | None = None, *, case_keys=(),
) -> str | None:
    """以指導教授姓名查 email。查不到回 None。

    優先用學生在該清冊案件中填的教授 E-mail（`case_advisors`，依案件鍵排序取第一個
    查得到的）；都沒有才查系辦維護的通訊錄 `advisors`。
    """
    contacts = _EMPTY if contacts is None else contacts
    case_advisors = contacts.get("case_advisors") or {}
    # 共同指導的清冊（如附件6）以「、」串接多位教授；取第一位查得到信箱的。
    for name in str(advisor_name or "").split("、"):
        name = name.strip()
        for key in sorted(case_keys or ()):
            address = _clean((case_advisors.get(key) or {}).get(name))
            if address:
                return address
        address = _clean(contacts.get("advisors", {}).get(name))
        if address:
            return address
    return None


def resolve_committees(case_keys, contacts: dict | None = None) -> str | None:
    """這些案件所有口試委員的 email（去重、依出現順序），以「, 」串接；一位都查不到回 None。"""
    contacts = _EMPTY if contacts is None else contacts
    found: list[str] = []
    for key in sorted(case_keys or ()):
        for address in (contacts.get("case_committees") or {}).get(key) or ():
            address = _clean(address)
            if address and address not in found:
                found.append(address)
    return ", ".join(found) or None


def resolve_office(contacts: dict | None = None) -> str | None:
    """系辦固定信箱。未設定回 None。"""
    contacts = _EMPTY if contacts is None else contacts
    return _clean(contacts.get("office"))


def missing_students(case_keys, contacts: dict | None = None) -> list[str]:
    """回傳通訊錄中查無 email 的案件鍵，供系辦人工補齊。"""
    return [k for k in case_keys if resolve_student(k, contacts) is None]
