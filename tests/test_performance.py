"""Оптимизация: фоновая проверка кита, пачки рынков, параллельные запросы, чтение БД во время записи."""
import threading
import time

import numpy as np
import pandas as pd

from conftest import fast_params
from whalefollow.agent import Agent
from whalefollow.db import DB
from whalefollow.ingest import pipeline
from whalefollow.scoring.classify import maker_flags
from whalefollow.services import Services
from whalefollow.util import RateLimiter, pmap
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld


def _old_two_sided(trades: pd.DataFrame) -> float:
    """Прежняя (медленная) реализация F1 — эталон для сравнения."""
    by_market = trades.groupby("market_id")
    two = 0
    for _, g in by_market:
        buys = g[g.side == "BUY"]
        both = buys["token_id"].nunique() >= 2
        bs = any((g[g.token_id == t].side.nunique() == 2) for t in g.token_id.unique())
        two += int(both or bs)
    return two / max(by_market.ngroups, 1)


def test_maker_flags_vectorized_matches_loop():
    rng = np.random.default_rng(3)
    n = 3000
    m = rng.integers(0, 300, n)
    trades = pd.DataFrame({"market_id": [f"m{i}" for i in m],
                           "token_id": [f"m{i}:{k}" for i, k in zip(m, rng.integers(0, 2, n))],
                           "side": rng.choice(["BUY", "SELL"], n, p=[0.85, 0.15]),
                           "size_usd": rng.uniform(1, 100, n)})
    pos = pd.DataFrame(columns=["status", "closed_ts", "opened_ts", "remaining_shares", "avg_entry"])
    t0 = time.time()
    f = maker_flags(trades, pos, fast_params())
    assert time.time() - t0 < 0.5
    assert abs(f["F1_two_sided"] - _old_two_sided(trades)) < 1e-12


class BatchGamma:
    """Каталог, где пачка отдаёт не всё: недостающее должно догрузиться по одному."""

    def __init__(self, n=120):
        self.db = {f"0x{i:064x}": {"conditionId": f"0x{i:064x}", "question": f"Q{i}", "clobTokenIds": f'["{i}a","{i}b"]',
                                   "outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]', "closed": False}
                   for i in range(n)}
        self.batch_calls, self.single_calls = 0, 0

    def markets_by_ids(self, ids):
        self.batch_calls += 1
        return [self.db[i] for i in ids if i in self.db and int(i, 16) % 7]   # каждый 7-й «потерялся»

    def market(self, cid):
        self.single_calls += 1
        return self.db.get(cid)


def test_ensure_markets_batches_and_fills_gaps(db):
    g = BatchGamma()
    ids = list(g.db)
    seen = []
    n = pipeline.ensure_markets(db, g, ids + ["0xmissing"], progress=lambda s, d, t: seen.append((s, d, t)))
    assert n == 120 and db.scalar("SELECT COUNT(*) AS n FROM markets") == 120
    assert g.batch_calls == 3                                  # 121 id → 3 пачки по 50
    assert g.single_calls == 18 + 1                            # 18 «потерянных» + несуществующий
    assert seen and seen[-1][0] == "markets" and seen[-1][1] == seen[-1][2]
    assert pipeline.ensure_markets(db, g, ids) == 0            # повторно ничего не запрашивается


def test_pmap_parallel_and_errors():
    def f(x):
        time.sleep(0.1)
        if x == 3:
            raise ValueError("boom")
        return x * 2
    t0 = time.time()
    res = pmap(f, range(8), workers=8)
    assert time.time() - t0 < 0.5                              # параллельно, а не 0,8 с
    assert [r for _, r, e in res if e is None] == [0, 2, 4, 8, 10, 12, 14]
    assert isinstance(res[3][2], ValueError)


def test_rate_limiter_burst_then_spacing():
    lim = RateLimiter(rps=50, burst=5)
    t0 = time.time()
    for _ in range(5):
        lim.wait()
    assert time.time() - t0 < 0.03                     # короткая пачка — без пауз
    for _ in range(6):
        lim.wait()
    assert time.time() - t0 >= 0.11                    # дальше ровно 50 в секунду
    # за любое окно в 10 с — не больше burst + 10·rps запросов (ниже лимитов Polymarket)
    lim2 = RateLimiter(rps=18)
    assert lim2.capacity == 18 and lim2.capacity + 10 * lim2.rate <= 200


def test_reads_not_blocked_by_long_write(tmp_path):
    db = DB(f"sqlite:///{tmp_path}/t.db")
    db.init_schema()
    db.set_setting("x", 1)
    started, release = threading.Event(), threading.Event()

    def writer():
        with db.tx():
            db.set_setting("x", 2)
            started.set()
            release.wait(5)                                    # «долгий пересчёт» держит запись

    th = threading.Thread(target=writer)
    th.start()
    started.wait(5)
    got = {}
    reader = threading.Thread(target=lambda: got.setdefault("v", db.get_setting("x")))
    t0 = time.time()
    reader.start()
    reader.join(2)
    assert not reader.is_alive() and time.time() - t0 < 1     # экран читает, не дожидаясь записи
    assert got["v"] == 1                                       # видит последнее сохранённое
    release.set()
    th.join()
    assert db.get_setting("x") == 2


def test_background_check_reports_stages():
    world = SyntheticWorld(seed=5, n_events=120, n_random=10)
    db = DB("sqlite://:memory:")
    db.init_schema()
    p = fast_params()
    agent = Agent(db, p, FakeData(world), FakeClob(world), FakeGamma(world), clock=lambda: world.now)
    svc = Services(db, p, agent=agent, data=FakeData(world), gamma=FakeGamma(world), clock=lambda: world.now)
    wallet = next(w for w, r in world.roles.items() if r == "skilled")
    first = svc.start_check(wallet, wait_s=0)
    assert first["ok"] and first["state"] in ("running", "done") and first["job_id"]
    assert [s["key"] for s in first["stages"]] == ["profile", "trades", "activity", "markets", "prices", "score"]
    for _ in range(300):
        st = svc.check_status(first["job_id"])
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "done" and st["checks"] and st["stats"]["trades"] > 0
    assert all(s["status"] == "done" for s in st["stages"])
    assert db.scalar("SELECT COUNT(*) AS n FROM price_history", default=0) > 0     # цены для проверки CLV загружены
    again = svc.start_check(wallet, wait_s=0)                  # повтор в течение 10 минут — тот же результат
    assert again["job_id"] == first["job_id"] and again["state"] == "done"
    assert svc.start_check("не адрес")["ok"] is False


class _GammaServer:
    """Имитация Gamma /markets по документации: без closed — только открытые; closed=any — все;
    список идентификаторов понимает либо повтором параметра (repeat), либо через запятую (comma)."""

    def __init__(self, n=130, array="repeat"):
        self.array, self.calls, self.headers = array, [], {}
        self.rows = {f"0x{i:064x}": {"conditionId": f"0x{i:064x}", "question": f"Q{i}", "closed": i % 3 != 0,
                                     "clobTokenIds": f'["{i}a","{i}b"]', "outcomes": '["Yes","No"]',
                                     "outcomePrices": '["1","0"]' if i % 3 else '["0.5","0.5"]'} for i in range(n)}

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        p = params or {}
        ids = p.get("condition_ids")
        if isinstance(ids, list):
            ids = ids if self.array == "repeat" else ids[-1:]          # «чужой» формат — сервер видит одно значение
        elif isinstance(ids, str):
            ids = ids.split(",") if self.array == "comma" else [ids]
        closed = str(p.get("closed", "false")).lower()
        if ids is not None and len(ids) > 100:
            return _Resp(422, [])
        out = [r for k, r in self.rows.items() if (ids is None or k in ids)
               and (closed == "any" or r["closed"] == (closed == "true"))]
        if ids is None:
            off, lim = int(p.get("offset", 0)), int(p.get("limit", 500))
            out = out[off:off + lim]
        return _Resp(200, out)


class _Resp:
    def __init__(self, code, body):
        self.status_code, self.body, self.text = code, body, ""

    def json(self):
        return self.body


def _client(server):
    from whalefollow.ingest.polymarket import GammaClient, _Http
    http = _Http("https://gamma.test", session=server, rps=0)
    return GammaClient(http=http)


def test_gamma_finds_closed_markets_by_id():
    for fmt in ("repeat", "comma"):
        srv = _GammaServer(array=fmt)
        g = _client(srv)
        closed_id = next(k for k, r in srv.rows.items() if r["closed"])
        assert g.market(closed_id)["conditionId"] == closed_id           # закрытый рынок находится
        ids = list(srv.rows)[:50]
        got = g.markets_by_ids(ids)
        assert len(got) == 50 and g._array_mode == fmt                   # формат списка определился сам
        n_before = len(srv.calls)
        assert len(g.markets_by_ids(list(srv.rows)[50:100])) == 50 and len(srv.calls) == n_before + 1
        assert all(c.get("closed") == "any" for c in srv.calls if "condition_ids" in c)


def test_gamma_catalog_pages_in_parallel_waves():
    srv = _GammaServer(n=1300)
    g = _client(srv)
    rows = list(g.markets(closed=False, page_size=100, workers=4))
    open_ids = [k for k, r in srv.rows.items() if not r["closed"]]
    assert [r["conditionId"] for r in rows] == open_ids                  # порядок и полнота как при обходе по одной


def test_missing_trade_markets_backfill(db):
    srv = _GammaServer()
    g = _client(srv)
    ids = list(srv.rows)[:40]
    db.insert_ignore("trades", [{"trade_id": f"t{i}", "ts": i, "ts_fetched": i, "wallet": "0xw", "token_id": f"{i}a",
                                 "market_id": m, "side": "BUY", "price": .5, "size_shares": 1, "size_usd": .5,
                                 "is_maker": None, "tx_hash": None} for i, m in enumerate(ids)])
    miss = pipeline.missing_trade_markets(db, wallets=["0xw"])
    assert sorted(miss) == sorted(ids)
    assert pipeline.ensure_markets(db, g, miss) == 40
    assert pipeline.missing_trade_markets(db) == []
    assert db.scalar("SELECT COUNT(*) AS n FROM markets WHERE status='resolved'") > 0   # исходы закрытых рынков на месте
