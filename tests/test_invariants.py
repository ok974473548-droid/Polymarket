"""Тесты-инварианты (hypothesis): свойства, которые обязаны выполняться на любых данных.

Позиции и PnL, комиссии Polymarket, исполнение по стакану, виртуальная покупка, «что было бы».
"""

import math

import polars as pl
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from whalefollow import fees
from whalefollow.backtest.whale_sim import simulate_whale
from whalefollow.db import DB
from whalefollow.execution.paper import PaperBroker, Portfolio
from whalefollow.positions import _build
from whalefollow.signals.orderbook import walk

from conftest import fast_params

FAST = settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SLOW = settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.too_slow])
price = st.floats(0.01, 0.99, allow_nan=False)
shares = st.floats(1.0, 5000.0, allow_nan=False)


# ---------------------------------------------------------------- позиции и PnL
trade = st.tuples(st.sampled_from(["BUY", "SELL"]), shares, price, st.integers(1, 10_000))
payout = st.sampled_from([None, 0.0, 1.0, 0.5])


def _frames(trades, pay, resolved_ts=None):
    tr = pl.DataFrame(
        [
            {
                "trade_id": f"t{i}",
                "wallet": "w",
                "token_id": "tok",
                "market_id": "m",
                "side": s,
                "price": p,
                "size_shares": n,
                "size_usd": n * p,
                "ts": ts,
            }
            for i, (s, n, p, ts) in enumerate(trades)
        ]
    )
    mk = pl.DataFrame(
        [
            {
                "market_id": "m",
                "event_id": "e",
                "category": "politics",
                "status": "x",
                "resolved_ts": resolved_ts,
                "end_ts": None,
            }
        ],
        schema_overrides={"resolved_ts": pl.Int64, "end_ts": pl.Int64},
    )
    tok = pl.DataFrame([{"token_id": "tok", "market_id": "m", "payout": pay}], schema_overrides={"payout": pl.Float64})
    return tr, mk, tok


@FAST
@given(st.lists(trade, min_size=1, max_size=12), payout, st.randoms())
def test_position_invariants(trades, pay, rnd):
    if not any(t[0] == "BUY" for t in trades):
        trades = [("BUY", 10.0, 0.5, 1), *trades]
    tr, mk, tok = _frames(trades, pay, resolved_ts=20_000 if pay is not None else None)
    p = _build(tr, mk, tok).row(0, named=True)
    buys = [t for t in trades if t[0] == "BUY"]
    bought = sum(t[1] for t in buys)
    cost = sum(t[1] * t[2] for t in buys)
    assert p["shares_bought"] == pytest.approx(bought)
    assert p["cost_usd"] == pytest.approx(cost)
    assert 0 <= p["shares_sold"] <= p["shares_bought"] + 1e-9  # продать больше купленного нельзя
    assert p["remaining_shares"] == pytest.approx(p["shares_bought"] - p["shares_sold"], abs=1e-6)
    assert min(t[2] for t in buys) - 1e-9 <= p["avg_entry"] <= max(t[2] for t in buys) + 1e-9
    if p["pnl_usd"] is not None:
        # потерять больше вложенного нельзя; заработать больше, чем «всё по 1$», — тоже
        assert p["pnl_usd"] >= -p["cost_usd"] - 1e-6
        assert p["pnl_usd"] <= p["proceeds_usd"] + p["remaining_shares"] - p["cost_usd"] + 1e-6
        assert p["exit_value_per_share"] * p["shares_bought"] - p["cost_usd"] == pytest.approx(p["pnl_usd"], abs=1e-6)
    if pay is not None:
        assert p["status"] == "resolved"
        assert p["pnl_usd"] == pytest.approx(p["proceeds_usd"] + p["remaining_shares"] * pay - p["cost_usd"], abs=1e-6)
        assert p["held_to_resolution"] == int(p["remaining_shares"] >= 0.5 * p["shares_bought"])
    elif p["status"] == "open":
        assert p["pnl_usd"] is None and p["exit_value_per_share"] is None and p["closed_ts"] is None
    # порядок строк во входной таблице не влияет на результат
    shuffled = trades[:]
    rnd.shuffle(shuffled)
    q = _build(*_frames(shuffled, pay, resolved_ts=20_000 if pay is not None else None)).row(0, named=True)
    for k in ("shares_bought", "cost_usd", "shares_sold", "proceeds_usd", "pnl_usd", "closed_ts", "opened_ts"):
        assert (p[k] is None and q[k] is None) or p[k] == pytest.approx(q[k], rel=1e-9, abs=1e-9)


# ---------------------------------------------------------------- комиссии
@FAST
@given(st.floats(0.0, 0.1), shares, st.floats(0.0, 1.0))
def test_fee_invariants(rate, n, p):
    f = fees.fee_per_share(rate, p)
    assert f >= 0 and f == pytest.approx(fees.fee_per_share(rate, 1 - p))  # симметрична относительно 0,5
    assert f <= rate * 0.25 + 1e-12  # максимум — при цене 0,5
    assert fees.fee_usd(rate, n, p) == pytest.approx(n * f, abs=1e-5)  # линейна по числу акций
    assert fees.fee_per_share(rate, 0.0) == 0 and fees.fee_per_share(rate, 1.0) == 0


# ---------------------------------------------------------------- исполнение по стакану
book = st.lists(st.tuples(price, st.floats(0.0, 2000.0)), min_size=0, max_size=8).map(lambda lv: sorted(lv))


def test_walk_skips_dust_levels():
    """Было: уровень с объёмом 5e-324 акций давал среднюю цену 0. Стало: такие уровни пропускаются."""
    f = walk([(0.5, 5e-324), (0.6, 100.0)], usd=1.0)
    assert f.avg_price == pytest.approx(0.6) and f.complete


@FAST
@given(book, st.floats(1.0, 3000.0))
def test_walk_invariants(levels, usd):
    f = walk(levels, usd=usd)
    valid = [(p, s) for p, s in levels if p > 0 and s > 1e-9]
    depth = sum(p * s for p, s in valid)
    assert f.filled_usd <= usd + 1e-6 and f.filled_usd <= depth + 1e-6
    assert f.complete == (depth >= usd - 1e-6)
    if f.shares > 0:
        assert valid[0][0] - 1e-9 <= f.avg_price <= f.worst_price + 1e-9  # средняя — между лучшей и худшей
        assert f.filled_usd == pytest.approx(f.avg_price * f.shares)
        more = walk(levels, usd=usd * 1.5)
        assert more.avg_price >= f.avg_price - 1e-9  # больше сумма — цена не лучше
    else:
        assert math.isnan(f.avg_price)


@FAST
@given(book, st.floats(1.0, 3000.0))
def test_walk_by_shares(levels, n):
    f = walk(levels, shares=n)
    assert f.shares <= n + 1e-6
    assert f.complete == (sum(s for p, s in levels if p > 0 and s > 1e-9) >= n - 1e-6)


# ---------------------------------------------------------------- виртуальная покупка
@SLOW
@given(book.filter(lambda lv: any(p > 0 and s > 0 for p, s in lv)), st.floats(1.0, 500.0), st.floats(0.0, 0.07))
def test_paper_buy_cash_accounting(levels, usd, rate):
    db = DB("sqlite://:memory:")
    db.init_schema()
    res = PaperBroker(db).buy("s1", "tok", usd, levels, fee_rate=rate, ts=10)
    if res is None:
        return
    assert res["usd"] <= usd + 1e-6 and res["fee"] >= 0
    assert res["fee"] == pytest.approx(fees.fee_usd(rate, res["shares"], res["avg_price"]), abs=1e-5)
    # деньги уменьшаются ровно на стоимость акций и комиссию
    assert Portfolio(db).cash(1000.0) == pytest.approx(1000.0 - res["usd"] - res["fee"], abs=1e-4)


# ---------------------------------------------------------------- «что было бы»
whale_trade = st.tuples(
    st.integers(0, 4), st.sampled_from(["BUY", "SELL"]), price, st.floats(10, 3000), st.integers(0, 80 * 86400)
)


@SLOW
@given(
    st.lists(whale_trade, min_size=1, max_size=25),
    st.lists(st.sampled_from([None, 0.0, 1.0]), min_size=5, max_size=5),
    st.floats(5, 100),
    st.floats(5, 200),
)
def test_what_if_invariants(trades, payouts, max_bet, day_limit):
    now = 90 * 86400
    db = DB("sqlite://:memory:")
    db.init_schema()
    for i, pay in enumerate(payouts):
        db.upsert(
            "markets",
            {
                "market_id": f"m{i}",
                "question": f"Q{i}",
                "category": "politics",
                "status": "resolved" if pay is not None else "open",
                "resolved_ts": 85 * 86400 if pay is not None else None,
                "fee_rate": 0.04,
            },
            ["market_id"],
        )
        db.upsert(
            "tokens", {"token_id": f"t{i}", "market_id": f"m{i}", "outcome_label": "Yes", "payout": pay}, ["token_id"]
        )
    db.insert_ignore(
        "trades",
        [
            {
                "trade_id": f"x{j}",
                "ts": ts,
                "ts_fetched": ts,
                "wallet": "0xw",
                "token_id": f"t{m}",
                "market_id": f"m{m}",
                "side": side,
                "price": p,
                "size_shares": usd / p,
                "size_usd": usd,
                "is_maker": None,
                "tx_hash": None,
            }
            for j, (m, side, p, usd, ts) in enumerate(trades)
        ],
    )
    s = {"bankroll_usd": 1000, "max_bet_usd": max_bet, "daily_limit_usd": day_limit, "categories": []}
    res = simulate_whale(db, fast_params(), ["0xw"], s, now, days=90)
    assert res["ok"]
    bets = res["bets"]
    assert len(bets) <= len({t[0] for t in trades if t[1] == "BUY"})  # одна копия на исход
    stake = min(fast_params().max_bet_frac * 1000, max_bet)
    by_day: dict = {}
    for b in bets:
        assert b["stake_usd"] == pytest.approx(stake, abs=0.01)
        assert b["entry_cents"] >= b["whale_cents"] - 1e-6  # наша цена не лучше цены кита
        assert b["fee_usd"] >= 0
        assert b["pnl_usd"] >= -(b["stake_usd"] + b["fee_usd"]) - 0.02  # больше вложенного не потерять
        by_day[b["ts"] // 86400] = by_day.get(b["ts"] // 86400, 0) + b["stake_usd"]
    assert all(v <= day_limit + 0.01 for v in by_day.values())  # дневной лимит соблюдён
