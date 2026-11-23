import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kontur import auth, copytrade, trading
from kontur import db as store
from kontur import polymarket as pm

COND = "0x" + "ab" * 32
YES, NO = "111", "222"


def gamma_market(closed=False, prices=("0.40", "0.60"), fee=True):
    return {
        "conditionId": COND, "slug": "test-market", "question": "Will it rain?", "groupItemTitle": "",
        "outcomes": '["Yes", "No"]', "outcomePrices": '["%s", "%s"]' % prices, "clobTokenIds": f'["{YES}", "{NO}"]',
        "closed": closed, "active": True, "acceptingOrders": True, "enableOrderBook": True,
        "feesEnabled": fee, "feeSchedule": {"exponent": 1, "rate": 0.05, "takerOnly": True},
        "orderMinSize": 5, "orderPriceMinTickSize": 0.01, "image": "", "events": [{"slug": "rain", "title": "Rain"}],
    }


def book(asks=((0.40, 100), (0.45, 100)), bids=((0.38, 100), (0.35, 100))):
    return {"token": YES, "asks": [list(a) for a in asks], "bids": [list(b) for b in bids], "min_size": 5, "tick": 0.01, "at": 0}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        path = str(Path(self.dir.name) / "t.sqlite3")
        store.init(path)
        self.db = store.connect(path)
        self.uid = auth.create_user(self.db, "owner", "password123", admin=True)
        self.acc = trading.main_account(self.db, self.uid)
        self.market = gamma_market()
        self.book = book()
        self.p1 = patch.object(pm, "markets_by_condition", side_effect=lambda conds: {COND: self.market})
        self.p2 = patch.object(pm, "book", side_effect=lambda tok: self.book)
        self.p1.start()
        self.p2.start()

    def tearDown(self):
        self.p1.stop()
        self.p2.stop()
        self.db.close()
        self.dir.cleanup()

    def balance(self, aid=None):
        return self.db.execute("SELECT balance FROM accounts WHERE id=?", (aid or self.acc["id"],)).fetchone()[0]


class TradingTests(Base):
    def test_start_balance_and_deposit_withdraw(self):
        self.assertEqual(self.balance(), 1000)
        trading.deposit(self.db, self.uid, self.acc["id"], 250)
        trading.withdraw(self.db, self.uid, self.acc["id"], 50)
        self.assertAlmostEqual(self.balance(), 1200)
        with self.assertRaises(ValueError):
            trading.withdraw(self.db, self.uid, self.acc["id"], 5000)

    def test_market_buy_charges_taker_fee(self):
        order = trading.place(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES, "amount": 40})
        fee_share = 0.05 * 0.4 * 0.6
        shares = round(40 / (0.4 + fee_share), 4)
        self.assertEqual(order["status"], "filled")
        self.assertAlmostEqual(order["filled"], shares, places=3)
        self.assertAlmostEqual(order["fees"], shares * fee_share, places=3)
        self.assertAlmostEqual(self.balance(), 960, places=3)
        pos = trading.position_rows(self.db, self.acc["id"])[0]
        self.assertAlmostEqual(pos["cost"], 40, places=3)

    def test_fee_free_market(self):
        self.market = gamma_market(fee=False)
        order = trading.place(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES, "amount": 40})
        self.assertAlmostEqual(order["filled"], 100, places=4)
        self.assertEqual(order["fees"], 0)

    def test_market_buy_walks_levels_and_respects_cap(self):
        order = trading.place(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES,
                                                  "amount": 100, "max_price": 0.41})
        self.assertLessEqual(order["filled"], 100)
        self.assertAlmostEqual(order["avg"], 0.40)

    def test_sell_realizes_pnl(self):
        trading.place(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES, "amount": 40})
        shares = trading.available_shares(self.db, self.acc["id"], YES)
        order = trading.place(self.db, self.uid, {"side": "sell", "type": "market", "condition": COND, "token": YES, "shares": shares})
        self.assertEqual(order["status"], "filled")
        closed = trading.position_rows(self.db, self.acc["id"], "closed")[0]
        self.assertLess(closed["realized"], 0)  # sold below the buy price and paid fees twice

    def test_cannot_sell_more_than_held(self):
        with self.assertRaises(ValueError):
            trading.place(self.db, self.uid, {"side": "sell", "type": "market", "condition": COND, "token": YES, "shares": 10})

    def test_limit_buy_rests_and_fills_as_maker(self):
        order = trading.place(self.db, self.uid, {"side": "buy", "type": "limit", "condition": COND, "token": YES,
                                                  "shares": 50, "price": 0.30})
        self.assertEqual(order["status"], "open")
        self.assertAlmostEqual(self.balance(), 985)  # 50 x 0.30 reserved
        self.book = book(asks=((0.29, 80),))
        trading.match_resting(self.db, {YES: self.book})
        filled = trading.orders(self.db, self.uid, self.acc["id"], "done")[0]
        self.assertEqual(filled["status"], "filled")
        self.assertAlmostEqual(filled["avg"], 0.30)
        self.assertEqual(filled["fees"], 0)
        self.assertAlmostEqual(self.balance(), 985)

    def test_limit_cancel_releases_reserve(self):
        order = trading.place(self.db, self.uid, {"side": "buy", "type": "limit", "condition": COND, "token": YES,
                                                  "shares": 50, "price": 0.30})
        trading.cancel(self.db, self.uid, order["id"])
        self.assertAlmostEqual(self.balance(), 1000)

    def test_limit_minimum_size(self):
        with self.assertRaises(ValueError):
            trading.place(self.db, self.uid, {"side": "buy", "type": "limit", "condition": COND, "token": YES, "shares": 2, "price": 0.3})

    def test_settlement_pays_winning_shares(self):
        self.market = gamma_market(fee=False)
        trading.place(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES, "amount": 40})
        trading.settle(self.db, COND, gamma_market(closed=True, prices=("1", "0")))
        self.assertAlmostEqual(self.balance(), 960 + 100)
        pos = trading.position_rows(self.db, self.acc["id"], "closed")[0]
        self.assertEqual(pos["status"], "redeemed")
        self.assertAlmostEqual(pos["realized"], 60)

    def test_quote_matches_place(self):
        q = trading.quote(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES, "amount": 40})
        order = trading.place(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES, "amount": 40})
        self.assertAlmostEqual(q["to_win"], order["filled"], places=4)


class CopyTests(Base):
    def setUp(self):
        super().setUp()
        self.profile = copytrade.create_profile(self.db, self.uid, "Киты")
        trading.transfer_from_main(self.db, self.uid, self.profile["id"], 200)
        self.leader = "0x" + "1" * 40
        self.trades = []
        self.p3 = patch.object(pm, "trades_page", side_effect=lambda **kw: {"rows": list(self.trades), "next": None})
        self.p4 = patch.object(pm, "positions_page", side_effect=lambda **kw: {"rows": [], "next": None})
        self.p3.start()
        self.p4.start()

    def tearDown(self):
        self.p3.stop()
        self.p4.stop()
        super().tearDown()

    def trade(self, side, price, size, ts):
        return {"proxy_wallet": self.leader, "side": side, "token_id": YES, "condition_id": COND, "size": size, "price": price,
                "timestamp": ts, "title": "Will it rain?", "event_slug": "rain", "outcome": "Yes", "transaction_hash": f"0x{ts}"}

    def make(self, **kw):
        body = {"leader": self.leader, "sizing": "fixed", "amount": 25, **kw}
        return copytrade.create(self.db, self.uid, self.profile["id"], body)

    def poll(self, cid):
        ct = store.one(self.db.execute("SELECT * FROM copy_trades WHERE id=?", (cid,)))
        return copytrade.poll(self.db, ct)

    def test_transfer_moves_cash(self):
        self.assertAlmostEqual(self.balance(), 800)
        self.assertAlmostEqual(self.balance(self.profile["id"]), 200)

    def test_past_trades_are_not_copied(self):
        self.trades = [self.trade("BUY", 0.4, 100, 1000)]
        ct = self.make()
        self.assertEqual(self.poll(ct["id"]), 0)

    def test_fixed_copy_and_events(self):
        ct = self.make()
        self.trades = [self.trade("BUY", 0.4, 1000, 9_999_999_999)]
        self.assertEqual(self.poll(ct["id"]), 1)
        ev = copytrade.events(self.db, self.uid, self.profile["id"])[0]
        self.assertEqual(ev["status"], "filled")
        self.assertAlmostEqual(ev["amount"], 25, places=2)
        self.assertEqual(self.poll(ct["id"]), 0)  # not copied twice

    def test_percent_and_price_range(self):
        ct = self.make(sizing="percent", percent=10, amount=None, price_min=0.5)
        self.trades = [self.trade("BUY", 0.4, 1000, 9_999_999_999)]
        self.poll(ct["id"])
        ev = copytrade.events(self.db, self.uid, self.profile["id"])[0]
        self.assertEqual(ev["status"], "skipped")
        self.assertIn("ниже диапазона", ev["reason"])

    def test_daily_limit(self):
        ct = self.make(daily_limit=30)
        self.trades = [self.trade("BUY", 0.4, 1000, 9_999_999_990), self.trade("BUY", 0.4, 1000, 9_999_999_991)]
        self.poll(ct["id"])
        events = copytrade.events(self.db, self.uid, self.profile["id"])
        self.assertEqual([e["status"] for e in reversed(events)], ["filled", "filled"])
        self.assertAlmostEqual(sum(e["amount"] for e in events), 30, places=1)

    def test_slippage_skips(self):
        ct = self.make(slippage=1)
        self.trades = [self.trade("BUY", 0.30, 1000, 9_999_999_999)]  # book asks start at 0.40
        self.poll(ct["id"])
        ev = copytrade.events(self.db, self.uid, self.profile["id"])[0]
        self.assertEqual(ev["status"], "skipped")

    def test_leader_sell_mirrors(self):
        ct = self.make()
        self.trades = [self.trade("BUY", 0.4, 1000, 9_999_999_990)]
        self.poll(ct["id"])
        self.trades = [self.trade("SELL", 0.38, 1000, 9_999_999_995)]
        self.poll(ct["id"])
        self.assertAlmostEqual(trading.available_shares(self.db, self.profile["id"], YES), 0, places=4)

    def test_paused_profile_does_nothing(self):
        ct = self.make()
        copytrade.pause_profile(self.db, self.uid, self.profile["id"], True)
        self.trades = [self.trade("BUY", 0.4, 1000, 9_999_999_999)]
        self.assertEqual(self.poll(ct["id"]), 0)

    def test_validation(self):
        with self.assertRaises(ValueError):
            self.make(amount=0.5)
        with self.assertRaises(ValueError):
            self.make(sizing="percent", percent=5000, amount=None)
        with self.assertRaises(ValueError):
            self.make(price_min=0.7, price_max=0.3)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        path = str(Path(self.dir.name) / "a.sqlite3")
        store.init(path)
        self.db = store.connect(path)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_setup_login_logout(self):
        token = auth.setup(self.db, {"username": "owner", "password": "password123"})
        self.assertEqual(auth.user_for(self.db, token)["username"], "owner")
        with self.assertRaises(PermissionError):
            auth.setup(self.db, {"username": "other", "password": "password123"})
        with self.assertRaises(PermissionError):
            auth.login(self.db, {"username": "owner", "password": "wrong-pass"}, "1.1.1.1")
        token2 = auth.login(self.db, {"username": "owner", "password": "password123"}, "1.1.1.1")
        auth.logout(self.db, token2)
        self.assertIsNone(auth.user_for(self.db, token2))

    def test_password_rules_and_change(self):
        with self.assertRaises(ValueError):
            auth.setup(self.db, {"username": "ow", "password": "password123"})
        with self.assertRaises(ValueError):
            auth.setup(self.db, {"username": "owner", "password": "short"})
        token = auth.setup(self.db, {"username": "owner", "password": "password123"})
        uid = auth.user_for(self.db, token)["id"]
        new = auth.change_password(self.db, uid, {"current": "password123", "password": "another-pass"})
        self.assertIsNone(auth.user_for(self.db, token))
        self.assertIsNotNone(auth.user_for(self.db, new))

    def test_login_throttle(self):
        auth.setup(self.db, {"username": "owner", "password": "password123"})
        for _ in range(10):
            with self.assertRaises(PermissionError):
                auth.login(self.db, {"username": "owner", "password": "bad-password"}, "9.9.9.9")
        with self.assertRaises(PermissionError) as ctx:
            auth.login(self.db, {"username": "owner", "password": "password123"}, "9.9.9.9")
        self.assertIn("Слишком много", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
