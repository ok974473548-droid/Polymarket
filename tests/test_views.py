"""Which chart an event page shows (kontur/views.py), the history behind it and the crypto round's data.

The events are real Gamma events of the 24.11.2026 research of polymarket.com (trimmed to 6 markets each), with the
chart polymarket.com drew for each of them that day."""

import gzip
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from kontur import bus, crypto, views
from kontur import db as store
from kontur import polymarket as pm

DATA = Path(__file__).parent / "data" / "events_24nov2026.json.gz"
EVENTS = json.loads(gzip.decompress(DATA.read_bytes()))
BY_SLUG = {e["slug"]: e for e in EVENTS}

# Where we knowingly differ from polymarket.com: no live price of gold and of stocks reaches us (Polymarket draws a
# round there, we draw the chance of "Up"), and B200 thresholds carry an internal flag Gamma does not show.
KNOWN = {"xau-updown-15m-1795522500": "single", "nflx-up-or-down-on-november-24-2026": "single",
         "b200-average-rental-price-in-december-2026": "none"}


def view(slug):
    return pm.event(BY_SLUG[slug])["view"]


class ChartChoice(unittest.TestCase):
    def test_every_event_gets_polymarkets_chart(self):
        for raw in EVENTS:
            with self.subTest(slug=raw["slug"]):
                self.assertEqual(pm.event(raw)["view"]["kind"], KNOWN.get(raw["slug"], raw["_polymarket"]))

    def test_independent_questions_have_no_chart(self):
        for slug in ("what-will-trump-say-in-november-2026", "israel-x-iran-ceasefire-continues-throughptptpt-20260716224448963",
                     "time-person-of-the-year-2026"):
            self.assertEqual(view(slug), {"kind": "none", "lines": [], "main": []})

    def test_one_question_is_one_line(self):
        v = view("trump-out-as-president-before-2027")
        self.assertEqual(v["kind"], "single")
        self.assertEqual(len(v["lines"]), 1)
        line = v["lines"][0]
        self.assertEqual((line["name"], line["index"], line["color"]), ("Yes", 0, views.SINGLE))
        raw = BY_SLUG["trump-out-as-president-before-2027"]["markets"][0]
        self.assertEqual(line["token"], json.loads(raw["clobTokenIds"])[0])

    def test_coin_price_events_and_rounds(self):
        self.assertEqual(view("btc-updown-5m-1795523100")["kind"], "round")
        self.assertEqual(view("btc-updown-5m-1795523100")["symbol"], "btc")
        hit = view("what-price-will-bitcoin-hit-in-november-2026")
        self.assertEqual((hit["kind"], hit["symbol"], hit["lines"]), ("asset", "btc", []))
        # ranges of the price on a day are mutually exclusive outcomes: lines, not the coin price
        self.assertEqual(view("bitcoin-price-on-november-24-2026")["kind"], "multi")

    def test_match_in_team_colours_main_market_first(self):
        v = view("nba-det-orl-2026-11-24")
        self.assertEqual([(l["name"], l["color"]) for l in v["lines"]], [("Pistons", "#C02040"), ("Magic", "#2080C0")])
        main = next(m for m in BY_SLUG["nba-det-orl-2026-11-24"]["markets"] if m.get("sportsMarketType") == "moneyline")
        self.assertEqual(v["main"], [main["conditionId"].lower()])
        self.assertEqual([l["index"] for l in v["lines"]], [0, 1])

    def test_football_draw_in_grey_between_the_teams(self):
        v = view("acle-daj-bjg-2026-11-24")
        self.assertEqual([l["name"] for l in v["lines"]], ["Daejeon Citizen FC", "Ничья", "Beijing Guoan"])
        self.assertEqual(v["lines"][1]["color"], views.DRAW)
        self.assertEqual(len(v["main"]), 3)

    def test_sides_of_one_colour_told_apart(self):
        a, b = view("atp-cui-hassan-2026-11-22")["lines"]            # both flags are red
        self.assertEqual(a["color"], "#C02121")
        self.assertNotEqual(a["color"], b["color"])

    def test_several_outcomes_own_colours_most_likely_first(self):
        v = view("presidential-election-winner-2028")
        prices = [l["price"] for l in v["lines"]]
        self.assertEqual(prices, sorted(prices, reverse=True))
        colors = {l["name"]: l["color"] for l in v["lines"]}
        self.assertEqual(colors["Alexandria Ocasio-Cortez"], "#2970FF")   # seriesColor "#456BD5,#2970FF": the dark-theme one
        self.assertEqual(colors["JD Vance"], "#FA534D")
        self.assertEqual(len(set(colors.values())), len(colors))
        weather = view("highest-temperature-in-london-on-november-25-2026")   # no colours of their own: the palette
        self.assertEqual([l["color"] for l in weather["lines"][:4]], views.PALETTE[:4])

    def test_dark_colours_made_readable(self):
        self.assertEqual(views.readable("#2797FF"), "#2797FF")
        light = views.readable("#002F6C")                              # navy on a near-black page
        bg = views._luminance(views.BACKGROUND)
        self.assertGreaterEqual((views._luminance(views._rgb(light)) + 0.05) / (bg + 0.05), 3)

    def test_series_colour(self):
        self.assertEqual(pm.series_color("#1E3A8A,#5B8DEF"), "#5B8DEF")
        self.assertEqual(pm.series_color("#FA534D"), "#FA534D")
        self.assertEqual(pm.series_color(""), "")
        self.assertEqual(pm.series_color("red; background:url(x)"), "")

    def test_event_fields_of_a_match(self):
        e = pm.event(BY_SLUG["atp-cui-hassan-2026-11-22"])
        self.assertEqual([t["name"] for t in e["teams"]], ["Jie Cui", "Benjamin Hassan"])
        self.assertEqual(e["teams"][0]["color"], "#C02121")
        self.assertEqual(e["series"], {"slug": "atp", "recurrence": "daily"})
        self.assertTrue(e["score"])
        raw = BY_SLUG["elon-musk-of-tweets-november-17-november-24-2026"]
        self.assertGreater(raw["tweetCount"], 0)
        self.assertEqual(pm.event(raw)["tweets"], raw["tweetCount"])
        listed = pm.event(BY_SLUG["nba-det-orl-2026-11-24"], full=False)     # catalog cards carry no chart choice
        self.assertNotIn("view", listed)


class History(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        path = str(Path(self.dir.name) / "t.sqlite3")
        store.init(path)
        self.db = store.connect(path)

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_periods_in_polymarkets_steps(self):
        steps = {w: pm.history_params("1", w) for w in ("1h", "6h", "1d", "1w", "1m", "max")}
        self.assertEqual({w: (p["interval"], p["fidelity"]) for w, p in steps.items()},
                         {"1h": ("1h", 1), "6h": ("6h", 1), "1d": ("1d", 1), "1w": ("1w", 5), "1m": ("1m", 30), "max": ("max", 720)})
        self.assertEqual(pm.history_params("1", "3m")["interval"], "max")      # a link from before 1.8.0

    def test_all_of_a_young_market_from_its_creation(self):
        now = 2_000_000_000
        day_old = pm.history_params("1", "max", since=now - 20 * 3600, now=now)
        self.assertEqual(day_old, {"market": "1", "startTs": now - 20 * 3600, "fidelity": 1})
        self.assertEqual(pm.history_params("1", "max", since=now - 3 * 86400, now=now)["fidelity"], 5)
        self.assertEqual(pm.history_params("1", "max", since=now - 20 * 86400, now=now)["fidelity"], 30)
        old = pm.history_params("1", "max", since=now - 90 * 86400, now=now)
        self.assertEqual((old["interval"], old["fidelity"]), ("max", 720))
        # a shorter period is not affected by the market's age
        self.assertEqual(pm.history_params("1", "1d", since=now - 3600, now=now)["interval"], "1d")

    def test_one_point_per_moment(self):
        raw = {"history": [{"t": 30, "p": 0.3}, {"t": 10, "p": 0.1}, {"t": 30, "p": 0.31}, {"t": 20, "p": "0.2"}, {"t": None, "p": 1}]}
        with patch.object(pm, "fetch", return_value=raw) as fetch:
            got = pm.prices(self.db, "123", "1w")["data"]
        self.assertEqual(got, [[10, 0.1], [20, 0.2], [30, 0.31]])
        q = parse_qs(urlparse(fetch.call_args[0][0]).query)
        self.assertEqual((q["interval"], q["fidelity"]), (["1w"], ["5"]))


class CryptoRound(unittest.TestCase):
    def fake(self, url):
        self.urls.append(url)
        if "/crypto/crypto-price" in url:
            return {"openPrice": 60000.5}
        if "/crypto/price-history" in url:
            return [{"timestamp": (self.start + i * 5) * 1000, "value": 60000 + i} for i in range(61)]
        if "/past-results" in url:
            return {"data": {"results": [{"startTime": "x", "outcome": "up", "openPrice": 1, "closePrice": 2}]}}
        raise AssertionError(url)

    def setUp(self):
        self.urls = []
        self.start = 1_795_523_100
        crypto._targets.clear()
        crypto._past.clear()

    def info(self, now):
        with patch.object(pm, "fetch", side_effect=lambda url, **kw: self.fake(url)), \
             patch.object(crypto, "want"), patch.object(crypto, "STREAM", None), patch.object(crypto.time, "time", return_value=now):
            return crypto.round_info(f"btc-updown-5m-{self.start}")

    def test_finished_round_is_drawn_whole_with_the_round_being_played(self):
        r = self.info(self.start + 300 * 3 + 40)                   # three rounds later
        self.assertTrue(r["finished"])
        self.assertEqual(len(r["points"]), 61)
        history = next(u for u in self.urls if "/crypto/price-history" in u)
        self.assertNotIn("historySeed", history)                   # the whole round, not the last minutes
        self.assertIn("twapEnabled=true", history)                 # the Chainlink price, not an exchange's
        self.assertEqual(r["live"], f"btc-updown-5m-{self.start + 900}")
        self.assertEqual(r["next"], f"btc-updown-5m-{self.start + 300}")

    def test_round_being_played_asks_the_last_minutes(self):
        r = self.info(self.start + 100)
        self.assertFalse(r["finished"])
        history = next(u for u in self.urls if "/crypto/price-history" in u)
        self.assertIn("historySeed=true", history)
        self.assertIn("twapEnabled=true", history)
        self.assertEqual(r["live"], f"btc-updown-5m-{self.start}")

    def test_variant_names_as_polymarket_sends_them(self):
        # polymarket.com 24.11.2026: other names get the open of another round (15 minutes: $59 417 instead of $59 999)
        names = {p: crypto.parse(f"btc-updown-{p}-{self.start}")["variant"] for p in ("5m", "15m", "1h", "4h", "1d")}
        self.assertEqual(names, {"5m": "fiveminute", "15m": "fifteen", "1h": "onehour", "4h": "fourhour", "1d": "oneday"})
        with patch.object(pm, "fetch", side_effect=lambda url, **kw: self.fake(url)), \
             patch.object(crypto, "want"), patch.object(crypto, "STREAM", None), patch.object(crypto.time, "time", return_value=self.start + 60):
            crypto.round_info(f"btc-updown-15m-{self.start}")
        self.assertTrue(all("variant=fifteen&" in u for u in self.urls))

    def test_round_trades_subscription_written_without_spaces(self):
        sub = crypto.trades_subscription("btc-updown-5m-1795523100")
        self.assertEqual(sub, {"topic": "activity", "type": "trades", "filters": '{"event_slug":"btc-updown-5m-1795523100"}'})

    def test_round_buys_become_floating_trades(self):
        stream = crypto.PriceStream()
        slug = f"btc-updown-5m-{self.start}"
        stream.rounds[slug] = time.monotonic() + 60
        q = bus.subscribe()
        try:
            base = {"eventSlug": slug, "timestamp": 1795540082}
            stream._trade({**base, "side": "BUY", "outcome": "Up", "size": 45, "price": 0.999})
            stream._trade({**base, "side": "BUY", "outcome": "Down", "size": 45, "price": 0.001})   # the other half: $0.05
            stream._trade({**base, "side": "SELL", "outcome": "Down", "size": 30, "price": 0.5})    # sales are not shown
            stream._trade({**base, "eventSlug": "eth-updown-5m-1", "side": "BUY", "outcome": "Up", "size": 9, "price": 0.5})
            got = []
            while not q.empty():
                kind, data, _ = q.get_nowait()
                if kind == "trade":
                    got.append(data)
        finally:
            bus.unsubscribe(q)
        self.assertEqual(got, [{"e": slug, "o": "up", "u": 44.95, "t": 1795540082}])

    def test_watched_rounds_subscribed_and_dropped(self):
        stream = crypto.PriceStream()
        sent = []
        ws = type("WS", (), {"send": lambda self, msg: sent.append(json.loads(msg))})()
        subscribed = set()
        stream.want("btc-updown-5m-1795523100")
        stream._follow_rounds(ws, subscribed)
        stream._follow_rounds(ws, subscribed)                  # nothing new: nothing sent again
        self.assertEqual([m["action"] for m in sent], ["subscribe"])
        self.assertEqual(subscribed, {"btc-updown-5m-1795523100"})
        stream.rounds["btc-updown-5m-1795523100"] = time.monotonic() - 1   # nobody looks any more
        stream._follow_rounds(ws, subscribed)
        self.assertEqual(sent[-1]["action"], "unsubscribe")
        self.assertEqual((subscribed, stream.rounds), (set(), {}))
        stream.want("not-a-round")                               # only round slugs are followed
        self.assertEqual(stream.rounds, {})

    def test_the_same_coins_as_the_chart_choice(self):
        self.assertIs(crypto.SYMBOLS, views.ROUND_SYMBOLS)
        self.assertIsNone(crypto.parse("xau-updown-15m-1795522500"))


if __name__ == "__main__":
    unittest.main()

@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RoundChartMath(unittest.TestCase):
    """The arithmetic of static/js/roundchart.js (the canvas itself is checked in a browser)."""

    def run_js(self, expr):
        path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "roundchart.js")
        code = f"import('{Path(path).resolve().as_uri()}').then((m) => console.log(JSON.stringify({expr})))"
        out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_axis_steps_are_round(self):
        got = self.run_js("[m.priceStep(23, 5), m.priceStep(0.0031, 5), m.priceStep(140, 4), m.stepDecimals(2.5), m.stepDecimals(0.05), m.stepDecimals(10)]")
        self.assertEqual(got, [5, 0.001, 50, 1, 2, 0])

    def test_prices_written_as_in_russian(self):
        got = self.run_js("[m.money(60185, 0), m.money(1684.26, 2), m.coinDecimals(60000), m.coinDecimals(2.34), m.coinDecimals(0.18)]")
        self.assertEqual(got, ["$60\u00a0185", "$1\u00a0684,26", 2, 4, 5])

    def test_live_line_steps_softly_between_prices(self):
        got = self.run_js("[m.valueAt([[0, 1], [2, 3]], 1), m.valueAt([[0, 1], [2, 3]], 0.5), m.valueAt([[0, 1], [2, 3]], 0.5, false), "
                          "m.valueAt([[0, 1], [2, 3]], 9), m.valueAt([[5, 1]], 4)]")
        self.assertEqual(got[0], 2)                  # half way: half the move
        self.assertLess(got[1], got[2])              # S-shaped: slow out of a price, faster in the middle
        self.assertEqual(got[3:], [3, None])         # after the newest price: flat; before the first: nothing

    def test_candles_open_where_the_last_closed(self):
        got = self.run_js("m.candles([[0, 1], [1, 3], [2, 2], [5, 4], [6, 1], [20, 9]], 0, 10, 5)")
        self.assertEqual(got, [{"t": 0, "o": 1, "h": 3, "l": 1, "c": 2}, {"t": 5, "o": 2, "h": 4, "l": 1, "c": 1}])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class StreamLinks(unittest.TestCase):
    """static/js/stream.js: the player of an event's broadcast (resolutionSource of esports matches, 25.11.2026)."""

    def test_players(self):
        path = Path(os.path.join(os.path.dirname(__file__), "..", "static", "js", "stream.js")).resolve().as_uri()
        links = ["https://www.twitch.tv/PGL", "https://kick.com/cct_cs2", "https://www.youtube.com/watch?v=87Q4eGjKbVc",
                 "https://youtu.be/87Q4eGjKbVc", "https://www.itftennis.com/en/tournament-calendar/", "https://www.twitch.tv/directory", ""]
        stub = ("globalThis.window = globalThis; globalThis.localStorage = null; globalThis.location = {hostname: 'x', pathname: '/', search: ''}; "
                "globalThis.document = {addEventListener() {}, querySelector() { return null; }, querySelectorAll() { return []; }}; "
                "globalThis.addEventListener = () => {}; globalThis.matchMedia = () => ({matches: false, addEventListener() {}}); ")
        code = (stub + f"import('{path}').then((m) => console.log(JSON.stringify("
                f"{json.dumps(links)}.map((l) => m.streamOf(l, 'polycontur.up.railway.app')))))")
        out = subprocess.run(["node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout)
        self.assertEqual(got[0]["site"], "Twitch")            # Twitch: link only (its embed starts with an ad)
        self.assertFalse(got[0]["embed"])
        self.assertEqual(got[1]["src"], "https://player.kick.com/cct_cs2?muted=true&autoplay=true")
        self.assertTrue(got[1]["embed"])
        self.assertEqual(got[2]["src"], "https://www.youtube-nocookie.com/embed/87Q4eGjKbVc?autoplay=1&mute=1&playsinline=1")
        self.assertEqual(got[3]["channel"], "87Q4eGjKbVc")
        self.assertEqual(got[4:], [None, None, None])         # a league's site, a Twitch section, nothing

