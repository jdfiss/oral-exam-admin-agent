"""來源指紋、主檔世代與不可變封存。"""
from __future__ import annotations
import hashlib
import json
import shutil
import uuid
from pathlib import Path
from . import clock, util as U


def digest(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def fingerprint(paths, directories=()):
    files = {str(Path(p).resolve()): digest(p) for p in paths if p is not None}
    for directory in directories:
        folder = Path(directory)
        for p in sorted(folder.glob("*.json")):
            files[str(p.resolve())] = digest(p)
    return files


def manifest_path(master_path):
    return Path(master_path).with_suffix(".sources.json")


def publish(master_path, sources, directories=()):
    data = {"generation": uuid.uuid4().hex, "at": clock.now().isoformat(),
            "sources": sources, "directories": [str(Path(p).resolve()) for p in directories],
            "master_hash": digest(master_path)}
    U.atomic_write_json(data, manifest_path(master_path))
    return data


def require_current(master_path, *, expected_generation=None, allow_legacy=True):
    path = manifest_path(master_path)
    if not path.is_file():
        if allow_legacy:
            return None
        raise ValueError("主檔缺來源世代，請重新建檔")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict) or not isinstance(data.get("sources"), dict)
                or not isinstance(data.get("generation"), str) or not data["generation"]
                or not isinstance(data.get("master_hash"), str)
                or not isinstance(data.get("directories", []), list)):
            raise ValueError("來源世代格式錯誤")
        actual = fingerprint(data["sources"], data.get("directories", ()))
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("主檔來源世代無法核對，請重新建檔") from exc
    if actual != data["sources"] or digest(master_path) != data["master_hash"]:
        raise ValueError("主檔已過期：來源有變動，請先成功重建再產檔或下載")
    if expected_generation is not None and data["generation"] != expected_generation:
        raise ValueError("文件屬於舊主檔世代，請重新產檔")
    return data


def freeze(destination, paths, *, detail=None):
    """在独立暫存資料夾完成複製與校驗，最後一次發布；不覆寫既有快照。"""
    destination = Path(destination)
    if destination.exists():
        return destination
    stage = destination.with_name(destination.name + ".pending-" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    try:
        for label, source in paths.items():
            if source is None:
                continue
            source = Path(source)
            if not source.exists():
                continue
            target = stage / label
            if source.is_dir():
                shutil.copytree(source, target, ignore=shutil.ignore_patterns("*.lock", "*.tmp", "archives", "freezes"))
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        hashes = {p.relative_to(stage).as_posix(): digest(p) for p in stage.rglob("*") if p.is_file()}
        U.atomic_write_json({"at": clock.now().isoformat(), "files": hashes, "detail": detail or {}}, stage / "manifest.json")
        stage.rename(destination)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return destination


def bind(master, master_path):
    master.source_master_path = str(Path(master_path).resolve())
    master.source_generation = None
    path = manifest_path(master_path)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            master.source_generation = data.get("generation") if isinstance(data, dict) else None
        except (OSError, ValueError):
            pass  # 舊主檔仍可唯讀查閱；正式操作由 require_current 拒絕。
    return master


def ensure_generation(master):
    path = getattr(master, "source_master_path", None)
    if path:
        if not getattr(master, "source_generation", None):
            raise ValueError("主檔世代未識別，請重新建檔")
        require_current(path, expected_generation=master.source_generation, allow_legacy=False)
