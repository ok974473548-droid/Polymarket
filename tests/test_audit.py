"""Регрессионные тесты по итогам аудита алгоритмов (v0.13.1): на каждую найденную ошибку — проверка."""

import json

import numpy as np
import pandas as pd
import pytest
from test_realtime import env  # noqa: F401 — общий мир с китом в режиме «копировать»

from whalefollow.backtest.whale_sim import simulate_whale
from whalefollow.db import DB
from whalefollow.decision.engine import decide, kelly
from whalefollow.estimator.calibration import Calibrator, record_outcomes
from whalefollow.execution.paper import PaperBroker, Portfolio
from whalefollow.ingest.polymarket import parse_market
from whalefollow.positions import _build, build_positions
from whalefollow.scoring import classify, scorer
from whalefollow.scoring.metrics import closed_positions
from whalefollow.scoring.stats import bh_qvalues
from whalefollow.scoring.sybil import run_antisybil
from whalefollow.services import Services
from whalefollow.tracked import migrate_watchlist

from conftest import fast_params
from synthetic import SyntheticWorld, load_world

DAY = 86400


def _trades(rows):
    return pd.DataFrame(
        [
            {
                "trade_id": f"t{i}",
                "wallet": "w",
                "token_id": r[0],
                "side": r[1],
                "size_shares": r[2],
                "size_usd": r[2] * r[3],
                "ts": r[4],
            }
            for i, r in enumerate(rows)
        ]
    )


def _mk(resolved_ts=np.nan, end_ts=np.nan):
    return (
        pd.DataFrame(
            [
                {
                    "market_id": "m",
                    "event_id": "e",
                    "category": "politics",
                    "status": "resolved",
                    "resolved_ts": resolved_ts,
                    "end_ts": end_ts,
                }
            ]
        ),
        pd.DataFrame([{"token_id": "y", "market_id": "m", "payout": 1.0}]),
    )


# ---------------------------------------------------------------- позиции
def test_oversold_position_does_not_count_foreign_proceeds():
    # купил 100 по 0,4; продал 300 по 0,6 (200 акций пришли не из покупок — split/перевод)
    m, t = _mk(resolved_ts=5000)
    p = _build(_trades([("y", "BUY", 100, 0.4, 100), ("y", "SELL", 300, 0.6, 200)]), m, t).iloc[0]
    assert p["shares_sold"] == 100 and p["proceeds_usd"] == pytest.approx(60.0)
    assert p["pnl_usd"] == pytest.approx(20.0)  # было 140: выручка за чужие 200 акций


def test_resolved_market_without_date_is_not_dropped_from_scoring():
    m, t = _mk(resolved_ts=np.nan, end_ts=9000)
    p = _build(_trades([("y", "BUY", 100, 0.4, 100)]), m, t)
    assert p.iloc[0]["closed_ts"] == 9000
    p["entity_id"] = "w"
    assert len(closed_positions(p, now=10_000)) == 1  # раньше closed_ts = NaN → позиция выпадала
    m2, t2 = _mk()  # нет ни даты разрешения, ни срока — последняя сделка
    assert _build(_trades([("y", "BUY", 100, 0.4, 100)]), m2, t2).iloc[0]["closed_ts"] == 100


# ---------------------------------------------------------------- отбор: поправка на множественные проверки
def test_single_check_uses_pvalue_pool():
    w = SyntheticWorld(seed=4)
    db = DB("sqlite://:memory:")
    db.init_schema()
    load_world(db, w)
    build_positions(db)
    skilled = next(x for x, r in w.roles.items() if r == "skilled")
    f = scorer.load_frames(db, [skilled])
    alone, _ = scorer.score_entities(
        f["pos"], f["trades"], f["wallets"], f["markets"], f["prices"], fast_params(), w.now, entities=[skilled]
    )
    pool = {f"other{i}": 0.6 for i in range(99)}
    pooled, _ = scorer.score_entities(
        f["pos"],
        f["trades"],
        f["wallets"],
        f["markets"],
        f["prices"],
        fast_params(),
        w.now,
        entities=[skilled],
        pvalue_pool=pool,
    )
    p = alone.iloc[0]["pvalue"]
    assert alone.iloc[0]["qvalue"] == pytest.approx(p)  # один тест — q = p
    assert pooled.iloc[0]["qvalue"] == pytest.approx(bh_qvalues([p] + [0.6] * 99)[0])
    assert pooled.iloc[0]["qvalue"] >= alone.iloc[0]["qvalue"]


# ---------------------------------------------------------------- маркет-мейкер: split/merge
def test_split_merge_is_maker_flag_and_not_two_sided_trading():
    rows = []
    for i in range(10):
        m = f"m{i}"
        rows.append({"market_id": m, "token_id": m + "y", "side": "BUY", "size_usd": 10.0, "source": None})
        if i < 4:  # split в 4 рынках из 10: обе стороны «куплены»
            rows += [
                {"market_id": m, "token_id": m + t, "side": "BUY", "size_usd": 5.0, "source": "split"} for t in "yn"
            ]
    trades = pd.DataFrame(rows)
    pos = pd.DataFrame(
        {"status": ["open"], "closed_ts": [np.nan], "opened_ts": [0], "remaining_shares": [1.0], "avg_entry": [0.5]}
    )
    f = classify.maker_flags(trades, pos, fast_params())
    assert f["F5_merge_split"] == pytest.approx(0.4)
    assert f["F1_two_sided"] == 0.0  # раньше каждый split считался двусторонней торговлей


# ---------------------------------------------------------------- решение
def test_whale_estimate_does_not_double_count_price_move():
    p = fast_params()
    kw = {
        "vwap": 0.56,
        "edge_cat": 0.08,
        "q_llm": None,
        "llm_trusted": False,
        "fee_per_share": 0.0,
        "bankroll": 1000,
        "conviction": 1.0,
        "days_left": 10,
        "params": p,
    }
    moved = decide(ask_now=0.55, whale_price=0.50, **kw)  # цена ушла с 50¢ до 55¢
    assert moved.q_whale == pytest.approx(0.58)  # 50¢ + 8¢, а не 55¢ + 8¢
    fell = decide(ask_now=0.45, whale_price=0.50, **{**kw, "vwap": 0.46})
    assert fell.q_whale == pytest.approx(0.53)  # цена упала — от текущей (осторожнее)


def test_kelly_includes_fee():
    p = fast_params()
    d = decide(
        ask_now=0.4,
        whale_price=0.4,
        vwap=0.4,
        edge_cat=0.2,
        q_llm=None,
        llm_trusted=False,
        fee_per_share=0.012,
        bankroll=1000,
        conviction=1.0,
        days_left=10,
        params=p,
    )
    assert d.kelly_full == pytest.approx(kelly(d.q_eff, 0.412))
    assert d.kelly_full < kelly(d.q_eff, 0.4)


# ---------------------------------------------------------------- агент: одна заявка — один сигнал, настоящий выход
def _setup_market(env, price=0.5):
    from whalefollow.ingest.polymarket import parse_market as pm

    world, db = env["world"], env["db"]
    m = world.add_live_market(category=env["cat"], pi=0.78, price=price)
    mrow, toks, _ = pm(world.gamma_market(m))
    db.upsert("markets", mrow, ["market_id"])
    db.upsert_many("tokens", toks, ["token_id"])
    return m


def _fill(wallet, token, cond, side, price, shares, ts, tx):
    return {
        "proxy_wallet": wallet,
        "token_id": token,
        "condition_id": cond,
        "side": side,
        "price": price,
        "size": shares,
        "timestamp": ts,
        "transaction_hash": tx,
    }


def test_one_order_split_across_makers_gives_one_signal(env):
    world, db, agent = env["world"], env["db"], env["agent"]
    agent.streams = None
    m = _setup_market(env)
    w = agent.wallets_of(env["whale"])[0]
    now = world.now
    fills = [_fill(w, m.yes_token, m.market_id, "BUY", 0.50 + 0.002 * i, 1200, now, "0xorder1") for i in range(5)]
    before = db.scalar("SELECT COUNT(*) AS n FROM signals")
    res = agent.process_stream_trades(fills)
    assert res["new_trades"] == 5 and res["signals"] == 1
    sig = db.one("SELECT * FROM signals WHERE token_id=? AND kind='entry'", (m.yes_token,))
    assert sig["whale_size_usd"] == pytest.approx(sum(0.5 + 0.002 * i for i in range(5)) * 1200)
    assert 0.50 < sig["whale_price"] < 0.51  # средняя цена всей заявки
    again = agent.process_stream_trades([_fill(w, m.yes_token, m.market_id, "BUY", 0.51, 300, now + 120, "0xorder2")])
    assert again["signals"] == 0 and again["merged"] == 1  # докупка через 2 минуты — не новый сигнал
    assert db.scalar("SELECT COUNT(*) AS n FROM signals") == before + 1


def test_partial_whale_sale_is_not_an_exit(env):
    world, db, agent = env["world"], env["db"], env["agent"]
    agent.streams = None
    m = _setup_market(env, price=0.62)  # оценка ИИ в мире — 72%: согласна с китом
    w = agent.wallets_of(env["whale"])[0]
    now = world.now
    agent.process_stream_trades([_fill(w, m.yes_token, m.market_id, "BUY", 0.62, 8000, now, "0xb1")])
    sig = db.one("SELECT signal_id, status FROM signals WHERE token_id=? AND kind='entry'", (m.yes_token,))
    if sig["status"] == "pending":
        agent.confirm(sig["signal_id"])
    assert any(p["token_id"] == m.yes_token for p in Portfolio(db).open_positions())
    r = agent.process_stream_trades([_fill(w, m.yes_token, m.market_id, "SELL", 0.65, 2000, now + 60, "0xs1")])
    assert r["exits"] == 0  # продал четверть — это не выход
    r = agent.process_stream_trades([_fill(w, m.yes_token, m.market_id, "SELL", 0.65, 5500, now + 120, "0xs2")])
    assert r["exits"] == 1  # осталось ~6% — вышел
    r = agent.process_stream_trades([_fill(w, m.yes_token, m.market_id, "SELL", 0.65, 400, now + 180, "0xs3")])
    assert r["exits"] == 0  # запрос уже ждёт решения — второй не нужен
    again = agent.process_stream_trades([_fill(w, m.yes_token, m.market_id, "BUY", 0.63, 900, now + 4000, "0xb2")])
    st = db.one(
        "SELECT d.reasons FROM signals s JOIN decisions d ON d.signal_id=s.signal_id WHERE s.token_id=? "
        "AND s.kind='entry' ORDER BY s.ts DESC LIMIT 1",
        (m.yes_token,),
    )
    assert again["skipped"] == 1 and "already_holding" in json.loads(st["reasons"])["codes"]


# ---------------------------------------------------------------- портфель и ставка
def test_reopened_position_and_partial_sale_pnl(db):
    b = PaperBroker(db)
    asks, bids = [(0.40, 1000)], [(0.60, 1000)]
    b.buy("s1", "tok", 40.0, asks, ts=100, fee_rate=0.0)  # 100 акций по 0,40
    b.sell("tok", 100, bids, ts=200, fee_rate=0.0)
    b.buy("s2", "tok", 40.0, asks, ts=300, fee_rate=0.0)  # снова вошли
    p = Portfolio(db).positions()["tok"]
    assert p["shares"] == pytest.approx(100) and p["closed_ts"] is None
    db.upsert("markets", {"market_id": "m", "question": "Q", "category": "politics", "status": "open"}, ["market_id"])
    db.upsert("tokens", {"token_id": "t2", "market_id": "m", "outcome_label": "Yes", "payout": None}, ["token_id"])
    b.buy("s3", "t2", 50.0, [(0.50, 1000)], ts=400, fee_rate=0.0)  # 100 акций по 0,50
    b.sell("t2", 50, [(0.70, 1000)], ts=500, fee_rate=0.0)  # половина по 0,70: +10
    db.upsert("price_history", {"token_id": "t2", "ts": 600, "price": 0.60}, ["token_id", "ts"])
    svc = Services(db, fast_params(), clock=lambda: 700)
    row = svc._bet_row(Portfolio(db).positions()["t2"], 700)
    assert row["pnl_usd"] == pytest.approx(10 + 50 * 0.10)  # продажа +10 и остаток +5; раньше было +5


# ---------------------------------------------------------------- калибровка
def test_calibration_compares_with_market_on_same_markets():
    q = [0.9] * 30 + [0.6] * 30
    y = [1] * 30 + [0] * 30
    pm = [None] * 30 + [0.5] * 30  # рынок известен только для «плохой» половины
    c = Calibrator().fit(q, y, pm)
    assert c.n_common == 30 and c.brier_llm_common == pytest.approx(0.36) and c.brier_market == pytest.approx(0.25)
    assert not c.trusted(20)  # раньше 0,185 < 0,25 → ИИ «лучше рынка»


# ---------------------------------------------------------------- исходы рынков
def _raw(prices, uma="resolved", closed=True):
    return {
        "conditionId": "m",
        "question": "Q",
        "clobTokenIds": '["a","b"]',
        "outcomes": '["Yes","No"]',
        "outcomePrices": json.dumps([str(p) for p in prices]),
        "closed": closed,
        "umaResolutionStatus": uma,
        "closedTime": "2026-01-01T00:00:00Z",
    }


def test_disputed_and_fifty_fifty_resolutions(db):
    m, t, _ = parse_market(_raw([1, 0], uma="disputed"))
    assert m["status"] == "disputed" and all(x["payout"] is None for x in t)  # спор — не платим
    m, t, _ = parse_market(_raw([0.5, 0.5]))
    assert m["status"] == "resolved" and [x["payout"] for x in t] == [0.5, 0.5] and m["resolved_ts"]
    m, t, _ = parse_market(_raw([0.5, 0.5], uma="", closed=True))
    assert m["status"] == "closed" and t[0]["payout"] is None
    db.upsert("tokens", {"token_id": "a", "market_id": "m", "outcome_label": "Yes", "payout": 0.5}, ["token_id"])
    db.upsert("calibration_log", {"est_id": "e1", "market_id": "m", "q": 0.7, "p_market": 0.6, "y": None}, ["est_id"])
    assert record_outcomes(db) == 0  # 50/50 — не исход «да/нет»


# ---------------------------------------------------------------- «что было бы»
def test_sim_follows_whale_exit_without_price_history():
    db = DB("sqlite://:memory:")
    db.init_schema()
    db.upsert(
        "markets",
        {"market_id": "m", "question": "Q", "category": "politics", "status": "open", "fee_rate": 0.0},
        ["market_id"],
    )
    db.upsert("tokens", {"token_id": "y", "market_id": "m", "outcome_label": "Yes", "payout": None}, ["token_id"])
    now = 100 * DAY
    rows = [("BUY", 0.40, 1000, now - 30 * DAY), ("SELL", 0.70, 1000, now - 20 * DAY)]
    db.insert_ignore(
        "trades",
        [
            {
                "trade_id": f"t{i}",
                "ts": ts,
                "ts_fetched": ts,
                "wallet": "w",
                "token_id": "y",
                "market_id": "m",
                "side": s,
                "price": p,
                "size_shares": sh,
                "size_usd": p * sh,
            }
            for i, (s, p, sh, ts) in enumerate(rows)
        ],
    )
    r = simulate_whale(db, fast_params(), ["w"], {"bankroll_usd": 1000, "max_bet_usd": 40, "categories": []}, now)
    b = r["bets"][0]
    assert b["status"] == "sold" and b["exit_cents"] == pytest.approx(69.0)  # цена кита − 1¢ проскальзывания
    assert b["whale_pnl_usd"] == pytest.approx(20 / 0.40 * 0.70 - 20, abs=0.01)


# ---------------------------------------------------------------- антисибил
def test_scoped_antisybil_keeps_other_entities(db):
    db.upsert(
        "entity_wallets",
        {"wallet": "0xother1", "entity_id": "E-0xother1", "link_reason": "x", "confidence": 1},
        ["wallet"],
    )
    run_antisybil(db, fast_params(), wallets=["0xmine"])
    assert db.one("SELECT entity_id FROM entity_wallets WHERE wallet='0xother1'")  # раньше удалялось всё


def test_dissolved_cluster_moves_watchlist_to_wallet(db):
    db.upsert(
        "watchlist",
        {"entity_id": "E-0xabc", "mode": "copy", "added_by": "user", "added_ts": 1, "categories_off": '["crypto"]'},
        ["entity_id"],
    )
    assert migrate_watchlist(db) == 1
    row = db.one("SELECT * FROM watchlist")
    assert row["entity_id"] == "0xabc" and row["mode"] == "copy" and json.loads(row["categories_off"]) == ["crypto"]
