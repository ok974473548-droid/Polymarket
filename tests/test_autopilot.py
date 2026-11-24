"""Player scouting and the autopilot roster (Polymarket calls are mocked)."""
import json
import time
import unittest
from unittest.mock import patch

from kontur import autopilot, copytrade, packs, scout, trading
from kontur import db as store
from kontur import polymarket as pm
from test_trading import COND, YES, Base, book

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

    def test_payout_exit_is_exact(self):
        # A redeemed position: the Data API's average price is rounded (0.602), so entry + realized / size = 0.99990.
        size = 33.8983
        won = scout.bet_from_position(pos("CLOSED", 0.602, size, realized=(0.9999049 - 0.602) * size), NOW)
        self.assertEqual(won["exit"], 1.0)  # until 1.4.4: 0.9999049 (shown as "$1,000" in the player dialog)
        lost = scout.bet_from_position(pos("CLOSED", 0.36, 59.03, realized=-(0.36 - 0.0002) * 59.03), NOW)
        self.assertEqual(lost["exit"], 0.0)
        sold = scout.bet_from_position(pos("CLOSED", 0.60, 100, realized=0.395 * 100), NOW)
        self.assertAlmostEqual(sold["exit"], 0.995)  # a real sale near $1 is kept
        # Old stored bets with the noise are settled on use: a payout has no exit fee.
        x, paid = scout.decided_exit({"state": "closed", "exit": 0.9999049})
        self.assertEqual((x, paid), (1.0, True))
        self.assertEqual(scout.decided_exit({"state": "closed", "exit": 0.995}), (0.995, False))

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
        verdict = scout.judge(m, "directional", {"selection": "winrate"})
        self.assertTrue(verdict["passed"], verdict["failed"])
        self.assertGreater(verdict["score"], 50)
        strict = scout.judge(m, "directional", {"selection": "winrate", "min_wr": 90})
        self.assertFalse(strict["passed"])
        self.assertIn("Доля выигрышей копий", strict["failed"])
        self.assertFalse(scout.judge(m, "market_maker", {"selection": "winrate"})["passed"])


def store_candidate(db, wallet, wins=45, losses=12, kind="directional", recent=3, name=""):
    bets = bets_for(wins, losses, step=2.5 * DAY) + bets_for(recent, 0, start=NOW - 10 * DAY)
    for i, b in enumerate(bets):
        b["token"] = f"{wallet[-6:]}-{i}"
    m = scout.metrics(bets, slip=0.01, now=NOW)
    m["slip_measured"] = 0.01
    db.execute("INSERT OR REPLACE INTO scout_wallets(wallet, name, image, sources, added, profiled, kind, facts, metrics, score) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (wallet, name or wallet[-4:], "", "[]", NOW, NOW, kind, "{}", json.dumps(m), scout.score(m)))
    packs.save(db, wallet, [{**b, "oidx": 0, "outcome": "Yes", "event_slug": "ev", "mark": None, "end": None} for b in bets])
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
        # roster mechanics are checked with the win-rate selection and market copies (skill and limit copies: below)
        autopilot.update_settings(self.db, self.uid, {"active_count": 5, "bench_size": 3, "selection": "winrate",
                                                      "copy_mode": "market"})
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

    def test_bet_result_counts_partial_sales(self):
        # Bought 100 shares for $50, sold 50 for $40, 50 left at the mid price $0.60 (worth $30).
        bet = {"status": "open", "value": 30.0, "proceeds": 40.0, "cost": 50.0, "pnl": None}
        # Before 1.4.2 the row showed only the unsold half: 30 - 50 * 50/100 = +5; the $40 from the sale was lost.
        self.assertAlmostEqual(autopilot.bet_result(bet), 20.0)  # 30 + 40 - 50
        self.assertAlmostEqual(autopilot.bet_result({**bet, "proceeds": 0.0}), -20.0)  # nothing sold yet: value - cost
        self.assertIsNone(autopilot.bet_result({**bet, "value": None}))  # no price yet
        self.assertEqual(autopilot.bet_result({"status": "closed", "value": None, "proceeds": 40.0, "cost": 50.0, "pnl": -10.0}), -10.0)

    def test_copy_stats_count_partial_sales(self):
        # The same copy as above: bought 100 for $50, sold 50 for $40, 50 left at $0.60.
        w = "0x" + "e" * 40
        self.db.execute("""INSERT INTO ap_bets(user_id, account_id, wallet, token_id, condition_id, opened, shares, bought, cost, proceeds)
                           VALUES(?,?,?,?,?,?,50,100,50,40)""", (self.uid, self.aid, w, "tp", "cp", self.now))
        self.db.execute("""INSERT INTO positions(account_id, token_id, condition_id, shares, cost, bought, last_price, status, updated)
                           VALUES(?,?,?,50,25,100,0.6,'open',?)""", (self.aid, "tp", "cp", self.now))
        st = autopilot.copy_stats(self.db, self.uid, w)
        # Before 1.4.2: 30 - 25 = +5 (the sale was left out); now the card matches the bets tab row.
        self.assertAlmostEqual(st["pnl"], 20.0)
        self.assertAlmostEqual(st["realized"], 15.0)  # 40 received for half of the $50
        self.assertAlmostEqual(st["pnl"], sum(r["result"] for r in autopilot.bets(self.db, self.uid, wallet=w)))

    def test_bets_and_events_rows(self):
        autopilot.review(self.db, self.uid)
        leader = self.players("active")[0]
        (status, reason, extra), _, _ = self.copy_buy(leader)
        self.assertEqual(status, "filled", reason)
        rows = autopilot.bets(self.db, self.uid)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertFalse(r["resolved"])
        self.assertAlmostEqual(r["result"], r["value"] - r["cost"], places=6)
        ct = store.one(self.db.execute("SELECT * FROM copy_trades WHERE leader=? AND account_id=?", (leader, self.aid)))
        trade = {"side": "BUY", "price": 0.40, "size": 1000, "title": "Will it rain?", "outcome": "Yes", "event_slug": "rain",
                 "token_id": YES, "condition_id": COND, "transaction_hash": "0xabc", "timestamp": self.now}
        copytrade._record(self.db, ct, status, reason, trade, **extra)
        ev = copytrade.events(self.db, self.uid, self.aid)[0]
        fee = self.db.execute("SELECT fees FROM orders WHERE id=?", (extra["order_id"],)).fetchone()[0]
        self.assertGreater(fee, 0)
        self.assertAlmostEqual(ev["fee"], fee)  # the copy's fee comes with the event for the details row

    def test_sell_when_data_api_is_behind_the_stream(self):
        autopilot.review(self.db, self.uid)
        leader = self.players("active")[0]
        (status, reason, _), ct, info = self.copy_buy(leader)
        self.assertEqual(status, "filled", reason)
        held = trading.available_shares(self.db, self.aid, YES)
        # The player sold all 1000 shares; the Data API still shows them (its last event is older than the sale).
        stale = {"rows": [{"token_id": YES, "current_size": 1000.0, "last_event_at": self.now - 600}], "next": None}
        sale = {"side": "SELL", "price": 0.38, "size": 1000, "timestamp": self.now}
        with patch.object(pm, "positions_page", return_value=stale):
            status, reason, extra = autopilot.copy_sell(self.db, self.uid, ct, sale, info)
        self.assertEqual(status, "filled", reason)
        self.assertAlmostEqual(extra["shares"], held, places=4)  # before 1.4.4: half (1000 / (1000 + 1000))
        self.assertEqual(store.one(self.db.execute("SELECT status FROM ap_bets WHERE wallet=?", (leader,)))["status"], "closed")

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


def bets_events(n, entry, wins, start=NOW - 170 * DAY, step=DAY, prefix="e"):
    """Decided bets of one player, each in its own event (skill is counted by event)."""
    out, t = [], start
    for i in range(n):
        win = (i + 1) * wins // n != i * wins // n
        out.append(dict(token=f"{prefix}{i}", condition=f"{prefix}c{i}", oidx=0, outcome="Yes", title="Will it happen?",
                        event_slug=f"{prefix}ev{i}", entry=entry, size=100, opened=t, end=None, short=0,
                        state="closed" if win else "resolved", exit=1.0 if win else 0.0, closed=t + DAY, mark=None))
        t += step
    return out


class SkillTests(unittest.TestCase):
    def test_fee_estimate_is_the_real_copy_fee(self):
        # 1.10.0: 0.05 * p * (1 - p) per share, as live copies pay (before: 0.02 * p * (1 - p))
        self.assertAlmostEqual(scout.fee_est(0.60), 0.012)
        r, edge = scout.copy_result(0.60, 1.0, 0.01, True)
        self.assertAlmostEqual(edge, 1 - 0.61 - 0.05 * 0.61 * 0.39)
        old_edge = 1 - 0.61 - 0.02 * 0.61 * 0.39
        self.assertAlmostEqual(old_edge - edge, 0.03 * 0.61 * 0.39)   # было → стало: $0.0071 less per share

    def test_bh_cut(self):
        self.assertAlmostEqual(scout.bh_cut([0.001, 0.01, 0.2, 0.5], q=0.10), 0.01)
        self.assertEqual(scout.bh_cut([0.3, 0.6]), 0.0)
        self.assertEqual(scout.bh_cut([]), 0.0)

    def test_favourite_buyer_against_skilled_underdog_buyer(self):
        # 88% wins buying at 80¢ is mostly the price; 50% wins buying at 30¢ is skill.
        fav = scout.metrics(bets_events(80, 0.80, 70) + bets_events(5, 0.80, 5, start=NOW - 5 * DAY, prefix="r"),
                            slip=0.005, now=NOW)
        dog = scout.metrics(bets_events(80, 0.30, 40) + bets_events(5, 0.30, 3, start=NOW - 5 * DAY, prefix="r"),
                            slip=0.005, now=NOW)
        for m in (fav, dog):
            m["slip_measured"] = 0.005
        self.assertGreater(fav["wr"], 0.85)
        self.assertLess(dog["wr"], 0.55)
        self.assertLess(fav["skill"]["excess"], dog["skill"]["excess"])
        self.assertLess(dog["skill"]["p"], 0.001)
        self.assertGreater(fav["skill"]["p"], 0.001)
        winrate = {"selection": "winrate"}
        self.assertTrue(scout.judge(fav, "directional", winrate)["passed"])      # until 1.10.0 the favourite was chosen
        self.assertFalse(scout.judge(dog, "directional", winrate)["passed"])     # and the skilled player was not
        skill = {"selection": "skill"}
        self.assertFalse(scout.judge(fav, "directional", skill, cut=0.001)["passed"])
        verdict = scout.judge(dog, "directional", skill, cut=0.001)
        self.assertTrue(verdict["passed"], verdict["failed"])
        self.assertGreater(verdict["score"], 50)
        self.assertFalse(scout.judge(dog, "directional", skill, cut=0.0)["passed"])  # nothing significant across all

    def test_skill_needs_enough_events(self):
        one_event = bets_for(45, 12)          # every bet in one event: no independent evidence
        self.assertIsNone(scout.metrics(one_event, now=NOW)["skill"])

    def test_live_copies_set_the_slippage(self):
        self.assertIsNone(scout.copy_slip(None, None))
        self.assertEqual(scout.copy_slip(0.01, None), 0.01)
        self.assertEqual(scout.copy_slip(0.01, {"mean": 0.058, "n": 10}), 0.058)
        self.assertEqual(scout.copy_slip(None, {"mean": -0.01, "n": 10}), scout.DEFAULT_SLIP)

    def test_expected_return(self):
        self.assertAlmostEqual(autopilot.expected_return({"skill": {"edge": 0.04}, "roi": 0.3, "n": 100}), 0.04)
        self.assertAlmostEqual(autopilot.expected_return({"skill": None, "roi": 0.2, "n": 40}), 0.1)
        self.assertIsNone(autopilot.expected_return({"skill": None, "roi": None, "n": 0}))


class LiveSlipTests(Base):
    def test_from_market_copies_of_every_user(self):
        leader = "0x" + "c" * 40
        acc = self.acc["id"]
        ct = self.db.execute("INSERT INTO copy_trades(account_id, leader, sizing, created) VALUES(?,?,'fixed',?)",
                             (acc, leader, time.time())).lastrowid
        for i, (paid, kind) in enumerate([(0.45, "market")] * 5 + [(0.40, "limit")] * 3):
            cur = self.db.execute("""INSERT INTO orders(account_id, token_id, condition_id, side, type, status, created, updated)
                                     VALUES(?,?,?,?,?,?,?,?)""", (acc, YES, COND, "buy", kind, "filled", time.time(), time.time()))
            self.db.execute("""INSERT INTO copy_events(copy_trade_id, account_id, leader, status, side, leader_price, price, order_id, at)
                               VALUES(?,?,?,'filled','buy',0.40,?,?,?)""", (ct, acc, leader, paid, cur.lastrowid, time.time()))
        live = scout.live_slip(self.db, leader)
        self.assertEqual(live["n"], 5)                # limit copies pay the player's price by design: left out
        self.assertAlmostEqual(live["mean"], 0.05)

    def test_old_profiles_are_recomputed_from_kept_bets(self):
        w = "0x" + "d" * 40
        bets = bets_events(40, 0.30, 20)
        packs.save(self.db, w, bets)
        old = scout.metrics(bets, now=NOW)
        old.pop("version")
        old.pop("skill")
        old["slip_measured"] = 0.01
        self.db.execute("INSERT INTO scout_wallets(wallet, name, image, sources, added, profiled, kind, facts, metrics, score) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)", (w, "x", "", "[]", NOW, NOW, "directional", "{}", json.dumps(old), 1))
        self.assertEqual(scout.refresh_metrics(self.db), 1)
        m = json.loads(self.db.execute("SELECT metrics FROM scout_wallets WHERE wallet=?", (w,)).fetchone()[0])
        self.assertEqual(m["version"], scout.METRICS_VERSION)
        self.assertIsNotNone(m["skill"])
        self.assertEqual(m["slip_measured"], 0.01)
        self.assertEqual(scout.refresh_metrics(self.db), 0)
        # the minute job stops looking once nothing is left to recompute
        from kontur import worker
        store.kv_set(self.db, "scout:wanted", "1")
        self.db.execute("UPDATE scout_wallets SET metrics=? WHERE wallet=?", (json.dumps(old), w))
        worker.job_rescore(self.db)
        self.assertEqual(store.kv_get(self.db, "scout:rescored", ""), "")
        worker.job_rescore(self.db)
        self.assertEqual(store.kv_get(self.db, "scout:rescored", ""), str(scout.METRICS_VERSION))


class CopyJournalMigrationTests(unittest.TestCase):
    def test_older_journal_rebuilt_with_every_row(self):
        import os
        import sqlite3
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "old.sqlite3")
        store.init(path)
        db = sqlite3.connect(path)
        # make it look like 1.9.0: the CHECK without 'waiting', a few rows
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='copy_events'").fetchone()[0]
        cols = [r[1] for r in db.execute("PRAGMA table_info(copy_events)")]
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("ALTER TABLE copy_events RENAME TO ce_tmp")
        db.execute(sql.replace(",'waiting'", ""))
        db.execute("DROP TABLE ce_tmp")
        db.execute("INSERT INTO users(id, username, pw_hash, pw_salt, created) VALUES(1,'u','x','y',0)")
        db.execute("INSERT INTO accounts(id, user_id, kind, name, balance, created) VALUES(1,1,'copy','c',0,0)")
        db.execute("INSERT INTO copy_trades(id, account_id, leader, sizing, created) VALUES(1,1,'0xabc','fixed',0)")
        for i in range(5):
            db.execute("INSERT INTO copy_events(copy_trade_id, account_id, leader, status, reason, side, at, key_hash) "
                       "VALUES(1,1,'0xabc','skipped','r',?,?,?)", ("buy", i, i))
        db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO copy_events(copy_trade_id, account_id, leader, status, side, at) VALUES(1,1,'x','waiting','buy',0)")
        db.close()
        store.init(path)                                      # the app starting with the new version
        db = store.connect(path)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM copy_events").fetchone()[0], 5)
        self.assertEqual([r[1] for r in db.execute("PRAGMA table_info(copy_events)")], cols)
        db.execute("INSERT INTO copy_events(copy_trade_id, account_id, leader, status, side, at) VALUES(1,1,'x','waiting','buy',0)")
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='copy_events'")}
        self.assertTrue({"copy_events_hash", "copy_events_account", "copy_events_at"} <= names)
        db.close()


class NewAutopilotTests(AutopilotTests):
    """1.10.0: limit copies, the price allowance, manual removal, a bigger roster, soft activity, warnings."""

    def trade(self, price, side="BUY", tx="0x1"):
        return {"side": side, "price": price, "size": 1000, "title": "Will it rain?", "outcome": "Yes", "event_slug": "rain",
                "token_id": YES, "condition_id": COND, "transaction_hash": tx, "timestamp": self.now}

    def ct_of(self, leader):
        return store.one(self.db.execute("SELECT * FROM copy_trades WHERE leader=? AND account_id=?", (leader, self.aid)))

    def event_of(self, ct):
        return store.one(self.db.execute("SELECT * FROM copy_events WHERE copy_trade_id=? ORDER BY id DESC", (ct["id"],)))

    def test_market_copy_price_allowance_relative(self):
        autopilot.review(self.db, self.uid)
        leader = self.players("active")[0]
        autopilot.update_settings(self.db, self.uid, {"copy_slip": 0.2, "copy_slip_pct": 50})
        (status, _, _), _, _ = self.copy_buy(leader, price=0.30)        # was: up to $0.15 more allowed → bought at 40–45¢
        self.assertEqual(status, "filled")
        autopilot.update_settings(self.db, self.uid, {"copy_slip_pct": 3})
        (status, reason, _), _, _ = self.copy_buy(leader, price=0.30)   # now: 3% of 30¢ (at least 1¢) → nothing to buy
        self.assertEqual(status, "skipped")
        self.assertIn("$0.01", reason)
        self.assertAlmostEqual(autopilot.price_allowance(0.15, {"copy_slip": 0.2, "copy_slip_pct": 3}), 0.01)
        self.assertAlmostEqual(autopilot.price_allowance(0.80, {"copy_slip": 0.2, "copy_slip_pct": 3}), 0.024)
        self.assertAlmostEqual(autopilot.price_allowance(0.80, {"copy_slip": 0.02, "copy_slip_pct": 3}), 0.02)

    def test_limit_copy_waits_then_fills(self):
        autopilot.update_settings(self.db, self.uid, {"copy_mode": "limit", "limit_wait_min": 2})
        autopilot.review(self.db, self.uid)
        leader = self.players("active")[0]
        ct = self.ct_of(leader)
        cash = self.db.execute("SELECT balance FROM accounts WHERE id=?", (self.aid,)).fetchone()[0]
        self.assertTrue(copytrade.handle(self.db, ct, self.trade(0.38), self.uid, source="stream"))
        ev = self.event_of(ct)
        self.assertEqual(ev["status"], "waiting", ev["reason"])                 # best ask 40¢ > the player's 38¢
        order = store.one(self.db.execute("SELECT * FROM orders WHERE id=?", (ev["order_id"],)))
        self.assertEqual((order["type"], order["status"], order["limit_price"]), ("limit", "open", 0.38))
        self.assertAlmostEqual(order["shares"], 26.31)                          # $10 copy / 38¢
        self.assertAlmostEqual(autopilot._open_cost(self.db, self.uid, wallet=leader), 26.31 * 0.38, places=4)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ap_bets").fetchone()[0], 0)
        trading.match_resting(self.db, {YES: book(asks=((0.37, 100),))})        # the price came back
        bet = store.one(self.db.execute("SELECT * FROM ap_bets WHERE wallet=?", (leader,)))
        self.assertAlmostEqual(bet["shares"], 26.31)
        self.assertAlmostEqual(bet["cost"], 26.31 * 0.38, places=4)            # the player's price, no taker fee
        ev = self.event_of(ct)
        self.assertEqual(ev["status"], "filled")
        self.assertAlmostEqual(ev["price"], 0.38)
        after = self.db.execute("SELECT balance FROM accounts WHERE id=?", (self.aid,)).fetchone()[0]
        self.assertAlmostEqual(cash - after, 26.31 * 0.38, places=4)

    def test_limit_copy_expires_or_is_withdrawn_on_sale(self):
        autopilot.update_settings(self.db, self.uid, {"copy_mode": "limit", "limit_wait_min": 2})
        autopilot.review(self.db, self.uid)
        a, b = self.players("active")[:2]
        cash = self.db.execute("SELECT balance FROM accounts WHERE id=?", (self.aid,)).fetchone()[0]
        copytrade.handle(self.db, self.ct_of(a), self.trade(0.38, tx="0xa"), self.uid)
        copytrade.handle(self.db, self.ct_of(b), self.trade(0.38, tx="0xb"), self.uid)
        # b sells before the price came back: the waiting buy is withdrawn
        copytrade.handle(self.db, self.ct_of(b), self.trade(0.36, side="SELL", tx="0xc"), self.uid)
        self.assertEqual(self.event_of(self.ct_of(b))["reason"], "Покупка ещё ждала цену игрока — снята")
        buy_b = store.one(self.db.execute("SELECT * FROM copy_events WHERE copy_trade_id=? AND side='buy'", (self.ct_of(b)["id"],)))
        self.assertEqual(buy_b["status"], "skipped")
        self.assertEqual(buy_b["reason"], "Игрок продал раньше, чем цена вернулась")
        # a's order runs out of time
        self.now += 3 * 60
        trading.expire_orders(self.db)
        ev = self.event_of(self.ct_of(a))
        self.assertEqual((ev["status"], ev["reason"]), ("skipped", "Цена не вернулась к цене игрока за отведённое время"))
        self.assertAlmostEqual(self.db.execute("SELECT balance FROM accounts WHERE id=?", (self.aid,)).fetchone()[0], cash, places=6)

    def test_limit_copy_fills_at_once_when_the_price_is_there(self):
        autopilot.update_settings(self.db, self.uid, {"copy_mode": "limit"})
        autopilot.review(self.db, self.uid)
        leader = self.players("active")[0]
        (status, reason, extra), _, _ = self.copy_buy(leader, price=0.40)
        self.assertEqual(status, "filled", reason)
        self.assertAlmostEqual(extra["price"], 0.40)

    def test_manual_removal_is_not_undone(self):
        autopilot.review(self.db, self.uid)
        target = self.players("bench")[0]
        autopilot.action(self.db, self.uid, target, "remove")
        autopilot.review(self.db, self.uid)
        self.assertIn(target, self.players("retired"))                 # until 1.10.0 it was back on the bench at once
        self.assertNotIn(target, self.players("bench"))
        until = self.db.execute("SELECT until FROM ap_players WHERE wallet=?", (target,)).fetchone()[0]
        self.assertGreaterEqual(until - self.now, 7 * DAY)
        self.assertEqual(len(self.players("bench")), 3)                 # its place went to someone else

    def test_bigger_roster_fills_new_places(self):
        autopilot.review(self.db, self.uid)
        self.assertEqual(len(self.players("active")), 5)
        autopilot.update_settings(self.db, self.uid, {"active_count": 7})
        autopilot.review(self.db, self.uid)
        self.assertEqual(len(self.players("active")), 7)                # until 1.10.0: 5 until the bench was ready
        self.lose(self.players("active")[0], 5)
        n_bench = len(self.players("bench"))
        autopilot.review(self.db, self.uid)
        self.assertLessEqual(len(self.players("active")), 7)
        self.assertEqual(self.db.execute("SELECT value FROM kv WHERE key=?", (f"ap:{self.uid}:bootstrapped",)).fetchone()[0], "7")
        self.assertLessEqual(len(self.players("bench")), n_bench + 1)

    def idle(self, wallet):
        m = json.loads(self.db.execute("SELECT metrics FROM scout_wallets WHERE wallet=?", (wallet,)).fetchone()[0])
        m["new_14d"] = 1
        self.db.execute("UPDATE scout_wallets SET metrics=?, profiled=? WHERE wallet=?", (json.dumps(m), self.now + 1, wallet))

    def test_idle_player_replaced_only_when_someone_can_take_over(self):
        for i in range(40, 48):                      # active players for the roster and the bench
            store_candidate(self.db, wallet(i), wins=50, losses=12, recent=8)
        autopilot.update_settings(self.db, self.uid, {"min_recent": 4, "allow_early": True, "early_days": 7})
        autopilot.review(self.db, self.uid)
        self.assertEqual(len(self.players("bench")), 3)
        idle = self.players("active")[0]
        self.now += 60
        self.idle(idle)                                                 # 1 new bet in 14 days, soft requirement 2
        autopilot.review(self.db, self.uid)
        self.assertIn(idle, self.players("active"))                     # nobody on the bench may enter yet
        reason = self.db.execute("SELECT reason FROM ap_players WHERE wallet=?", (idle,)).fetchone()[0]
        self.assertIn("новых ставок", reason.lower())
        self.now += 8 * DAY                                             # the bench may enter early now
        self.idle(idle)
        autopilot.review(self.db, self.uid)
        self.assertNotIn(idle, self.players("active"))
        self.assertEqual(len(self.players("active")), 5)

    def test_warnings_for_settings_that_lose_or_lock_money(self):
        self.assertEqual(len(autopilot.warnings(autopilot.settings(self.db, self.uid))), 1)   # win-rate selection at 70%
        autopilot.update_settings(self.db, self.uid, {"selection": "skill"})
        self.assertEqual(autopilot.warnings(autopilot.settings(self.db, self.uid)), [])
        autopilot.update_settings(self.db, self.uid, {"price_max": 0.99, "max_days_to_end": 0, "copy_slip": 0.2,
                                                      "copy_slip_pct": 50})
        text = " ".join(autopilot.warnings(autopilot.settings(self.db, self.uid)))
        self.assertIn("99¢", text)
        self.assertIn("Без ограничения срока", text)
        self.assertIn("$0.20", text)
        ov = autopilot.overview(self.db, self.uid)
        self.assertEqual(len(ov["warnings"]), 3)
        with self.assertRaises(ValueError):
            autopilot.update_settings(self.db, self.uid, {"copy_mode": "fast"})


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
