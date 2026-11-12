import math
# -*- coding: utf-8 -*-
"""跨學期的對照表 —— 這是消掉『附件3 重打』的關鍵。

委員總表：committee_id -> 身分／撥帳資料
    同一位校外委員每學期都來，資料只需輸入一次。
    姓名、單位、職稱與學歷都不是唯一身分證據。附件2只建立案件級 P...
    暫定身分；附件3提供身分證字號後才升級／合併為跨案件 C... 正式身分。

師生對照表：學生 -> 指導教授
    附件2 沒有指導教授欄，但附件4／5 都要。
"""
import csv
import datetime as _dt
import hashlib
import unicodedata
from pathlib import Path

import openpyxl

from . import config as C
from . import util as U

COMMITTEE_COLS = ["委員編號", "委員姓名", "英文姓名", "服務單位",
                   "最高學歷學校", "最高學位",
                   "身分證字號", "金融機構", "帳號", "戶籍地址", "Email",
                   "最後更新", "來源"]
ALIAS_SHEET = "委員別名"
ALIAS_COLS = ["委員姓名", "服務單位", "委員編號", "來源", "最後更新"]
ADVISOR_COLS = ["學期", "學號", "學生姓名", "指導教授", "來源", "最後更新"]


# ────────────────────────── 委員總表 ──────────────────────────
class CommitteeRegistry:
    """委員身分的持久化對照表，以 surrogate committee_id 為鍵。

    P...：由案件鍵＋姓名＋服務單位確定性產生，不跨案件、不持久化。
    C...：附件3強識別確認後才建立並持久化；相同身分證自動合併。
    alias 可一對多，因為同名同單位仍可能是不同人，不能作為合併證據。
    """

    def __init__(self):
        self.rows = {}              # committee_id -> dict
        self._by_name_unit = {}     # (canon_name, norm_unit) -> set(committee_id)
        self._by_name = {}          # canon_name -> set(committee_id)
        self._by_identity = {}      # 正規化身分證字號 -> committee_id
        self.alias_rows = {}        # (canon_name, norm_unit, committee_id) -> 顯示資料
        self._next = 1

    @classmethod
    def load(cls, path=None):
        path = path or C.REG_MEMBER
        r = cls()
        if not path.exists():
            return r
        workbook = openpyxl.load_workbook(path, data_only=True)
        ws = workbook.active
        data = list(ws.iter_rows(values_only=True))
        if not data:
            return r
        hdr = [str(h or "").strip() for h in data[0]]
        for raw in data[1:]:
            d = {hdr[i]: raw[i] for i in range(min(len(hdr), len(raw)))}
            cid = str(d.get("委員編號") or "").strip()
            name = str(d.get("委員姓名") or "").strip()
            if not cid or not name or not r._identity_key(d.get("身分證字號")):
                continue
            r._register(cid, d)
        if ALIAS_SHEET in workbook.sheetnames:
            alias_data = list(workbook[ALIAS_SHEET].iter_rows(values_only=True))
            if alias_data:
                alias_headers = [str(value or "").strip() for value in alias_data[0]]
                for raw in alias_data[1:]:
                    alias = {
                        alias_headers[index]: raw[index]
                        for index in range(min(len(alias_headers), len(raw)))
                    }
                    alias_committee_id = str(alias.get("委員編號") or "").strip()
                    if alias_committee_id not in r.rows:
                        continue
                    r.add_alias(
                        alias.get("委員姓名"),
                        alias.get("服務單位"),
                        alias_committee_id,
                        source=str(alias.get("來源") or "委員別名"),
                        updated=str(alias.get("最後更新") or ""),
                    )
        return r

    def import_source(self, source):
        """累積資料是主體；重新上傳只補缺值與別名，不重編或覆寫既有身分。"""
        for old_id, row in source.rows.items():
            identity = self._identity_key(row.get("身分證字號"))
            target = self.resolve_identity(identity)
            if target is None:
                target = old_id if old_id not in self.rows else "C%04d" % self._next
                self._register(target, {**row, "委員編號": target})
            else:
                for field, value in row.items():
                    if U.blank(self.rows[target].get(field)) and not U.blank(value):
                        self.rows[target][field] = value
            self.add_alias(row.get("委員姓名"), row.get("服務單位"), target, source="來源匯入")
            for alias in source.alias_rows.values():
                if alias.get("委員編號") == old_id:
                    self.add_alias(alias.get("委員姓名"), alias.get("服務單位"), target, source="來源別名")

    def _register(self, cid, d):
        self.rows[cid] = d
        name = str(d.get("委員姓名") or "")
        unit = d.get("服務單位")
        self.add_alias(name, unit, cid, source=str(d.get("來源") or "委員總表"),
                       updated=str(d.get("最後更新") or ""))
        self._by_name.setdefault(U.canon_name(name), set()).add(cid)
        identity_key = self._identity_key(d.get("身分證字號"))
        if identity_key:
            existing = self._by_identity.get(identity_key)
            if existing and existing != cid:
                raise ValueError("委員總表包含重複身分識別，無法確定唯一委員編號")
            self._by_identity[identity_key] = cid
        num = U.as_int(cid.lstrip("C"), 0) if cid.startswith("C") else 0
        self._next = max(self._next, num + 1)

    @staticmethod
    def _identity_key(value):
        if U.blank(value):
            return ""
        return "".join(unicodedata.normalize("NFKC", str(value)).upper().split())

    def add_alias(self, name, unit, committee_id, *, source="附件2", updated=""):
        name_text = str(name or "").strip()
        unit_text = str(unit or "").strip()
        if not name_text or not committee_id:
            return
        key = (U.canon_name(name_text), U.norm_unit(unit_text))
        self._by_name_unit.setdefault(key, set()).add(committee_id)
        self._by_name.setdefault(key[0], set()).add(committee_id)
        alias_key = (*key, committee_id)
        prior = self.alias_rows.get(alias_key, {})
        sources = {item for item in str(prior.get("來源") or "").split("＋") if item}
        sources.update(item for item in str(source or "").split("＋") if item)
        self.alias_rows[alias_key] = {
            "委員姓名": name_text,
            "服務單位": unit_text,
            "委員編號": committee_id,
            "來源": "＋".join(sorted(sources)),
            "最後更新": updated or _dt.date.today().isoformat(),
        }

    def alias_candidates(self, name, unit):
        """列出精確姓名＋單位 alias 候選；不代表身分已確認。"""
        key = (U.canon_name(name), U.norm_unit(unit))
        return sorted(self._by_name_unit.get(key, set()))

    def name_candidates(self, name):
        """列出同名候選；只供診斷，不作自動合併。"""
        return sorted(self._by_name.get(U.canon_name(name), set()))

    def new_confirmed_id(self, name, unit, identity_number, source="附件3"):
        identity_key = self._identity_key(identity_number)
        if not identity_key:
            raise ValueError("正式委員編號必須有強識別")
        if identity_key in self._by_identity:
            raise ValueError("強識別已對應既有正式委員編號")
        cid = "C%04d" % self._next
        self._next += 1
        d = {
            "委員編號": cid,
            "委員姓名": name,
            "服務單位": unit,
            "身分證字號": identity_number,
            "最後更新": _dt.date.today().isoformat(),
            "來源": source,
        }
        self._register(cid, d)
        return cid

    @staticmethod
    def is_provisional(committee_id):
        return str(committee_id or "").startswith("P")

    def new_provisional_id(self, case_key, name, unit, source="附件2"):
        """建立案件級暫定身分；沒有強識別前不跨案件合併。"""
        fingerprint = "\x1f".join(
            (str(case_key), U.canon_name(name), U.norm_unit(unit))
        ).encode("utf-8")
        cid = "P" + hashlib.sha256(fingerprint).hexdigest()[:12].upper()
        self.rows.setdefault(
            cid,
            {
                "委員編號": cid,
                "委員姓名": name,
                "服務單位": unit,
                "最後更新": _dt.date.today().isoformat(),
                "來源": source,
            },
        )
        return cid

    def resolve_identity(self, identity_number):
        return self._by_identity.get(self._identity_key(identity_number))

    def merge(self, source_id, target_id):
        """把同一身分證確認為同一人的暫存編號自動併入既有編號。"""
        if source_id == target_id:
            return target_id
        source = self.rows.get(source_id)
        target = self.rows.get(target_id)
        if source is None or target is None:
            raise ValueError("委員編號合併失敗：來源或目標不存在")
        self.add_alias(
            source.get("委員姓名"),
            source.get("服務單位"),
            target_id,
            source="＋".join(
                item
                for item in (str(source.get("來源") or ""), "附件3身分確認")
                if item
            ),
        )
        for column in COMMITTEE_COLS:
            if U.blank(target.get(column)) and not U.blank(source.get(column)):
                target[column] = source[column]
        for key, alias in list(self.alias_rows.items()):
            if alias.get("委員編號") == source_id:
                del self.alias_rows[key]
                alias["委員編號"] = target_id
                alias["最後更新"] = _dt.date.today().isoformat()
                alias_key = (key[0], key[1], target_id)
                self.alias_rows[alias_key] = alias
                ids = self._by_name_unit.get((key[0], key[1]), set())
                ids.discard(source_id)
                ids.add(target_id)
        for ids in self._by_name.values():
            if source_id in ids:
                ids.discard(source_id)
                ids.add(target_id)
        for identity_key, owner in list(self._by_identity.items()):
            if owner == source_id:
                self._by_identity[identity_key] = target_id
        del self.rows[source_id]
        return target_id

    def get(self, committee_id):
        return self.rows.get(committee_id)

    def upsert_identity(self, committee_id, *, name=None, english_name=None, unit=None,
                         school=None, degree=None, source="附件2"):
        """把附件2 的身分欄位（姓名/單位/學歷）補進總表，不覆蓋已有值。"""
        d = self.rows.setdefault(committee_id, {"委員編號": committee_id})
        for col, val in (("委員姓名", name), ("英文姓名", english_name), ("服務單位", unit),
                         ("最高學歷學校", school), ("最高學位", degree)):
            if val and not U.blank(d.get(col)):
                continue
            if val:
                d[col] = val
        d["最後更新"] = _dt.date.today().isoformat()
        if not U.blank(d.get("來源")):
            sources = {s for s in str(d["來源"]).split("＋") if s}
            sources.add(source)
            d["來源"] = "＋".join(sorted(sources))
        else:
            d["來源"] = source
        if self.is_provisional(committee_id):
            return
        # 已由強識別確認的正式身分，才加入跨案件 alias 索引。
        self.add_alias(name, unit, committee_id, source=source)

    def upsert_banking(self, committee_id, *, id_no=None, bank=None,
                        account=None, address=None, email=None, source="附件3"):
        """把附件3 填回來的撥帳資料存進總表，下次自動帶出。"""
        d = self.rows.setdefault(committee_id, {"委員編號": committee_id})
        identity_key = self._identity_key(id_no)
        existing_identity = self._identity_key(d.get("身分證字號"))
        if identity_key and existing_identity and identity_key != existing_identity:
            raise ValueError("同一委員編號收到不同身分識別")
        owner = self._by_identity.get(identity_key) if identity_key else None
        if owner and owner != committee_id:
            raise ValueError("同一身分識別對應到多個委員編號")
        for col, val in (("身分證字號", id_no), ("金融機構", bank),
                         ("帳號", account), ("戶籍地址", address), ("Email", email)):
            if not U.blank(val):
                d[col] = val
        d["最後更新"] = _dt.date.today().isoformat()
        if not U.blank(d.get("來源")):
            sources = {s for s in str(d["來源"]).split("＋") if s}
            sources.add(source)
            d["來源"] = "＋".join(sorted(sources))
        else:
            d["來源"] = source
        if identity_key:
            self._by_identity[identity_key] = committee_id

    def apply_to(self, committee_id, target):
        """把總表已知的資料補進目標物件（Committee dataclass 或相容物件）。

        不覆蓋已有值。回傳是否有補到。
        """
        d = self.rows.get(committee_id)
        if not d:
            return False
        touched = False
        for f in ("委員姓名", "英文姓名", "服務單位", "最高學歷學校", "最高學位",
                  "身分證字號", "金融機構", "帳號", "戶籍地址", "Email"):
            if U.blank(getattr(target, f, None)) and not U.blank(d.get(f)):
                setattr(target, f, d.get(f))
                touched = True
        return touched

    def save(self, path=None):
        path = Path(path or C.REG_MEMBER_OUTPUT)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "委員總表"
        ws.append(COMMITTEE_COLS)
        for cid in sorted(self.rows):
            d = self.rows[cid]
            if self.is_provisional(cid) or not self._identity_key(d.get("身分證字號")):
                continue
            ws.append([d.get(c) for c in COMMITTEE_COLS])
        for i, w in enumerate([10, 12, 20, 30, 24, 12, 16, 18, 20, 40, 24, 12, 16], 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        alias_sheet = wb.create_sheet(ALIAS_SHEET)
        alias_sheet.append(ALIAS_COLS)
        for key in sorted(self.alias_rows):
            alias = self.alias_rows[key]
            owner = self.rows.get(alias.get("委員編號"))
            if owner and self._identity_key(owner.get("身分證字號")):
                alias_sheet.append([alias.get(column) for column in ALIAS_COLS])
        alias_sheet.freeze_panes = "A2"
        return U.atomic_save_workbook(wb, path, literal_strings=True)


# ────────────────────────── 師生對照表 ──────────────────────────
class AdvisorRegistry:
    def __init__(self):
        self.rows = {}          # (學期, 學號) -> dict

    @classmethod
    def load(cls, path=None):
        path = path or C.REG_ADVISOR
        r = cls()
        if not path.exists():
            return r
        ws = openpyxl.load_workbook(path, data_only=True).active
        data = list(ws.iter_rows(values_only=True))
        if not data:
            return r
        hdr = [str(h or "").strip() for h in data[0]]
        for raw in data[1:]:
            d = {hdr[i]: raw[i] for i in range(min(len(hdr), len(raw)))}
            sid = str(d.get("學號") or "").strip()
            adv = str(d.get("指導教授") or "").strip()
            if not adv:
                continue
            if sid:
                r.rows[(str(d.get("學期") or "").strip(), sid)] = d
        return r

    def get(self, term, sid, name=None):
        d = self.rows.get((str(term).strip(), str(sid).strip()))
        if d:
            return str(d.get("指導教授") or "").strip(), "師生對照表"
        return "", ""

    def upsert(self, term, sid, name, advisor, source):
        if not advisor:
            return
        self.rows[(str(term).strip(), str(sid).strip())] = {
            "學期": term, "學號": sid, "學生姓名": name,
            "指導教授": advisor, "來源": source,
            "最後更新": _dt.date.today().isoformat(),
        }

    def save(self, path=None):
        path = Path(path or C.REG_ADVISOR_OUTPUT)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "師生對照表"
        ws.append(ADVISOR_COLS)
        for key in sorted(self.rows, key=lambda k: (str(k[0]), str(k[1]))):
            d = self.rows[key]
            ws.append([d.get(c) for c in ADVISOR_COLS])
        for i, w in enumerate([8, 14, 12, 14, 18, 12], 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        return U.atomic_save_workbook(wb, path, literal_strings=True)


# ────────────────────────── 距離表 ──────────────────────────
class AmbiguousDistance(ValueError):
    """服務單位同時模糊符合距離表多個單位（例如「交通大學」與「陽明交通大學」）。"""

    def __init__(self, unit, candidates):
        self.unit = unit
        self.candidates = list(candidates)
        super().__init__(f"距離表單位比對有歧義：{unit}")

    @property
    def note(self) -> str:
        return "%s：「%s」同時符合 %s，請在距離表補上完整單位名稱或確認對照" % (
            C.AMBIGUOUS_DISTANCE_PREFIX, str(self.unit).strip(), "、".join(f"「{c}」" for c in self.candidates))


class DistanceTable:
    """服務單位 -> 單程公里。交通費 = round(km × 3 × 2)。"""

    def __init__(self, rows=None, aliases=None):
        self.rows = rows or {}
        # 專員確認過的「服務單位寫法 -> 距離表單位」對照（ai_proposals.ALIASES_FILE）。
        # 只收已確認的對應；AI 未確認的建議永遠不會進來，不影響交通費。
        self.aliases = dict(aliases or {})

    def resolve(self, unit):
        """回傳實際查距離表用的單位；有已確認對照且目標仍在距離表時改用目標。"""
        u = U.norm_unit(unit)
        target = self.aliases.get(u)
        if u not in self.rows and target in self.rows:
            return target
        return u

    @classmethod
    def load(cls, path=None):
        path = Path(path or C.DISTANCE_CSV)
        rows = {}
        if path.exists():
            with open(path, encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                if not {"服務單位", "單程公里"}.issubset(reader.fieldnames or []):
                    raise ValueError("距離表需要服務單位、單程公里欄位")
                for number, d in enumerate(reader, 2):
                    unit = U.norm_unit(d.get("服務單位"))
                    try:
                        km = float(d.get("單程公里", ""))
                    except (ValueError, TypeError) as exc:
                        raise ValueError(f"距離表第{number}列距離格式錯誤") from exc
                    if not unit or not math.isfinite(km) or km < 0:
                        raise ValueError(f"距離表第{number}列需要單位及非負有限距離")
                    if unit in rows:
                        raise ValueError(f"距離表重複單位：{unit}")
                    rows[unit] = km
        return cls(rows)

    def km_for(self, unit):
        u = self.resolve(unit)
        if u in self.rows:
            candidates = [(u, self.rows[u])]
        else:
            candidates = [(k, v) for k, v in self.rows.items() if u and k and (k in u or u in k)]
        if len(candidates) > 1:
            raise AmbiguousDistance(unit, sorted(k for k, _ in candidates))
        if not candidates:
            return None
        km = candidates[0][1]
        if not math.isfinite(km) or km < 0:
            raise ValueError(f"距離表距離無效：{unit}")
        return km

    def fare(self, unit):
        """回傳 (交通費, 說明)。校內 0；查不到距離回傳 (None, '')。"""
        if U.is_fee_exempt_unit(unit):
            return 0, ""
        try:
            km = self.km_for(unit)
        except AmbiguousDistance as exc:
            # 歧義只影響這位委員：交通費留空、說明寫明候選，檢核列 NO_DISTANCE 交專員，
            # 不中止整批建檔或每日自動化。
            return None, exc.note
        if km is None:
            return None, ""
        fee = round(km * C.MILEAGE_RATE * C.ROUND_TRIP)
        note = "%s<->%s 約%gkm*%d*%d=%d" % (
            C.UNIV_SHORT_NAME, str(unit).strip(), km, C.MILEAGE_RATE, C.ROUND_TRIP, fee)
        resolved = self.resolve(unit)
        if resolved != U.norm_unit(unit):
            note += "（依專員確認對照：%s）" % resolved
        return fee, note
