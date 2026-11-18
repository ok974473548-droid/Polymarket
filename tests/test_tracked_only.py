"""Сбор данных только по китам из списка пользователя."""

from whalefollow import tracked
from whalefollow.agent import Agent
from whalefollow.db import DB
from whalefollow.scheduler import Scheduler
from whalefollow.services import Services

from conftest import fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld, load_world


class CountingData(FakeData):
    def __init__(self, world):
        super().__init__(world)
        self.users, self.feed_calls = [], 0

    def trades(self, user=None, **kw):
        if user:
            self.users.append(user.lower())
        else:
            self.feed_calls += 1
        return super().trades(user=user, **kw)


class CountingGamma(FakeGamma):
    def __init__(self, world):
        super().__init__(world)
        self.catalog_calls = 0

    def markets(self, **kw):
        self.catalog_calls += 1
        return super().markets(**kw)


class CountingClob(FakeClob):
    def __init__(self, world):
        super().__init__(world)
        self.price_tokens = []

    def prices_history(self, token_id, *a, **k):
        self.price_tokens.append(token_id)
        return super().prices_history(token_id, *a, **k)


def _setup():
    w = SyntheticWorld(seed=3, n_events=100, n_random=10)
    db = DB("sqlite://:memory:")
    db.init_schema()
    data, gamma, clob = CountingData(w), CountingGamma(w), CountingClob(w)
    agent = Agent(db, fast_params(), data, clob, gamma, clock=lambda: w.now)
    sch = Scheduler(db, fast_params(), gamma, data, clob, agent, clock=lambda: w.now)
    return w, db, data, gamma, clob, agent, sch


def test_nothing_collected_without_whales():
    w, db, data, gamma, clob, agent, sch = _setup()
    for g in ("fast", "books", "slow", "heavy"):
        sch.run_due(g)
    assert data.users == [] and data.feed_calls == 0  # ни ленты, ни чужих кошельков
    assert gamma.catalog_calls == 0 and clob.price_tokens == []
    assert db.scalar("SELECT COUNT(*) AS n FROM trades") == 0
    st = Services(db, fast_params(), clock=lambda: w.now).status()["bootstrap"]
    assert st["tracked"] == 0 and st["auto_discover"] is False


def test_only_added_whale_is_collected():
    w, db, data, gamma, clob, agent, sch = _setup()
    whale = next(x for x, r in w.roles.items() if r == "skilled")
    other = next(x for x, r in w.roles.items() if r == "random")
    db.upsert(
        "watchlist",
        {"entity_id": whale, "mode": "watch", "added_by": "user", "added_ts": 1, "categories_off": "[]"},
        ["entity_id"],
    )
    for g in ("fast", "slow", "heavy"):
        sch.run_due(g)
    assert set(data.users) == {whale} and data.feed_calls == 0  # опрашивается только этот кошелёк
    assert {r["wallet"] for r in db.query("SELECT DISTINCT wallet FROM trades")} == {whale}
    toks = {r["token_id"] for r in db.query("SELECT DISTINCT token_id FROM trades")}
    assert clob.price_tokens and set(clob.price_tokens) <= toks  # цены — только по рынкам кита
    assert db.one("SELECT 1 AS x FROM wallet_scores WHERE entity_id=?", (whale,))
    assert not db.one("SELECT 1 AS x FROM wallet_scores WHERE entity_id=?", (other,))
    assert {r["entity_id"] for r in db.query("SELECT entity_id FROM watchlist")} == {
        whale
    }  # система никого не добавила


def test_purge_removes_other_wallets_only():
    w = SyntheticWorld(seed=2, n_events=60, n_random=5)
    db = DB("sqlite://:memory:")
    db.init_schema()
    load_world(db, w)
    whale = next(x for x, r in w.roles.items() if r == "skilled")
    db.upsert(
        "watchlist",
        {"entity_id": whale, "mode": "copy", "added_by": "user", "added_ts": 1, "categories_off": "[]"},
        ["entity_id"],
    )
    # наша виртуальная ставка в рынке, где кита нет: её цены должны сохраниться
    tok = db.scalar("SELECT token_id FROM trades WHERE wallet<>? LIMIT 1", (whale,))
    db.upsert(
        "orders",
        {
            "order_id": "o1",
            "signal_id": "s1",
            "ts": 1,
            "token_id": tok,
            "side": "BUY",
            "limit_price": 0.5,
            "size_usd": 10,
            "status": "filled",
            "is_paper": 1,
        },
        ["order_id"],
    )
    db.upsert(
        "fills",
        {
            "fill_id": "f1",
            "order_id": "o1",
            "ts": 1,
            "token_id": tok,
            "side": "BUY",
            "price": 0.5,
            "size_shares": 20,
            "fee_usd": 0,
        },
        ["fill_id"],
    )
    db.insert_ignore(
        "price_history", [{"token_id": tok, "ts": 5, "price": 0.5}, {"token_id": "orphan", "ts": 5, "price": 0.5}]
    )
    svc = Services(db, fast_params())
    before = svc.data_usage()
    assert before["trades_other"] > 0 and before["tracked_wallets"] == 1
    res = svc.purge_untracked()
    assert res["removed"]["trades"] == before["trades_other"]
    assert {r["wallet"] for r in db.query("SELECT DISTINCT wallet FROM trades")} == {whale}
    assert db.one("SELECT 1 AS x FROM price_history WHERE token_id=?", (tok,))  # наша ставка — цены на месте
    assert not db.one("SELECT 1 AS x FROM price_history WHERE token_id='orphan'")
    assert svc.data_usage()["trades_other"] == 0


def test_merged_wallets_move_to_one_watchlist_row(db):
    for wal, mode in (("0xa", "watch"), ("0xb", "copy")):
        db.upsert(
            "watchlist",
            {
                "entity_id": wal,
                "mode": mode,
                "added_by": "user",
                "added_ts": 5,
                "categories_off": '["sports"]' if wal == "0xa" else "[]",
            },
            ["entity_id"],
        )
        db.upsert(
            "entity_wallets", {"wallet": wal, "entity_id": "E-1", "link_reason": "x", "confidence": 1}, ["wallet"]
        )
    assert tracked.migrate_watchlist(db) == 2
    rows = db.query("SELECT * FROM watchlist")
    assert len(rows) == 1 and rows[0]["entity_id"] == "E-1" and rows[0]["mode"] == "copy"
    assert rows[0]["categories_off"] == '["sports"]'
    assert set(tracked.tracked_wallets(db)) == {"0xa", "0xb"}


def test_auto_discover_setting(db):
    svc = Services(db, fast_params())
    assert svc.get_settings()["auto_discover"] is False
    assert svc.update_settings({"auto_discover": "yes"})["errors"]
    svc.update_settings({"auto_discover": True})
    assert tracked.auto_discover(db) is True
