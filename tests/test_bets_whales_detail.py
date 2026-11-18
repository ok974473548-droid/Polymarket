"""Подробности в разделах «Киты» и «Мои ставки»: статистика кита, страница ставки, ручная продажа."""
import pytest

from conftest import fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld
from whalefollow.agent import Agent
from whalefollow.db import DB
from whalefollow.estimator.base import FunctionEstimator
from whalefollow.scheduler import Scheduler
from whalefollow.services import Services


@pytest.fixture(scope="module")
def world():
    w = SyntheticWorld(seed=4)
    db = DB("sqlite://:memory:")
    db.init_schema()
    db.set_setting("auto_discover", True)
    p = fast_params()
    agent = Agent(db, p, FakeData(w), FakeClob(w), FakeGamma(w), estimator=FunctionEstimator(lambda c: 0.72),
                  clock=lambda: w.now)
    sch = Scheduler(db, p, FakeGamma(w), FakeData(w), FakeClob(w), agent, clock=lambda: w.now,
                    discover_max_new=200, discover_pages=30, prices_backfill_per_run=5000)
    sch.run_due("slow")
    sch.run_due("heavy")
    sch.run_due("fast")
    db.set_setting("auto_discover", False)
    svc = Services(db, p, agent=agent, clock=lambda: w.now)
    token = None
    for r in db.query("SELECT entity_id FROM watchlist WHERE mode='copy'"):
        e = r["entity_id"]
        cats = [c["category"] for c in db.query("SELECT category FROM wallet_category_scores WHERE entity_id=? AND "
                                                "status='approved'", (e,)) if c["category"] in ("economics", "politics",
                                                                                                "sports", "tech")]
        if not cats:
            continue
        m = w.add_live_market(category=cats[0], pi=0.75, price=0.55)
        w.whale_trade_now(agent.wallets_of(e)[0], m, m.yes_token, 3000)
        w.now += 6
        sch.run_due("fast")
        sig = db.one("SELECT * FROM signals WHERE market_id=?", (m.market_id,))
        if sig and agent.confirm(sig["signal_id"]).get("status") == "executed":
            token = sig["token_id"]
            break
    return {"w": w, "db": db, "svc": svc, "agent": agent, "token": token}


def test_whale_stats_and_bets(world):
    svc, db = world["svc"], world["db"]
    e = db.scalar("SELECT entity_id FROM watchlist WHERE mode='copy' LIMIT 1")
    st = svc.whale_stats(e)
    assert st["bets"] > 0 and st["closed"] > 0 and 0 <= st["win_pct"] <= 100
    assert len(st["spark"]) == 12 and st["avg_bet_usd"] > 0
    prof = svc.whale_profile(e)
    assert all(b["state"] != "держит" for b in prof["whale_bets"])          # закрытые отдельно от открытых
    assert all(b["state"] == "держит" for b in prof["whale_open"])
    row = svc.whales()["items"][0]
    assert {"stats", "events", "reconcile"} <= set(row)


def test_bet_row_and_detail(world):
    svc, token = world["svc"], world["token"]
    assert token, "в синтетическом мире должна получиться хотя бы одна купленная ставка"
    pos = svc.positions("open")
    r = next(x for x in pos["items"] if x["token_id"] == token)
    assert r["whale_state"] == "держит"                                      # по сделкам кита, без ночного пересчёта
    assert r["entry_cents"] and r["whale_cents"] == 55.0 and r["fees_usd"] > 0
    assert pos["summary"]["fees_usd"] >= r["fees_usd"]
    d = svc.bet_detail(token)
    kinds = [e["kind"] for e in d["events"]]
    assert kinds[:3] == ["whale", "signal", "buy"] and d["marks"]["ours"]["price"]
    assert d["model"]["q_pct"] and svc.bet_detail("нет-такой") is None


def test_manual_sell(world):
    svc, token, db = world["svc"], world["token"], world["db"]
    res = svc.sell_bet(token)
    assert res["ok"] and res["shares"] > 0 and res["fee"] > 0
    assert not [p for p in svc.pf.open_positions() if p["token_id"] == token]
    closed = next(x for x in svc.positions("closed")["items"] if x["token_id"] == token)
    assert closed["result"] == "продана" and closed["is_open"] is False
    assert svc.sell_bet(token)["ok"] is False                               # продавать уже нечего
