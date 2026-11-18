import pytest

from whalefollow.config import Params
from whalefollow.decision.engine import decide, kelly
from whalefollow.decision.risk import RiskManager
from whalefollow.execution.paper import PaperBroker
from whalefollow.signals import copyability
from whalefollow.signals.orderbook import walk

ASKS = [(0.55, 100.0), (0.56, 100.0), (0.60, 1000.0)]
P = Params()


def test_walk_vwap():
    f = walk(ASKS, usd=110.0)          # 55 + 55 = 100 + 98.2 акций
    assert f.complete and f.shares == pytest.approx(100 + 55 / 0.56)
    assert 0.55 < f.avg_price < 0.56


def test_kelly_example_from_tz():
    assert kelly(0.65, 0.55) == pytest.approx(0.2222, abs=1e-4)
    assert kelly(0.5, 0.55) == 0


def _decide(**kw):
    base = dict(ask_now=0.55, vwap=0.55, edge_cat=0.10, q_llm=None, llm_trusted=False, fee_per_share=0.0,
                bankroll=1000.0, conviction=1.0, days_left=14.0, params=P)
    base.update(kw)
    return decide(**base)


def test_buy_when_edge_large_enough():
    d = _decide()
    assert d.decision == "BUY"
    assert d.edge_net == pytest.approx(0.65 - 0.02 - 0.55)
    assert d.size_usd == pytest.approx(0.25 * kelly(0.63, 0.55) * 1000, abs=0.01)


def test_skip_small_edge_and_disagreement():
    assert _decide(edge_cat=0.03).reasons == ["small_edge"]
    assert _decide(q_llm=0.40).reasons == ["disagree"]          # кит 0.65 против 0.40 — пропуск, не усреднение


def test_llm_weight_depends_on_calibration():
    d1 = _decide(q_llm=0.60, llm_trusted=False)
    d2 = _decide(q_llm=0.60, llm_trusted=True)
    assert d1.q_final == pytest.approx(0.75 * 0.65 + 0.25 * 0.60)
    assert d2.q_final == pytest.approx(0.5 * 0.65 + 0.5 * 0.60)


def _check(**kw):
    base = dict(signal={"ts": 1000, "whale_price": 0.55, "whale_size_usd": 2000, "market_id": "m", "token_id": "y"},
                asks=ASKS, market={"status": "open", "liquidity_usd": 50000, "category": "economics", "end_ts": 1000 + 14 * 86400},
                score={"edge_shrunk": 0.1, "hold_share": 0.9, "median_hold_h": 300},
                cat_score={"status": "approved", "edge_shrunk": 0.1}, entity_positions=[], median_cost_cat=2000,
                our_size_usd=20.0, params=P, now=1005, categories_on=["economics"])
    base.update(kw)
    return copyability.check(**base)


def test_copyability_ok_and_reasons():
    assert _check().ok
    assert "too_late" in _check(now=1000 + 120).reasons
    assert "price_moved" in _check(signal={"ts": 1000, "whale_price": 0.50, "whale_size_usd": 2000,
                                           "market_id": "m", "token_id": "y"}).reasons
    assert "thin_book" in _check(our_size_usd=500.0).reasons
    assert "scalper" in _check(score={"edge_shrunk": 0.1, "hold_share": 0.3, "median_hold_h": 2}).reasons
    assert "category_off_by_user" in _check(categories_on=["sports"]).reasons
    assert "whale_hedged" in _check(entity_positions=[{"market_id": "m", "token_id": "n", "remaining_shares": 10}]).reasons
    assert "low_conviction" in _check(median_cost_cat=10_000).reasons


def test_risk_caps_and_kill_switch(db):
    rm = RiskManager(db, P)
    r = rm.cap(100.0, market={"event_id": "e", "category": "economics"}, entity_id="w", now=10_000)
    assert r.size_usd == 20.0 and "capped_max_bet" in r.reasons      # 2% от $1000 < $40
    db.set_setting("daily_limit_usd", 30.0)
    PaperBroker(db).buy("s1", "tok", 25.0, ASKS, ts=10_000)
    r = rm.cap(20.0, market={"event_id": "e", "category": "economics"}, entity_id="w", now=10_001)
    assert r.size_usd == pytest.approx(5.0, abs=0.01)
    rm.set_stopped(True)
    assert rm.cap(10.0, market={}, entity_id="w").blocked


def test_daily_loss_stop_pauses(db):
    db.upsert("tokens", {"token_id": "tok", "market_id": "m", "outcome_label": "Yes", "payout": None}, ["token_id"])
    rm = RiskManager(db, P)
    now = 1_790_000_000
    rm.check_stops(now)                                    # фиксирует капитал на начало дня
    PaperBroker(db).buy("s1", "tok", 100.0, [(0.5, 1000.0)], ts=now)
    db.upsert("price_history", {"token_id": "tok", "ts": now + 1, "price": 0.01}, ["token_id", "ts"])
    assert rm.check_stops(now + 2) == "daily_loss_stop"
    assert rm.cap(10.0, market={}, entity_id="w", now=now + 3).blocked
