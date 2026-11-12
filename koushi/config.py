# -*- coding: utf-8 -*-
"""集中管理所有可調參數。

正式環境（KOUSHI_ENV=production）必須另外設定 KOUSHI_DATA_ROOT、KOUSHI_OUTPUT_ROOT
與機構識別（見 deploy/），否則拒絕啟動。
"""
import datetime as _dt
import os
from . import clock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESOURCE_DIR = ROOT / "resource"
INPUT_DIR = Path(os.environ.get("KOUSHI_DATA_ROOT") or RESOURCE_DIR / "mockdata")
DATA_DIR = INPUT_DIR / "data"
TPL_DIR = RESOURCE_DIR / "template"
OUTPUT_ROOT = Path(os.environ.get("KOUSHI_OUTPUT_ROOT") or ROOT / "outputs")
if os.environ.get("KOUSHI_ENV") == "production":
    if (not os.environ.get("KOUSHI_DATA_ROOT") or not os.environ.get("KOUSHI_OUTPUT_ROOT")
            or not INPUT_DIR.is_absolute() or not OUTPUT_ROOT.is_absolute()):
        raise ValueError("正式環境必須設定獨立的絕對資料與輸出目錄")
    if os.environ.get("KOUSHI_DEMO_ROOT"):
        raise ValueError("正式環境不得混用示範資料配置")
    mock = (RESOURCE_DIR / "mockdata").resolve()
    if any(p.resolve().is_relative_to(mock) for p in (INPUT_DIR, OUTPUT_ROOT)):
        raise ValueError("正式環境不得使用 mockdata")
    if INPUT_DIR.resolve().is_relative_to(OUTPUT_ROOT.resolve()) or OUTPUT_ROOT.resolve().is_relative_to(INPUT_DIR.resolve()):
        raise ValueError("正式來源與輸出目錄必須分開")
STATE_DIR = OUTPUT_ROOT / "_state"


def output_date_tag(day: _dt.date | None = None) -> str:
    """回傳批次日期；使用 ISO 格式，檔名與資料夾都能直接排序。"""
    return (day or clock.today()).isoformat()


def output_dir(day: _dt.date | None = None) -> Path:
    return OUTPUT_ROOT / output_date_tag(day)


def master_path(day: _dt.date | None = None) -> Path:
    tag = output_date_tag(day)
    return output_dir(day) / f"口試主檔-{tag}.xlsx"


# 目前主檔：網頁與每日自動化共用同一個固定路徑，不隨日期換檔（review-2026-09-23 P0-5）。
# 每次建檔另存一份當日快照到 outputs/<日期>/ 供追溯。
CURRENT_DIR = OUTPUT_ROOT / "current"
CURRENT_MASTER = CURRENT_DIR / "口試主檔.xlsx"

OUT_DIR = output_dir()
OUTPUT_DATA_DIR = OUT_DIR / "data"

# ── 來源檔（附件 2；附件1 已併入附件2，不再單獨輸入）──────────
SRC_ATT2 = INPUT_DIR / "附件2-系所審核名單(委員資料).xlsx"
SRC_ATT3_DIR = INPUT_DIR / "附件3"     # 附件3 Excel（歷史資料／人工匯入用，非目前主流程）

# ── 附件3等效資料來源：學生介面填寫，送出即有效（2026-09-23 取消系辦審核）────
SRC_SUBMISSIONS_DIR = INPUT_DIR / "submissions"

# ── 主檔與對照表 ─────────────────────────────────────
MASTER = CURRENT_MASTER
REG_MEMBER = DATA_DIR / "委員個資.xlsx"      # 輸入快照；建檔過程不覆寫
PORTAL_ATT2 = DATA_DIR / "附件2-Portal匯出.xlsx"  # 系辦於填寫截止日上傳的 Portal 匯出（案件來源）
REG_ADVISOR = DATA_DIR / "師生對照表.xlsx"   # 僅歷史匯入／bootstrap 使用；指導教授改由學生填寫
REG_MEMBER_OUTPUT = OUTPUT_DATA_DIR / "委員個資.xlsx"
REG_ADVISOR_OUTPUT = OUTPUT_DATA_DIR / "師生對照表.xlsx"

# ── 範本（格式必須維持不變，一律以原檔填空）────────────
TPL_ATT4_OFFICIAL = TPL_DIR / "附件4-學位口試一覽.doc"
# 沒有官方 .doc 時改用示範範本 .docx（python -m koushi.demo_templates 產生，內容虛構）
TPL_ATT4 = (TPL_ATT4_OFFICIAL if TPL_ATT4_OFFICIAL.is_file() or not (TPL_DIR / "附件4-學位口試一覽.docx").is_file()
            else TPL_DIR / "附件4-學位口試一覽.docx")
TPL_ATT6 = TPL_DIR / "附件6-學生口試費清冊.xlsx"

# ── 口試費率 ─────────────────────────────────────────
# 依 110/5/31 109學年度第4次校務基金管理委員會修正通過
FEE_PER_PAPER = {"博": 2000, "碩": 1000}
# 附件3 範本說明：審查博士班學生一名 2000 元（以 6 名為上限）、碩士班一名 1000 元（以 3 名為上限）。
# 計算單位為「每位委員在同一清冊場次（同學期、學位別、指導教授、日期）」（2026-10-10 確認維持）。
FEE_PAPER_CAP = {"博": 6, "碩": 3}
# 每場口試委員下限（學生送出與主檔檢核共用；只在這裡改）。
MIN_COMMITTEES = {"碩": 3, "博": 5}
# 以下兩項 2026-10-10 確認維持現值（已確認流程決策第五節第 8–9 條）。
ADVISOR_EXAM_FEE_EXEMPT = False   # True：指導教授同時擔任委員時不支領口試費
HOME_UNIV_FEE_EXEMPT = False      # True：本校他系委員比照本系，免撥帳資料與交通費

# ── 交通費：自用汽車每公里 3 元，來回計 ────────────────
MILEAGE_RATE = 3
ROUND_TRIP = 2
DISTANCE_CSV = DATA_DIR / "距離表.csv"      # 服務單位,單程公里
# 距離表模糊比對有歧義時寫進交通費說明的開頭；檢核據此把 NO_DISTANCE 說明改寫成候選清單。
AMBIGUOUS_DISTANCE_PREFIX = "距離表比對有歧義"

# ── 機構識別與校內判定 ─────────────────────────────────
# 程式預設為匿名示範值（展示、錄影、測試不會出現真實校系名稱）；
# 正式環境一律由 .env 設定真實值（範本見 deploy/.env.example），未設定時拒絕啟動。
def _env_text(key: str, default: str) -> str:
    return (os.environ.get(key) or "").strip() or default


def _env_keys(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """逗號分隔的比對關鍵字；未設定時用預設值。"""
    raw = (os.environ.get(key) or "").replace("，", ",")
    keys = tuple(k.strip() for k in raw.split(",") if k.strip())
    return keys or default


UNIV_NAME = _env_text("KOUSHI_UNIV_NAME", "示範大學")            # 畫面抬頭、附件6 標題
# 附件6 交通費備註「<簡稱><->單位」用的簡稱；未設定時由校名去掉「國立」「大學」（國立範例大學 → 範例）
UNIV_SHORT_NAME = _env_text("KOUSHI_UNIV_SHORT_NAME", UNIV_NAME.replace("國立", "").replace("大學", "") or UNIV_NAME)
DEPT_NAME = _env_text("KOUSHI_DEPT_NAME", "示範學系")            # 畫面抬頭
OFFICE_SIGNATURE = _env_text("KOUSHI_OFFICE_SIGNATURE", DEPT_NAME + "系辦")  # 信件署名、聯繫窗口
# 委員服務單位含任一關鍵字即視為本系／本校（比對前會去掉空白與「國立」）
HOME_DEPT_KEYS = _env_keys("KOUSHI_HOME_DEPT_KEYS", (UNIV_NAME + DEPT_NAME,))
HOME_UNIV_KEYS = _env_keys("KOUSHI_HOME_UNIV_KEYS", (UNIV_NAME,))
HOME_DEPT_LABEL = _env_text("KOUSHI_HOME_DEPT_LABEL", "本校" + DEPT_NAME)  # 附件6 服務單位欄的寫法


# ── 範本殘留的場域字樣（附件4 標題右側班別提醒、附件6 說明列的校內會議出處）──────
# 正式格式預設照範本原樣；示範／評審模式（KOUSHI_DEMO_ROOT）預設改成中性寫法，
# 避免展示畫面與下載檔帶出特定學校的班別名稱與會議紀錄。兩者都可用環境變數明確指定。
def demo_mode() -> bool:
    return bool(os.environ.get("KOUSHI_DEMO_ROOT"))


def att4_title_note() -> str | None:
    """附件4 標題右側的班別提醒：None＝沿用範本；字串＝改成這段文字（空字串＝移除）。
    KOUSHI_ATT4_TITLE_NOTE 有設定（含空字串）時以它為準；否則示範模式移除、正式沿用範本。"""
    if "KOUSHI_ATT4_TITLE_NOTE" in os.environ:
        return os.environ["KOUSHI_ATT4_TITLE_NOTE"].strip()
    return "" if demo_mode() else None


def att6_neutral_note() -> bool:
    """附件6 說明列是否拿掉「依○年○月○日…委員會修正通過」的校內出處（費率文字保留）。
    KOUSHI_ATT6_NEUTRAL_NOTE=1／0 明確指定；未設定時示範模式拿掉、正式沿用範本。"""
    raw = (os.environ.get("KOUSHI_ATT6_NEUTRAL_NOTE") or "").strip()
    if raw:
        return raw not in ("0", "false", "False", "no")
    return demo_mode()


INSTITUTION_ENV = ("KOUSHI_UNIV_NAME", "KOUSHI_DEPT_NAME", "KOUSHI_HOME_DEPT_KEYS", "KOUSHI_HOME_UNIV_KEYS")
if os.environ.get("KOUSHI_ENV") == "production":
    _missing = [k for k in INSTITUTION_ENV if not (os.environ.get(k) or "").strip()]
    if _missing:
        # 校內判定影響口試費與交通費；正式環境不得默默套用示範值。
        raise ValueError("正式環境必須設定機構識別：" + "、".join(_missing) + "（見 deploy/.env.example）")

# ── 班別對照（決定附件4 落在哪一欄）─────────────────────
# 附件4 欄位：博士班 / 碩士一般 / 碩士專班
PROGRAM_DOCTOR = "博士班"
PROGRAM_MASTER = "碩士一般"
PROGRAM_INSERV = "碩士專班"
# 「碩士專班」欄只收在職專班。
# 依既有附件4 反查 173 位學生的實際歸欄：在職專班 17 人全在專班欄，
# 軟工碩士班 9 人、AI碩士班 9 人則全部歸在「碩士一般」，無一例外。
# （標題右上的「軟工碩士班／AI碩士班／外籍生」是班別提醒，不是欄位定義。）
INSERV_KEYS = ("在職專班",)

# ── 案件種類 ─────────────────────────────────────────
KIND_APPLY = "學位考試申請"
KIND_CHANGE = "考試委員異動"
KIND_CANCEL = "學位考試撤銷"
EXCLUDED_KINDS = (KIND_CANCEL,)          # 不進附件4／5／6

# ── 截止日（工作日）────────────────────────────────────
DEFICIENCY_DEADLINE_DAYS = 5       # 補件截止日：通知後 5 個工作日
CONFIRMATION_DEADLINE_DAYS = 3     # 教授確認截止日：寄出後 3 個工作日
# 補件截止前幾個工作日開始每日催件；提前天數尚未正式拍板，此為暫定預設值。
DEFICIENCY_REMINDER_LEAD_DAYS = 2

# 委員身分／撥帳資料由 CommitteeRegistry（委員總表.xlsx）跨批次持久化；
# 主檔是唯讀快照，不提供 MANUAL_* 欄位。
