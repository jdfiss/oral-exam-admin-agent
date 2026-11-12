"""不接觸正式資料的備份與還原驗證。"""
import importlib.util
from pathlib import Path
import pytest

@pytest.fixture
def backup_module(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[1]/"deploy"/"backup_state.py"
    spec=importlib.util.spec_from_file_location("delivery_backup",path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.C,"STATE_DIR",tmp_path/"outputs"/"state")
    monkeypatch.setattr(module.C,"SRC_SUBMISSIONS_DIR",tmp_path/"inputs"/"submissions")
    monkeypatch.setattr(module.C,"MASTER",tmp_path/"outputs"/"current"/"master.xlsx")
    return module


def sources(tmp_path):
    result={"inputs":tmp_path/"inputs","outputs":tmp_path/"outputs"}
    for label,folder in result.items():
        folder.mkdir(parents=True,exist_ok=True)
        (folder/(label+".json")).write_text('{"version":2}',encoding="utf-8")
    (result["outputs"]/"web").mkdir()
    (result["outputs"]/"web"/"attachment.xlsx").write_bytes(b"document bytes")
    return result


def test_backup_roundtrip_includes_documents_and_hashes(backup_module,tmp_path):
    data=sources(tmp_path)
    archive=backup_module.backup(tmp_path/"backups",30,sources=data)
    manifest=backup_module.verify(archive)
    assert "outputs/web/attachment.xlsx" in manifest["files"]
    target=backup_module.restore(archive,tmp_path/"restored")
    for name in manifest["files"]:
        label,relative=name.split("/",1)
        assert (target/name).read_bytes()==(data[label]/relative).read_bytes()
    with pytest.raises(ValueError,match="禁止覆寫"):
        backup_module.restore(archive,target)


@pytest.mark.parametrize("days",[0,-1,3651])
def test_invalid_retention_rejected(backup_module,tmp_path,days):
    with pytest.raises(ValueError):
        backup_module.backup(tmp_path/"backups",days,sources={})


def test_missing_or_empty_source_never_publishes(backup_module,tmp_path):
    with pytest.raises(ValueError):
        backup_module.backup(tmp_path/"backups",30,sources={"inputs":tmp_path/"missing"})
    empty=tmp_path/"empty";empty.mkdir()
    with pytest.raises(ValueError):
        backup_module.backup(tmp_path/"backups",30,sources={"inputs":empty})
    assert not list((tmp_path/"backups").glob("*.zip"))
    assert not list((tmp_path/"backups").glob("*.tmp"))


def test_partial_write_failure_preserves_previous_backup(backup_module,tmp_path,monkeypatch):
    data=sources(tmp_path)
    archive=backup_module.backup(tmp_path/"backups",30,sources=data)
    old=archive.read_bytes()
    def fail(*args,**kwargs):raise OSError("disk full")
    monkeypatch.setattr(backup_module.zipfile.ZipFile,"writestr",fail)
    with pytest.raises(OSError,match="disk full"):
        backup_module.backup(tmp_path/"backups",30,sources=data)
    assert archive.read_bytes()==old and len(list((tmp_path/"backups").glob("*.zip")))==1
    assert not list((tmp_path/"backups").glob("*.tmp"))
