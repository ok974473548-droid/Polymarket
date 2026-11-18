"""Блок 3: Kalshi и букмекеры как прогнозы, согласие и уверенность китов, подстройка порога по исходам."""

import json

import pytest

from whalefollow.estimator.external import (
    BookmakerSource,
    KalshiSource,
    devig,
    kalshi_price,
    kalshi_rows,
    sync_kalshi,
    sync_odds,
)
from whalefollow.estimator.sources import build_sources, gather_forecasts
from whalefollow.services import Services

from conftest import fast_params

EVENTS = [
    {
        "event_ticker": "KXFEDDECISION-26DEC",
        "series_ticker": "KXFEDDECISION",
        "title": "Fed decision in December 2026",
        "markets": [
            {
                "ticker": "KXFED-26DEC-C25",
                "status": "active",
                "yes_sub_title": "Cut 25bps",
                "yes_bid_dollars": "0.6100",
                "yes_ask_dollars": "0.6300",
                "close_time": "2026-12-10T19:00:00Z",
            },
            {
                "ticker": "KXFED-26DEC-H0",
                "status": "active",
                "yes_sub_title": "Hold",
                "yes_bid_dollars": "0.10",
                "yes_ask_dollars": "0.40",
                "last_price_dollars": "0.33",
            },
            {"ticker": "KXFED-26DEC-X", "status": "finalized", "yes_sub_title": "Hike", "last_price_dollars": "0.01"},
        ],
    },
    {
        "event_ticker": "KXBTC",
        "title": "Bitcoin price at end of 2026",
        "markets": [
            {"ticker": "KXBTC-150K", "status": "active", "yes_sub_title": "Above 150k", "last_price_dollars": "0.2"}
        ],
    },
]


class _Kalshi:
    def open_events(self):
        return iter(EVENTS)


def test_kalshi_prices_and_matching(db):
    assert kalshi_price(EVENTS[0]["markets"][0]) == 0.62  # узкий спред — середина
    assert kalshi_price(EVENTS[0]["markets"][1]) == 0.33  # широкий — последняя сделка
    rows = kalshi_rows(EVENTS)
    assert {r["ext_id"] for r in rows} == {"KXFED-26DEC-C25", "KXFED-26DEC-H0", "KXBTC-150K"}  # закрытый пропущен
    assert sync_kalshi(db, _Kalshi()) == 3
    src = KalshiSource(db)
    got = src.forecasts("Will the Fed cut rates by 25bps in December 2026?")
    assert got and got[0].source == "Kalshi" and "Fed decision" in got[0].title
    assert src.forecasts("Will Taylor Swift release a new album?") == []  # нет похожего — ничего не выдумываем


ODDS = [
    {
        "id": "ev1",
        "home_team": "Los Angeles Lakers",
        "away_team": "Boston Celtics",
        "commence_time": "2099-11-20T03:00:00Z",
        "bookmakers": [
            {
                "key": "a",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Los Angeles Lakers", "price": 1.80},
                            {"name": "Boston Celtics", "price": 2.10},
                        ],
                    }
                ],
            },
            {
                "key": "b",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Los Angeles Lakers", "price": 1.75},
                            {"name": "Boston Celtics", "price": 2.20},
                        ],
                    }
                ],
            },
        ],
    }
]


class _Odds:
    remaining = "480"

    def odds(self, sport):
        return ODDS if sport == "basketball_nba" else []


def test_devig_sums_to_one():
    p = devig(ODDS[0])
    assert sum(p.values()) == pytest.approx(1.0) and p["Los Angeles Lakers"] > p["Boston Celtics"]
    home = (1 / 1.80 + 1 / 1.75) / 2  # медиана вероятностей двух букмекеров
    away = (1 / 2.10 + 1 / 2.20) / 2
    assert p["Los Angeles Lakers"] == pytest.approx(home / (home + away), rel=1e-9)


def _sports_market(db, mid, question, labels):
    db.upsert(
        "markets", {"market_id": mid, "question": question, "category": "sports", "status": "open"}, ["market_id"]
    )
    for i, lab in enumerate(labels):
        db.upsert(
            "tokens", {"token_id": f"{mid}-{i}", "market_id": mid, "outcome_label": lab, "payout": None}, ["token_id"]
        )
    return db.one("SELECT * FROM markets WHERE market_id=?", (mid,))


def test_bookmakers_match_teams(db):
    assert sync_odds(db, _Odds(), ["basketball_nba", "icehockey_nhl"]) == 1
    assert db.get_state("odds_remaining") == "480"
    src = BookmakerSource(db)
    pl = devig(ODDS[0])
    m1 = _sports_market(db, "m1", "Lakers vs. Celtics", ["Lakers", "Celtics"])
    f = src.forecasts(m1["question"], m1)
    assert f and f[0].probability == pytest.approx(pl["Los Angeles Lakers"], abs=1e-4)
    m2 = _sports_market(db, "m2", "Will the Celtics beat the Lakers?", ["Yes", "No"])
    assert src.forecasts(m2["question"], m2)[0].probability == pytest.approx(pl["Boston Celtics"], abs=1e-4)
    m3 = _sports_market(db, "m3", "Will the Knicks beat the Heat?", ["Yes", "No"])
    assert src.forecasts(m3["question"], m3) == []
    m4 = dict(m1, category="politics")
    assert src.forecasts(m4["question"], m4) == []  # не спорт — не сравниваем
    stored = gather_forecasts(db, m1, [src])
    assert stored and stored[0]["source"] == "Букмекеры"


def test_build_sources_includes_new(db):
    _, fc = build_sources({"manifold": False}, db=db)
    assert [type(s).__name__ for s in fc] == ["KalshiSource", "BookmakerSource"]
    _, fc = build_sources({"manifold": False, "kalshi": False, "bookmakers": False}, db=db)
    assert fc == []


def test_tuning_recommends_threshold(db):
    svc = Services(db, fast_params())
    db.upsert("tokens", {"token_id": "w", "market_id": "m", "outcome_label": "Yes", "payout": 1.0}, ["token_id"])
    db.upsert("tokens", {"token_id": "l", "market_id": "m", "outcome_label": "No", "payout": 0.0}, ["token_id"])
    rows = []
    # большой запас выгоды — чаще выигрыш; маленький — проигрыш
    for i in range(40):
        rows.append(("w" if i % 4 else "l", 0.06))
    for i in range(40):
        rows.append(("l" if i % 3 else "w", 0.015))
    for i, (tok, edge) in enumerate(rows):
        sid = f"s{i}"
        db.upsert(
            "signals",
            {"signal_id": sid, "ts": i, "token_id": tok, "market_id": "m", "kind": "entry", "status": "skipped"},
            ["signal_id"],
        )
        db.upsert(
            "decisions",
            {
                "signal_id": sid,
                "edge_net": edge,
                "vwap_exec": 0.5,
                "decision": "SKIP",
                "reasons": json.dumps({"details": {"fee_per_share": 0.01}}),
            },
            ["signal_id"],
        )
    t = svc.tuning()
    assert (
        t["with_outcome"] == 80
        and t["recommended"] == pytest.approx(0.04)
        or t["recommended"] in (0.02, 0.03, 0.04, 0.05, 0.06)
    )
    assert t["recommended"] >= 0.02  # отсекает сигналы с маленьким запасом
    assert svc.set_tuning(0.5)["ok"] is False
    assert svc.set_tuning(t["recommended"])["ok"] and svc.tuning()["current"] == t["recommended"]
    assert svc.update_settings({"param_overrides": {"kelly_fraction": 1}})["errors"]


def test_whale_context_consensus(db):
    from whalefollow.agent import Agent

    a = Agent(db, fast_params(), data=None, clob=None)
    for e, tok, mode in (("0xa1", "t_yes", "copy"), ("0xb2", "t_yes", "watch"), ("0xc3", "t_no", "copy")):
        db.upsert(
            "watchlist",
            {"entity_id": e, "mode": mode, "added_by": "user", "added_ts": 1, "categories_off": "[]"},
            ["entity_id"],
        )
        db.upsert(
            "positions",
            {
                "position_id": f"p{e}",
                "wallet": e,
                "entity_id": e,
                "token_id": tok,
                "market_id": "m",
                "status": "open",
                "remaining_shares": 10,
                "cost_usd": 5,
            },
            ["position_id"],
        )
    for t in ("t_yes", "t_no"):
        db.upsert("tokens", {"token_id": t, "market_id": "m", "outcome_label": t, "payout": None}, ["token_id"])
    sig = {"entity_id": "0xwhale", "market_id": "m", "token_id": "t_yes", "whale_size_usd": 900.0}
    ent_pos = [{"token_id": "t_yes", "remaining_shares": 50, "cost_usd": 300.0}, {"token_id": "x", "cost_usd": 100.0}]
    ctx = a.whale_context(sig, ent_pos)
    assert len(ctx["same"]) == 2 and len(ctx["opposite"]) == 1
    assert ctx["typical_bet_usd"] == 200.0 and ctx["size_ratio"] == 4.5 and ctx["adding"] is True
