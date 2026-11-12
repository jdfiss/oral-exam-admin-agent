"""一致性備份、校驗及隔離還原；金鑰與儲存保護依校方核准方案另行保管。"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from koushi import config as C, util as U, clock
PREFIX="koushi-backup-"


def _files(sources):
    result={}
    for label,folder in sources.items():
        folder=Path(folder)
        if not folder.is_dir():
            raise ValueError(f"備份來源不存在：{label}")
        found={label+"/"+p.relative_to(folder).as_posix():p for p in folder.rglob("*")
               if p.is_file() and not p.name.startswith("~$") and not p.name.endswith((".lock",".tmp"))}
        if not found:
            raise ValueError(f"備份來源為空：{label}")
        if any(not p.resolve().is_relative_to(folder.resolve()) for p in found.values()):
            raise ValueError("備份來源包含目錄外的連結")
        result.update(found)
    return result


def verify(path):
    with zipfile.ZipFile(path) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError("備份包含重複檔名")
        bad=archive.testzip()
        if bad:
            raise ValueError("備份CRC校驗失敗")
        manifest=json.loads(archive.read("manifest.json"))
        if not manifest.get("files") or set(archive.namelist()) != set(manifest["files"])|{"manifest.json"}:
            raise ValueError("備份清單不完整")
        for name,expected in manifest["files"].items():
            normalized=Path(name)
            if normalized.is_absolute() or ".." in normalized.parts or "\\" in name or ":" in name:
                raise ValueError("備份路徑不安全")
            if hashlib.sha256(archive.read(name)).hexdigest()!=expected:
                raise ValueError("備份內容校驗失敗")
        return manifest


def backup(dest:Path,keep_days:int,now:dt.datetime|None=None,*,sources=None)->Path:
    if type(keep_days) is not int or not 1<=keep_days<=3650:
        raise ValueError("備份保留天數須為1至3650")
    sources={"inputs":C.INPUT_DIR,"outputs":C.OUTPUT_ROOT,"templates":C.TPL_DIR} if sources is None else sources
    if not sources:
        raise ValueError("沒有備份來源")
    dest=Path(dest).resolve()
    if any(dest.is_relative_to(Path(p).resolve()) for p in sources.values()):
        raise ValueError("備份目的地不可位於資料來源內")
    now=now or clock.now()
    dest.mkdir(parents=True,exist_ok=True)
    target=dest/f"{PREFIX}{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.zip"
    descriptor,temporary=tempfile.mkstemp(prefix=".backup-",suffix=".tmp",dir=dest)
    os.close(descriptor)
    try:
        with U.state_lock(C.STATE_DIR),U.state_lock(C.SRC_SUBMISSIONS_DIR),U.state_lock(C.MASTER.parent):
            files=_files(sources)
            hashes={}
            with zipfile.ZipFile(temporary,"w",compression=zipfile.ZIP_DEFLATED) as archive:
                for name,path in sorted(files.items()):
                    content=path.read_bytes()
                    hashes[name]=hashlib.sha256(content).hexdigest()
                    archive.writestr(name,content)
                manifest={"at":now.isoformat(),"files":hashes,"python":platform.python_version(),
                          "program_hashes":{p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in [*ROOT.glob("requirements*.txt"),*ROOT.joinpath("koushi").rglob("*.py")]}}
                archive.writestr("manifest.json",json.dumps(manifest,ensure_ascii=False))
            if set(_files(sources))!=set(files) or any(hashlib.sha256(p.read_bytes()).hexdigest()!=hashes[n] for n,p in files.items()):
                raise ValueError("備份期間來源變動，未發布本次備份")
            verify(temporary)
            os.replace(temporary,target)
        cutoff=now.timestamp()-keep_days*86400
        for old in dest.glob(f"{PREFIX}*.zip"):
            if old!=target and old.stat().st_mtime<cutoff:
                old.unlink()
        U.atomic_write_json({"ok":True,"at":clock.now().isoformat(),"files":len(hashes),"archive":target.name},
                            C.STATE_DIR/"backup_status.json")
        return target
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def restore(path:Path,destination:Path)->Path:
    manifest=verify(path)
    destination=Path(destination).resolve()
    if destination.exists():
        raise ValueError("還原目的地必須尚不存在，禁止覆寫現有資料")
    destination.parent.mkdir(parents=True,exist_ok=True)
    stage=destination.with_name(destination.name+".pending-"+uuid.uuid4().hex)
    stage.mkdir()
    try:
        with zipfile.ZipFile(path) as archive:
            for name in manifest["files"]:
                target=stage/name
                if not target.resolve().is_relative_to(stage.resolve()):
                    raise ValueError("還原路徑不安全")
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(archive.read(name))
        U.atomic_write_json(manifest,stage/"manifest.json")
        stage.rename(destination)
    except BaseException:
        if stage.parent==destination.parent:
            shutil.rmtree(stage)
        raise
    return destination


def main(argv=None):
    parser=argparse.ArgumentParser(description="一致性備份或隔離還原")
    parser.add_argument("--dest",required=True)
    parser.add_argument("--keep-days",type=int,default=30)
    parser.add_argument("--restore",type=Path)
    args=parser.parse_args(argv)
    try:
        if args.restore:
            print("已隔離還原：",restore(args.restore,Path(args.dest)))
        else:
            if os.environ.get("KOUSHI_ENV")!="production":
                raise ValueError("正式備份請透過 deploy/run-backup.ps1 載入正式目錄配置")
            required=[C.MASTER,C.PORTAL_ATT2,C.REG_MEMBER,C.DISTANCE_CSV,C.STATE_DIR/"settings.json",C.TPL_ATT4,C.TPL_ATT6]
            if any(not p.is_file() for p in required):
                raise ValueError("正式備份缺少必要來源、主檔或設定")
            print("已備份：",backup(Path(args.dest),args.keep_days))
    except Exception as exc:
        U.atomic_write_json({"ok":False,"at":clock.now().isoformat(),"error":str(exc)},C.STATE_DIR/"backup_status.json")
        print("備份／還原失敗：",str(exc),file=sys.stderr)
        return 1
    return 0

if __name__=="__main__":
    sys.exit(main())
