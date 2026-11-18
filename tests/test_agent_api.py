"""Сквозной сценарий агента и HTTP API на синтетическом рынке."""

import json
import re
import threading
import urllib.request

import pytest

from whalefollow.agent import Agent
from whalefollow.api.server import serve
from whalefollow.db import DB
from whalefollow.estimator.base import FunctionEstimator
from whalefollow.execution.paper import LiveTradingDisabled
from whalefollow.ingest.polymarket import parse_market
from whalefollow.positions import assign_entities, build_positions
from whalefollow.scoring.scorer import run_scoring
from whalefollow.scoring.sybil import run_antisybil
from whalefollow.services import Services

from conftest import approved_ids, fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld, load_world


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
        estimator=FunctionEstimator(lambda ctx: 0.72),
        clock=lambda: world.now,
    )
    agent.run_once()  # первая догрузка истории сигналов не создаёт
    best = approved_ids(scores)
    whale = None
    for e in best:
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
    assert whale, "нужен допущенный кит с включённой темой"
    svc = Services(db, params, agent=agent, data=FakeData(world), gamma=FakeGamma(world), clock=lambda: world.now)
    return {"world": world, "db": db, "params": params, "agent": agent, "whale": whale, "cat": cat, "svc": svc}


def _live_market(env, price=0.55):
    world, db = env["world"], env["db"]
    m = world.add_live_market(category=env["cat"], pi=0.75, price=price)
    mrow, toks, _ = parse_market(world.gamma_market(m))
    db.upsert("markets", mrow, ["market_id"])
    db.upsert_many("tokens", toks, ["token_id"])
    return m


def test_no_signals_from_history(env):
    assert env["db"].scalar("SELECT COUNT(*) AS n FROM signals", default=0) == 0


def test_signal_pending_then_confirmed(env):
    world, agent, db = env["world"], env["agent"], env["db"]
    m = _live_market(env)
    world.whale_trade_now(agent.wallets_of(env["whale"])[0], m, m.yes_token, 3000)
    res = agent.run_once()
    assert res["signals"] == 1 and res["pending"] == 1
    sig = db.one("SELECT * FROM signals WHERE status='pending'")
    dec = db.one("SELECT * FROM decisions WHERE signal_id=?", (sig["signal_id"],))
    assert dec["decision"] == "BUY" and dec["q_llm"] == pytest.approx(0.72) and dec["params_version"]
    detail = env["svc"].signal_detail(sig["signal_id"])
    assert detail["recommendation"] == "BUY" and detail["if_lose_usd"] == -detail["size_usd"]
    out = agent.confirm(sig["signal_id"])
    assert out["status"] == "executed" and out["shares"] > 0
    assert env["svc"].positions("open")["counts"]["open"] == 1
    env["market"] = m


def test_skip_when_news_disagree(env):
    world, agent, db = env["world"], env["agent"], env["db"]
    agent.estimator = FunctionEstimator(lambda ctx: 0.30)
    m = _live_market(env)
    world.whale_trade_now(agent.wallets_of(env["whale"])[0], m, m.yes_token, 3000)
    agent.run_once()
    dec = db.one(
        "SELECT d.* FROM decisions d JOIN signals s ON s.signal_id=d.signal_id WHERE s.market_id=?", (m.market_id,)
    )
    assert dec["decision"] == "SKIP" and "disagree" in json.loads(dec["reasons"])["codes"]
    agent.estimator = FunctionEstimator(lambda ctx: 0.72)


def test_whale_exit_asks_user(env):
    world, agent, db = env["world"], env["agent"], env["db"]
    m = env["market"]
    world.whale_trade_now(agent.wallets_of(env["whale"])[0], m, m.yes_token, 3000, side="SELL")
    assert agent.run_once()["exits"] == 1
    ex = db.one("SELECT * FROM signals WHERE kind='whale_exit'")
    pos = env["svc"].positions("open")
    assert pos["need_decision"] and pos["need_decision"][0]["recommendation"] == "hold"
    assert agent.resolve_exit(ex["signal_id"], "sell")["status"] == "sold"
    assert env["svc"].positions("open")["counts"]["open"] == 0


def test_signal_expires(env):
    world, agent, db = env["world"], env["agent"], env["db"]
    m = _live_market(env)
    world.whale_trade_now(agent.wallets_of(env["whale"])[0], m, m.yes_token, 3000)
    agent.run_once()
    sid = db.one("SELECT signal_id FROM signals WHERE market_id=?", (m.market_id,))["signal_id"]
    world.now += env["params"].signal_ttl_s + 5
    assert agent.confirm(sid)["status"] == "expired"


def test_live_mode_is_blocked(env):
    svc = env["svc"]
    assert "mode" in svc.update_settings({"mode": "manual"})["errors"]
    env["db"].set_setting("mode", "manual")
    world, agent, db = env["world"], env["agent"], env["db"]
    m = _live_market(env)
    world.whale_trade_now(agent.wallets_of(env["whale"])[0], m, m.yes_token, 3000)
    agent.run_once()
    sid = db.one("SELECT signal_id FROM signals WHERE market_id=?", (m.market_id,))["signal_id"]
    with pytest.raises(LiveTradingDisabled):
        agent.confirm(sid)
    env["db"].set_setting("mode", "paper")


def test_settlement_and_notifications(env):
    world, agent, db = env["world"], env["agent"], env["db"]
    m = _live_market(env)
    world.whale_trade_now(agent.wallets_of(env["whale"])[0], m, m.yes_token, 3000)
    agent.run_once()
    sid = db.one("SELECT signal_id FROM signals WHERE market_id=?", (m.market_id,))["signal_id"]
    assert agent.confirm(sid)["status"] == "executed"
    # рынок разрешился в YES
    m.t_resolve = world.now - 1
    mrow, toks, _ = parse_market(world.gamma_market(m))
    db.upsert("markets", mrow, ["market_id"])
    db.upsert_many("tokens", toks, ["token_id"])
    agent.run_once()
    titles = [n["title"] for n in db.query("SELECT title FROM notifications")]
    assert any(t.startswith("Ставка выиграла") for t in titles)
    assert env["svc"].results("all")["bets"]["won"] >= 1


def _http(port, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_api(env):
    httpd = serve(env["svc"], port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        code, dash = _http(port, "GET", "/api/dashboard")
        assert code == 200 and "balance_usd" in dash and "daily_limit" in dash
        code, wh = _http(port, "GET", "/api/whales")
        assert code == 200 and any(w["copy"] for w in wh["items"])
        code, prof = _http(port, "GET", f"/api/whales/{env['whale']}")
        assert code == 200 and prof["topics"] and prof["status"] == "Надёжный"
        code, res = _http(port, "GET", "/api/results?period=all")
        assert code == 200 and res["bets"]["made"] >= 1
        code, _ = _http(port, "GET", "/api/positions?status=closed")
        assert code == 200
        code, st = _http(port, "PUT", "/api/settings", {"max_bet_usd": 30, "mode": "auto"})
        assert code == 200 and st["settings"]["max_bet_usd"] == 30 and "mode" in st["errors"]
        random_wallet = next(w for w, r in env["world"].roles.items() if r == "random")
        code, chk = _http(
            port,
            "POST",
            "/api/whales/check",
            {"address": f"https://polymarket.com/profile/{random_wallet}", "wait": True},
        )
        assert code == 200 and chk["fits"] is False and len(chk["checks"]) == 3
        code, add = _http(port, "POST", "/api/whales", {"entity_id": chk["entity_id"], "mode": "copy"})
        assert code == 200 and add["mode"] == "watch" and add["message"]  # не прошёл — только следить
        code, ob = _http(port, "POST", "/api/onboarding", {"jurisdiction_ok": True, "risk_ack": False})
        assert code == 200 and ob["ok"] is False
        code, st = _http(port, "POST", "/api/stop")
        assert code == 200 and st["stopped"] is True
        code, st = _http(port, "POST", "/api/resume")
        assert st["stopped"] is False
        code, j = _http(port, "GET", "/api/journal?limit=50")
        assert code == 200 and j["items"] and all(i["params_version"] for i in j["items"])
        code, _ = _http(port, "GET", "/api/nope")
        assert code == 404
    finally:
        httpd.shutdown()


def test_web_ui_served(env):
    httpd = serve(env["svc"], port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as r:
            index = r.read().decode()
        # сборка Vite: главный скрипт и стили — в /assets/ с хэшем в имени
        js = re.search(r'src="(/assets/index-[^"]+\.js)"', index).group(1)
        css = re.search(r'href="(/assets/index-[^"]+\.css)"', index).group(1)
        for path, needle in (
            ("/", b"WhaleFollow"),
            (js, b"#\\/diagnostics"),
            (css, b"--accent"),
            ("/manifest.webmanifest", b'"display": "standalone"'),
            ("/sw.js", b"showNotification"),
            ("/whales/anything", b"<!doctype html>"),
        ):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
                assert r.status == 200 and needle in r.read(), path
                if path.startswith("/assets/"):
                    assert "immutable" in r.headers["Cache-Control"]  # хэшированные файлы — навсегда в кэше
                else:
                    assert r.headers["Cache-Control"] == "no-cache"
        for bad in ("/../pyproject.toml", "/%2e%2e/schema.sql", "/nope.js"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{bad}", timeout=10)
                raise AssertionError(bad)
            except urllib.error.HTTPError as e:
                assert e.code == 404
    finally:
        httpd.shutdown()
