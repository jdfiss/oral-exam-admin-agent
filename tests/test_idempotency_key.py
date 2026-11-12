# -*- coding: utf-8 -*-
"""drafts.compute_idempotency_key 的防重寄保證。"""

from koushi.drafts import compute_idempotency_key


def test_same_input_same_key():
    tracking = {"case_key": "1141-111900001", "issue_code": "MISSING_ID"}
    k1 = compute_idempotency_key("deficiency", tracking)
    k2 = compute_idempotency_key("deficiency", tracking)
    assert k1 == k2


def test_different_type_different_key():
    tracking = {"case_key": "1141-111900001"}
    k1 = compute_idempotency_key("deficiency", tracking)
    k2 = compute_idempotency_key("collection", tracking)
    assert k1 != k2


def test_different_tracking_different_key():
    k1 = compute_idempotency_key("deficiency", {"case_key": "1141-111900001"})
    k2 = compute_idempotency_key("deficiency", {"case_key": "1141-111900002"})
    assert k1 != k2


def test_key_is_hex_string_of_fixed_length():
    k = compute_idempotency_key("deficiency", {"x": 1})
    assert len(k) == 16
    assert all(c in "0123456789abcdef" for c in k)


def test_dict_key_order_does_not_matter():
    t1 = {"a": 1, "b": 2}
    t2 = {"b": 2, "a": 1}
    assert compute_idempotency_key("t", t1) == compute_idempotency_key("t", t2)
