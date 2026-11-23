"""Player scouting and the autopilot roster (Polymarket calls are mocked)."""
import json
import time
import unittest
from unittest.mock import patch

from kontur import autopilot, copytrade, scout, trading
from kontur import db as store
from kontur import polymarket as pm
from test_trading import COND, YES, Base

DAY = 86400
NOW = 1_800_000_000.0


def pos(status, entry, size, realized=0.0, price=None, redeemable=False, opened=NOW - 30 * DAY, last=None, oidx=0,
        cond=None, title="Will it rain?", token=None):
    return {"token_id": token or f"t{abs(hash((cond, oidx, opened, entry))) % 10**9}", "condition_id": cond or f"0x{abs(hash(opened)) % 10**9:064x}",
            "outcome": "Yes" if oidx == 0 else "No", "outcome_index": oidx, "title": title, "slug": "", "event_slug": "rain",
            "avg_price": entry, "current_price": price, "total_size": size, "realized_pnl": realized, "status": status,
            "redeemable": redeemable, "end_date": None, "first_entry_at": opened, "last_event_at": last or opened + DAY}


def bets_for(wins, losses, entry=0.6, start=NOW - 170 * DAY, step=DAY, short=False):
    """Decided bets: winners redeemed (CLOSED), losers left OPEN+redeemable at $0, as on Polymarket."""
    out, t = [], start
    title = "Bitcoin Up or Down - 3PM ET" if short else "Will it rain?"
    for i in range(wins + losses):
        n = wins + losses
        win = (i + 1) * losses // n == i * losses // n  # losses spread evenly
        out.append(dict(token=f"tok{i}", condition=f"c{i}", oidx=0, outcome="Yes", title=title, event_slug="ev", entry=entry,
                        size=100, opened=t, end=None, short=int(short), state="closed" if win else "resolved",
                        exit=1.0 if win else 0.0, closed=t + DAY, mark=None))
        t += step
    return out


class MathTests(unittest.TestCase):
    def test_wilson(self):
        self.assertAlmostEqual(scout.wilson_lb(18, 20), 0.699, places=2)
        self.assertAlmostEqual(scout.wilson_lb(80, 100), 0.711, places=2)
        self.assertEqual(scout.wilson_lb(0, 0), 0.0)

    def test_copy_result_pays_slippage_and_fees(self):
        r, edge = scout.copy_result(0.60, 1.0, 0.01, True)
        fee = scout.fee_est(0.61)
        self.assertAlmostEqual(edge, 1 - 0.61 - fee)
        self.assertAlmostEqual(r, edge / (0.61 + fee))
        self.assertLess(scout.copy_result(0.60, 0.0, 0.01, True)[0], -0.99)

    def test_positions_to_bets(self):
        win = scout.bet_from_position(pos("CLOSED", 0.6, 100, realized=40), NOW)
        self.assertEqual((win["state"], win["exit"]), ("closed", 1.0))
        sold = scout.bet_from_position(pos("CLOSED", 0.5, 100, realized=10), NOW)
        self.assertAlmostEqual(sold["exit"], 0.6)
        lost = scout.bet_from_position(pos("OPEN", 0.6, 100, price=0, redeemable=True), NOW)
        self.assertEqual((lost["state"], lost["exit"]), ("resolved", 0.0))
        live = scout.bet_from_position(pos("OPEN", 0.6, 100, price=0.7), NOW)
        self.assertEqual((live["state"], live["mark"]), ("open", 0.7))
        short = scout.bet_from_position(pos("OPEN", 0.6, 100, price=0.7, title="Bitcoin Up or Down - 3PM ET"), NOW)
        self.assertEqual(short["short"], 1)


class MetricsTests(unittest.TestCase):
    def test_losers_left_open_are_counted(self):
        m = scout.metrics(bets_for(30, 10), now=NOW)
        self.assertEqual((m["n"], m["wins"]), (40, 30))
        self.assertAlmostEqual(m["wr"], 0.75)
        self.assertAlmostEqual(m["lb"], scout.wilson_lb(30, 40))
        self.assertGreater(m["roi"], 0.15)

    def test_as_of_hides_later_outcomes(self):
        bets = bets_for(30, 10)
        cut = NOW - 150 * DAY
        m = scout.metrics(bets, now=NOW, as_of=cut)
        self.assertTrue(all(b["closed"] <= cut for b in bets[:m["n"]]))
        self.assertLess(m["n"], 40)

    def test_short_term_and_cheap_bets_ignored(self):
        bets = bets_for(10, 0, short=True) + bets_for(5, 0, entry=0.99)
        self.assertEqual(scout.metrics(bets, now=NOW)["n"], 0)

    def test_behaviour(self):
        m = scout.metrics(bets_for(40, 10), now=NOW)
        self.assertEqual(scout.behaviour(bets_for(40, 10), [], m, NOW)[0], "directional")
        self.assertEqual(scout.behaviour(bets_for(40, 10, short=True), [], m, NOW)[0], "short_crypto")
        self.assertEqual(scout.behaviour(bets_for(40, 10, entry=0.97), [], m, NOW)[0], "sniper")
        mm = bets_for(20, 5)
        mm += [{**b, "oidx": 1, "token": b["token"] + "n"} for b in mm]
        self.assertEqual(scout.behaviour(mm, [], m, NOW)[0], "market_maker")
        self.assertEqual(scout.behaviour(bets_for(5, 1, start=NOW - 20 * DAY), [], scout.metrics(bets_for(5, 1, start=NOW - 20 * DAY), now=NOW), NOW)[0], "thin")

    def test_judge(self):
        bets = bets_for(45, 12, step=2.5 * DAY) + bets_for(3, 0, start=NOW - 10 * DAY)
        m = scout.metrics(bets, slip=0.01, now=NOW)
        m["slip_measured"] = 0.01
        verdict = scout.judge(m, "directional", {})
        self.assertTrue(verdict["passed"], verdict["failed"])
        self.assertGreater(verdict["score"], 50)
        strict = scout.judge(m, "directional", {"min_wr": 90})
        self.assertFalse(strict["passed"])
        self.assertIn("Доля выигрышей копий", strict["failed"])
        self.assertFalse(scout.judge(m, "market_maker", {})["passed"])


def store_candidate(db, wallet, wins=45, losses=12, kind="directional", recent=3, name=""):
    bets = bets_for(wins, losses, step=2.5 * DAY) + bets_for(recent, 0, start=NOW - 10 * DAY)
    for i, b in enumerate(bets):
        b["token"] = f"{wallet[-6:]}-{i}"
    m = scout.metrics(bets, slip=0.01, now=NOW)
    m["slip_measured"] = 0.01
    db.execute("INSERT OR REPLACE INTO scout_wallets(wallet, name, image, sources, added, profiled, kind, facts, metrics, score) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (wallet, name or wallet[-4:], "", "[]", NOW, NOW, kind, "{}", json.dumps(m), scout.score(m)))
    db.executemany("""INSERT OR REPLACE INTO scout_bets(wallet, token, condition, oidx, outcome, title, event_slug, entry, size, opened,
                      closed, exit, state, mark, end_ts, short) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   [(wallet, b["token"], b["condition"], 0, "Yes", b["title"], "ev", b["entry"], b["size"], b["opened"], b["closed"],
                     b["exit"], b["state"], None, None, b["short"]) for b in bets])
    return m


def wallet(i):
    return "0x" + f"{i:040x}"


class AutopilotTests(Base):
    def setUp(self):
        super().setUp()
        self.p3 = patch.object(pm, "trades_page", side_effect=lambda **kw: {"rows": list(self.trades), "next": None})
        self.p4 = patch.object(pm, "positions_page", side_effect=lambda **kw: {"rows": [], "next": None})
        self.p5 = patch.object(time, "time", side_effect=lambda: self.now)
        self.trades = []
        self.now = NOW
        for p in (self.p3, self.p4, self.p5):
            p.start()
        for i in range(1, 16):
            store_candidate(self.db, wallet(i), wins=40 + i, losses=12)
        store_candidate(self.db, wallet(99), kind="market_maker")
        autopilot.update_settings(self.db, self.uid, {"active_count": 5, "bench_size": 3})
        autopilot.enable(self.db, self.uid, True, budget=500)
        self.aid = autopilot.account_id(self.db, self.uid)

    def tearDown(self):
        for p in (self.p3, self.p4, self.p5):
            p.stop()
        super().tearDown()

    def players(self, state):
        return [r[0] for r in self.db.execute("SELECT wallet FROM ap_players WHERE user_id=? AND state=?", (self.uid, state))]

    def test_bootstrap_roster_and_bench(self):
        autopilot.review(self.db, self.uid)
        active, bench = self.players("active"), self.players("bench")
        self.assertEqual((len(active), len(bench)), (5, 3))
        self.assertNotIn(wallet(99), active + bench)
        self.assertIn(wallet(15), active)  # best record first
        cts = self.db.execute("SELECT COUNT(*) FROM copy_trades WHERE account_id=? AND autopilot=1 AND mode='normal'", (self.aid,)).fetchone()[0]
        self.assertEqual(cts, 5)
        self.assertNotIn(self.aid, [p["id"] for p in copytrade.profiles(self.db, self.uid)])

    def test_first_roster_fills_gradually(self):
        self.db.execute("DELETE FROM scout_wallets WHERE wallet NOT IN (?, ?)", (wallet(1), wallet(2)))
        autopilot.update_settings(self.db, self.uid, {"bench_size": 0})
        autopilot.review(self.db, self.uid)
        self.assertEqual(len(self.players("active")), 2)
        for i in range(20, 25):
            store_candidate(self.db, wallet(i))
        autopilot.review(self.db, self.uid)
        self.assertEqual(len(self.players("active")), 5)  # found later, still placed without probation
        store_candidate(self.db, wallet(30))
        loser = self.players("active")[0]
        self.lose(loser, 5)
        autopilot.review(self.db, self.uid)
        self.assertNotIn(wallet(30), self.players("active"))  # after the first full roster only the bench feeds it

    def copy_buy(self, leader, price=0.40, size=1000):
        ct = store.one(self.db.execute("SELECT * FROM copy_trades WHERE leader=? AND account_id=?", (leader, self.aid)))
        trade = {"side": "BUY", "price": price, "size": size, "title": "Will it rain?", "outcome": "Yes", "event_slug": "rain"}
        info = trading.market_info(COND, YES)
        return autopilot.copy_buy(self.db, self.uid, ct, trade, info), ct, info

    def test_copy_size_is_share_of_player(self):
        autopilot.review(self.db, self.uid)
        leader = self.players("active")[0]
        (status, reason, extra), _, _ = self.copy_buy(leader)
        self.assertEqual(status, "filled", reason)
        self.assertAlmostEqual(extra["amount"], 500 / 5 * 0.10, places=1)  # 10% of the player's share
        bet = store.one(self.db.execute("SELECT * FROM ap_bets WHERE wallet=?", (leader,)))
        self.assertEqual(bet["status"], "open")

    def test_event_cap_and_price_range(self):
        autopilot.review(self.db, self.uid)
        leader = self.players("active")[0]
        (status, reason, _), _, _ = self.copy_buy(leader, price=0.985)
        self.assertEqual(status, "skipped")
        self.assertIn("вне диапазона", reason)
        autopilot.update_settings(self.db, self.uid, {"event_cap_pct": 2})
        for _ in range(3):
            res, _, _ = self.copy_buy(leader)
        self.assertEqual(res[0], "skipped")
        self.assertIn("событие", res[1])

    def test_sell_only_player_shares(self):
        autopilot.review(self.db, self.uid)
        a, b = self.players("active")[:2]
        self.copy_buy(a)
        _, ct_b, info = self.copy_buy(b)
        bet_b = store.one(self.db.execute("SELECT * FROM ap_bets WHERE wallet=?", (b,)))
        status, reason, extra = autopilot.copy_sell(self.db, self.uid, ct_b, {"side": "SELL", "price": 0.38, "size": 1000}, info)
        self.assertEqual(status, "filled", reason)
        self.assertAlmostEqual(extra["shares"], bet_b["shares"], places=4)
        self.assertEqual(store.one(self.db.execute("SELECT status FROM ap_bets WHERE wallet=?", (b,)))["status"], "closed")
        self.assertEqual(store.one(self.db.execute("SELECT status FROM ap_bets WHERE wallet=?", (a,)))["status"], "open")

    def lose(self, leader, count):
        for i in range(count):
            self.db.execute("""INSERT INTO ap_bets(user_id, account_id, wallet, token_id, condition_id, opened, shares, bought, cost,
                               proceeds, status, closed, pnl) VALUES(?,?,?,?,?,?,0,10,5,0,'closed',?,-5)""",
                            (self.uid, self.aid, leader, f"x{i}", "c", self.now, self.now + i))

    def test_losing_player_replaced_from_bench(self):
        autopilot.review(self.db, self.uid)
        loser = self.players("active")[0]
        bench_before = set(self.players("bench"))
        self.now += 8 * DAY  # bench players are eligible for early entry
        self.lose(loser, 5)
        autopilot.review(self.db, self.uid)
        self.assertNotIn(loser, self.players("active"))
        self.assertIn(loser, self.players("retired"))
        self.assertEqual(len(self.players("active")), 5)
        self.assertTrue(bench_before & set(self.players("active")))
        ct = store.one(self.db.execute("SELECT mode FROM copy_trades WHERE leader=? AND account_id=?", (loser, self.aid)))
        self.assertEqual(ct["mode"], "done")  # no open copies left, so exits are finished too
        kinds = [r[0] for r in self.db.execute("SELECT kind FROM ap_log WHERE user_id=?", (self.uid,))]
        self.assertIn("replace", kinds)
        reason = self.db.execute("SELECT reason FROM ap_players WHERE wallet=?", (loser,)).fetchone()[0]
        self.assertIn("Серия из 5", reason)

    def test_pinned_player_is_kept(self):
        autopilot.review(self.db, self.uid)
        star = self.players("active")[0]
        autopilot.action(self.db, self.uid, star, "pin")
        self.lose(star, 6)
        autopilot.review(self.db, self.uid)
        self.assertIn(star, self.players("active"))

    def test_bench_waits_without_early_entry(self):
        autopilot.update_settings(self.db, self.uid, {"allow_early": False})
        autopilot.review(self.db, self.uid)
        loser = self.players("active")[0]
        self.lose(loser, 5)
        autopilot.review(self.db, self.uid)
        self.assertEqual(len(self.players("active")), 4)  # nobody ready yet: the place waits

    def test_swaps_per_day_limit(self):
        autopilot.update_settings(self.db, self.uid, {"max_swaps_per_day": 1})
        autopilot.review(self.db, self.uid)
        a, b = self.players("active")[:2]
        self.lose(a, 5)
        self.lose(b, 5)
        autopilot.review(self.db, self.uid)
        self.assertEqual(len({a, b} & set(self.players("retired"))), 1)

    def test_manual_actions_and_ban(self):
        autopilot.review(self.db, self.uid)
        target = self.players("bench")[0]
        autopilot.action(self.db, self.uid, target, "ban")
        self.assertIn(target, self.players("banned"))
        autopilot.review(self.db, self.uid)
        self.assertNotIn(target, self.players("bench") + self.players("active"))
        with self.assertRaises(ValueError):
            autopilot.update_settings(self.db, self.uid, {"active_count": 50})

    def test_disable_pauses_account(self):
        autopilot.enable(self.db, self.uid, False)
        self.assertEqual(self.db.execute("SELECT paused FROM accounts WHERE id=?", (self.aid,)).fetchone()[0], 1)
        autopilot.review(self.db, self.uid)
        self.assertEqual(self.players("active"), [])

    def test_backtest_picks_and_scores_after_cut(self):
        res = scout.backtest(self.db, autopilot.settings(self.db, self.uid), 5, days_back=60, now=NOW)
        self.assertLessEqual(len(res["players"]), 5)
        self.assertIsNotNone(res["baseline"])
        for p in res["players"]:
            self.assertIn("after", p)

    def test_overview(self):
        autopilot.review(self.db, self.uid)
        ov = autopilot.overview(self.db, self.uid)
        self.assertEqual(len(ov["players"]["active"]), 5)
        self.assertAlmostEqual(ov["share"], 100, places=1)
        self.assertIsNotNone(ov["stats"]["expected_wr"])
        card = ov["players"]["active"][0]
        self.assertIn("copies", card)
        detail = autopilot.player(self.db, self.uid, card["wallet"])
        self.assertTrue(detail["verdict"]["passed"])


if __name__ == "__main__":
    unittest.main()


class ProfileCrawlTests(Base):
    def test_profile_wallet_from_api_rows(self):
        w = wallet(7)
        closed = [pos("CLOSED", 0.6, 100, realized=40, opened=NOW - (i + 1) * 3 * DAY, cond=f"c{i}") for i in range(40)]
        losers = [pos("OPEN", 0.6, 100, price=0, redeemable=True, opened=NOW - (i + 1) * 5 * DAY, cond=f"l{i}") for i in range(12)]
        live = [pos("OPEN", 0.55, 100, price=0.6, opened=NOW - i * DAY, cond=f"o{i}") for i in range(4)]

        def positions_page(**kw):
            return {"rows": closed if kw["status"] == "CLOSED" else losers + live, "next": None}
        trades = [{"side": "BUY", "price": 0.5, "size": 100, "timestamp": NOW - 3600 * (i + 2), "token_id": f"k{i}", "name": "Ann"}
                  for i in range(30)]
        scout.add_wallet(self.db, w)
        with patch.object(pm, "positions_page", side_effect=positions_page), \
                patch.object(pm, "trades_page", return_value={"rows": trades, "next": None}), \
                patch.object(pm, "price_window", side_effect=lambda tok, a, b, **kw: [[int(a) + 200, 0.51]]):
            m = scout.profile_wallet(self.db, w, now=NOW)
        self.assertEqual((m["n"], m["wins"]), (52, 40))
        self.assertAlmostEqual(m["slip_measured"], 0.01, places=4)
        cand = scout.candidate(self.db, w)
        self.assertEqual(cand["kind"], "directional")
        self.assertEqual(cand["name"], "Ann")
        self.assertEqual(len(scout.stored_bets(self.db, w)), 56)

    def test_discover_adds_profitable_wallets(self):
        rows = [{"rank": 1, "wallet": wallet(1), "name": "a", "image": "", "pnl": 100.0, "volume": 1.0},
                {"rank": 2, "wallet": wallet(2), "name": "b", "image": "", "pnl": -5.0, "volume": 1.0}]
        with patch.object(pm, "leaderboard", return_value={"data": {"rows": rows}}):
            scout.discover(self.db)
        found = [r[0] for r in self.db.execute("SELECT wallet FROM scout_wallets")]
        self.assertEqual(found, [wallet(1)])
        self.assertEqual(scout.next_wallet(self.db), wallet(1))


class FastScoutingTests(Base):
    """1.1.1: scouting runs in its own thread pool; results of the model are unchanged."""

    def _add(self, i, sources):
        self.db.execute("INSERT INTO scout_wallets(wallet, name, image, sources, added) VALUES(?,?,?,?,?)",
                        (wallet(i), "", "", json.dumps(sources), NOW + i))

    def test_next_wallet_skips_busy_and_prefers_wider_sources(self):
        self._add(1, ["overall:week"])
        self._add(2, ["overall:week", "sports:month", "overall:all"])
        self._add(3, ["sports:week", "crypto:all"])
        self.assertEqual(scout.next_wallet(self.db), wallet(2))
        self.assertEqual(scout.next_wallet(self.db, {wallet(2)}), wallet(3))
        self.assertEqual(scout.next_wallet(self.db, {wallet(2), wallet(3)}), wallet(1))
        self.assertIsNone(scout.next_wallet(self.db, {wallet(1), wallet(2), wallet(3)}))

    def test_pool_profiles_each_wallet_once_in_parallel(self):
        import threading
        from kontur import worker
        for i in range(1, 25):
            self._add(i, ["overall:all"])
        store.kv_set(self.db, "scout:wanted", "1")
        seen, lock, running, peak = [], threading.Lock(), [0], [0]

        def fake_profile(db, w, now=None):
            with lock:
                seen.append(w)
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            db.execute("UPDATE scout_wallets SET profiled=?, metrics='{}' WHERE wallet=?", (time.time(), w))
            with lock:
                running[0] -= 1

        path = self.db.execute("PRAGMA database_list").fetchone()[2]
        with patch.object(scout, "profile_wallet", side_effect=fake_profile):
            pool = worker.ScoutPool(path, threads=4)
            pool.start()
            deadline = time.time() + 10
            while time.time() < deadline and len(seen) < 24:
                time.sleep(0.05)
            pool.stop.set()
            for t in pool.threads:
                t.join(5)
        self.assertEqual(sorted(seen), sorted(wallet(i) for i in range(1, 25)))
        self.assertGreater(peak[0], 1)

    def test_pool_records_upstream_errors(self):
        from kontur import worker
        self._add(1, ["overall:all"])
        store.kv_set(self.db, "scout:wanted", "1")
        with patch.object(scout, "profile_wallet", side_effect=pm.UpstreamError("Источник ответил HTTP 500")):
            self.assertEqual(worker.job_scout(self.db), wallet(1))
        row = self.db.execute("SELECT error, profiled FROM scout_wallets WHERE wallet=?", (wallet(1),)).fetchone()
        self.assertIn("500", row[0])
        self.assertIsNotNone(row[1])

    def test_429_pauses_every_caller_of_the_host(self):
        pm._cool_down("example.test", 0.3)
        start = time.monotonic()
        pm._pace("example.test", 0.01)
        self.assertGreaterEqual(time.monotonic() - start, 0.25)
