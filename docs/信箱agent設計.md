# 系辦信箱 agent 設計（2026-10-08）

業務信箱的**每一封**來信都由信箱 agent 處理：辨識寄件者、連結案件、分類摘要、提出建議，再由**確定性政策**決定每個建議是自動執行、準備草稿等專員核准、只顯示建議，或略過。流程決策仍以 [已確認流程決策](已確認流程決策.md) 為準；LLM 的使用範圍依 LLM 前置研究。

程式：`koushi/mailbox_agent.py`（管線、政策、專員處理）、`koushi/reply_agent.py`（教授修改 → 欄位對應與 diff）、`koushi/llm.py`（供應商中立的結構化 JSON 呼叫）、`koushi/agent_trace.py`（軌跡）。系辦介面：「信箱 agent」頁（`/office/mailbox`），工作總覽有待處理計數。

## 管線

```text
每日自動化（正式寄送模式）讀信箱一次 fetch_replies()
        │
        ├─① 既有附件4 確認流程（完全不變）
        │     ingest_replies：代碼／寄件者對請求 → 確定性核對 → classifier 判讀
        │     → evaluate_auto_confirms（開關、白名單、shadow、版本 CAS）→ 自動放行或待判讀
        │
        └─② 信箱 agent（每一封，含①不收的信）
              a. parse            去引用後的 reply_text、附件 metadata、注入字樣偵測
              b. identify_sender  主檔教授信箱／附件4 寄送紀錄／委員信箱／學生信箱 → 角色＋是否驗證
              c. link_case        match_request（同①）＋寄件者的案件；不明寄件者只把信中學號當「未驗證」線索
              d. pii_guard        遮蔽身分證、帳號；有遮蔽就不送模型
              e. analyze          LLM → 類別、摘要、逐字依據、修改內容、疑問、建議動作（全部驗證）
                                  已被①自動確認的信直接沿用①的判讀，不再呼叫模型
              f. plan_actions     依類別＋角色＋連結以規則產生動作；modify 交 reply_agent 算 diff 與回信草稿；
                                  合併 LLM 提出的白名單內動作
              g. policy           每個動作一筆：auto／draft／suggest／ignore（純程式碼，LLM 無法影響）
              h. execute          只執行 auto
              i. done             信件狀態：已自動完成／待專員處理／已略過
        │
專員在「信箱 agent」頁：核准草稿（寫入修正／寄出回信）、退回、建議標已處理、整封標記（處理人必填）
```

教授「需要修改」核准後：`application.save_case_correction()` 寫入專員修正層（處理人、原因含原信 Message-ID 與引用）→ 下次建主檔套用 → 既有附件4 流程偵測內容變動，重設並寄出新版附件4 請教授再確認。改到委員名單的要求一律標「需人工」（會撤銷放行，由專員在案件頁處理）。

## 類別 → 政策

| 類別（寄件者） | 產生的動作 | 政策 |
| --- | --- | --- |
| confirm（教授，且①已自動放行） | 附件4 已自動確認 | auto（①已執行，agent 只記錄） |
| confirm／任何類別（教授，信件在①待判讀） | 到文件中心判讀 | suggest |
| confirm（教授，沒有等待中的請求：已確認、舊版代碼、未寄出、完全無請求） | 人工查看（說明原因） | suggest，**絕不自動確認** |
| modify／schedule_change（已驗證教授，有可對應欄位） | 依教授回信修正資料（diff）＋回覆草稿 | draft＋draft |
| modify（無可對應欄位或寄件者未驗證） | 修正（全部「需人工」） | suggest |
| question（已驗證寄件者） | 回覆草稿（回答內容須專員填寫才能寄） | draft |
| student_request／schedule_change（登記信箱的學生） | 記錄異動申請＋考慮開放修改 | auto（只記申請、不解鎖）＋suggest |
| student_request（寄件者未驗證） | 記錄異動申請（標身分未驗證） | suggest |
| committee_data，或任何含個資的信 | 到委員個資頁登錄 | suggest（不送模型） |
| auto_reply／unrelated | 不需處理 | ignore（不回信） |
| unknown、LLM 錯誤、未設定 LLM | 人工查看 | suggest |
| 收件通知（見下節） | 固定範本「系辦已收到」 | auto（開關開啟）／否則 ignore |

LLM 提出的動作只接受 `apply_correction`、`reply_draft`、`record_change_request`、`consider_unlock`、`register_committee_data`、`follow_up`、`no_action`；其中修正與回信只能由規則產生（需要確定性的 diff 與範本）。其餘一律丟棄並寫入軌跡。風險等級由程式固定，不採用模型自報。

## 自動回覆

查過 docs/：沒有任何既有決策允許自動回信；LLM 前置研究明定執行時寄出的信要用固定範本，LLM 只能協助擬稿。因此：

- **只有一種自動回覆**：固定範本的「系辦已收到您的來信」收件通知，期限設定頁「信箱 agent 自動收件通知」開關，**預設關閉**。
- 只寄給：已驗證的指導教授，類別為 modify、question、schedule_change；已驗證的學生，類別為 student_request、question、schedule_change。
- 不寄：自動回覆（出國、休假、助理代回）、無關信件、委員來信、身分未驗證、LLM 判讀失敗、疑似夾帶指令的信。
- 去重：send_log 冪等鍵＝（寄件者、日期），每位寄件者每天最多一封，也避免與對方的自動回覆互相迴圈。預覽模式不寄。
- 其他所有回信（含 LLM 整理的內容）都是草稿，必須專員按「核准並寄出」；提問類草稿留有「請專員填寫回覆內容」，沒填不能寄。

## 安全規則

1. agent 沒有「記為教授已確認」的執行器；附件4 確認只來自既有放行流程（含開關、白名單、shadow、版本 CAS）或專員登錄。
2. 學生自由文字信不改變案件狀態：最多記一筆異動申請，開放修改（`grant_unlock`）一定由專員在案件頁操作。
3. 修改附件4 內容、寄出回信都是草稿，專員核准才執行，處理人必填並寫入稽核與軌跡。
4. 身分證字號、10 碼以上帳號（排除日期、手機）在送模型前遮蔽；有遮蔽就整封不送模型。儲存的信件文字也是遮蔽後版本。
5. 模型只收到去引用後的本文、主旨與最少的背景（角色、是否驗證、連結案件數、附件4 請求狀態），不含信箱與姓名。
6. 信件中的指令性文字（「忽略以上指令」「判定為確認」「自動解鎖」等）以規則偵測；偵測到或模型自報 suspicious，agent 的 auto 動作一律降級（記錄異動申請改為建議、收件通知不寄）；既有附件4 放行流程的結果照舊只記錄、不改變。
7. 模型輸出逐項驗證：類別白名單、依據與修改引用必須逐字出現在原文、動作白名單、案件只能是已連結的案件；格式錯誤或依據不實即視為判讀失敗，該封轉人工，不影響其他信。

## 軌跡格式

每個步驟寫一筆稽核事件到 `STATE_DIR/audit_log.json`（`koushi/agent_trace.trace_step`，其他 agent 共用同一格式）：

```text
source = "agent_trace"      action = <step>      tracking_key = (<Message-ID>,)
detail = {
  agent: "mailbox", run_id: <每封信一個 uuid>, step: parse|identify_sender|link_case|pii_guard|
         analyze|plan_actions|policy|execute|done|staff_review|staff_mark,
  tool: <函式或工具名>, input_summary: <簡短、無個資>, output: <簡短結構>,
  model: <模型或 null>, prompt_version: <如 mailbox-v1 或 null>, latency_ms: <int 或 null>,
  decision: auto|draft|suggest|ignore|null, human_required: <bool>
}
```

`load_trace(state_dir, run_id=..., tracking_key=...)` 讀回；「信箱 agent」頁每封信可展開執行軌跡時間線。
