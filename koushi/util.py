# -*- coding: utf-8 -*-
"""共用小工具：姓名／日期／學期／班別的正規化。"""
import contextlib
import functools
import inspect
import re
import unicodedata
import datetime as _dt
import os
import tempfile
import threading
import time
from pathlib import Path
from . import config as C


def _atomic_replace(destination: Path, write_fn):
    """在暫存檔完成寫入後原子替換，避免中斷留下半份檔案。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    fd_consumed = False
    try:
        write_fn(descriptor, temporary_name)
        fd_consumed = True
        _replace_with_retry(temporary_name, destination)
    except BaseException:
        if not fd_consumed:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return destination


def _replace_with_retry(source: str, destination: Path, *, attempts: int = 50) -> None:
    """Windows 上目標檔正被其他執行緒／程序讀取時 os.replace 會短暫拋 PermissionError，稍候重試。"""
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02)


def literal_cell(cell, value):
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"
    return cell


def atomic_save_workbook(workbook, path, *, literal_strings=False):
    """在目標目錄完成寫入後再原子替換，避免中斷留下半個 Excel。"""
    if literal_strings:
        for sheet in workbook:
            for row in sheet:
                for cell in row:
                    if isinstance(cell.value, str):
                        cell.data_type = "s"
    def write_fn(fd, tmp_name):
        os.close(fd)
        workbook.save(Path(tmp_name))
    return _atomic_replace(Path(path), write_fn)


def atomic_write_json(data: dict, path) -> Path:
    """原子寫入 JSON，供所有跨次執行的追蹤狀態檔使用。"""
    import json
    def write_fn(fd, _tmp_name):
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return _atomic_replace(Path(path), write_fn)


class StateDataError(ValueError):
    """持久狀態不可讀；必須停止操作，不能改用空狀態重新寄信。"""


def read_json_state(path):
    import json
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("頂層必須是物件")
        return raw
    except (OSError, ValueError) as exc:
        raise StateDataError(f"狀態檔 {path.name} 無法核對，操作已停止；請從已校驗備份恢復，勿刪檔後重寄") from exc


class BuildLockTimeout(OSError):
    """無法在時限內取得建檔鎖。"""


@contextlib.contextmanager
def build_lock(master_path: str | Path, *, timeout: float = 30):
    """取得主檔寫入鎖，保護讀→算→寫的完整區段。

    鎖檔放在主檔旁邊（.master.lock），用 OS 層級的排他鎖：
    Windows 用 msvcrt.locking，POSIX 用 fcntl.flock。
    """
    with _file_lock(Path(master_path).parent / ".master.lock", timeout=timeout, label="建檔鎖"):
        yield


@contextlib.contextmanager
def _file_lock(lock_path: Path, *, timeout: float, label: str):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                _os_lock(fd)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise BuildLockTimeout(
                        f"無法在 {timeout}s 內取得{label}：{lock_path}"
                    )
                time.sleep(0.1)
        yield
    finally:
        if acquired:
            _os_unlock(fd)
        os.close(fd)


_STATE_LOCKS: dict[str, threading.RLock] = {}
_STATE_LOCKS_GUARD = threading.Lock()
_STATE_LOCK_DEPTH = threading.local()


@contextlib.contextmanager
def state_lock(state_dir: str | Path, *, timeout: float = 120):
    """追蹤狀態目錄的寫入鎖，保護「讀取→修改→寫回」整段，避免併行更新互相覆蓋。

    atomic_write_json 只保證不寫出半個檔案；兩個請求同時讀舊版、各自改完再
    寫回，後寫的仍會吃掉先寫的。所有會改狀態檔（稽核、放行、寄送記錄、缺件
    追蹤……）的流程都要包在這把鎖裡。

    可重入：同一執行緒巢狀取得不會自鎖。跨執行緒用 RLock、跨程序（網頁與
    每日排程）用鎖檔 .state.lock 的 OS 排他鎖。
    """
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    key = os.path.normcase(str(directory.resolve()))
    with _STATE_LOCKS_GUARD:
        rlock = _STATE_LOCKS.setdefault(key, threading.RLock())
    if not rlock.acquire(timeout=timeout):
        raise BuildLockTimeout(f"無法在 {timeout}s 內取得狀態鎖：{directory}")
    depths = _STATE_LOCK_DEPTH.__dict__.setdefault("depths", {})
    try:
        if depths.get(key, 0) == 0:
            with _file_lock(directory / ".state.lock", timeout=timeout, label="狀態鎖"):
                depths[key] = 1
                try:
                    yield
                finally:
                    depths[key] = 0
        else:
            depths[key] += 1
            try:
                yield
            finally:
                depths[key] -= 1
    finally:
        rlock.release()


def locked_state(param: str = "state_dir"):
    """裝飾器：整個函式在 state_lock(該參數指到的狀態目錄) 內執行。"""
    def decorator(fn):
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            state_dir = signature.bind_partial(*args, **kwargs).arguments[param]
            with state_lock(state_dir):
                return fn(*args, **kwargs)
        return wrapper
    return decorator


def _os_lock(fd: int) -> None:
    """排他鎖（非阻塞），拿不到就拋 OSError。"""
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _os_unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


# 異體字：Portal 與人工輸入常混用，若不統一，同一位委員會在附件6
# 被拆成兩列、口試費重複計。只放「確定同字」的對應，形近字不放。
_VARIANTS = {
    "温": "溫", "峯": "峰", "堃": "坤", "裏": "裡",
    "户": "戶", "羣": "群", "衆": "眾", "喫": "吃",
}


def canon_name(s):
    """姓名比對用的正規化形式（全形轉半形、去空白、異體字統一）。"""
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).strip()
    s = re.sub(r"\s+", "", s)
    return "".join(_VARIANTS.get(c, c) for c in s)

_PAREN = re.compile(r"[（(].*?[）)]")
_SPLIT = re.compile(r"[、,，/／\s\u3000]+")


def clean_name(raw):
    """'王小明(WANG, HSIAO-MING)' -> ('王小明', 'WANG, HSIAO-MING')

    附件2 把中英文姓名塞在同一欄，括號可能沒關（例：'陳大文)'）。
    """
    if raw is None:
        return "", ""
    s = str(raw).strip()
    m = re.match(r"^([^（(]+)[（(](.*?)[）)]?$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s.rstrip(")）").strip(), ""


def split_names(cell):
    """把 '王小明、李小華' 或含換行的儲存格拆成姓名 list。"""
    if not cell:
        return []
    out = []
    for x in _SPLIT.split(str(cell).replace("\n", " ")):
        x = x.strip().lstrip("*").strip()
        if x and not x[0].isdigit():
            out.append(x)
    return out


def norm_unit(unit):
    """服務單位精確比對形式：Unicode 正規化並移除空白，不作模糊合併。"""
    if unit is None:
        return ""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(unit))).strip()


def is_home_dept(unit):
    u = norm_unit(unit).replace("國立", "")
    return any(k.replace("國立", "") in u for k in C.HOME_DEPT_KEYS)


def is_fee_exempt_unit(unit):
    """免撥帳資料與交通費的服務單位：本系；若設定開啟，也包含本校他系。"""
    return is_home_dept(unit) or (C.HOME_UNIV_FEE_EXEMPT and is_home_univ(unit))


def is_home_univ(unit):
    u = norm_unit(unit).replace("國立", "")
    return any(k.replace("國立", "") in u for k in C.HOME_UNIV_KEYS)


def campus_side(unit):
    return "校內" if is_home_univ(unit) else "校外"


def display_unit(unit):
    """附件6 的服務單位寫法：本系委員一律寫 config.HOME_DEPT_LABEL（如『本校○○學系』）。"""
    return C.HOME_DEPT_LABEL if is_home_dept(unit) else str(unit or "").strip()


def degree_of(dept):
    """系所字串 -> 學位別 博／碩"""
    return "博" if "博士" in str(dept or "") else "碩"


def program_of(dept):
    """系所字串 -> 附件4 的欄別（博士班／碩士一般／碩士專班）"""
    d = str(dept or "")
    if "博士" in d:
        return C.PROGRAM_DOCTOR
    if any(k in d for k in C.INSERV_KEYS):
        return C.PROGRAM_INSERV
    return C.PROGRAM_MASTER


def case_key(term, sid):
    return "%s-%s" % (term, sid)


def parse_date(v):
    """接受 datetime／'2026-06-18'／'6/18'／'?'／'待定'，回傳 (date or None, 原字串)。"""
    if v is None or str(v).strip() == "":
        return None, ""
    if isinstance(v, (_dt.datetime, _dt.date)):
        d = v.date() if isinstance(v, _dt.datetime) else v
        return d, "%d/%d" % (d.month, d.day)
    s = str(v).strip()
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        d = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return d, "%d/%d" % (d.month, d.day)
    m = re.match(r"^(\d{1,2})\s*/\s*(\d{1,2})$", s)
    if m:
        return None, "%s/%s" % (m.group(1), m.group(2))
    return None, s          # '?'、'待定'、'7/9.7/20' 等原樣保留


def term_half(term):
    """學期碼 1142 -> '下學期'；1141 -> '上學期'"""
    t = str(term).strip()
    return "下學期" if t.endswith("2") else "上學期"


def term_year_label(term):
    """1142 -> '114學年度下學期'"""
    t = str(term).strip()
    return "%s學年度%s" % (t[:3], term_half(t))


def as_int(v, default=0):
    try:
        if v is None or str(v).strip() == "":
            return default
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return default


def as_float(v, default=None):
    try:
        if v is None or str(v).strip() == "":
            return default
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def blank(v):
    return v is None or str(v).strip() == ""
