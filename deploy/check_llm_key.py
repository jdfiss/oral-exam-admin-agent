"""模型金鑰自我檢查：找出「金鑰明明填了卻怪怪的」原因，並實際呼叫一次 API。

用法：.venv\\Scripts\\python.exe deploy\\check_llm_key.py [--no-call]
--no-call 只檢查設定、不連網。

只讀取、不寫入；金鑰一律只顯示前後 4 碼，錯誤訊息裡的金鑰也會遮掉，輸出可以直接截圖給組長。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

KEY_NAMES = ("GEMINI_API_KEY", "OPENAI_API_KEY")
WATCHED = KEY_NAMES + ("OPENAI_BASE_URL", "KOUSHI_LLM_PROVIDER", "KOUSHI_LLM_MODEL")


def mask(value: str) -> str:
    if len(value) <= 10:
        return f"（長度 {len(value)}，太短，可能沒貼完整）"
    return f"{value[:4]}…{value[-4:]}（長度 {len(value)}）"


def scan_env_file(path: Path) -> tuple[dict[str, str], list[str]]:
    """逐行檢查 .env 的格式問題；回傳（去掉引號與註解後的值, 問題清單）。

    檢查的是程式讀 .env 時會出錯的寫法（koushi.mail.email_sender._parse 不處理 BOM、單引號與行尾註解）。
    """
    raw = path.read_bytes()
    problems = []
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append(f"{path.name} 存成「UTF-8 含 BOM」：第一行的變數會讀不到。"
                        "用 VS Code 開啟 → 右下角「UTF-8 with BOM」→「以編碼儲存」→ 選「UTF-8」")
    values = {}
    for number, line in enumerate(raw.decode("utf-8-sig", "replace").splitlines(), 1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        key, eq, value = text.partition("=")
        key = key.strip()
        if not eq:
            problems.append(f"第 {number} 行沒有等號：{key[:20]}")
            continue
        if key not in WATCHED:
            continue
        value = value.strip()
        if "#" in value and value[value.index("#") - 1:value.index("#")].isspace():
            problems.append(f"第 {number} 行 {key} 行尾有註解，註解會被當成值的一部分：把 # 之後刪掉")
            value = value[:value.index("#")].strip()
        if value[:1] in "'\"" or value[-1:] in "'\"":
            if value[:1] == "'" or value[-1:] == "'":
                problems.append(f"第 {number} 行 {key} 用了單引號，程式不會去掉：把引號刪掉")
            value = value.strip("'\"")
        if " " in value:
            problems.append(f"第 {number} 行 {key} 的值中間有空白：重新複製金鑰")
        values[key] = value
    return values, problems


def key_slot_problems(env: dict[str, str]) -> list[str]:
    """金鑰填錯欄位：最常見的是把 Gemini 金鑰填進 OPENAI_API_KEY 卻沒設 OPENAI_BASE_URL。"""
    problems = []
    openai_key = env.get("OPENAI_API_KEY", "")
    base_url = env.get("OPENAI_BASE_URL", "")
    if openai_key.startswith("AIza") and "generativelanguage" not in base_url:
        problems.append("OPENAI_API_KEY 填的是 Gemini 金鑰（AIza 開頭），會被送到 OpenAI 而回「金鑰錯誤」："
                        "改成 GEMINI_API_KEY=…")
    if env.get("GEMINI_API_KEY", "").startswith("sk-"):
        problems.append("GEMINI_API_KEY 填的是 OpenAI 金鑰（sk- 開頭）：改成 OPENAI_API_KEY=…")
    if (env.get("KOUSHI_LLM_PROVIDER") or "").strip().lower() == "offline":
        problems.append("KOUSHI_LLM_PROVIDER=offline：有金鑰也不會用，要用真實模型就刪掉這行")
    return problems


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    call = "--no-call" not in sys.argv[1:]
    from koushi.mail.email_sender import ENV_PATHS

    # 程式讀設定的順序：系統環境變數最優先，其次專案根目錄 .env，最後 koushi\.env（koushi.runtime.load_local_env）
    before = {name: os.environ.get(name, "") for name in WATCHED}
    problems: list[str] = []
    files: dict[Path, dict[str, str]] = {}
    for path in reversed(ENV_PATHS):
        if path.is_file():
            values, found = scan_env_file(path)
            files[path] = values
            problems += found
            print(f"讀到設定檔：{path}")
    if not files:
        print("沒有 .env：在專案根目錄新增 .env，填一行 GEMINI_API_KEY=AIza… 或 OPENAI_API_KEY=sk-…")

    effective = {}
    for name in WATCHED:
        sources = [("系統環境變數", before[name])] + [(str(p.relative_to(ROOT)), v.get(name, "")) for p, v in files.items()]
        sources = [(where, value) for where, value in sources if value]
        if not sources:
            continue
        where, value = sources[0]
        effective[name] = value
        shown = mask(value) if name in KEY_NAMES else value
        print(f"  {name} = {shown}　← 來自 {where}")
        if len(sources) > 1 and len({v for _, v in sources}) > 1:
            ignored = "、".join(w for w, _ in sources[1:])
            problems.append(f"{name} 在多處設定且值不同，實際用的是「{where}」，{ignored} 的值被忽略。"
                            + ("刪掉系統環境變數裡的舊值（設定 → 系統 → 進階系統設定 → 環境變數），再重開 PowerShell"
                               if where == "系統環境變數" else "只留一處"))
    problems += key_slot_problems(effective)

    from koushi.llm import offline_selected, provider_config
    conf = provider_config()
    print()
    if offline_selected():
        print("程式判斷：強制離線（KOUSHI_LLM_PROVIDER=offline），不會用任何金鑰")
    elif conf is None or conf["provider"] == "ollama":
        print("程式判斷：沒有可用的雲端金鑰" if conf is None else f"程式判斷：本機模型 {conf['model']}（不需金鑰）")
    else:
        print(f"程式判斷：供應商 {conf['provider']}、模型 {conf['model']}、金鑰 {mask(conf['api_key'])}")

    print()
    if problems:
        print("⚠ 發現問題：")
        for item in problems:
            print(f"  - {item}")
    else:
        print("設定格式沒有發現問題")

    if not call or conf is None or conf["provider"] == "ollama":
        return 1 if problems or conf is None else 0
    print("\n實際呼叫一次 API（花費趨近於 0）…")
    from openai import OpenAI
    client = OpenAI(api_key=conf["api_key"], base_url=conf["base_url"], timeout=30)
    started = time.time()
    try:
        reply = client.chat.completions.create(model=conf["model"], max_tokens=5,
                                               messages=[{"role": "user", "content": "回覆 OK 兩個字"}])
    except Exception as exc:  # 把錯誤原因翻成白話；金鑰本身遮掉
        message = str(exc).replace(conf["api_key"], "<金鑰>")
        hint = ("金鑰錯誤或已失效：重新到官網複製，確認填在對的欄位" if "401" in message or "invalid" in message.lower()
                else "額度用完或呼叫太頻繁：等一下再試，或改用付費金鑰" if "429" in message
                else "帳號沒有這個模型的權限：在 .env 設 KOUSHI_LLM_MODEL 換模型" if "404" in message
                else "連不上：檢查網路或公司／學校防火牆" if "connect" in message.lower() or "timeout" in message.lower()
                else "")
        print(f"❌ 失敗：{type(exc).__name__}: {message[:300]}")
        if hint:
            print(f"   可能原因：{hint}")
        return 1
    print(f"✅ 成功（{time.time() - started:.1f} 秒）：回覆 {reply.choices[0].message.content!r}，實際模型 {reply.model}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
