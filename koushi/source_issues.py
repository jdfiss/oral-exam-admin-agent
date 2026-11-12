"""沒有案件鍵的來源異常與逐筆處理證據。"""
import hashlib
import json
from pathlib import Path
from . import clock, util as U, audit_log


def key(issue):
    fields = {k: issue.get(k) for k in ("案件鍵", "問題代碼", "欄位", "說明", "來源")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def records(state_dir):
    path = Path(state_dir) / "source_issue_resolutions.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@U.locked_state()
def resolve(state_dir, *, issue, actor, reason):
    if not actor.strip() or not reason.strip():
        raise ValueError("來源異常處理須填處理人及處理證據／原因")
    data = records(state_dir)
    data[key(issue)] = {"actor": actor.strip(), "reason": reason.strip(), "at": clock.now().isoformat()}
    U.atomic_write_json(data, Path(state_dir) / "source_issue_resolutions.json")
    audit_log.record(Path(state_dir) / "audit_log.json", "source_issue", "resolved",
        tracking_key=(key(issue),), detail=data[key(issue)])


def unresolved(master, state_dir):
    done = records(state_dir)
    return [i for i in master.source_issues if not i.get("案件鍵") and key(i) not in done]
