"""Удаление кита со всеми данными, «Перестать следить», ежедневная чистка базы и сжатие файла."""

from __future__ import annotations

import sqlite3

import pytest

from whalefollow import retention, tracked
from whalefollow.db import DB
from whalefollow.services import Services

from conftest import fast_params

NOW = 2_000_000_000
DAY = 86400
A, B, C = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40


def _trade(db: DB, tid: str, wallet: str, token: str, market: str, ts: int = NOW - DAY) -> None:
    db.upsert(
        "trades",
        {
            "trade_id": tid,
            "ts": ts,
            "ts_fetched": ts,
            "wallet": wallet,
            "token_id": token,
            "market_id": market,
            "side": "BUY",
            "price": 0.5,
            "size_shares": 10,
            "size_usd": 5,
            "tx_hash": "0x" + tid,
        },
        ["trade_id"],
    )


def _world(db: DB) -> None:
    """Кит E-a (два кошелька A и B), отдельный кит C; у E-a есть сигнал с нашей ставкой и сигнал без неё."""
    for w in (A, B):
        db.upsert(
            "entity_wallets", {"wallet": w, "entity_id": "E-" + A, "link_reason": "x", "confidence": 1}, ["wallet"]
        )
    for e, mode in (("E-" + A, "copy"), (C, "watch")):
        db.upsert(
            "watchlist",
            {"entity_id": e, "mode": mode, "added_by": "user", "added_ts": NOW - 9 * DAY, "categories_off": "[]"},
            ["entity_id"],
        )
    _trade(db, "t1", A, "tokA", "mA")
    _trade(db, "t2", B, "tokB", "mB")
    _trade(db, "t3", C, "tokC", "mC")
    _trade(db, "t4", A, "tokC", "mC")  # общий рынок с китом C — его цены нужны C
    for tok in ("tokA", "tokB", "tokC"):
        db.executemany(
            "INSERT INTO price_history (token_id, ts, price) VALUES (?,?,?)",
            [(tok, NOW - i * 3600, 0.5) for i in range(3)],
        )
    for w, e in ((A, "E-" + A), (B, "E-" + A), (C, C)):
        db.upsert(
            "positions",
            {"position_id": "p" + w, "wallet": w, "entity_id": e, "token_id": "tok" + w[2].upper(), "market_id": "m"},
            ["position_id"],
        )
    db.upsert(
        "wallet_scores", {"entity_id": "E-" + A, "computed_at": NOW, "status": "approved"}, ["entity_id", "computed_at"]
    )
    db.upsert("wallet_scores", {"entity_id": C, "computed_at": NOW, "status": "approved"}, ["entity_id", "computed_at"])
    db.upsert(
        "transfers", {"transfer_id": "x1", "ts": NOW, "from_wallet": A, "to_wallet": C, "amount": 1}, ["transfer_id"]
    )
    for sid in ("s_bet", "s_nobet"):
        db.upsert(
            "signals",
            {"signal_id": sid, "ts": NOW, "entity_id": "E-" + A, "wallet": A, "token_id": "tokA", "status": "new"},
            ["signal_id"],
        )
        db.upsert("decisions", {"signal_id": sid, "decided_ts": NOW, "decision": "BUY"}, ["signal_id"])
    db.upsert("orders", {"order_id": "o1", "signal_id": "s_bet", "token_id": "tokA", "ts": NOW}, ["order_id"])
    db.upsert("fills", {"fill_id": "f1", "order_id": "o1", "token_id": "tokA", "ts": NOW, "price": 0.5}, ["fill_id"])


def _n(db: DB, sql: str, params: tuple = ()) -> int:
    return int(db.scalar(sql, params, 0) or 0)


def test_delete_whale_removes_everything_but_our_bets(db):
    _world(db)
    out = retention.delete_whale(db, "E-" + A)
    assert out["trades"] == 3 and out["watchlist"] == 1
    assert _n(db, "SELECT COUNT(*) AS n FROM trades WHERE wallet IN (?,?)", (A, B)) == 0
    assert _n(db, "SELECT COUNT(*) AS n FROM positions WHERE entity_id=?", ("E-" + A,)) == 0
    assert _n(db, "SELECT COUNT(*) AS n FROM entity_wallets") == 0
    assert _n(db, "SELECT COUNT(*) AS n FROM wallet_scores WHERE entity_id=?", ("E-" + A,)) == 0
    assert _n(db, "SELECT COUNT(*) AS n FROM transfers") == 0
    # сигнал без ставки удалён вместе с решением; со ставкой — остался, ставка и исполнение тоже
    assert [r["signal_id"] for r in db.query("SELECT signal_id FROM signals")] == ["s_bet"]
    assert [r["signal_id"] for r in db.query("SELECT signal_id FROM decisions")] == ["s_bet"]
    assert _n(db, "SELECT COUNT(*) AS n FROM fills") == 1
    # цены: рынок B больше никому не нужен; A — наша ставка; C — нужен киту C
    toks = {r["token_id"] for r in db.query("SELECT DISTINCT token_id FROM price_history")}
    assert toks == {"tokA", "tokC"}
    # другой кит не тронут
    assert _n(db, "SELECT COUNT(*) AS n FROM trades WHERE wallet=?", (C,)) == 1
    assert tracked.tracked_entities(db) == [C]


def test_delete_by_member_wallet(db):
    _world(db)
    ent, wallets = retention.whale_ids(db, B.upper().replace("0X", "0x"))
    assert ent == "E-" + A and wallets == sorted([A, B])


def test_unfollowed_whale_hidden_and_purged(db):
    _world(db)
    svc = Services(db, fast_params(), clock=lambda: NOW)
    svc.set_whale(C, mode="off")
    assert [r["entity_id"] for r in svc.whales()["items"]] == ["E-" + A]
    # ежедневная чистка: данные кита, за которым перестали следить, уходят; список не трогается
    retention.cleanup(db, now=NOW)
    assert _n(db, "SELECT COUNT(*) AS n FROM trades WHERE wallet=?", (C,)) == 0
    assert _n(db, "SELECT COUNT(*) AS n FROM wallet_scores WHERE entity_id=?", (C,)) == 0
    assert _n(db, "SELECT COUNT(*) AS n FROM trades WHERE wallet IN (?,?)", (A, B)) == 3


def test_cleanup_keeps_recent_checks(db):
    _world(db)
    _trade(db, "t9", "0x" + "d" * 40, "tokD", "mD")
    db.upsert(
        "check_jobs", {"job_id": "j1", "address": "0x" + "d" * 40, "state": "done", "started": NOW - DAY}, ["job_id"]
    )
    retention.cleanup(db, now=NOW)
    assert _n(db, "SELECT COUNT(*) AS n FROM trades WHERE token_id='tokD'") == 1  # проверка была вчера — ждём
    retention.cleanup(db, now=NOW + 5 * DAY)
    assert _n(db, "SELECT COUNT(*) AS n FROM trades WHERE token_id='tokD'") == 0


def test_cleanup_by_age(db):
    _world(db)
    old, fresh = NOW - 400 * DAY, NOW - DAY
    db.executemany(
        "INSERT INTO orderbook_snapshots (token_id, ts, bids, asks, trigger) VALUES (?,?,?,?,?)",
        [
            ("tokA", NOW - 40 * DAY, "[]", "[]", "schedule"),
            ("tokA", NOW - 40 * DAY + 1, "[]", "[]", "signal"),
            ("tokA", NOW - 200 * DAY, "[]", "[]", "signal"),
        ],
    )
    db.executemany(
        "INSERT INTO price_history (token_id, ts, price) VALUES (?,?,?)",
        [("ev1", old, 0.3), ("ev1", fresh, 0.4)],  # страница события: без сделок и ставок
    )
    db.upsert("markets", {"market_id": "mOld", "status": "resolved", "resolved_ts": old}, ["market_id"])
    db.upsert("tokens", {"token_id": "tOld", "market_id": "mOld"}, ["token_id"])
    db.upsert("markets", {"market_id": "mA", "status": "resolved", "resolved_ts": old}, ["market_id"])  # сделки есть
    db.upsert("ext_markets", {"source": "manifold", "ext_id": "1", "updated_ts": old}, ["source", "ext_id"])
    db.upsert("ext_markets", {"source": "manifold", "ext_id": "2", "updated_ts": fresh}, ["source", "ext_id"])
    db.upsert("news_items", {"item_id": "n1", "ts_fetched": old}, ["item_id"])
    db.upsert("notifications", {"notif_id": "x", "ts": old, "kind": "signal", "is_read": 0}, ["notif_id"])
    out = retention.cleanup(db, now=NOW)
    assert [r["trigger"] for r in db.query("SELECT trigger FROM orderbook_snapshots")] == ["signal"]
    assert [r["ts"] for r in db.query("SELECT ts FROM price_history WHERE token_id='ev1'")] == [fresh]
    assert {r["market_id"] for r in db.query("SELECT market_id FROM markets")} == {"mA"}
    assert _n(db, "SELECT COUNT(*) AS n FROM tokens WHERE token_id='tOld'") == 0
    assert [r["ext_id"] for r in db.query("SELECT ext_id FROM ext_markets")] == ["2"]
    assert _n(db, "SELECT COUNT(*) AS n FROM news_items") == 0
    assert _n(db, "SELECT COUNT(*) AS n FROM notifications") == 0
    assert out["orderbook_snapshots"] == 2


def test_shadow_analyses_thinned_one_per_day(db):
    rows = []
    for d in range(5):
        for h in (1, 5, 9):
            ts = NOW - d * DAY - h * 3600
            rows.append((f"a{d}{h}", "ev", ts, "manual", "tok", "shadow"))
    rows.append(("page1", "ev", NOW - 10 * DAY, "manual", "tok", "page"))
    db.executemany(
        "INSERT INTO event_analyses (id, event_id, ts, mode, outcome_key, origin) VALUES (?,?,?,?,?,?)", rows
    )
    retention.cleanup(db, now=NOW)
    by_day: dict[int, int] = {}
    for r in db.query("SELECT ts FROM event_analyses WHERE origin='shadow'"):
        by_day[r["ts"] // DAY] = by_day.get(r["ts"] // DAY, 0) + 1
    recent = {k: v for k, v in by_day.items() if k >= (NOW - 3 * DAY) // DAY}
    old = {k: v for k, v in by_day.items() if k < (NOW - 3 * DAY) // DAY}
    assert all(v == 1 for v in old.values()) and sum(recent.values()) >= 7
    assert _n(db, "SELECT COUNT(*) AS n FROM event_analyses WHERE origin='page'") == 1  # анализы страницы не трогаем


def test_cleanup_disabled(db):
    _world(db)
    db.set_setting("retention", {"enabled": False})
    assert retention.cleanup(db, now=NOW) == {"enabled": False}


@pytest.mark.sqlite_only
def test_compact_sqlite_file(tmp_path):
    path = tmp_path / "wf.db"
    db = DB(f"sqlite:///{path}")
    db.init_schema()
    first = retention.compact(db)  # в работе полный VACUUM не делаем — только при запуске
    assert first["vacuum"] == "ждёт перезапуска (init-db)"
    assert retention.compact(db, full_ok=True)["vacuum"] == "full"
    assert sqlite3.connect(path).execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    db.executemany(
        "INSERT INTO price_history (token_id, ts, price) VALUES (?,?,?)", [("t" * 70, i, 0.5) for i in range(30000)]
    )
    sqlite3.connect(path).execute("PRAGMA wal_checkpoint(TRUNCATE)")
    big = path.stat().st_size
    db.execute("DELETE FROM price_history")
    res = retention.compact(db, min_free_mb=0.1)
    assert res["vacuum"] == "incremental" and path.stat().st_size < big / 2


@pytest.mark.sqlite_only
def test_price_history_without_rowid(tmp_path):
    db = DB(f"sqlite:///{tmp_path / 'x.db'}")
    db.init_schema()
    sql = db.scalar("SELECT sql FROM sqlite_master WHERE name='price_history'")
    assert "WITHOUT ROWID" in sql.upper()
    db.executemany(
        "INSERT OR IGNORE INTO price_history (token_id, ts, price) VALUES (?,?,?)", [("a", 1, 0.5), ("a", 1, 0.6)]
    )
    assert db.scalar("SELECT price FROM price_history") == 0.5  # первичный ключ работает как раньше


def test_api_delete(db):
    from fastapi.testclient import TestClient

    from whalefollow.api.app import create_app

    _world(db)
    c = TestClient(create_app(Services(db, fast_params(), clock=lambda: NOW)))
    r = c.request("DELETE", "/api/whales/" + C, json={})
    assert r.status_code == 200 and r.json()["rows"] > 0
    assert [x["entity_id"] for x in c.get("/api/whales").json()["items"]] == ["E-" + A]
