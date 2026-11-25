"""Copying by category (1.16.0): the category of an event, a player's skill by category, and what is copied."""
import json
import os
import time
import unittest

from kontur import categories, scout
from kontur import db as store
from test_autopilot import NOW, bets_events

DAY = 86400


class CategoryOfEvent(unittest.TestCase):
    def test_tags_decide_in_order(self):
        self.assertEqual(categories.of_tags(["sports", "games", "esports", "counter-strike-2"]), "esports")
        self.assertEqual(categories.of_tags(["politics", "trump", "tweets-markets"]), "culture")
        self.assertEqual(categories.of_tags(["weather", "highest-temperature", "tokyo"]), "weather")
        self.assertEqual(categories.of_tags(["finance", "equities"]), "finance")
        self.assertIsNone(categories.of_tags(["hide-from-new", "recurring"]))

    def test_slug_and_title(self):
        cases = {
            ("highest-temperature-in-tokyo-on-november-25-2026", "Will the highest temperature in Tokyo be 14°C?"): "weather",
            ("cs2-imp11-fly-2026-11-25", "Counter-Strike: Imperial vs FlyQuest (BO3)"): "esports",
            ("lal-bar-rma-2026-11-29", "Barcelona vs. Real Madrid"): "sports",
            ("what-will-trump-say-this-week", 'Will Trump say "tariff" this week?'): "culture",
            ("fed-decision-in-december", "Fed decision in December?"): "finance",
            ("presidential-election-winner-2028", "Presidential Election Winner 2028"): "politics",
            ("israel-strikes-iran-by", "Israel strikes Iran by December 31?"): "geopolitics",
            ("bitcoin-above-on-november-25", "Bitcoin above ___ on November 25?"): "crypto",
        }
        for (slug, title), cat in cases.items():
            self.assertEqual(categories.of_text(slug, title), cat, slug)

    def test_rules_agree_with_tags_on_real_events(self):
        # 102 events of the research base (24.11.2026), up to 12 of every category, with the category of their tags
        path = os.path.join(os.path.dirname(__file__), "data", "categories_sample.json")
        sample = json.load(open(path))
        agree = sum(categories.of_text(slug, title) == cat for slug, title, cat in sample)
        self.assertGreaterEqual(agree / len(sample), 0.8)

    def test_cached_tags_come_first(self):
        db = store.connect(":memory:")
        db.execute("CREATE TABLE event_tags(event_slug TEXT PRIMARY KEY, tags TEXT, at REAL)")
        db.execute("INSERT INTO event_tags VALUES('odd-one', ?, ?)", (json.dumps(["esports"]), time.time()))
        got = categories.of_events(db, {"odd-one": "Will the highest temperature be 14°C?", "fed-decision": "Fed decision?"})
        self.assertEqual(got, {"odd-one": "esports", "fed-decision": "finance"})


def player(groups, now=NOW):
    """Bets of a player: [(category slug word, n events, entry, wins)]."""
    bets = []
    for word, n, entry, wins in groups:
        part = bets_events(n, entry, wins, prefix=word, start=now - 170 * DAY, step=150 * DAY / n)
        for b in part:
            b["event_slug"] = {"pol": "presidential-election-winner-", "wx": "highest-temperature-in-paris-",
                               "spo": "nba-lal-bos-2026-11-0"}[word] + b["event_slug"]
            b["title"] = {"pol": "Who will win the election?", "wx": "Highest temperature in Paris?", "spo": "Lakers vs. Celtics"}[word]
        bets += part
    return bets


class SkillByCategory(unittest.TestCase):
    def setUp(self):
        # strong in politics (buys at 40¢, wins 75%), loses in weather (buys at 60¢, wins 35%), little sports
        self.m = scout.metrics(player([("pol", 40, 0.40, 30), ("wx", 30, 0.60, 10), ("spo", 4, 0.5, 2)]), slip=0.005, now=NOW)
        self.s = {**scout.DEFAULTS}

    def test_stats_by_category(self):
        cats = self.m["cats"]
        self.assertEqual(set(cats), {"politics", "weather", "sports"})
        self.assertEqual(cats["politics"]["n_ev"], 40)
        self.assertGreater(cats["politics"]["edge"], 0.1)
        self.assertLess(cats["weather"]["edge"], 0)
        # few events: drawn towards the player's overall edge
        self.assertLess(abs(cats["sports"]["edge"] - self.m["skill"]["edge"]), abs(cats["sports"]["mean"] - self.m["skill"]["edge"]))

    def test_verdicts(self):
        v = {c: scout.cat_verdict(cs, self.s, 0.01) for c, cs in self.m["cats"].items()}
        self.assertEqual(v, {"politics": "strong", "weather": "weak", "sports": "unknown"})

    def test_soft_and_strict(self):
        cut, ccut = 0.01, 0.01
        soft, strict = {**self.s, "cat_mode": "soft"}, {**self.s, "cat_mode": "strict"}
        whole = scout.overall_ok(self.m, self.s, cut)
        self.assertEqual(scout.cat_decision(self.m, "politics", soft, cut, ccut), (True, "strong"))
        self.assertEqual(scout.cat_decision(self.m, "weather", soft, cut, ccut), (False, "weak"))
        self.assertEqual(scout.cat_decision(self.m, "sports", soft, cut, ccut), (whole, "unknown"))
        self.assertEqual(scout.cat_decision(self.m, "sports", strict, cut, ccut), (False, "unknown"))
        self.assertEqual(scout.cat_decision(self.m, "weather", {**self.s, "cat_mode": "off"}, cut, ccut), (True, None))

    def test_specialist_passes_selection_by_category(self):
        spec = scout.metrics(player([("pol", 30, 0.40, 24), ("wx", 40, 0.50, 17)]), slip=0.005, now=NOW)
        spec.update(slip_measured=0.005, top_share=0.1, new_14d=5)
        cut, ccut = 0.0001, 0.01           # nothing proven as a whole across all players, politics proven
        off = scout.judge(spec, "directional", {"cat_mode": "off"}, cut, ccut)
        strict = scout.judge(spec, "directional", {"cat_mode": "strict"}, cut, ccut)
        soft = scout.judge(spec, "directional", {"cat_mode": "soft"}, cut, ccut)
        self.assertFalse(off["passed"])
        self.assertTrue(strict["passed"], strict["failed"])
        self.assertTrue(soft["passed"], soft["failed"])
        self.assertIn("Политика", next(c for c in strict["checks"] if c["key"] == "cats")["value"])


if __name__ == "__main__":
    unittest.main()
