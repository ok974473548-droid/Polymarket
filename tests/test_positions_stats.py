import numpy as np
import pandas as pd
import pytest

from whalefollow.positions import _build
from whalefollow.scoring.stats import bh_qvalues, bootstrap_wmean, null_pvalue, shrink

MK = pd.DataFrame(
    [
        {"market_id": "m1", "event_id": "e1", "category": "economics", "status": "resolved", "resolved_ts": 500},
        {"market_id": "m2", "event_id": "e2", "category": "sports", "status": "open", "resolved_ts": None},
    ]
)
TOK = pd.DataFrame(
    [{"token_id": "y1", "market_id": "m1", "payout": 1.0}, {"token_id": "y2", "market_id": "m2", "payout": None}]
)


def _t(i, ts, w, tok, side, price, shares):
    return {
        "trade_id": str(i),
        "ts": ts,
        "wallet": w,
        "token_id": tok,
        "market_id": None,
        "side": side,
        "price": price,
        "size_shares": shares,
        "size_usd": price * shares,
    }


def test_position_resolved_with_partial_sell():
    tr = pd.DataFrame(
        [
            _t(1, 10, "w", "y1", "BUY", 0.4, 100),
            _t(2, 20, "w", "y1", "BUY", 0.6, 100),
            _t(3, 30, "w", "y1", "SELL", 0.7, 50),
        ]
    )
    p = _build(tr, MK, TOK).iloc[0]
    assert p["avg_entry"] == pytest.approx(0.5)
    # R = (35 выручки + 150 оставшихся × 1) / 200 купленных
    assert p["exit_value_per_share"] == pytest.approx(0.925)
    assert p["pnl_usd"] == pytest.approx(85.0)
    assert p["status"] == "resolved" and p["held_to_resolution"] == 1 and p["closed_ts"] == 500


def test_position_fully_sold_before_resolution():
    tr = pd.DataFrame([_t(1, 10, "w", "y2", "BUY", 0.3, 100), _t(2, 50, "w", "y2", "SELL", 0.5, 100)])
    p = _build(tr, MK, TOK).iloc[0]
    assert p["status"] == "closed" and p["exit_value_per_share"] == pytest.approx(0.5)
    assert p["held_to_resolution"] == 0 and p["closed_ts"] == 50


def test_open_position_has_no_exit():
    tr = pd.DataFrame([_t(1, 10, "w", "y2", "BUY", 0.3, 100)])
    p = _build(tr, MK, TOK).iloc[0]
    assert p["status"] == "open" and np.isnan(p["exit_value_per_share"])


def test_shrink_example_from_tz():
    assert shrink(0.12, 10, 25) == pytest.approx(0.0343, abs=1e-4)
    assert shrink(0.12, 200, 25) == pytest.approx(0.1067, abs=1e-4)


def test_bh_qvalues_known():
    q = bh_qvalues([0.01, 0.04, 0.03, 0.5])
    assert q == pytest.approx([0.04, 0.0533, 0.0533, 0.5], abs=1e-3)


def test_null_pvalue_separates_skill_from_luck():
    rng = np.random.default_rng(0)
    n = 150
    p = rng.uniform(0.2, 0.8, n)
    w = np.ones(n)
    lucky = (rng.random(n) < p).astype(float)  # исходы как у рынка — навыка нет
    skilled = (rng.random(n) < np.clip(p + 0.15, 0, 1)).astype(float)
    assert null_pvalue(p, skilled, w, 4000, rng) < 0.01
    assert null_pvalue(p, lucky, w, 4000, rng) > 0.01


def test_bootstrap_ci_contains_mean():
    rng = np.random.default_rng(1)
    x = rng.normal(0.05, 0.3, 200)
    lo, hi = bootstrap_wmean(x, np.ones_like(x), 2000, rng)
    assert lo < x.mean() < hi
