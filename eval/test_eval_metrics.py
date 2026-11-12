# -*- coding: utf-8 -*-
"""評測工具本身的測試（不呼叫 API）。執行：.venv\\Scripts\\python.exe -m pytest eval -q"""

import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL_DIR))

import data as D  # noqa: E402
import metrics as M  # noqa: E402
import report as R  # noqa: E402


def _rec(pred, **kw):
    return {"set": "dev", "variant": "reply_text", "id": kw.pop("id", 1), "predicted": pred, **kw}


def test_auto_release_gate_matches_production_conditions():
    assert M.would_auto_release(_rec("confirm"))
    assert not M.would_auto_release(_rec("confirm", has_reservation=True))
    assert not M.would_auto_release(_rec("confirm", questions=["地點？"]))
    assert not M.would_auto_release(_rec("confirm", modifications=[{"field": "日期"}]))
    assert not M.would_auto_release(_rec("confirm", production_rejected="判讀依據不是逐字引用"))
    assert not M.would_auto_release(_rec("modify"))
    assert not M.would_auto_release(_rec("error"))


def test_false_release_counts_reservation_confirm_as_unsafe():
    gold = {("dev", 1): {"expected_intent": "confirm", "expected_has_reservation": True, "category": "x"},
            ("dev", 2): {"expected_intent": "confirm", "category": "x"},
            ("dev", 3): {"expected_intent": "question", "category": "x"}}
    recs = [_rec("confirm", id=1), _rec("confirm", id=2), _rec("question", id=3)]
    s = M.summarize(recs, gold)
    assert s["auto_release"]["false_release"] == 1
    assert s["auto_release"]["false_release_ids"] == [1]
    assert s["auto_release"]["needless_human"] == 0
    assert s["accuracy"] == 1.0
    assert s["reservation"]["fn"] == 1


def test_skipped_blank_reply_goes_to_human():
    gold = {("dev", 1): {"expected_intent": "confirm", "category": "x"}}
    s = M.summarize([_rec("skipped", skipped=True)], gold)
    assert s["n_evaluated"] == 0
    assert s["auto_release"]["needless_human"] == 1


def test_modification_scoring_field_and_value():
    gold = [{"row": "", "field": "日期", "new_value": "12/25"}, {"row": "", "field": "方式", "new_value": "線上"}]
    pred = [{"field": "口試方式", "new_value": "線上口試"}, {"field": "date", "new_value": "12 月 25 日"}]
    s = M.score_modifications(gold, pred)
    assert (s["tp"], s["field_tp"], s["value_tp"]) == (2, 2, 2)
    # 欄位空白、新值對 → 只算新值
    s = M.score_modifications(gold[:1], [{"field": "", "new_value": "改成 12/25"}])
    assert (s["tp"], s["field_tp"], s["value_tp"]) == (0, 0, 1)
    # gold 沒有新值（只說要換委員）→ 欄位對即算對
    s = M.score_modifications([{"field": "委員", "new_value": ""}], [{"field": "口試委員", "new_value": "待定"}])
    assert s["tp"] == 1


def test_value_match_numbers():
    assert M.value_match("12/25", "12 月 25 日")
    assert not M.value_match("12/25", "12/20")
    assert M.value_match("工程館 A521", "工程館A521")


def test_quoted_set_cleaning_layer():
    cases = {c["id"]: c for c in D.load_quoted()}
    assert all(c["source"] == "synthetic" for c in cases.values())
    # 常見引用格式：去引用後只剩教授本人的話
    for cid in ("Q01", "Q02", "Q03", "Q04", "Q05", "Q06", "Q09", "Q11", "Q12"):
        assert "確認無誤" not in cases[cid]["cleaned_text"], cid
        assert cases[cid]["professor_reply"] in cases[cid]["cleaned_text"], cid
        assert "確認無誤" in cases[cid]["raw_text"], cid
    # 已知限制：底部回覆被整段切掉（空白 → 轉人工，安全方向）
    assert cases["Q14"]["cleaned_text"] == ""


def test_dev_gold_annotations_complete():
    dev = D.load_dev()
    assert len(dev) == 60
    for c in dev:
        assert c["source"] == "synthetic"
        if c["expected_intent"] == "modify":
            assert c["expected_modifications"], c["id"]
    assert sum(1 for c in dev if c["expected_has_reservation"]) == 5


def test_legacy_result_rescoring():
    path = EVAL_DIR / "results" / "eval_gpt-4o-mini_20260929_091300.json"
    summaries, _, _ = R.analyse(R.load_result(path))
    s = summaries[("dev", "reply_text")]
    assert s["correct"] == 56 and s["n_evaluated"] == 59
    assert s["auto_release"]["false_release"] == 0
