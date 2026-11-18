"""Пробный режим: планировщик с пустой базы на синтетическом рынке, прогресс приёмки, Telegram."""

import pytest

from whalefollow.agent import Agent
from whalefollow.db import DB
from whalefollow.estimator.base import FunctionEstimator
from whalefollow.notifier import TelegramNotifier
from whalefollow.scheduler import Scheduler
from whalefollow.services import Services

from conftest import fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld


@pytest.fixture(scope="module")
def live():
    world = SyntheticWorld(seed=4)
    db = DB("sqlite://:memory:")
    db.init_schema()
    db.set_setting("auto_discover", True)  # здесь проверяется режим автопоиска китов
    params = fast_params()
    agent = Agent(
        db,
        params,
        FakeData(world),
        FakeClob(world),
        FakeGamma(world),
        estimator=FunctionEstimator(lambda ctx: 0.72),
        clock=lambda: world.now,
    )
    sched = Scheduler(
        db,
        params,
        FakeGamma(world),
        FakeData(world),
        FakeClob(world),
        agent,
        clock=lambda: world.now,
        discover_max_new=200,
        discover_pages=30,
        prices_backfill_per_run=5000,
    )
    ran = sched.run_due("slow") + sched.run_due("heavy")
    return {"world": world, "db": db, "agent": agent, "sched": sched, "ran": ran, "params": params}


def test_bootstrap_from_empty_db(live):
    db = live["db"]
    assert live["ran"] == [
        "markets",
        "discover",
        "resolutions",
        "prices",
        "transfers",
        "activity",
        "recategorize",
        "kalshi",
        "odds",
        "score",
        "score_changes",
        "housekeeping",
        "weekly",
        "backup",
    ]
    assert all(j.last_ok for j in live["sched"].jobs if j.group in ("slow", "heavy")), [
        j.last_error for j in live["sched"].jobs
    ]
    assert db.scalar("SELECT COUNT(*) AS n FROM wallet_sync", default=0) >= 50
    assert db.scalar("SELECT COUNT(*) AS n FROM price_history", default=0) > 0
    copy = db.query("SELECT entity_id FROM watchlist WHERE mode='copy'")
    assert copy, "после первого прохода должен появиться живой список китов"
    roles = live["world"].roles
    for r in copy:
        wallet = r["entity_id"][2:] if r["entity_id"].startswith("E-") else r["entity_id"]
        assert roles.get(wallet) in ("skilled", "sybil"), "в список попал не «умный» игрок"


def test_slow_jobs_respect_intervals(live):
    assert live["sched"].run_due("slow") == [] and live["sched"].run_due("heavy") == []  # время не сдвинулось


def test_fast_loop_catches_whale_trade(live):
    world, db, agent = live["world"], live["db"], live["agent"]
    live["sched"].run_due("fast")  # первая догрузка — сигналов из истории нет
    assert db.scalar("SELECT COUNT(*) AS n FROM signals", default=0) == 0
    whale, cat = None, None
    for r in db.query("SELECT entity_id FROM watchlist WHERE mode='copy'"):
        cats = [
            c["category"]
            for c in db.query(
                "SELECT category FROM wallet_category_scores WHERE entity_id=? AND status='approved'", (r["entity_id"],)
            )
            if c["category"] in ("economics", "politics", "sports", "tech")
        ]
        if cats:
            whale, cat = r["entity_id"], cats[0]
            break
    assert whale
    m = world.add_live_market(category=cat, pi=0.75, price=0.55)
    world.whale_trade_now(agent.wallets_of(whale)[0], m, m.yes_token, 3000)
    world.now += 6
    assert "agent" in live["sched"].run_due("fast")
    sig = db.one("SELECT * FROM signals WHERE market_id=?", (m.market_id,))
    assert sig and sig["status"] == "pending"
    world.now += 20
    assert live["sched"].run_due("books") == ["books", "warm"]  # warm у фейковых клиентов ничего не делает
    assert db.scalar("SELECT COUNT(*) AS n FROM orderbook_snapshots WHERE trigger='schedule'", default=0) > 0


def test_trial_progress(live):
    t = Services(live["db"], live["params"], clock=lambda: live["world"].now).trial()
    keys = [c["key"] for c in t["criteria"]]
    assert keys == ["signals", "weeks", "calibration", "drawdown"]
    assert t["signals"]["total"] >= 1 and t["verdict"]["status"] == "running"
    assert {j["name"] for j in t["jobs"]} >= {"agent", "books", "score", "discover"}
    assert t["criteria"][2]["ok"] is None  # исходов ещё мало — калибровка не оценивается


class _FakeSession:
    def __init__(self):
        self.posts = []

    def post(self, url, json, timeout):
        self.posts.append((url, json))

        class R:
            def raise_for_status(self):
                return None

        return R()


def test_telegram_respects_settings():
    db = DB("sqlite://:memory:")
    db.init_schema()
    sess = _FakeSession()
    tg = TelegramNotifier(db, "TOKEN", "42", public_url="http://box:8788", session=sess)
    db.listeners.append(tg.on_notify)
    db.notify("signal", "Новый сигнал от кита 5C00", "Вопрос?", "s_1")
    tg.q.join()
    assert len(sess.posts) == 1
    url, body = sess.posts[0]
    assert "botTOKEN/sendMessage" in url and body["chat_id"] == "42"
    assert "http://box:8788/#/signal/s_1" in body["text"]
    db.set_setting("notify", {"telegram": True, "signals": False})
    db.notify("signal", "ещё сигнал")
    tg.q.join()
    assert len(sess.posts) == 1  # выключено в настройках — не отправляем
