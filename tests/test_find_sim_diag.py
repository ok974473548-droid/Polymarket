"""Поиск китов в рейтинге, «что было бы при копировании», диагностика."""
import time

import pytest
import httpx

from conftest import fast_params
from synthetic import FakeData, SyntheticWorld, load_world
from whalefollow import diagnostics
from whalefollow.backtest.whale_sim import simulate_whale
from whalefollow.db import DB
from whalefollow.ingest.polymarket import ApiError, DataClient
from whalefollow.positions import build_positions
from whalefollow.services import Services


class _Http:
    """Data API: /v2/leaderboard с курсором (1200 строк всего); /v1 — старый формат."""

    def __init__(self, fail_v2=None, fail_v1=False):
        self.calls, self.fail_v2, self.fail_v1 = [], fail_v2, fail_v1

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/v2/leaderboard":
            if self.fail_v2:
                raise ApiError(f"GET https://data-api.polymarket.com/v2/leaderboard failed: HTTP {self.fail_v2}: x")
            start, total = int(params.get("cursor") or 0), 1200
            rows = [{"rank": i + 1, "user_id": f"0x{i:040X}", "user_name": f"user{i}", "pnl": 5000.0 - i,
                     "volume": 10000.0 * (i + 1), "verified": i == 0} for i in range(start, min(start + params["limit"], total))]
            nxt = start + len(rows)
            return {"data": rows, "pagination": {"next_cursor": str(nxt) if nxt < total else None,
                                                 "has_more": nxt < total, "limit": params["limit"]}}
        if self.fail_v1:
            raise ApiError("GET https://data-api.polymarket.com/v1/leaderboard failed: HTTP 410: gone")
        return [{"rank": str(i + 1), "proxyWallet": f"0x{i:040x}", "userName": f"user{i}", "vol": 10000.0 * (i + 1),
                 "pnl": 5000.0 - i * 100, "verifiedBadge": i == 0} for i in range(params["limit"])]


def test_leaderboard_v2_params_and_paging():
    http = _Http()
    dc = DataClient(http=http)
    rows = dc.leaderboard("politics", "week", "vol", limit=80, offset=30)
    path, p = http.calls[0]
    assert path == "/v2/leaderboard" and p == {"time_period": "week", "category": "politics", "sort_by": "VOLUME",
                                               "limit": 110}
    assert len(rows) == 80 and rows[0]["rank"] == 31 and rows[0]["wallet"] == f"0x{30:040x}"   # адрес в нижнем регистре
    assert dc.lb_version == "v2" and len(http.calls) == 1
    # больше 1000 строк — продолжение по курсору
    http2 = _Http()
    rows = DataClient(http=http2).leaderboard("overall", "month", "pnl", limit=100, offset=950)
    assert [c[1].get("cursor") for c in http2.calls] == [None, "1000"] and http2.calls[1][1]["limit"] == 50
    assert len(rows) == 100 and rows[-1]["rank"] == 1050
    assert DataClient(http=_Http()).leaderboard("overall", "all", "pnl", limit=50, offset=1190)[-1]["rank"] == 1200


def test_leaderboard_falls_back_to_v1_and_reports_v2_error():
    http = _Http(fail_v2=404)
    dc = DataClient(http=http)
    rows = dc.leaderboard("sports", "day", "pnl", limit=20)
    assert [c[0] for c in http.calls] == ["/v2/leaderboard", "/v1/leaderboard"] and dc.lb_version == "v1"
    assert len(rows) == 20 and rows[0]["verified"] and http.calls[1][1]["timePeriod"] == "DAY"
    with pytest.raises(ApiError, match="v2/leaderboard.*HTTP 410"):
        DataClient(http=_Http(fail_v2=410, fail_v1=True)).leaderboard()


class _LbData(FakeData):
    def __init__(self, world):
        super().__init__(world)
        self.n = 0

    def leaderboard(self, category, period, order, limit=50, offset=0):
        self.n += 1
        return [{"rank": 1, "wallet": "0xaaa", "name": "whale", "pnl": 900.0, "volume": 3000.0, "x": None,
                 "verified": False},
                {"rank": 2, "wallet": "0xbbb", "name": None, "pnl": 500.0, "volume": 0, "x": None, "verified": False}]


def test_leaderboard_service_marks_and_cache(db):
    w = SyntheticWorld(seed=1, n_events=10, n_random=2)
    data = _LbData(w)
    svc = Services(db, fast_params(), data=data)
    db.upsert("watchlist", {"entity_id": "0xaaa", "mode": "watch", "added_by": "user", "added_ts": 1,
                            "categories_off": "[]"}, ["entity_id"])
    r = svc.leaderboard("politics", "month", "pnl")
    assert r["ok"] and r["items"][0]["in_list"] and not r["items"][1]["in_list"]
    assert r["items"][0]["roi_pct"] == 30.0 and r["items"][1]["roi_pct"] is None      # объём 0 — не делим
    assert db.scalar("SELECT COUNT(*) AS n FROM trades") == 0                          # история игроков не грузится
    svc.leaderboard("politics", "month", "pnl")
    assert data.n == 1                                                                 # повтор — из кэша
    assert svc.leaderboard("weird", "x", "y")["period"] == "month"


def test_simulation_separates_skill_from_luck():
    w = SyntheticWorld(seed=4)
    db = DB("sqlite://:memory:")
    db.init_schema()
    load_world(db, w)
    build_positions(db)
    svc = Services(db, fast_params(), clock=lambda: w.now)
    skilled = next(x for x, r in w.roles.items() if r == "skilled")
    rnd = next(x for x, r in w.roles.items() if r == "random")
    a, b = svc.simulate(skilled)["summary"], svc.simulate(rnd)["summary"]
    assert a["n"] > 0 and b["n"] > 0
    assert a["roi_pct"] > b["roi_pct"]
    assert a["fees_usd"] > 0 and a["stake_usd"] == 20.0                            # 2% от $1000, не больше $40
    assert a["whale_roi_pct"] >= a["roi_pct"] - 1                                   # задержка не улучшает цену
    # жёсткий дневной лимит: не больше одной ставки в день
    s = simulate_whale(db, fast_params(), [skilled], {"bankroll_usd": 1000, "max_bet_usd": 40, "daily_limit_usd": 20,
                                                      "categories": []}, w.now)
    days = [b["ts"] // 86400 for b in s["bets"]]
    assert len(days) == len(set(days))
    assert simulate_whale(db, fast_params(), [], {}, w.now)["ok"] is False


def test_diagnostics_run_and_report(db):
    def ok():
        return True, "отвечает"

    def timeout():
        raise httpx.ReadTimeout("timeout")

    def forbidden():
        resp = httpx.Response(403, request=httpx.Request("GET", "https://x.test"))
        resp.raise_for_status()
    items = [{"key": "a", "label": "A", "group": "polymarket", "fn": ok},
             {"key": "b", "label": "B", "group": "kalshi", "fn": timeout},
             {"key": "c", "label": "C", "group": "polymarket", "fn": forbidden}]
    out = diagnostics.run(db, items)
    by = {c["key"]: c for c in out["checks"]}
    assert by["a"]["ok"] and not out["ok"]
    assert "нет ответа" in by["b"]["detail"] and by["b"]["hint"]
    assert "403" in by["c"]["detail"] and "ограничен" in by["c"]["detail"]
    d = Services(db, fast_params()).diagnostics(live=False)                          # берёт последнюю проверку
    assert d["probe"]["ts"] == out["ts"] and "ERR C" in d["report"] and "OK  A" in d["report"]
    assert "0x" not in d["report"]                                                    # в отчёте нет адресов
    keys = {p["key"] for p in diagnostics.probes(db)}
    assert keys >= {"gamma", "trades", "leaderboard", "clob", "kalshi", "activity", "positions", "closed_positions",
                    "wallet_trades", "ws_market", "ws_live"}


def test_diagnostics_status_codes_and_warm_timing(db):
    def gone():
        resp = httpx.Response(410, request=httpx.Request("GET", "https://x.test"))
        resp.raise_for_status()

    def busy():
        resp = httpx.Response(503, request=httpx.Request("GET", "https://x.test"))
        resp.raise_for_status()
    calls = {"n": 0}

    def slow_then_fast():                      # первый запрос — с установкой соединения
        calls["n"] += 1
        time.sleep(0.12 if calls["n"] == 1 else 0.0)
        return True, "отвечает"
    items = [{"key": "g", "label": "G", "group": "polymarket", "fn": gone},
             {"key": "b", "label": "B", "group": "polymarket", "fn": busy},
             {"key": "w", "label": "W", "group": "polymarket", "fn": slow_then_fast},
             {"key": "s", "label": "S", "group": "stream", "fn": slow_then_fast, "warm": False}]
    by = {c["key"]: c for c in diagnostics.run(db, items)["checks"]}
    assert "410" in by["g"]["detail"] and "отключён" in by["g"]["detail"] and "Обновите" in by["g"]["hint"]
    assert "сети" not in by["g"]["hint"]                    # 410 — не проблема сети или страны
    assert "сбой на стороне сервиса" in by["b"]["detail"]
    w = by["w"]
    assert w["cold_ms"] >= 100 and w["warm_ms"] is not None and w["warm_ms"] < 60 and w["ms"] == w["warm_ms"]
    assert w["speed"] == "мгновенно"
    assert by["s"]["warm_ms"] is None                       # поток проверяется один раз
    assert diagnostics.speed_label(120) == "быстро" and diagnostics.speed_label(900) == "медленно"


def test_leaderboard_service_pages_from_memory(db):
    class Many(FakeData):
        def __init__(self, world):
            super().__init__(world)
            self.calls = []

        def leaderboard(self, category, period, order, limit=50, offset=0):
            self.calls.append((limit, offset))
            return [{"rank": i + 1, "wallet": f"0x{i:040x}", "name": None, "pnl": 1.0, "volume": 10.0, "x": None,
                     "verified": False} for i in range(120)][offset:offset + limit]
    data = Many(SyntheticWorld(seed=1, n_events=10, n_random=2))
    svc = Services(db, fast_params(), data=data)
    a = svc.leaderboard("overall", "week", "pnl", 0)
    b = svc.leaderboard("overall", "week", "pnl", 50)
    c = svc.leaderboard("overall", "week", "pnl", 100)
    assert len(a["items"]) == 50 and a["has_more"] and b["items"][0]["rank"] == 51 and b["has_more"]
    assert len(c["items"]) == 20 and not c["has_more"]
    assert data.calls == [(500, 0)]                         # один запрос, дальше листание из памяти

    class Gone(FakeData):
        def leaderboard(self, *a, **k):
            raise ApiError("GET https://data-api.polymarket.com/v2/leaderboard failed: HTTP 410: gone")
    r = Services(db, fast_params(), data=Gone(SyntheticWorld(seed=1, n_events=10, n_random=2))).leaderboard()
    assert not r["ok"] and "отключён" in r["error"] and "Обновите" in r["error"]
