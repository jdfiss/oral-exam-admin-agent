# -*- coding: utf-8 -*-
"""引擎狀態持久化 —— JSON 檔讀寫，跨次執行保留追蹤紀錄。

狀態檔位於輸出目錄下 engines_state.json，結構：
{
    "deficiency": [DeficiencyItem dict, ...],
    "confirmation": [ConfirmationRequest dict, ...],
    "collection": [CollectionItem dict, ...]
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .. import util as U

from .deficiency import (
    DeficiencyItem,
    item_from_dict as deficiency_from_dict,
    item_to_dict as deficiency_to_dict,
)
from .confirmation import (
    ConfirmationRequest,
    request_from_dict as confirmation_from_dict,
    request_to_dict as confirmation_to_dict,
)
from .collection import (
    CollectionItem,
    item_from_dict as collection_from_dict,
    item_to_dict as collection_to_dict,
)


@dataclass
class EngineState:
    """三個引擎的持久化狀態。"""
    deficiency: list[DeficiencyItem]
    confirmation: list[ConfirmationRequest]
    collection: list[CollectionItem]


def _default_path(output_dir: Path) -> Path:
    return output_dir / "engines_state.json"


def load(path: Path) -> EngineState:
    """從 JSON 載入狀態。檔案不存在時回傳空狀態。"""
    if not path.is_file():
        return EngineState(deficiency=[], confirmation=[], collection=[])
    raw = U.read_json_state(path)
    try:
        if any(not isinstance(raw.get(key, []), list) for key in ("deficiency", "confirmation", "collection")):
            raise ValueError("引擎清單格式錯誤")
        return EngineState(
            deficiency=[deficiency_from_dict(d) for d in raw.get("deficiency", [])],
            confirmation=[confirmation_from_dict(d) for d in raw.get("confirmation", [])],
            collection=[collection_from_dict(d) for d in raw.get("collection", [])],
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise U.StateDataError("引擎狀態格式不相容，操作已停止，請核對備份與程式版本") from exc


def save(state: EngineState, path: Path) -> Path:
    """寫入 JSON。回傳寫入路徑。"""
    raw = {
        "deficiency": [deficiency_to_dict(i) for i in state.deficiency],
        "confirmation": [confirmation_to_dict(r) for r in state.confirmation],
        "collection": [collection_to_dict(i) for i in state.collection],
    }
    return U.atomic_write_json(raw, path)


def load_from_dir(output_dir: Path) -> EngineState:
    return load(_default_path(output_dir))


def save_to_dir(state: EngineState, output_dir: Path) -> Path:
    return save(state, _default_path(output_dir))
