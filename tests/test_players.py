"""Игроки и рынки по готовым данным Polymarket: поиск, карточка, форма, пачка, панель рынка, период «что было бы»."""

import time

from whalefollow.backtest.whale_sim import simulate_whale
from whalefollow.db import DB
from whalefollow.ingest.polymarket import DataClient, GammaClient
from whalefollow.players import Players, form_summary, form_weeks, max_drawdown
from whalefollow.positions import build_positions
from whalefollow.services import Services

from conftest import fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld, load_world

W = "0x" + "ab" * 20
DAY = 86400


class ReadyData:
    """Готовые ответы Data API v2 (в разборе — как у DataClient)."""

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []
        now = int(time.time())
        # прибыль: 20 недель роста по $100, потом 3 недели падения по $50
        self.points = [
            {"timestamp": now - (23 - k) * 7 * DAY, "economic_pnl": 100.0 * min(k, 20) - 50.0 * max(0, k - 20)}
            for k in range(24)
        ]

    def _hit(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(name + " недоступен")

    def user_stats(self, user):
        self._hit("stats")
        return {
            "proxy_wallet": user,
            "trades": 321,
            "biggest_win": 5400.0,
            "join_date": int(time.time()) - 400 * DAY,
            "all_time_pnl": {"economic_pnl": 1850.0, "volume_usdc": 250000.0, "trade_count": 1200},
        }

    def user_pnl(self, user, interval="1m", fidelity="1d"):
        self._hit("pnl")
        return self.points

    def user_volume(self, user, start=None, end=None):
        self._hit("vol")
        return {"volume_usdc": 42000.0, "trade_count": 88}

    def value(self, user):
        self._hit("value")
        return 9100.0

    def standing(self, user, period="all"):
        self._hit("rank")
        return {
            "rank_pnl": 140 if period == "all" else 0,
            "rank_volume": 900,
            "user_name": "whale_one",
            "verified": True,
        }

    def positions_page(self, user=None, condition=None, status="OPEN", sort_by=None, limit=20):
        self._hit("positions")
        return [
            {
                "token_id": "t1",
                "condition_id": "m1",
                "title": "Will X happen?",
                "outcome": "Yes",
                "current_size": 500,
                "avg_price": 0.41,
                "current_price": 0.55,
                "current_value": 275.0,
                "total_pnl": 70.0,
                "end_date": "2026-12-31T00:00:00Z",
                "event_slug": "will-x",
            }
        ]

    def holders(self, condition, limit=12, include_pnl=True):
        self._hit("holders")
        return {
            "t1": [
                {
                    "proxy_wallet": W,
                    "name": "whale_one",
                    "amount": 500,
                    "avg_price": 0.41,
                    "total_pnl": 70,
                    "current_value": 275,
                }
            ],
            "t2": [{"proxy_wallet": "0x" + "cd" * 20, "pseudonym": "Other", "amount": 900}],
        }


class ReadyGamma:
    def public_profile(self, address):
        return {
            "name": "whale_one",
            "profileImage": "https://img.example/a.png",
            "xUsername": "whale1",
            "verifiedBadge": True,
            "createdAt": "2025-01-01T00:00:00Z",
        }

    def search_profiles(self, q, limit=6):
        return [
            {"wallet": W, "name": "whale_one", "image": None},
            {"wallet": "0x" + "ef" * 20, "name": "whale_one_fan"},
        ]

    def market(self, cid):
        return {
            "conditionId": cid,
            "question": "Will X happen?",
            "description": "Resolves Yes if X.",
            "outcomes": '["Yes","No"]',
            "outcomePrices": '["0.55","0.45"]',
            "clobTokenIds": '["t1","t2"]',
            "volumeNum": 120000,
            "liquidityNum": 15000,
            "endDate": "2026-12-31T00:00:00Z",
            "events": [{"slug": "will-x", "title": "X"}],
        }


def test_form_from_ready_curve():
    data = ReadyData()
    series = [{"ts": p["timestamp"], "pnl": p["economic_pnl"]} for p in data.points]
    weeks = form_weeks(series, 26, series[-1]["ts"])
    assert [w["pnl"] for w in weeks[-3:]] == [-50.0, -50.0, -50.0]
    f = form_summary(weeks)
    assert f["streak_now"] == 0 and f["streak_best"] >= 15 and f["state"] == "cold" and f["last4_pnl"] == -50.0
    assert max_drawdown(series) == 150.0


def test_player_card_uses_ready_data_and_survives_partial_failures():
    p = Players(ReadyData(), ReadyGamma())
    c = p.card(W)
    assert c["ok"] and c["name"] == "whale_one" and c["verified"] and c["x"] == "whale1"
    assert c["pnl_all_usd"] == 1850.0 and c["markets"] == 321 and c["value_usd"] == 9100.0
    assert c["volume_30d_usd"] == 42000.0 and c["rank_pnl"] == 140 and c["rank_pnl_month"] is None  # 0 — вне рейтинга
    assert c["pnl_30d_usd"] == 50.0 and c["pnl_7d_usd"] == -50.0 and c["form"]["state"] == "cold"
    assert c["positions"][0]["avg_cents"] == 41.0 and c["positions"][0]["url"] == "https://polymarket.com/event/will-x"
    calls = len(p.data.calls)
    assert p.card(W) is c and len(p.data.calls) == calls  # второй раз — из кэша
    part = Players(ReadyData(fail={"value", "rank"}), ReadyGamma()).card(W)
    assert part["ok"] and part["value_usd"] is None and set(part["partial"]) == {"value", "rank_all", "rank_month"}
    dead = Players(ReadyData(fail={"stats", "pnl", "value", "rank", "vol", "positions"}), None).card(W)
    assert dead["ok"] is False and "не ответил" in dead["error"]


def test_resolve_link_address_and_name():
    p = Players(ReadyData(), ReadyGamma())
    assert p.resolve(f"https://polymarket.com/profile/{W.upper()}")[0] == {
        "wallet": W,
        "name": "whale_one",
        "image": "https://img.example/a.png",
        "exact": True,
    }
    by_name = p.resolve("https://polymarket.com/@whale_one")
    assert by_name[0]["wallet"] == W and by_name[0]["exact"] and not by_name[1]["exact"]
    assert p.resolve("@whale_one")[0]["wallet"] == W
    assert p.resolve("x") == [] and p.resolve("") == []


def test_market_panel_holders_and_whale_position():
    p = Players(ReadyData(), ReadyGamma())
    m = p.market("m1", "t1", [W])
    assert (
        m["rules"] == "Resolves Yes if X."
        and m["volume_usd"] == 120000
        and m["url"] == "https://polymarket.com/event/will-x"
    )
    assert [o["ours"] for o in m["outcomes"]] == [True, False] and m["outcomes"][0]["price"] == 0.55
    h = {x["wallet"]: x for x in m["holders"]}
    assert h[W]["ours"] and h[W]["same_side"] and h[W]["outcome"] == "Yes" and h[W]["avg_cents"] == 41.0
    assert m["whale_positions"][0]["wallet"] == W and m["whale_positions"][0]["pnl_usd"] == 70.0


def test_client_methods_hit_v2_paths():
    class Http:
        def __init__(self):
            self.calls = []

        def get(self, path, params):
            self.calls.append((path, dict(params)))
            if path == "/v2/user-pnl":
                return {"data": {"points": [{"timestamp": 1, "realized_pnl": 2}]}}
            if path == "/v2/holders":
                return {"data": [{"token_id": "t1", "holders": [{"proxy_wallet": W}]}], "pagination": {}}
            if path == "/public-search":
                return {"profiles": [{"proxyWallet": W.upper(), "name": "n"}, {"name": "no wallet"}]}
            return {"data": {"value": 5}}

    h = Http()
    d = DataClient(http=h)
    assert d.user_pnl(W, "all", "1d") == [{"timestamp": 1, "realized_pnl": 2}]
    assert d.value(W) == 5 and d.holders("m1")["t1"][0]["proxy_wallet"] == W
    d.standing(W, "month")
    d.positions_page(W, condition="m1", sort_by="CURRENT_VALUE", limit=5)
    paths = [c[0] for c in h.calls]
    assert paths == ["/v2/user-pnl", "/v2/value", "/v2/holders", "/v2/leaderboard", "/v2/positions"]
    assert h.calls[3][1] == {"user": W, "time_period": "month"}
    assert h.calls[4][1] == {"status": "OPEN", "limit": 5, "user": W, "condition": "m1", "sort_by": "CURRENT_VALUE"}
    g = GammaClient(http=h)
    assert g.search_profiles("n") == [{"wallet": W, "name": "n", "pseudonym": None, "image": None}]


def test_services_add_players_copy_after_check(db):
    w = SyntheticWorld(seed=5, n_events=120, n_random=10)
    fd = FakeData(w)
    for name in ("user_stats", "user_pnl", "user_volume", "value", "standing", "positions_page", "holders"):
        setattr(fd, name, getattr(ReadyData(), name))
    svc = Services(db, fast_params(), data=fd, gamma=FakeGamma(w), clock=lambda: w.now)
    skilled = next(x for x, r in w.roles.items() if r == "skilled")
    rnd = next(x for x, r in w.roles.items() if r == "random")
    r = svc.add_players([skilled, rnd, "мусор"], mode="copy")
    assert r["ok"] and len(r["added"]) == 2 and all(a["mode"] == "watch" for a in r["added"])
    assert set(db.get_state("copy_after_check")) == {skilled, rnd}
    for a in r["added"]:  # ждём фоновые проверки
        for _ in range(400):
            st = svc.check_status(a["job_id"])
            if st["state"] != "running":
                break
            time.sleep(0.05)
        assert st["state"] == "done" and st["card"]["ok"] and st["stages"][0]["key"] == "profile"
    modes = {x["entity_id"]: x["mode"] for x in db.query("SELECT entity_id, mode FROM watchlist")}
    res = {a["wallet"]: svc.check_status(a["job_id"])["fits"] for a in r["added"]}
    for a in r["added"]:
        assert modes[a["entity_id"]] == ("copy" if res[a["wallet"]] else "watch")
    assert db.get_state("copy_after_check") == []
    card = svc.player_card(skilled)
    assert card["in_list"] and card["pnl_all_usd"] == 1850.0 and card["entity_id"]
    assert svc.resolve_players(skilled)["items"][0]["in_list"]


def test_simulation_for_chosen_dates():
    w = SyntheticWorld(seed=4)
    db = DB("sqlite://:memory:")
    db.init_schema()
    load_world(db, w)
    build_positions(db)
    skilled = next(x for x, r in w.roles.items() if r == "skilled")
    s = {"bankroll_usd": 1000, "max_bet_usd": 40, "categories": []}
    full = simulate_whale(db, fast_params(), [skilled], s, w.now, days=365)
    start, end = w.now - 200 * DAY, w.now - 100 * DAY
    part = simulate_whale(db, fast_params(), [skilled], s, w.now, start_ts=start, end_ts=end)
    assert part["ok"] and 0 < part["summary"]["n"] < full["summary"]["n"]
    assert all(start <= b["ts"] <= end for b in part["bets"])
    curve = part["summary"]["curve"]
    assert curve[0]["pnl_usd"] == 0.0 and curve[-1]["pnl_usd"] == part["summary"]["closed_pnl_usd"]  # итог совпадает
    svc = Services(db, fast_params(), clock=lambda: w.now)
    viaapi = svc.simulate(skilled, start=start, end=end)
    assert (
        viaapi["ok"] and viaapi["summary"]["start_ts"] == start and all(start <= b["ts"] <= end for b in viaapi["bets"])
    )
    assert svc.simulate(skilled, start=w.now - 100, end=w.now)["ok"] is False


def test_market_panel_service(db):
    w = SyntheticWorld(seed=3, n_events=20, n_random=2)
    load_world(db, w)
    fd = FakeData(w)
    fd.holders = ReadyData().holders
    fd.positions_page = ReadyData().positions_page
    svc = Services(db, fast_params(), data=fd, gamma=ReadyGamma(), clob=FakeClob(w), clock=lambda: w.now)
    tok = db.one("SELECT token_id FROM tokens LIMIT 1")["token_id"]
    m = svc.market_panel(tok)
    assert m["ok"] and m["rules"] and m["holders"] and "in_list" in m["holders"][0] and m["fee_rate"] is not None
    assert svc.market_panel("нет такого") is None
