# -*- coding: utf-8 -*-
"""資料檢核 —— 決定哪些案件可以進附件4／5／6，哪些要退回系辦補件。

嚴重度：
    阻擋  該案不會出現在對應的附件裡
    警示  會產出，但欄位留白／需要系辦確認
"""
from . import config as C
from . import util as U

BLOCK, WARN = "阻擋", "警示"

OWNER_STUDENT = "student"
OWNER_OFFICE = "office"
OWNER_POST_EXAM = "post_exam"
OWNER_NONE = "none"

ISSUE_OWNER = {
    # ── 學生責任（介面填寫不完整或格式錯誤）──
    "MISSING_SUBMISSION": OWNER_STUDENT,
    "NO_EXAM_DATE": OWNER_STUDENT,
    "SUBMISSION_PARSE_ERROR": OWNER_STUDENT,
    "ADVISOR_SOURCE_UNAVAILABLE": OWNER_STUDENT,
    "MISSING_COMMITTEE_EMAIL": OWNER_STUDENT,
    "MISSING_COMMITTEE_LIST": OWNER_STUDENT,
    "NO_COMMITTEE": OWNER_STUDENT,
    # ── 系辦責任（例外狀況由專員處理；距離表）──
    "SUBMISSION_WITHOUT_PORTAL_CASE": OWNER_OFFICE,
    "COMMITTEE_MISMATCH": OWNER_OFFICE,
    "COMMITTEE_RANK_EXCEPTION": OWNER_OFFICE,
    "COMMITTEE_MATCHED_BY_NAME": OWNER_OFFICE,
    "EXAM_DATE_TOO_EARLY": OWNER_OFFICE,
    # 委員名單以附件2 為準，學生無法在本系統補正，交專員處理
    "COMMITTEE_COUNT_BELOW_MIN": OWNER_OFFICE,
    # 由 application.generate_artifacts() 在分階段產檔時動態加入
    "CASE_REVIEW_PENDING": OWNER_OFFICE,
    # 由 application.review_queue() 依委員索取狀態動態加入
    "COMMITTEE_DATA_MISSING_BEFORE_EXAM": OWNER_OFFICE,
    "DUPLICATE_COMMITTEE_IDENTITY": OWNER_OFFICE,
    "NO_DISTANCE": OWNER_OFFICE,
    "DUPLICATE_SUBMISSION": OWNER_OFFICE,
    # ── 委員個資：系統向委員本人索取，口試現場可用附件6 補齊，不找學生 ──
    "UNVERIFIED_COMMITTEE_IDENTITY": OWNER_POST_EXAM,
    "MISSING_BANK_INFO": OWNER_POST_EXAM,
    # 教授尚未確認附件4：由 application.generate_artifacts() 在產口試後
    # 附件時動態加入，不是 validation.check() 本身會產生的代碼。
    "CONFIRMATION_PENDING": OWNER_POST_EXAM,
    # ── 不發通知（資訊性）──
    "CASE_CANCELLED": OWNER_NONE,
    "COMMITTEE_CHANGED": OWNER_NONE,
    # ── 歷史附件2/3匯入（過渡期保留，主流程不觸發）──
    "MISSING_ATTACHMENT_3": OWNER_STUDENT,
    "ATTACHMENT_3_PARSE_ERROR": OWNER_STUDENT,
    "ATTACHMENT_2_MISSING_CASE_KEY": OWNER_OFFICE,
    "ATTACHMENT_2_MISSING_COMMITTEE_IDENTITY": OWNER_OFFICE,
    "DUPLICATE_ATTACHMENT_2_COMMITTEE": OWNER_OFFICE,
    "ATTACHMENT_2_MULTIPLE_RECORDS": OWNER_OFFICE,
    "ATTACHMENT_3_WITHOUT_APPLICATION": OWNER_OFFICE,
    "ATTACHMENT_2_CASE_FIELD_CONFLICT": OWNER_OFFICE,
    "DUPLICATE_ATTACHMENT_3": OWNER_OFFICE,
}


def _issue(e, code, fields, msg, level=WARN, block=False,
           block_att4=None, block_att6=None):
    if block_att4 is None:
        block_att4 = block
    if block_att6 is None:
        block_att6 = block
    blocking = block_att4 or block_att6
    return {
        "案件鍵": e.案件鍵, "學期": e.學期, "學號": e.學號, "姓名": e.姓名,
        "嚴重度": level, "問題代碼": code, "欄位": fields,
        "說明": msg, "阻擋產出": "是" if blocking else "否",
        "阻擋附件4": "是" if block_att4 else "否",
        "阻擋附件6": "是" if block_att6 else "否",
        "來源": "檢核",
        "責任": ISSUE_OWNER.get(code, OWNER_OFFICE),
    }


# ── 委員職級（2026-09-23）：碩士需助理教授以上、博士需副教授以上 ──
# 只標記給專員參考，不阻擋；判斷不了的也標出，由專員在人工關卡決定。
_RANK_REQUIRED = {"碩": 1, "博": 2}
_MIN_COMMITTEES = C.MIN_COMMITTEES
_RANK_LABEL = {"碩": "助理教授", "博": "副教授"}
# 先比對較長的詞，避免「助理教授」「副教授」被「教授」吃掉。
_TITLE_RANKS = (
    ("院士", 3), ("助理教授", 1), ("副教授", 2), ("教授", 3),
    ("助研究員", 1), ("副研究員", 2), ("研究員", 3),
    ("assistant professor", 1), ("associate professor", 2), ("professor", 3),
)
_UNCERTAIN_MODIFIERS = ("兼任", "客座", "退休", "名譽", "約聘", "專案", "合聘", "adjunct", "visiting", "emeritus")


def _title_rank(title):
    """職稱 -> (職級, 無法判斷的原因)；職級 None 表示無法自動判斷。"""
    text = str(title or "").strip().lower()
    if not text:
        return None, "未提供職稱"
    for modifier in _UNCERTAIN_MODIFIERS:
        if modifier in text:
            return None, f"職稱含「{modifier}」，需人工判斷"
    for keyword, rank in _TITLE_RANKS:
        if keyword in text:
            return rank, ""
    return None, f"無法從職稱「{title}」判斷職級"


def rank_exception(degree, member):
    """回傳職級例外說明；符合規定時回傳空字串。附件2「委員資格」註明符合者優先採信。

    附件2 另有「屬稀少或特殊性…」「有博士學位且學術有成就…」等系所認定的特殊
    資格；這類委員若職稱未達或無法判斷，說明會標成「Portal 特殊資格」並附上原文，
    讓專員一眼區分「系所已認定的特殊資格」與「職稱未達」（review-2026-09-23 P1-16）。
    """
    qualification = str(member.委員資格 or "").strip()
    if qualification.startswith("符合"):
        return ""
    required = _RANK_REQUIRED.get(degree, 1)
    rank, reason = _title_rank(member.職稱)
    if rank is not None and rank >= required:
        return ""
    if rank is not None:
        reason = f"{'博士' if degree == '博' else '碩士'}口試委員需{_RANK_LABEL.get(degree, '助理教授')}以上，職稱為「{member.職稱}」"
    if qualification:
        return f"［Portal 特殊資格］附件2 委員資格：{qualification}；{reason}"
    return f"［職稱未達或無法判斷］{reason}"


def check(master):
    """回傳 issues list。master 需含 committees（委員身分對照）。"""
    out = list(master.source_issues)
    for e in master.exams:
        if not e.生效:
            out.append(_issue(e, "CASE_CANCELLED", "案件種類",
                              "案件已撤銷，不納入附件4／5／6", BLOCK, True))
            continue

        if e.案件種類 == C.KIND_CHANGE:
            out.append(_issue(e, "COMMITTEE_CHANGED", "案件種類",
                              "此案為考試委員異動，依附件2目前版本建檔"))

        if not e.委員:
            out.append(_issue(e, "NO_COMMITTEE", "委員名單",
                              "附件2 與介面都沒有委員資料（委員名單必填）", BLOCK, True))
        elif len(e.委員) < _MIN_COMMITTEES.get(e.學位別, 3):
            out.append(_issue(e, "COMMITTEE_COUNT_BELOW_MIN", "委員名單",
                              "%s口試委員至少 %d 位，目前 %d 位" % (
                                  "博士" if e.學位別 == "博" else "碩士",
                                  _MIN_COMMITTEES.get(e.學位別, 3), len(e.委員))))
        if e.委員 and e.有附件3 and not e.介面委員已填:
            out.append(_issue(e, "MISSING_COMMITTEE_LIST", "委員名單",
                              "尚未在介面填寫口試委員名單與 E-mail（必填）",
                              block_att4=False, block_att6=True))

        if not e.指導教授:
            out.append(_issue(e, "ADVISOR_SOURCE_UNAVAILABLE", "指導教授",
                              "尚未在介面填寫指導教授", BLOCK, True))
        if not e.口試日期 and not e.口試日期原文:
            out.append(_issue(e, "NO_EXAM_DATE", "口試日期",
                              "尚未填寫口試日期，附件4 日期欄會留白"))

        if not e.有附件3:
            out.append(_issue(e, "MISSING_SUBMISSION", "介面填寫",
                              "尚未收到介面填寫（口試日期、指導教授、委員 E-mail）"))

        for m in e.委員:
            committee = master.committees.get(m.committee_id)
            name = m.委員姓名 or (committee.委員姓名 if committee else m.committee_id)
            unit = m.服務單位 or (committee.服務單位 if committee else "")
            if str(m.committee_id).startswith("P"):
                out.append(_issue(
                    e,
                    "UNVERIFIED_COMMITTEE_IDENTITY",
                    "委員：%s" % name,
                    "委員身分為暫定（P...），待委員本人提供身分資料後才能跨案件合併",
                    block_att4=False, block_att6=True,
                ))
            reason = rank_exception(e.學位別, m)
            if reason:
                out.append(_issue(e, "COMMITTEE_RANK_EXCEPTION", "委員：%s" % name,
                                  "職級待專員確認：%s" % reason))
            if e.介面委員已填 and (not committee or U.blank(m.Email or committee.Email)):
                out.append(_issue(e, "MISSING_COMMITTEE_EMAIL", "委員：%s" % name,
                                  "尚未在介面填寫委員 E-mail（系統要寄信向委員索取個資）",
                                  block_att4=False, block_att6=True))
            if U.is_fee_exempt_unit(unit):
                continue
            if not committee or not committee.banking_complete(unit):
                miss = [f for f in ("身分證字號", "金融機構", "帳號", "戶籍地址")
                        if not committee or U.blank(getattr(committee, f))]
                out.append(_issue(e, "MISSING_BANK_INFO",
                                  "委員：%s" % name,
                                  "校外委員撥帳資料不全，缺：%s" % "、".join(miss),
                                  block_att4=False, block_att6=True))
            if m.交通費 is None:
                ambiguous = str(m.交通費說明 or "").startswith(C.AMBIGUOUS_DISTANCE_PREFIX)
                out.append(_issue(e, "NO_DISTANCE",
                                  "委員：%s" % name,
                                  m.交通費說明 if ambiguous else "外部距離資料尚未提供此服務單位：%s" % unit,
                                  block_att4=False, block_att6=True))

    return out


def blocked_keys(issues):
    return {i["案件鍵"] for i in issues if i.get("阻擋產出") == "是"}


def attachment4_blocked_keys(issues):
    """Critical：阻擋附件4 產出的案件。"""
    return {i["案件鍵"] for i in issues if i.get("阻擋附件4") == "是"}


def attachment6_blocked_keys(issues):
    """阻擋附件5／6 產出的案件。"""
    return {i["案件鍵"] for i in issues if i.get("阻擋附件6") == "是"}


# 只影響「委員個資」的問題：口試前交付版附件6 個資四欄本來就留白，委員在口試現場
# 自己填（已確認流程決策第五節第 4 條、第七節第 1–2 條），所以不擋交付版；
# 只擋口試後含個資的完整版。
PII_ONLY_ATT6_CODES = frozenset({"MISSING_BANK_INFO", "UNVERIFIED_COMMITTEE_IDENTITY"})


def attachment6_prefill_blocked_keys(issues):
    """阻擋口試前交付版（個資留白）附件6 的案件：委員未交個資或身分暫定不擋。"""
    return {i["案件鍵"] for i in issues
            if i.get("阻擋附件6") == "是" and i.get("問題代碼") not in PII_ONLY_ATT6_CODES}


def summary(issues):
    from collections import Counter
    c = Counter(i["問題代碼"] for i in issues)
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))
