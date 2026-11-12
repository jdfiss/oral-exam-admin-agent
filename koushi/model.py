# -*- coding: utf-8 -*-
"""口試主檔的資料結構。

設計原則
--------
1. 一場口試 = 一筆 Exam（以 案件鍵 = 學期-學號 唯一識別）。
2. 委員的「身分」與「這場口試的配對」分開存：
   Committee 是案件級暫定 P... 或強識別確認後的跨案件 C... 委員身分，
   Member 是 (案件, 委員) 的關聯表，只放這次配對才成立的屬性
   （職稱、委員資格、口試篇數、口試費、交通費……）。
   姓名＋服務單位不可靠且可能同名碰撞，所以案件剛建立時（不論來自附件2
   或系辦審核通過的介面填寫）不會因資料看起來相同就跨案件共享個資；
   強識別（身分證字號）出現後才升級或合併為正式 surrogate key。
3. 每個欄位都標明權威來源；主檔是自動產生的唯讀快照，不接受人工補值。
4. 附件4／5／6 都只是這份結構的投影，不得各自持有獨立資料。
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import datetime as _dt

from . import util as U


# 欄位來源標記，會寫進主檔的「欄位來源」分頁供系辦查閱
FIELD_SOURCE = {
    # 口試主表 exams
    "案件鍵": "推導(學期-學號)", "學期": "附件2", "學院": "附件2", "系所": "附件2",
    "學位別": "推導(系所)", "班別": "推導(系所)", "學號": "附件2", "姓名": "附件2",
    "案件種類": "附件2", "審核狀態": "附件2",
    "簽核完成時間": "附件2",
    "題目含特殊字": "附件2", "論文題目": "附件2(有題目欄時)／介面", "生效": "推導(撤銷案=否)",
    "指導教授": "介面(可多位，以、分隔)", "指導教授來源": "介面",
    "口試日期": "介面", "口試日期原文": "介面",
    "口試開始時間": "介面", "口試結束時間": "介面", "口試地點": "介面",
    "口試方式": "介面",
    "有附件2": "稽核", "有附件3": "稽核", "介面委員已填": "稽核",
    # 委員總表 committee（身分，跨案件跨學期只存一筆）
    "committee_id": "推導(案件暫定／強識別)", "委員姓名": "附件2／介面(附件2無委員時)",
    "英文姓名": "附件2", "服務單位": "附件2／介面(附件2無委員時)",
    "最高學歷學校": "附件2", "最高學位": "附件2", "校內外": "推導(服務單位)",
    "身分證字號": "口委資料庫／委員本人", "金融機構": "口委資料庫／委員本人",
    "帳號": "口委資料庫／委員本人", "戶籍地址": "口委資料庫／委員本人", "Email": "介面",
    # 委員明細 members（案件 × 委員 的配對）
    "委員順序": "附件2／推導(介面填寫順序)", "服務單位快照": "附件2／介面",
    "職稱": "附件2／介面(附件2空白時)", "委員資格": "附件2", "委員備註": "附件2",
    "是否指導教授": "推導",
    "附件3口試篇數": "介面", "附件3合計": "介面",
    "單程公里": "外部距離資料", "口試費": "學位費率",
    "交通費": "外部距離資料推導(視訊為0)", "交通費說明": "外部距離資料推導",
}


@dataclass
class Committee:
    """一位口試委員的身分資料，跨案件、跨學期只存一筆。"""
    committee_id: str = ""
    委員姓名: str = ""
    英文姓名: str = ""
    服務單位: str = ""
    最高學歷學校: str = ""
    最高學位: str = ""
    # ── 撥帳（敏感）──
    身分證字號: str = ""
    金融機構: str = ""
    帳號: str = ""
    戶籍地址: str = ""
    Email: str = ""

    @property
    def 校內外(self):
        return U.campus_side(self.服務單位)

    def banking_complete(self, unit=None):
        if U.is_fee_exempt_unit(self.服務單位 if unit is None else unit):
            return True          # 校內走系內程序，不需撥帳資料
        return all(str(getattr(self, f) or "").strip()
                   for f in ("身分證字號", "金融機構", "帳號", "戶籍地址"))


    @property
    def 撥帳齊全(self):
        return self.banking_complete()


@dataclass
class Member:
    """一位委員在某一場口試中的配對紀錄（案件 × 委員的關聯表）。"""
    committee_id: str = ""
    委員順序: int = 0
    服務單位: str = ""              # 該次口試的單位快照；委員跨期可能換單位
    職稱: str = ""
    association_id: str = ""
    委員姓名: str = ""
    Email: str = ""
    視訊出席: bool = False
    委員資格: str = ""
    委員備註: str = ""
    # ── 推導 ──
    是否指導教授: bool = False
    # ── 費用（附件3）──
    單程公里: Optional[float] = None
    口試費: Optional[int] = None      # 單價（每篇），非總額
    交通費: Optional[int] = None
    交通費說明: str = ""
    附件3口試篇數: Optional[int] = None
    附件3合計: Optional[int] = None
    # ── 稽核 ──
    資料來源: str = "附件2"


@dataclass
class Exam:
    """一場學位口試（一位學生一筆）。"""
    # ── 附件2（案件基本資料）──
    學期: str = ""
    學號: str = ""
    姓名: str = ""
    學院: str = ""
    系所: str = ""
    案件種類: str = ""
    審核狀態: str = ""
    簽核完成時間: Optional[_dt.datetime] = None
    題目含特殊字: str = ""
    論文題目: str = ""             # 附件2 有題目欄時以 Portal 為準，否則取介面填寫
    # ── 推導 ──
    學位別: str = ""
    班別: str = ""
    生效: bool = True
    # ── 指導教授（介面填寫；共同指導以「、」分隔）──
    指導教授: str = ""
    指導教授來源: str = ""
    advisor_emails: dict[str, str] = field(default_factory=dict)
    student_email: str = ""
    # ── 附件3 ──
    口試日期: Optional[_dt.date] = None
    口試日期原文: str = ""
    口試開始時間: str = ""         # 預留欄位；不參與附件4／5／6分組
    口試結束時間: str = ""
    口試地點: str = ""
    口試方式: str = ""            # ''／視訊／部分視訊
    # ── 委員（關聯表）──
    委員: List[Member] = field(default_factory=list)
    # ── 稽核 ──
    有附件2: bool = False
    有附件3: bool = False
    介面委員已填: bool = False   # 委員名單與 E-mail 必填（依本系規定）

    # ---- 推導欄位 ----
    @property
    def 案件鍵(self):
        return U.case_key(self.學期, self.學號)

    @property
    def 指導教授們(self):
        return [name for name in (s.strip() for s in self.指導教授.split("、")) if name]

    @property
    def 費率(self):
        from . import config as C
        return C.FEE_PER_PAPER.get(self.學位別, C.FEE_PER_PAPER["碩"])

    def derive(self):
        """由已載入的來源欄位補齊所有推導欄位。可重複呼叫。"""
        self.學位別 = U.degree_of(self.系所)
        self.班別 = U.program_of(self.系所)
        from . import config as C
        self.生效 = self.案件種類 not in C.EXCLUDED_KINDS
        for i, m in enumerate(self.委員, 1):
            if not m.委員順序:
                m.委員順序 = i
            if m.口試費 is None:
                m.口試費 = self.費率
        return self

    @property
    def 日期字串(self):
        if self.口試日期原文:
            return self.口試日期原文
        if self.口試日期:
            return "%d/%d" % (self.口試日期.month, self.口試日期.day)
        return ""

    @property
    def 待補欄位(self):
        out = []
        if not self.指導教授:
            out.append("指導教授")
        if not self.口試日期 and not self.口試日期原文:
            out.append("口試日期")
        if not self.委員:
            out.append("委員名單")
        return out

    @property
    def 資料狀態(self):
        if not self.生效:
            return "已撤銷"
        missing = self.待補欄位
        if not missing:
            return "齊全"
        return "待補：" + "、".join(missing)

@dataclass
class Master:
    """整份主檔。"""
    exams: List[Exam] = field(default_factory=list)
    committees: Dict[str, Committee] = field(default_factory=dict)
    source_issues: List[dict] = field(default_factory=list)

    def by_key(self):
        return {e.案件鍵: e for e in self.exams}

    def committee_of(self, member: Member) -> Optional[Committee]:
        return self.committees.get(member.committee_id)

    def active(self, term=None):
        """可進附件4／5／6 的案件。"""
        out = [e for e in self.exams if e.生效]
        if term:
            out = [e for e in out if str(e.學期) == str(term)]
        return out

    def terms(self):
        return sorted({str(e.學期) for e in self.exams})
