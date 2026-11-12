# -*- coding: utf-8 -*-
"""系辦問答助理：唯讀、會用工具、回答必須通過程式檢查。

流程：
1. 問題先代號化（案件鍵、學號、姓名 → CASE_/STU_/PER_ 代號，長數字與 E-mail 遮蔽）。
2. 明顯要求「做事」（寄信、放行、修改……）的問題不呼叫模型，直接回覆該去哪一頁操作；
   問規則／政策（費用怎麼算、期限怎麼訂）的問題也不呼叫模型，說明助理只查案件資料並指向設定頁。
3. 模型以 JSON 逐步選工具（最多 MAX_TOOL_STEPS 次），再輸出回答與引用的步驟。
4. 程式檢查回答（`verify_answer`）：每個代號、每個數字都必須出現在引用的工具結果；
   提到案件狀態時，狀態詞必須是工具結果中該案件的狀態；列舉型問題（「哪些案件待專員放行」）
   列出的案件必須確實是問題要的狀態。任一項不符就整段作廢，
   改顯示原始工具結果表格與「無法驗證，改顯示原始資料」。
5. 每一步寫入 agent_trace（只存代號化後的內容）。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field

from . import agent_trace
from .llm import last_usage, model_name
from .assistant_tools import (
    TOOLS, TOKEN_RE, Aliaser, Snapshot, make_index, run_tool, status_vocabulary,
)

AGENT = "assistant"
PROMPT_VERSION = "assistant-v1"
SCHEMA_NAME = "assistant_step"
MAX_TOOL_STEPS = 5
MAX_REFLECTIONS = 1   # 回答沒通過 grounding check 時，把問題回饋給模型重來的次數（docs/自我修正設計.md）
UNVERIFIED_NOTE = "無法驗證，改顯示原始資料"
REFUSAL_TEXT = "問答助理只能查詢資料，不能寄信、放行、修改或登錄任何東西。請到下列頁面由系辦人員操作："

# 動作 → 可以操作的頁面（web 層用 endpoint 名稱組網址）
ACTION_PAGES = {
    "review": ("工作總覽：委員資格審核", "office_overview", "review-queue"),
    "confirmation": ("文件中心：教授確認與回信判讀", "office_documents", ""),
    "committee": ("委員個資：寄送狀態與手動登錄", "office_committees", ""),
    "settings": ("期限設定", "office_settings_page", ""),
    "import": ("匯入資料", "office_import", ""),
    "correction": ("工作總覽：案件詳情（專員修正／開放修改）", "office_overview", ""),
    "mail": ("通訊記錄（寄送狀態）", "office_communications", ""),
    "other": ("工作總覽", "office_overview", ""),
}
_ACTION_KEYWORDS = (
    ("review", ("放行", "已處理", "審核通過")),
    ("committee", ("委員個資", "撥帳", "身分證", "帳號", "回填")),
    ("confirmation", ("教授", "附件4", "學位口試一覽", "回信", "確認信")),
    ("settings", ("截止日", "期限", "設定", "學期")),
    ("import", ("上傳", "匯入", "附件2")),
    ("correction", ("修改", "更正", "修正", "解鎖", "開放修改", "改成", "改到", "改為", "改期", "更改", "變更",
                    "調整", "延後", "提前", "取消", "刪除")),
    ("mail", ("寄", "催", "發信", "通知")),
)
_ACT_PREFIX = r"(幫我|幫忙|替我|麻煩你?|請你|你去|直接|立刻|馬上|請把|請將|^請(?!問))"
_ACT_VERBS = (r"(寄|發送|發信|催|放行|核准|通過|退回|修改|更正|修正|改成|改到|改為|改期|更改|變更|調整|延後|提前|"
              r"取消|換成|刪除|登錄|上傳|匯入|解鎖|開放修改|設定|設為|標記|標為|確認掉|處理掉)")
_QUERY_HINT = re.compile(r"(查|列出|列一下|哪些|哪個|哪位|多少|幾|有沒有|是否|狀態|卡在|嗎|？|\?)")
_ACTION_RE = re.compile(_ACT_PREFIX + r"[^。？?]{0,24}?" + _ACT_VERBS)  # 中間可夾案件編號
# 「（請）幫我把…改到…」「可以幫我把…寄給…嗎？」：祈使句＋處置式「把／將」＋寫入動詞，就算句尾有「嗎？」也是要求做事
_IMPERATIVE_RE = re.compile(r"(幫我|幫忙|替我|麻煩你?|請你|請|你去|可以|能不能|能否|可否)(幫我|替我|幫忙)?(把|將)"
                            r"[^。？?]{0,30}?" + _ACT_VERBS)
# 規則／政策問題（費用怎麼算、期限怎麼訂）：工具裡沒有規則，交給模型只會變成猜測，程式直接說明範圍
_POLICY_RE = re.compile(r"(怎麼算|怎樣算|如何算|如何計算|怎麼計算|計算方式|算法|怎麼報|如何報|能不能報|可不可以報|"
                        r"規定|辦法|法規|依據|標準|為什麼要|為何要|為什麼是|為何是)")
_FEE_RE = re.compile(r"(交通費|口試費|出席費|審查費|鐘點費|差旅費|酬勞|車馬費|報帳|核銷)")
_FEE_ASK_RE = re.compile(r"(多少|怎麼|如何|幾元|幾塊|能不能|可不可以|可以)")
_DATE_ASK_RE = re.compile(r"(哪一?天|幾號|幾月幾日|什麼時候|何時)")
POLICY_TEXT = ("問答助理只查詢口試案件資料（卡在哪關、缺件、教授確認、委員個資是否回填、期限日期等），"
               "不解釋口試費、交通費或期限的計算規則，也不讓模型推測。規則請看系辦操作說明與下列頁面：")
POLICY_PAGES = [
    ("期限設定（各項期限天數與截止日）", "office_settings_page", ""),
    ("匯入資料（交通費歷史資料、口試委員歷史資料）", "office_import", ""),
    ("文件中心（系統產生的附件6 口試費清冊）", "office_documents", ""),
]
# system prompt 指定的兩句範圍外回覆：模型照抄時視為拒答（out_of_scope），不當成一般回答顯示
SCOPE_REPLY = "我只能回答口試案件相關的資料問題"
POLICY_REPLY = "我只能查詢口試案件資料，規則請看系辦操作說明或期限設定頁"
_CLAIMED_ACTION_RE = re.compile(r"(我|已)(已經?)?(幫|為|替)(你|您)")
_ORDINAL_RE = re.compile(r"(?m)^\s*\d+[.、)）]\s*")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_DOC_LABEL_RE = re.compile(r"(附件|附表)\s*\d+")   # 文件名稱（附件2／附件4／附件6）裡的數字不是事實數字
_SENTENCE_SPLIT = re.compile(r"[。\n；;！!？?]")
# 列舉型問題（「哪些案件…」）才檢查回答列出的案件是否符合問題要的狀態；問特定案件時不檢查
_LISTING_Q = re.compile(r"(哪些|哪幾|哪位|哪個|哪一|有誰|是誰|列出|名單|清單|有沒有)")
_NEGATION = re.compile(r"(沒有|不是|並非|不在|不屬於|而非|尚未逾期|還沒逾期|未逾期)")
# 問題裡的說法 → 工具結果中的狀態詞（任一符合即可）
_REQUESTED_STATUS = (
    (("待專員放行", "待放行", "還沒放行", "尚未放行", "未放行", "沒放行", "沒有放行", "等放行", "需要放行"),
     ("待專員放行",)),
    (("補件逾期", "缺件逾期"), ("補件逾期",)),
    (("逾期",), ("逾期未回", "補件逾期")),
    (("未回填", "沒回填", "沒有回填", "還沒回填", "尚未回填", "沒填個資", "沒交個資"), ("未回填",)),
    (("需修改", "要求修改", "要修改"), ("需修改",)),
)
_NOT_FILTERABLE = {"已放行", "放行後名單變動"}   # 只出現在單一案件的放行資料，清單工具不會逐案標示


def _system_prompt(today: str) -> str:
    tools = "\n".join(f"- {t.signature}：{t.description}" for t in TOOLS.values())
    return f"""你是系辦「問答助理」，只能用下列唯讀工具查詢口試案件資料，再用繁體中文回答系辦人員。
每次只輸出一個 JSON 物件，三種之一：
1. 查資料：{{"type":"tool_call","tool":"工具名稱","args":{{...}}}}
2. 回答：{{"type":"answer","answer":"回答內容","sources":[用到的步驟編號]}}
3. 對方要你執行動作（寄信、放行、修改資料、登錄、上傳、刪除、變更設定等）：你沒有任何寫入能力，輸出
   {{"type":"action_request","action":"review|confirmation|committee|settings|import|correction|mail|other","case":"案件代號或空字串"}}
規則：
- 案件、學生、人名都已換成代號（CASE_01、STU_01、PER_01）。回答時照抄代號，不要猜真實姓名或學號。
- 回答中的每個代號與數字都必須出自 sources 所列步驟的工具結果；不要推測，也不要自行加總或計算工具沒給的數字。
- 案件狀態一律照抄工具給的原文（例如「待專員放行」「逾期未回」），不要換說法。
- 條列用「- 」開頭，不要用 1. 2. 編號。回答簡短。
- 列出某狀態的案件時（例如「哪些案件待專員放行」），用工具的篩選參數查，只列工具結果中確實是該狀態的案件；查不到就回答「目前沒有…」。
- 與口試案件資料無關的問題，回答「{SCOPE_REPLY}」，sources 為 []。
- 規則或政策問題（口試費、交通費怎麼算、期限為什麼這樣訂）不在工具範圍內，不要推測，回答「{POLICY_REPLY}」，sources 為 []。
今天是 {today}。
可用工具：
{tools}"""


def _user_prompt(question: str, steps: list["Step"], *, force_answer: bool, feedback=()) -> str:
    lines = [f"問題：{question}", ""]
    if steps:
        lines.append("已執行的工具步驟：")
        for s in steps:
            lines.append(f"步驟 {s.index}：{s.tool}({json.dumps(s.args, ensure_ascii=False)}) → "
                         f"{json.dumps(s.output, ensure_ascii=False)}")
    else:
        lines.append("尚未執行任何工具。")
    if feedback:
        lines.append("\n你上一個回答沒有通過系統檢查（回答必須完全由工具結果佐證）：")
        lines.extend(f"- {p}" for p in feedback)
        lines.append("請補查需要的工具，或只用上面工具結果裡的數字、代號與狀態改寫回答；查不到就直接說查不到。")
    if force_answer:
        lines.append(f"\n已達查詢上限（{MAX_TOOL_STEPS} 步），不能再呼叫工具，請直接輸出 answer。")
    lines.append("\n請輸出下一個 JSON。")
    return "\n".join(lines)


@dataclass
class Step:
    index: int
    tool: str
    args: dict
    output: dict
    latency_ms: int


@dataclass
class TraceEntry:
    step: str
    tool: str | None = None
    input: str = ""
    output: object = None
    latency_ms: int | None = None
    decision: str | None = None


@dataclass
class AssistantAnswer:
    run_id: str
    kind: str                     # answer | unverified | refused | out_of_scope | error
    text: str = ""                # 代號化的回答（顯示時換回真實值）
    note: str = ""
    steps: list[Step] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)
    links: list[tuple[str, str, str]] = field(default_factory=list)   # (標籤, endpoint, anchor)
    link_case: str | None = None
    problems: list[str] = field(default_factory=list)
    aliaser: Aliaser | None = None
    model: str = ""
    latency_ms: int = 0

    @property
    def tokens(self) -> set[str]:
        text = self.text + json.dumps([s.output for s in self.steps], ensure_ascii=False)
        return {m.group(0) for m in TOKEN_RE.finditer(text)}


# ── 動作請求 ─────────────────────────────────────────────

def detect_action(question: str) -> str | None:
    """確定性判斷「要求助理做事」；回傳動作類別，查詢類問題回 None。"""
    q = str(question or "").strip()
    if _IMPERATIVE_RE.search(q):
        return guess_action(q)
    if not _ACTION_RE.search(q) or _QUERY_HINT.search(q):
        return None
    return guess_action(q)


def detect_policy(question: str) -> bool:
    """確定性判斷「問規則／政策」（費用怎麼算、期限怎麼訂）：工具沒有規則資料，不交給模型猜。"""
    q = str(question or "").strip()
    if _FEE_RE.search(q) and _FEE_ASK_RE.search(q):
        return True
    # 「規定的補件截止日是哪天？」問的是日期（get_deadlines 查得到），不算規則問題
    return bool(_POLICY_RE.search(q)) and not _DATE_ASK_RE.search(q)


def guess_action(text: str) -> str:
    for action, words in _ACTION_KEYWORDS:
        if any(w in text for w in words):
            return action
    return "other"


def _action_links(action: str) -> list[tuple[str, str, str]]:
    label, endpoint, anchor = ACTION_PAGES.get(action, ACTION_PAGES["other"])
    links = [(label, endpoint, anchor)]
    if action != "other":
        links.append(ACTION_PAGES["other"])
    return links


# ── 回答檢查 ─────────────────────────────────────────────

def _numbers(text: str) -> set[str]:
    out = set()
    for raw in _NUMBER_RE.findall(_DOC_LABEL_RE.sub(" ", TOKEN_RE.sub(" ", text))):
        try:
            value = float(raw)
        except ValueError:
            continue
        out.add(str(int(value)) if value.is_integer() else str(value))
    return out


def _token_numbers(obj, inherited=frozenset(), out=None, loose=None) -> tuple[dict[str, set[str]], set[str]]:
    """（代號 → 跟它在同一筆資料裡的數字, 不屬於任何代號的數字）；用來檢查「哪個案件是哪個值」（審查 F05）。"""
    out = {} if out is None else out
    loose = set() if loose is None else loose
    if isinstance(obj, list):
        for v in obj:
            _token_numbers(v, inherited, out, loose)
        return out, loose
    if not isinstance(obj, dict):
        return out, loose
    scalars = [v for v in obj.values() if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    scalars += [x for v in obj.values() if isinstance(v, list) for x in v if isinstance(x, (str, int, float))]
    text = " ".join(str(v) for v in scalars)
    tokens = set(inherited) | {m.group(0) for m in TOKEN_RE.finditer(text)}
    numbers = _numbers(text)
    if tokens:
        for t in tokens:
            out.setdefault(t, set()).update(numbers)
    else:
        loose |= numbers
    for v in obj.values():
        if isinstance(v, dict) or (isinstance(v, list) and any(isinstance(x, (dict, list)) for x in v)):
            _token_numbers(v, frozenset(tokens), out, loose)
    return out, loose


def _token_labels(obj, vocab, inherited=frozenset(), out=None) -> dict[str, set[str]]:
    """每個代號 → 工具結果中跟它出現在同一筆（或上層）資料裡的狀態詞。"""
    out = {} if out is None else out
    if isinstance(obj, list):
        for v in obj:
            _token_labels(v, vocab, inherited, out)
        return out
    if not isinstance(obj, dict):
        return out
    direct_strings = []
    for v in obj.values():
        if isinstance(v, str):
            direct_strings.append(v)
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            direct_strings.extend(v)
    text = " ".join(direct_strings)
    tokens = set(inherited) | {m.group(0) for m in TOKEN_RE.finditer(text)}
    labels = _labels_in(text, vocab)
    for t in tokens:
        out.setdefault(t, set()).update(labels)
    for v in obj.values():
        if isinstance(v, (dict, list)) and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            _token_labels(v, vocab, frozenset(tokens), out)
    return out


def _labels_in(text: str, vocab) -> set[str]:
    found = set()
    for word in vocab:  # 長詞在前，比對到就挖掉，避免短詞重複命中
        if word in text:
            found.add(word)
            text = text.replace(word, " ")
    return found


def _scope_reply(text: str) -> str | None:
    """模型照抄 prompt 指定的範圍外回覆（容許標點與空白差異）→ "scope"／"policy"；否則 None。"""
    norm = re.sub(r"[\s。，、！!.,「」]", "", text)
    if norm == re.sub(r"[，。]", "", SCOPE_REPLY):
        return "scope"
    if norm == re.sub(r"[，。]", "", POLICY_REPLY):
        return "policy"
    return None


def verify_answer(answer: str, sources, steps: list[Step], question: str, today: str,
                  track_labels: dict | None = None) -> list[str]:
    """回傳不符合的原因；空清單＝通過。answer、question 都是代號化後的文字。"""
    problems = []
    vocab = status_vocabulary(track_labels)
    body = _ORDINAL_RE.sub("", answer)
    tokens = {m.group(0) for m in TOKEN_RE.finditer(body)}
    numbers = _numbers(body)
    labels = _labels_in(body, vocab)
    if _CLAIMED_ACTION_RE.search(body):
        problems.append("回答聲稱已代為執行動作")
    has_facts = bool(tokens or numbers or labels)
    if not isinstance(sources, list):
        sources = []
    by_index = {s.index: s for s in steps}
    cited = []
    for src in sources:
        try:
            idx = int(src)
        except (TypeError, ValueError):
            problems.append(f"引用的步驟不存在：{src}")
            continue
        if idx not in by_index:
            problems.append(f"引用的步驟不存在：{src}")
        elif "error" in by_index[idx].output:
            problems.append(f"引用的步驟 {idx} 是錯誤結果")
        else:
            cited.append(by_index[idx])
    if has_facts and not cited:
        problems.append("回答含案件、數字或狀態，但沒有引用任何工具結果")
        return problems
    # 沒有查任何資料就下結論（審查 F05）；反問使用者（以問號結尾）不算結論
    if not cited and _scope_reply(body) is None and not re.search(r"[？?]\s*$", body.strip()):
        problems.append("回答沒有引用任何工具結果")
        return problems
    evidence = json.dumps([s.output for s in cited], ensure_ascii=False)
    known_tokens = {m.group(0) for m in TOKEN_RE.finditer(evidence)}
    for t in sorted(tokens - known_tokens):
        problems.append(f"代號 {t} 不在引用的工具結果中")
    allowed = _numbers(evidence) | _numbers(question) | _numbers(today)
    for n in sorted(numbers - allowed):
        problems.append(f"數字 {n} 不在引用的工具結果中")
    token_labels = _token_labels([s.output for s in cited], vocab)
    token_numbers, loose_numbers = _token_numbers([s.output for s in cited])
    free = loose_numbers | _numbers(question) | _numbers(today)
    for sentence in _SENTENCE_SPLIT.split(body):
        s_tokens = {m.group(0) for m in TOKEN_RE.finditer(sentence)}
        if s_tokens:   # 同一句提到的案件，數字要來自那個案件自己的資料，不能張冠李戴（審查 F05）
            paired = set().union(*(token_numbers.get(t, set()) for t in s_tokens)) | free
            for n in sorted((_numbers(sentence) & numbers) - paired):
                problems.append(f"數字 {n} 不是工具結果中 {'、'.join(sorted(s_tokens))} 的資料")
        s_labels = _labels_in(sentence, vocab)
        if not s_tokens or not s_labels:
            continue
        valid = set().union(*(token_labels.get(t, set()) for t in s_tokens))
        for label in sorted(s_labels - valid):
            problems.append(f"「{label}」不是工具結果中 {'、'.join(sorted(s_tokens))} 的狀態")
    problems.extend(_filter_mismatches(body, question, token_labels, vocab))
    return problems


def requested_statuses(question: str, vocab) -> set[str]:
    """列舉型問題要的狀態（「哪些案件卡在待專員放行」→ {待專員放行}）；問特定案件或判斷不出來回空集合。"""
    q = str(question or "")
    if TOKEN_RE.search(q) or not _LISTING_Q.search(q):
        return set()
    found = _labels_in(q, vocab)
    wanted = {w for w in found if w not in _NOT_FILTERABLE}
    rest = q
    for word in sorted(found, key=len, reverse=True):
        rest = rest.replace(word, " ")
    for phrases, labels in _REQUESTED_STATUS:   # 長說法在前，比對到就挖掉，避免「補件逾期」又被「逾期」命中
        hit = [p for p in phrases if p in rest]
        if hit:
            wanted.update(labels)
            for p in hit:
                rest = rest.replace(p, " ")
    return wanted


def _filter_mismatches(body: str, question: str, token_labels: dict, vocab) -> list[str]:
    """回答列出的案件／人員必須符合問題要的狀態：問「待專員放行」卻列出「附件6尚未交付」的案件 → 不通過。"""
    wanted = requested_statuses(question, vocab)
    if not wanted:
        return []
    problems = []
    for sentence in _SENTENCE_SPLIT.split(body):
        s_tokens = {m.group(0) for m in TOKEN_RE.finditer(sentence)}
        if not s_tokens or _NEGATION.search(sentence):
            continue
        actual = set().union(*(token_labels.get(t, set()) for t in s_tokens))
        if actual and not actual & wanted:
            problems.append(f"問題問的是「{'／'.join(sorted(wanted))}」，但工具結果中 "
                            f"{'、'.join(sorted(s_tokens))} 的狀態是「{'、'.join(sorted(actual))}」")
    return problems


# ── 主流程 ──────────────────────────────────────────────

class _Run:
    def __init__(self, state_dir, run_id, model):
        self.state_dir, self.run_id, self.model = state_dir, run_id, model
        self.trace: list[TraceEntry] = []

    def record(self, step, *, tool=None, input_summary="", output=None, latency_ms=None,
               decision=None, human_required=False, usage=None):
        self.trace.append(TraceEntry(step=step, tool=tool, input=input_summary, output=output,
                                     latency_ms=latency_ms, decision=decision))
        if self.state_dir is not None:
            agent_trace.trace_step(
                self.state_dir, agent=AGENT, run_id=self.run_id, step=step, tracking_key=self.run_id,
                tool=tool, input_summary=input_summary, output=output, model=self.model,
                prompt_version=PROMPT_VERSION, latency_ms=latency_ms, decision=decision,
                human_required=human_required, usage=usage)


def ask(question: str, *, snapshot: Snapshot, llm, state_dir=None, run_id: str | None = None) -> AssistantAnswer:
    started = time.perf_counter()
    run_id = run_id or uuid.uuid4().hex[:12]
    model = str(model_name(llm) or type(llm).__name__)
    run = _Run(state_dir, run_id, model)
    aliaser = Aliaser(snapshot)
    index = make_index(snapshot)
    today = snapshot.today.isoformat()
    q = aliaser.scrub(str(question or "").strip()[:500])
    run.record("question", input_summary=q)

    def finish(answer: AssistantAnswer) -> AssistantAnswer:
        answer.trace, answer.aliaser, answer.model = run.trace, aliaser, model
        answer.latency_ms = int((time.perf_counter() - started) * 1000)
        return answer

    def refuse(action: str, case_ref: str = "", source: str = "rule", usage=None) -> AssistantAnswer:
        case_key = aliaser.resolve_case(case_ref) if case_ref else None
        run.record("refused", input_summary=f"action={action}", decision=f"action_request:{source}",
                   human_required=True, usage=usage)
        return finish(AssistantAnswer(run_id=run_id, kind="refused", text=REFUSAL_TEXT,
                                      links=_action_links(action), link_case=case_key))

    if not q:
        return finish(AssistantAnswer(run_id=run_id, kind="error", text="請輸入問題。"))
    action = detect_action(question)
    if action:
        # 問題裡有指到案件（案件鍵、學號或姓名已換成代號）→ 一併附上該案件詳情的連結
        case_ref = next((m.group(0) for m in TOKEN_RE.finditer(q) if m.group(0).startswith(("CASE_", "STU_"))), "")
        return refuse(action, case_ref)
    if detect_policy(question):
        run.record("out_of_scope", input_summary="policy", decision="policy:rule")
        return finish(AssistantAnswer(run_id=run_id, kind="out_of_scope", text=POLICY_TEXT,
                                      links=list(POLICY_PAGES)))

    steps: list[Step] = []
    system = _system_prompt(today)
    feedback: list[str] = []
    reflections = 0
    for call in range(MAX_TOOL_STEPS + 1 + MAX_REFLECTIONS * 2):
        force = len(steps) >= MAX_TOOL_STEPS
        t0 = time.perf_counter()
        try:
            out = llm.complete_json(system, _user_prompt(q, steps, force_answer=force, feedback=feedback),
                                    schema_name=SCHEMA_NAME)
            if not isinstance(out, dict):
                raise ValueError("模型輸出不是 JSON 物件")
        except Exception as exc:
            run.record("llm_error", output={"error": type(exc).__name__},
                       latency_ms=_ms(t0), decision="error", human_required=True, usage=last_usage(llm))
            return finish(AssistantAnswer(
                run_id=run_id, kind="error", steps=steps,
                text="問答助理暫時無法使用（模型連線或回應失敗），請稍後再試，或直接查看工作總覽。",
                note=UNVERIFIED_NOTE if steps else "", links=[ACTION_PAGES["other"]]))
        kind = out.get("type")
        decision_ms = _ms(t0)
        usage = last_usage(llm)   # 這一輪模型呼叫的 token 用量，記在這一輪的軌跡步驟上
        if kind == "tool_call" and not force:
            name = str(out.get("tool", ""))
            args = out.get("args") if isinstance(out.get("args"), dict) else {}
            t1 = time.perf_counter()
            output = run_tool(name, args, snapshot, aliaser, index)
            step = Step(index=len(steps) + 1, tool=name, args=args, output=output, latency_ms=_ms(t1))
            steps.append(step)
            run.record("tool_call", tool=name, input_summary=json.dumps(args, ensure_ascii=False),
                       output=output, latency_ms=decision_ms + step.latency_ms, decision="tool_call",
                       usage=usage)
            continue
        if kind == "action_request":
            return refuse(str(out.get("action") or guess_action(question)),
                          str(out.get("case") or ""), source="model", usage=usage)
        if kind == "answer":
            text = str(out.get("answer") or "").strip()
            sources = out.get("sources")
            scope_reply = _scope_reply(text)
            if scope_reply and not sources:
                run.record("out_of_scope", input_summary=text[:200], decision=f"{scope_reply}:model", usage=usage)
                if scope_reply == "policy":
                    return finish(AssistantAnswer(run_id=run_id, kind="out_of_scope", text=POLICY_TEXT,
                                                  links=list(POLICY_PAGES), steps=steps))
                return finish(AssistantAnswer(run_id=run_id, kind="out_of_scope", text=SCOPE_REPLY, steps=steps))
            problems = verify_answer(text, sources, steps, q, today, snapshot.track_labels) if text \
                else ["回答是空的"]
            reflect = bool(problems) and reflections < MAX_REFLECTIONS
            run.record("grounding_check", input_summary=text[:2000],
                       output={"sources": sources, "problems": problems}, latency_ms=decision_ms,
                       decision="pass" if not problems else "repair" if reflect else "fail",
                       human_required=bool(problems) and not reflect, usage=usage)
            if not problems:
                return finish(AssistantAnswer(run_id=run_id, kind="answer", text=text, steps=steps))
            if reflect:
                # 反思：把檢查不通過的具體原因回饋給模型，讓它補查工具或改寫，最多 MAX_REFLECTIONS 次
                reflections += 1
                feedback = list(problems)
                run.record("reflect", input_summary="；".join(problems)[:300], decision="repair")
                continue
            return finish(AssistantAnswer(run_id=run_id, kind="unverified", steps=steps, note=UNVERIFIED_NOTE,
                                          problems=problems))
        problem = "超過查詢步數上限仍未回答" if kind == "tool_call" else f"模型輸出格式不符：{kind!r}"
        run.record("grounding_check", output={"problems": [problem]}, latency_ms=decision_ms,
                   decision="fail", human_required=True, usage=usage)
        return finish(AssistantAnswer(run_id=run_id, kind="unverified", steps=steps, note=UNVERIFIED_NOTE,
                                      problems=[problem]))
    run.record("grounding_check", output={"problems": ["超過模型呼叫上限仍未回答"]}, decision="fail",
               human_required=True)
    return finish(AssistantAnswer(run_id=run_id, kind="unverified", steps=steps, note=UNVERIFIED_NOTE,
                                  problems=["超過模型呼叫上限仍未回答"]))


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


# ── 顯示 ─────────────────────────────────────────────────

def segments(text: str, aliaser: Aliaser | None) -> list[tuple[str, str]]:
    """代號化文字 → [(種類, 值)]，種類為 text／case／stu／per；網頁層據此把案件鍵做成連結。"""
    text = str(text or "").replace("**", "")
    out, pos = [], 0
    for m in TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.append(("text", text[pos:m.start()]))
        out.append(aliaser.display(m.group(0)) if aliaser else ("text", m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.append(("text", text[pos:]))
    return out


def output_tables(output: dict) -> list[dict]:
    """工具輸出 → 簡單表格（純量欄位一張「欄位／值」表，每個物件清單各一張表）。"""
    if not isinstance(output, dict):
        return [{"title": "", "headers": ["值"], "rows": [[str(output)]]}]
    scalars, tables = [], []
    for key, value in output.items():
        if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            headers = []
            for row in value:
                headers += [k for k in row if k not in headers]
            tables.append({"title": key, "headers": headers,
                           "rows": [[_cell(row.get(h)) for h in headers] for row in value]})
        else:
            scalars.append([key, _cell(value)])
    if scalars:
        tables.insert(0, {"title": "", "headers": ["欄位", "值"], "rows": scalars})
    return tables


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "、".join(_cell(v) for v in value)
    if isinstance(value, dict):
        return "；".join(f"{k}：{_cell(v)}" for k, v in value.items())
    return str(value)
