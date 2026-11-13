# -*- coding: utf-8 -*-
"""測試共用設定。

程式預設的機構識別是匿名示範值（示範大學／示範學系）；既有測試資料以一組「正式環境」
校系撰寫（校內／校外判定、口試費規則都依此斷言），所以在 koushi.config 載入前明確指定。
這組校系是虛構的「國立範例大學」，刻意與程式預設值不同，才能驗證設定值確實生效；
比賽匿名審查不得出現真實校系，見 tests/test_anonymity.py。
子程序（CLI 測試）也會繼承這些環境變數。
"""
import os

TEST_INSTITUTION_ENV = {
    "KOUSHI_UNIV_NAME": "國立範例大學",
    "KOUSHI_DEPT_NAME": "資訊工程學系",
    "KOUSHI_OFFICE_SIGNATURE": "範例大學資工系辦",
    "KOUSHI_HOME_DEPT_KEYS": "範例大學資訊工程,範例大學資工",
    "KOUSHI_HOME_UNIV_KEYS": "範例大學",
    "KOUSHI_HOME_DEPT_LABEL": "本校資訊工程學系",
}
for _key, _value in TEST_INSTITUTION_ENV.items():
    os.environ[_key] = _value

# 測試一律不讀本機 .env、不帶 LLM 金鑰：開發者 .env 裡的真金鑰不能讓測試呼叫真實模型
os.environ["KOUSHI_NO_LOCAL_ENV"] = "1"
for _key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OPENAI_BASE_URL", "KOUSHI_LLM_MODEL", "KOUSHI_LLM_PROVIDER",
             "KOUSHI_LLM_MIN_INTERVAL"):
    os.environ.pop(_key, None)

# 剛下載的程式碼沒有附件4／附件6 範本（官方範本含真實資料不進 Git）：自動補虛構示範範本，
# 不需 LibreOffice，也不必先跑 README 步驟 1，pytest 直接就能全過
from koushi.demo_templates import use_demo_templates_if_missing  # noqa: E402

use_demo_templates_if_missing()
