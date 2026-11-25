"""Smart search: phrase parsing, the local index, ranking, filters, personal data and saved-search alerts."""
import json
import time
import unittest
from unittest.mock import patch

from kontur import notify, query, search
from kontur import db as store
from kontur import polymarket as pm
from test_trading import Base

DAY = 86400


def ev(slug, title, tags=(), vol24=0.0, vol=0.0, price=0.5, end_days=30, created_days=100, labels=(), eid=None):
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    contracts = [{"label": lab, "question": f"{title}: {lab}", "closed": False, "change24h": 0.01,
                  "outcomes": [{"name": "Yes", "price": price}, {"name": "No", "price": 1 - price}]} for lab in labels] or \
                [{"label": "", "question": title, "closed": False, "change24h": 0.02,
                  "outcomes": [{"name": "Yes", "price": price}, {"name": "No", "price": 1 - price}]}]
    return {"slug": slug, "id": eid or str(abs(hash(slug)) % 10**6), "title": title, "image": "",
            "tags": [{"slug": t, "label": t.title()} for t in tags], "end": iso(now + end_days * DAY),
            "start": iso(now - created_days * DAY), "volume": vol, "volume24h": vol24, "liquidity": 1000, "oi": 0,
            "count": len(contracts), "contracts": contracts}


class ParseTests(unittest.TestCase):
    def test_traders_filters(self):
        q = query.parse("трейдеры политика винрейт > 70% активные")
        self.assertEqual(q["kind"], "traders")
        self.assertEqual(q["filters"]["category"], "politics")
        self.assertEqual(q["filters"]["wr_min"], 70)
        self.assertTrue(q["filters"]["active"])

    def test_whale_trades(self):
        q = query.parse("киты купили да франция за сутки")
        self.assertEqual(q["kind"], "trades")
        self.assertEqual(q["filters"]["side"], "BUY")
        self.assertEqual(q["filters"]["period_h"], 24)
        self.assertEqual([w["en"] for w in q["words"]], ["france"])

    def test_layout_and_translit(self):
        self.assertIn(("layout", "miami"), query.parse("ьшфьш")["words"][0]["alts"])
        self.assertEqual(query.parse("трамп")["words"][0]["en"], "trump")

    def test_address_and_link(self):
        w = "0x" + "a" * 40
        self.assertEqual(query.parse(w)["go"], f"/traders/{w}")
        self.assertEqual(query.parse("https://polymarket.com/event/fed-decision")["go"], "/event/fed-decision")


class IndexTests(Base):
    def setUp(self):
        super().setUp()
        # no network: Polymarket's own search (the fallback for fewer than 3 markets in the index) answers nothing
        self.no_gamma = patch.object(pm, "search", return_value={"data": {"events": [], "profiles": [], "tags": []}})
        self.no_gamma.start()
        self.addCleanup(self.no_gamma.stop)
        events = [ev("trump-2028", "Will Trump run in 2028?", ["politics"], vol24=50_000, vol=1e6, price=0.3),
                  ev("fed-december", "Fed decision in December", ["economy"], vol24=200_000, vol=5e6, price=0.8, end_days=10),
                  ev("btc-150k", "Bitcoin above $150k by June?", ["crypto"], vol24=90_000, price=0.12, created_days=2),
                  ev("france-pm", "Next French prime minister", ["politics", "france"], vol24=10_000, labels=["Bardella", "Attal"])]
        for e in events:
            search.index_event(self.db, e)
        now = time.time()
        self.whale = "0x" + "1" * 40
        self.db.execute("INSERT INTO board VALUES('overall:all', 1, ?, 'BigFish', '', 2e6, 9e6, ?)", (self.whale, now))
        self.db.execute("INSERT INTO board VALUES('politics:month', 3, ?, 'BigFish', '', 5e5, 1e6, ?)", (self.whale, now))
        self.db.execute("""INSERT INTO wallets(wallet, name, win_rate, networth, active, positions, pnl)
                           VALUES(?, 'BigFish', 74, 3e6, 12, 300, 2e6)""", (self.whale,))
        self.small = "0x" + "2" * 40
        self.db.execute("INSERT INTO board VALUES('overall:all', 40, ?, 'trumpfan', '', 1e4, 5e4, ?)", (self.small, now))
        self.db.execute("INSERT INTO wallets(wallet, name, win_rate, networth, active, positions, pnl) VALUES(?, 'trumpfan', 52, 2e4, 0, 80, 1e4)",
                        (self.small,))
        store.kv_set(self.db, "tags", json.dumps({"rows": [{"slug": "politics", "label": "Politics", "count": 900}]}))
        search.index_people(self.db)

    def titles(self, res):
        return [r["title"] for r in res["rows"]]

    def test_russian_finds_english(self):
        res = search.full(self.db, self.uid, "трамп", "events")
        self.assertEqual(self.titles(res)[0], "Will Trump run in 2028?")

    def test_typo_and_layout(self):
        self.assertIn("Bitcoin above $150k by June?", self.titles(search.full(self.db, self.uid, "bitcon", "events")))
        self.assertIn("Fed decision in December", self.titles(search.full(self.db, self.uid, "аув", "events")))  # "fed", wrong layout

    def test_contract_labels_are_searchable(self):
        self.assertEqual(self.titles(search.full(self.db, self.uid, "bardella", "events")), ["Next French prime minister"])

    def test_event_filters(self):
        res = search.full(self.db, self.uid, "рынки дешевле 20¢", None)
        self.assertEqual(res["kind"], "events")
        self.assertEqual(self.titles(res), ["Bitcoin above $150k by June?"])
        res = search.full(self.db, self.uid, "политика", "events")
        self.assertEqual(set(self.titles(res)), {"Will Trump run in 2028?", "Next French prime minister"})

    def test_facet_override_and_clear(self):
        res = search.full(self.db, self.uid, "политика", "events", {"category": "none"})
        self.assertEqual(res["total"], 4)
        res = search.full(self.db, self.uid, "", "events", {"sort": "hot"})
        self.assertEqual(self.titles(res)[0], "Fed decision in December")

    def test_traders(self):
        res = search.full(self.db, self.uid, "трейдеры винрейт > 70%", None)
        self.assertEqual(self.titles(res), ["BigFish"])
        res = search.full(self.db, self.uid, "киты", None)
        self.assertEqual(self.titles(res), ["BigFish"])
        self.assertEqual(self.titles(search.full(self.db, self.uid, "@trumpfan", None)), ["trumpfan"])

    def test_quick_groups_and_watch_boost(self):
        res = search.quick(self.db, self.uid, "trump")
        self.assertIn("events", res["groups"])
        self.assertEqual(res["groups"]["traders"]["rows"][0]["title"], "trumpfan")
        self.db.execute("INSERT INTO watchlist VALUES(?, 'event', 'france-pm', '', '', ?)", (self.uid, time.time()))
        res = search.full(self.db, self.uid, "", "events", {"category": "politics"})
        self.assertTrue(res["rows"][0]["watched"])

    def test_category_relaxed_when_empty(self):
        res = search.full(self.db, self.uid, "bitcoin политика", "events")
        self.assertEqual(res["relaxed"], "category")
        self.assertEqual(self.titles(res), ["Bitcoin above $150k by June?"])

    def test_category_word_filters_tags_to_that_category(self):
        # a category word ("политика") returned every tag by popularity before 1.17.2; now only politics-family tags
        from kontur import categories
        tag = lambda slug: search._item({"kind": "tag", "key": slug, "title": slug, "image": "",
                                         "data": json.dumps({"slug": slug, "count": 5}), "pop": 5}, search._personal(self.db, None))
        now = time.time()
        self.assertTrue(categories.TAG_INDEX.get("elections") == "politics")
        self.assertTrue(search._passes(tag("elections"), {"category": "politics"}, now))
        self.assertTrue(search._passes(tag("trump"), {"category": "politics"}, now))
        self.assertFalse(search._passes(tag("sports"), {"category": "politics"}, now))
        self.assertFalse(search._passes(tag("recurring"), {"category": "politics"}, now))
        self.assertTrue(search._passes(tag("sports"), {}, now))        # no category: every tag passes

    def test_history_and_saved_alerts(self):
        search.full(self.db, self.uid, "трамп", None, record=True)
        self.assertEqual(search.home(self.db, self.uid)["recent"], ["трамп"])
        s = search.save(self.db, self.uid, {"name": "Крипта", "query": "bitcoin", "kind": "events", "notify": True})
        self.assertEqual(search.check_alerts(self.db), 0)  # first run only remembers what is there
        search.index_event(self.db, ev("btc-200k", "Bitcoin above $200k?", ["crypto"], vol24=5))
        self.db.execute("UPDATE saved_searches SET checked=0 WHERE id=?", (s["id"],))
        self.assertEqual(search.check_alerts(self.db), 1)
        note = store.one(self.db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC", (self.uid,)))
        self.assertIn("Bitcoin above $200k?", note["body"])
        self.assertEqual(note["link"], f"/search?saved={s['id']}")

    def test_mine(self):
        self.db.execute("""INSERT INTO positions(account_id, token_id, condition_id, event_slug, title, outcome, shares, cost, updated)
                           VALUES(?, 't1', 'c1', 'trump-2028', 'Will Trump run in 2028?', 'Yes', 10, 3, ?)""", (self.acc["id"], time.time()))
        rows = search.full(self.db, self.uid, "мои позиции трамп", None)["rows"]
        self.assertEqual([r["kind"] for r in rows], ["position"])

    def test_trades_filtered(self):
        big = {"proxy_wallet": self.whale, "side": "BUY", "size": 40000, "price": 0.5, "outcome": "Yes", "timestamp": time.time() - 600,
               "transaction_hash": "0xa"}
        small = {**big, "size": 10, "transaction_hash": "0xb"}
        with patch.object(pm, "trades", return_value={"data": {"rows": [big, small]}}):
            res = search.full(self.db, self.uid, "киты купили трамп за сутки", None)
        self.assertEqual(res["kind"], "trades")
        self.assertEqual([t["transaction_hash"] for t in res["rows"]], ["0xa"])

    def test_note_events_keeps_full_body(self):
        search.note_events(self.db, [{"slug": "france-pm", "title": "Next French prime minister", "contracts": []}])
        self.assertEqual(self.titles(search.full(self.db, self.uid, "attal", "events")), ["Next French prime minister"])


if __name__ == "__main__":
    unittest.main()
