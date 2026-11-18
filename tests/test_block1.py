"""Блок 1: журнал операций кошелька, сверка с Polymarket, комиссии, задержка сигналов."""

import sqlite3

import pytest

from whalefollow import fees, reconcile
from whalefollow.db import DB
from whalefollow.execution.paper import PaperBroker, Portfolio
from whalefollow.ingest import pipeline
from whalefollow.ingest.polymarket import DataClient, parse_market
from whalefollow.positions import build_positions
from whalefollow.scoring import scorer

W = "0x" + "aa" * 20
M1, M2 = "0x" + "01" * 32, "0x" + "02" * 32


def _market(cid, closed=True, yes_wins=True, event="ev1", tags=None):
    return {
        "conditionId": cid,
        "question": f"Q {cid[-4:]}?",
        "clobTokenIds": f'["{cid[-4:]}y","{cid[-4:]}n"]',
        "outcomes": '["Yes","No"]',
        "outcomePrices": ('["1","0"]' if yes_wins else '["0","1"]') if closed else '["0.6","0.4"]',
        "closed": closed,
        "events": [{"id": event, "title": "E", "tags": tags or []}],
        "category": "sports",
    }


def _load(db, *raws):
    for raw in raws:
        m, t, e = parse_market(raw)
        db.upsert("events", e, ["event_id"])
        db.upsert("markets", m, ["market_id"])
        db.upsert_many("tokens", t, ["token_id"])


def _trade(i, tok, mid, side, price, size, ts=100):
    return {
        "trade_id": f"t{i}",
        "ts": ts + i,
        "ts_fetched": 1,
        "wallet": W,
        "token_id": tok,
        "market_id": mid,
        "side": side,
        "price": price,
        "size_shares": size,
        "size_usd": price * size,
        "is_maker": None,
        "tx_hash": None,
    }


class _Http:
    """Data API v2: /v2/activity и /v2/positions — {data, pagination}, только курсор (offset → HTTP 400)."""

    def __init__(self, n):
        self.rows = [
            {
                "proxy_wallet": W,
                "timestamp": 10_000 - i,
                "type": "SPLIT",
                "condition_id": M1,
                "size": 1,
                "usdc_size": 1,
                "transaction_hash": f"0x{i}",
                "token_id": None,
            }
            for i in range(n)
        ]
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        assert "offset" not in params  # v2 отвергает offset
        if path == "/v2/positions":
            st = params.get("status", "OPEN") if "cursor" not in params else params["status"]
            rows = {
                "OPEN": [
                    {
                        "token_id": "t1",
                        "condition_id": M1,
                        "realized_pnl": 5,
                        "unrealized_pnl": 2,
                        "current_price": 0.6,
                        "title": "Q",
                    }
                ],
                "CLOSED": [{"token_id": "t2", "realized_pnl": -3, "title": "Q2"}],
            }.get(st, [])
            return {"data": rows, "pagination": {"has_more": False, "next_cursor": None, "limit": 1000, "offset": 0}}
        assert path == "/v2/activity"
        start = int(params.get("cursor") or 0)
        lim = 1000
        page = self.rows[start : start + lim]
        nxt = start + len(page)
        return {
            "data": page,
            "pagination": {
                "has_more": nxt < len(self.rows),
                "next_cursor": str(nxt) if nxt < len(self.rows) else None,
                "limit": lim,
                "offset": 0,
            },
        }


def test_activity_v2_cursor_paging():
    http = _Http(6200)
    got = list(DataClient(http=http).activity(W))
    assert len(got) == 6200 and got[0]["proxyWallet"] == W and got[0]["conditionId"] == M1  # прежний вид строк
    assert http.calls[0][0] == "/v2/activity" and http.calls[0][1]["sort_direction"] == "DESC"
    assert http.calls[1][1] == {"user": W, "cursor": "1000"}
    assert len(http.calls) == 7


def test_positions_v2_mapped_to_old_shape():
    http = _Http(0)
    dc = DataClient(http=http)
    op = dc.positions(W)
    assert [c[1]["status"] for c in http.calls] == ["OPEN", "REDEEMABLE", "REDEEMABLE_LOST"]
    assert op == [{**op[0], "asset": "t1", "realizedPnl": 5, "cashPnl": 2, "curPrice": 0.6}]
    cl = dc.closed_positions(W)
    assert http.calls[-1][1]["status"] == "CLOSED" and cl[0]["asset"] == "t2" and cl[0]["realizedPnl"] == -3


def test_activity_from_v2_not_applied_twice(db):
    old = {
        "proxyWallet": W,
        "timestamp": 50,
        "type": "SPLIT",
        "conditionId": M1,
        "size": "100",
        "usdcSize": "100",
        "transactionHash": "0xAA",
    }  # записано старым методом
    db.insert_ignore("activities", [dict(pipeline.parse_activity(old), activity_id="legacy-id")])

    class D:
        def activity(self, user, start=None):
            return [
                {
                    "proxy_wallet": W,
                    "timestamp": 50,
                    "type": "SPLIT",
                    "condition_id": M1,
                    "size": 100.0,
                    "usdc_size": 100.0,
                    "transaction_hash": "0xaa",
                },
                {
                    "proxy_wallet": W,
                    "timestamp": 70,
                    "type": "MERGE",
                    "condition_id": M1,
                    "size": 10.0,
                    "usdc_size": 10.0,
                    "transaction_hash": "0xbb",
                },
            ]

    assert pipeline.sync_activities(db, D(), [W]) == 1  # только новая операция
    assert db.scalar("SELECT COUNT(*) AS n FROM activities") == 2


def test_split_merge_become_trades_and_positions(db):
    _load(db, _market(M1, yes_wins=True))
    db.insert_ignore(
        "activities",
        [
            {
                "activity_id": "a1",
                "wallet": W,
                "ts": 50,
                "type": "SPLIT",
                "market_id": M1,
                "token_id": None,
                "outcome_index": None,
                "size": 100,
                "usdc_size": 100,
                "price": 0,
                "tx_hash": "0x1",
            },
            {
                "activity_id": "a2",
                "wallet": W,
                "ts": 60,
                "type": "MERGE",
                "market_id": M1,
                "token_id": None,
                "outcome_index": None,
                "size": 40,
                "usdc_size": 40,
                "price": 0,
                "tx_hash": "0x2",
            },
        ],
    )
    assert pipeline.apply_activities(db, [W]) == 4
    assert pipeline.apply_activities(db, [W]) == 0  # повторно не дублируется
    rows = db.query("SELECT side, price, size_shares, source FROM trades WHERE wallet=? ORDER BY ts, token_id", (W,))
    assert {r["source"] for r in rows} == {"split", "merge"} and all(r["price"] == 0.5 for r in rows)
    build_positions(db, [W])
    pos = {p["token_id"]: p for p in db.query("SELECT * FROM positions WHERE wallet=?", (W,))}
    # 100 комплектов за $100, 40 слил обратно за $40; Yes выиграл: 60 × $1 = $60 → итог 0
    total = sum(p["pnl_usd"] or 0 for p in pos.values())
    assert total == pytest.approx(0.0, abs=1e-6)


def test_conversion_events_excluded_from_scoring(db):
    _load(db, _market(M1, event="ev1"), _market(M2, event="ev2"))
    db.insert_ignore(
        "trades", [_trade(1, M1[-4:] + "y", M1, "BUY", 0.4, 100), _trade(2, M2[-4:] + "y", M2, "BUY", 0.4, 100)]
    )
    db.insert_ignore(
        "activities",
        [
            {
                "activity_id": "c1",
                "wallet": W,
                "ts": 70,
                "type": "CONVERSION",
                "market_id": M1,
                "token_id": None,
                "outcome_index": None,
                "size": 10,
                "usdc_size": 0,
                "price": 0,
                "tx_hash": "0x3",
            }
        ],
    )
    build_positions(db, [W])
    f = scorer.load_frames(db, [W])
    assert set(f["pos"]["market_id"]) == {M2}  # событие с конвертацией не участвует в оценке


class _PM:
    def __init__(self, closed, open_=()):
        self.c, self.o = list(closed), list(open_)

    def positions(self, user):
        return self.o

    def closed_positions(self, user):
        return self.c


def test_reconcile_verdicts(db):
    _load(db, _market(M1, yes_wins=True), _market(M2, yes_wins=False, event="ev2"))
    db.insert_ignore(
        "trades", [_trade(1, M1[-4:] + "y", M1, "BUY", 0.4, 100), _trade(2, M2[-4:] + "y", M2, "BUY", 0.5, 50)]
    )
    build_positions(db, [W])
    good = _PM(
        [
            {"asset": M1[-4:] + "y", "realizedPnl": 60.0, "title": "A"},
            {"asset": M2[-4:] + "y", "realizedPnl": -25.0, "title": "B"},
        ]
    )
    r = reconcile.reconcile(db, good, W, [W], now=1000)
    assert r["verdict"] == "ok" and r["n_matched"] == 2 and r["diff_usd"] == pytest.approx(0, abs=0.01)
    bad = _PM(
        [
            {"asset": M1[-4:] + "y", "realizedPnl": 20.0, "title": "A"},
            {"asset": M2[-4:] + "y", "realizedPnl": -25.0, "title": "B"},
        ]
    )
    r2 = reconcile.reconcile(db, bad, W, [W], now=2000)
    assert r2["verdict"] == "bad"
    d = reconcile.describe(reconcile.latest(db, W))
    assert d["verdict"] == "bad" and d["top"][0]["title"] == "A" and "расхождение" in d["text"]
    assert reconcile.reconcile(db, _PM([]), W, [W], now=3000)["verdict"] == "no_data"


def test_fee_model_matches_docs():
    assert fees.fee_usd(0.07, 100, 0.5) == pytest.approx(1.75)  # пример из документации: крипто, 50¢
    assert fees.fee_usd(0.05, 100, 0.5) == pytest.approx(1.25)  # спорт
    assert fees.fee_usd(0.07, 100, 0.99) == pytest.approx(0.0693)
    assert fees.rate_for("politics") == 0.04 and fees.rate_for("politics", ["geopolitics"]) == 0.0
    m, _, _ = parse_market(_market(M1, closed=False, tags=[{"slug": "geopolitics"}]))
    assert m["fee_rate"] == 0.0
    m, _, _ = parse_market(_market(M2, closed=False))
    assert m["fee_rate"] == 0.05


def test_paper_buy_charges_taker_fee(db):
    res = PaperBroker(db).buy("s1", "tok", 50.0, [(0.5, 1000)], fee_rate=0.05, ts=10)
    assert res["shares"] == pytest.approx(100) and res["fee"] == pytest.approx(1.25)
    assert Portfolio(db).cash(1000.0) == pytest.approx(1000 - 50 - 1.25)


def test_old_database_gets_new_columns(tmp_path):
    p = tmp_path / "old.db"
    DB(f"sqlite:///{p}").init_schema()
    c = sqlite3.connect(p)  # «база v0.7»: без новых колонок
    c.execute("DROP INDEX ix_trades_source")
    c.execute("DROP TABLE alembic_version")  # тогда ещё не было Alembic
    for t, col in (("signals", "detected_ts"), ("signals", "info"), ("markets", "fee_rate"), ("trades", "source")):
        c.execute(f"ALTER TABLE {t} DROP COLUMN {col}")
    c.commit()
    c.close()
    db = DB(f"sqlite:///{p}")
    db.init_schema()
    assert {"detected_ts", "info"} <= db._columns("signals") and "fee_rate" in db._columns("markets")
    assert "source" in db._columns("trades")


def test_v2_paging_stops_on_repeated_cursor_and_deadline():
    import time as _t

    from whalefollow.ingest.polymarket import ApiError

    class Loop:
        n = 0

        def get(self, path, params):
            Loop.n += 1
            return {
                "data": [{"token_id": "t1", "realized_pnl": 1}],
                "pagination": {"has_more": True, "next_cursor": "same"},
            }

    rows = DataClient(http=Loop()).closed_positions(W)
    assert len(rows) == 1 and Loop.n == 2  # тот же курсор/те же строки — стоп, без зависания

    class Endless:
        def get(self, path, params):
            Endless.i = getattr(Endless, "i", 0) + 1
            return {
                "data": [{"token_id": f"t{Endless.i}"}],
                "pagination": {"has_more": True, "next_cursor": str(Endless.i)},
            }

    with pytest.raises(ApiError, match="не уложились"):
        DataClient(http=Endless()).closed_positions(W, deadline=_t.time() - 1)
