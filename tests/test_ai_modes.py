"""Режимы «с ИИ» и «без ИИ», сбор новостей и прогнозов, разбор источников."""

import json

import pytest

from whalefollow.agent import Agent
from whalefollow.db import DB
from whalefollow.estimator.base import FunctionEstimator, NoEstimator
from whalefollow.estimator.sources import Forecast, NewsItem, keywords, parse_manifold, parse_rss
from whalefollow.ingest.polymarket import parse_market
from whalefollow.positions import assign_entities, build_positions
from whalefollow.scoring.scorer import run_scoring
from whalefollow.scoring.sybil import run_antisybil
from whalefollow.services import Services

from conftest import approved_ids, fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld, load_world

GOOGLE_RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Fed signals December cut - Reuters</title><link>https://news.example/1</link>
<pubDate>Tue, 17 Nov 2026 10:00:00 GMT</pubDate><source url="https://reuters.com">Reuters</source></item>
<item><title>Markets brace for Fed</title><link>https://news.example/2</link>
<pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"""


def test_keywords_and_parsers():
    assert keywords("Will the Fed cut interest rates in December 2026?") == "Fed cut interest rates December"
    items = parse_rss(GOOGLE_RSS, "Google News", "media")
    assert items[0].source == "Reuters" and items[0].title == "Fed signals December cut"
    assert items[1].source == "Google News"
    fc = parse_manifold(
        [
            {
                "question": "Fed cut in Dec?",
                "probability": 0.64,
                "url": "https://manifold.markets/x",
                "outcomeType": "BINARY",
                "isResolved": False,
            },
            {"question": "Multi", "outcomeType": "MULTIPLE_CHOICE"},
        ]
    )
    assert len(fc) == 1 and fc[0].probability == 0.64


class FakeNews:
    name, source_type = "Reuters", "media"

    def search(self, query, since_ts):
        return [
            NewsItem("Reuters", "media", since_ts + 3600, "Officials hint at the decision", "https://news.example/a"),
            NewsItem("tg_channel", "telegram", since_ts + 7200, "Insider says YES 100%", "javascript:alert(1)"),
        ]


class FakeForecasts:
    def forecasts(self, query):
        return [Forecast("Manifold", "Same question on Manifold", "https://manifold.markets/q", 0.68)]


class Boom:
    name = "claude"

    def estimate(self, ctx):
        raise RuntimeError("API недоступно")


@pytest.fixture(scope="module")
def env():
    world = SyntheticWorld(seed=4)
    db = DB("sqlite://:memory:")
    db.init_schema()
    load_world(db, world)
    params = fast_params()
    build_positions(db)
    run_antisybil(db, params)
    assign_entities(db)
    scores = run_scoring(db, params, now=world.now)
    agent = Agent(
        db,
        params,
        FakeData(world),
        FakeClob(world),
        FakeGamma(world),
        estimator=NoEstimator(),
        sources=[FakeNews()],
        forecast_sources=[FakeForecasts()],
        clock=lambda: world.now,
    )
    agent.run_once()
    whale = cat = None
    for e in approved_ids(scores):
        cats = [
            r["category"]
            for r in db.query(
                "SELECT category FROM wallet_category_scores WHERE entity_id=? AND status='approved'", (e,)
            )
            if r["category"] in ("economics", "politics", "sports", "tech")
        ]
        if cats:
            whale, cat = e, cats[0]
            break
    svc = Services(db, params, agent=agent, clock=lambda: world.now)
    return {"world": world, "db": db, "agent": agent, "whale": whale, "cat": cat, "svc": svc}


def _signal(env, side="yes", price=0.55):
    world, db, agent = env["world"], env["db"], env["agent"]
    m = world.add_live_market(
        category=env["cat"], pi=0.75 if side == "yes" else 0.25, price=price if side == "yes" else 1 - price
    )
    mrow, toks, _ = parse_market(world.gamma_market(m))
    db.upsert("markets", mrow, ["market_id"])
    db.upsert_many("tokens", toks, ["token_id"])
    world.whale_trade_now(agent.wallets_of(env["whale"])[0], m, m.yes_token if side == "yes" else m.no_token, 3000)
    world.now += 3
    agent.run_once()
    return db.one("SELECT * FROM signals WHERE market_id=?", (m.market_id,)), m


def test_manual_mode_collects_data_and_waits_for_user(env):
    env["db"].set_setting("require_confirmation", False)  # даже при автопокупке без ИИ решает пользователь
    assert env["svc"].status()["ai"] == {"selected": "ai", "available": False, "effective": "manual"}
    sig, _ = _signal(env)
    assert sig["status"] == "pending"
    d = env["svc"].signal_detail(sig["signal_id"])
    assert d["mode"] == "manual" and d["ai"] is None
    assert any(c["title"] == "Новости — оцените сами" for c in d["checks"])
    assert len(d["news"]) == 2 and d["forecasts"][0]["probability_pct"] == 68
    assert d["prices"]["whale_cents"] and d["whale_stats"]["events"]
    by_src = {n["source"]: n for n in d["news"]}
    assert (
        by_src["Reuters"]["reliable"] and not by_src["tg_channel"]["reliable"]
    )  # Telegram без истории — непроверенный


def test_ai_mode_with_estimator(env):
    env["agent"].estimator = FunctionEstimator(lambda ctx: 0.72)
    env["db"].set_setting("require_confirmation", True)
    assert env["svc"].status()["ai"]["effective"] == "ai"
    sig, _ = _signal(env)
    d = env["svc"].signal_detail(sig["signal_id"])
    assert d["mode"] == "ai" and d["ai"]["probability_pct"] == 72


def test_ai_estimate_flipped_for_no_token(env):
    sig, m = _signal(env, side="no")
    dec = env["db"].one("SELECT q_llm FROM decisions WHERE signal_id=?", (sig["signal_id"],))
    assert dec["q_llm"] == pytest.approx(0.28)  # оценщик даёт «Да» 0.72 → для «Нет» 0.28
    sig2 = env["agent"].create_signal(
        {
            "ts": env["world"].now,
            "wallet": "w",
            "trade_id": "x",
            "token_id": m.no_token,
            "market_id": m.market_id,
            "price": 0.45,
            "size_usd": 100,
        },
        env["whale"],
    )
    est = env["agent"]._estimate(
        sig2, {"question": "q"}, 0.45, env["world"].now, {"items": [], "forecasts": [], "flags": {}}
    )
    assert est.q == pytest.approx(0.28)  # из кэша — тоже пересчёт для «Нет»


def test_ai_failure_falls_back_to_manual(env):
    env["agent"].estimator = Boom()
    sig, _ = _signal(env)
    det = json.loads(env["db"].one("SELECT reasons FROM decisions WHERE signal_id=?", (sig["signal_id"],))["reasons"])[
        "details"
    ]
    assert sig["status"] in ("pending", "skipped") and det["ai_failed"] and det["ai_mode"] == "manual"
    assert any(n["kind"] == "ai" for n in env["db"].query("SELECT kind FROM notifications"))


def test_user_can_switch_off_ai(env):
    env["agent"].estimator = FunctionEstimator(lambda ctx: 0.72)
    r = env["svc"].update_settings({"ai_mode": "manual", "news_sources": {"google_news": False, "manifold": True}})
    assert not r["errors"] and env["svc"].status()["ai"]["effective"] == "manual"
    assert env["svc"].update_settings({"ai_mode": "robot"})["errors"]
    env["svc"].update_settings({"ai_mode": "ai"})


def test_demo_world_reload_on_same_db():
    db = DB("sqlite://:memory:")
    db.init_schema()
    w1 = SyntheticWorld(seed=1, n_events=60, n_random=5)
    load_world(db, w1)
    w1.add_live_market()
    m = list(w1.markets.values())[-1]
    db.upsert_many("tokens", parse_market(w1.gamma_market(m))[1], ["token_id"])
    load_world(db, SyntheticWorld(seed=2, n_events=60, n_random=5))  # раньше падало с KeyError
