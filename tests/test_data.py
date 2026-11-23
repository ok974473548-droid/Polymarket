"""News parsing, trader statistics, rating listing and HTTP routes (Polymarket calls are mocked)."""
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from kontur import db as store
from kontur import news, traders
from kontur import polymarket as pm

GOOGLE = b"""<?xml version="1.0"?><rss><channel>
<item><title>Fed holds rates steady - Reuters</title><link>https://news.google.com/a</link><pubDate>Mon, 23 Nov 2026 10:00:00 GMT</pubDate><source url="https://reuters.com">Reuters</source></item>
<item><title>No link item</title><link>javascript:alert(1)</link></item>
</channel></rss>"""
BING = b"""<?xml version="1.0"?><rss xmlns:News="https://www.bing.com/news/search?q=x&amp;format=rss"><channel>
<item><title>Bitcoin tops $100k</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;url=https%3a%2f%2fexample.com%2fbtc</link><pubDate>Mon, 23 Nov 2026 09:00:00 GMT</pubDate><News:Source>CoinDesk</News:Source></item>
</channel></rss>"""


def wrap(data):
    return {"data": data, "fetched": time.time(), "stale": False, "error": None}


class NewsTest(unittest.TestCase):
    def test_google_title_source_split(self):
        items = news._parse(GOOGLE, "Google News")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Fed holds rates steady")
        self.assertEqual(items[0]["source"], "Reuters")
        self.assertIsNotNone(items[0]["at"])

    def test_bing_link_unwrapped(self):
        items = news._parse(BING, "Bing News")
        self.assertEqual(items[0]["link"], "https://example.com/btc")
        self.assertEqual(items[0]["source"], "CoinDesk")

    def test_bad_xml(self):
        self.assertEqual(news._parse(b"<html>", "Google News"), [])

    def test_query_strips_noise(self):
        self.assertTrue(news.query_for("Fed Decision in December?"))


class SeriesTest(unittest.TestCase):
    def test_streak_and_changes(self):
        day = 86400
        pts = [{"t": i * day, "pnl": 100.0 * i, "volume": 1000.0 * i} for i in range(40)]
        pts[-1]["pnl"] = pts[-2]["pnl"] - 50  # last day is a loss
        pnl15, eff, streak = traders._series_stats(pts)
        self.assertAlmostEqual(pnl15, pts[-1]["pnl"] - pts[-16]["pnl"])
        self.assertEqual(streak, -1)
        self.assertAlmostEqual(eff, (pts[-1]["pnl"] - pts[-31]["pnl"]) / (pts[-1]["volume"] - pts[-31]["volume"]) * 100)

    def test_rising_streak(self):
        pts = [{"t": i * 86400, "pnl": float(i)} for i in range(8)]
        self.assertEqual(traders._series_stats(pts)[2], 7)

    def test_too_short(self):
        self.assertEqual(traders._series_stats([{"t": 0, "pnl": 1}]), (None, None, None))

    def test_tags(self):
        tags = traders.tags_for({"networth": 2e6, "win_rate": 75, "positions": 80, "active": 120, "streak": 6}, rank=3)
        self.assertEqual(set(tags), {"whale", "sniper", "active", "hot", "top10"})


BOARD = [{"rank": i + 1, "wallet": "0x" + f"{i:040x}", "name": f"trader{i}", "image": "", "pnl": 1000.0 - i * 10, "volume": 5000.0}
         for i in range(30)]


class ListingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        path = str(Path(self.dir.name) / "t.sqlite3")
        store.init(path)
        self.db = store.connect(path)
        self.p = patch.object(pm, "leaderboard", side_effect=lambda db, **kw: wrap({"rows": BOARD, "next": None}))
        self.p.start()
        traders.sync_board(self.db, "overall")
        for i, r in enumerate(BOARD[:10]):
            self.db.execute("UPDATE wallets SET stats_at=?, networth=?, win_rate=?, positions=?, active=? WHERE wallet=?",
                            (time.time(), 100.0 * i, 40.0 + i, 10 + i, i % 3, r["wallet"]))

    def tearDown(self):
        self.p.stop()
        self.db.close()
        self.dir.cleanup()

    def test_default_sort_and_paging(self):
        res = traders.listing(self.db, {"size": "10", "page": "2"})
        self.assertEqual(res["total"], 30)
        self.assertEqual([r["rank"] for r in res["rows"]], list(range(11, 21)))

    def test_sort_asc_nulls_last(self):
        res = traders.listing(self.db, {"sort": "networth", "dir": "asc", "size": "10"})
        self.assertEqual(res["rows"][0]["networth"], 0.0)
        self.assertEqual(res["rows"][9]["networth"], 900.0)

    def test_filters(self):
        res = traders.listing(self.db, {"win_min": "45", "active": "1", "search": "trader"})
        self.assertTrue(all(r["win_rate"] >= 45 for r in res["rows"]))
        self.assertTrue(all(r["active"] > 0 for r in res["rows"]))

    def test_top10_tag(self):
        res = traders.listing(self.db, {"tag": "top10"})
        self.assertEqual(res["total"], 10)


class RoutesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        path = str(Path(cls.dir.name) / "t.sqlite3")
        store.init(path)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.server.db_path = path
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def call(self, path, body=None, cookie=""):
        headers = {"Origin": self.base, "Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"null"), r.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"null"), ""

    def test_flow(self):
        status, data, _ = self.call("/api/health")
        self.assertEqual((status, data["version"]), (200, app.VERSION))
        self.assertTrue(self.call("/api/session")[1]["setup"])
        self.assertEqual(self.call("/api/account")[0], 401)
        status, _, cookie = self.call("/api/auth/setup", {"username": "owner", "password": "password123"})
        self.assertEqual(status, 200)
        cookie = cookie.split(";")[0]
        status, acc, _ = self.call("/api/account", cookie=cookie)
        self.assertEqual((status, acc["cash"]), (200, 1000))
        self.assertEqual(self.call("/api/account/deposit", {"amount": 250}, cookie)[0], 200)
        self.assertEqual(self.call("/api/account", cookie=cookie)[1]["cash"], 1250)
        self.assertEqual(self.call("/api/account/deposit", {"amount": -5}, cookie)[0], 400)
        status, _, _ = self.call("/api/watchlist", {"kind": "event", "key": "rain", "label": "Rain", "on": True}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(self.call("/api/watchlist", cookie=cookie)[1]), 1)

    def test_post_needs_same_origin(self):
        req = urllib.request.Request(self.base + "/api/auth/login", data=b"{}", headers={"Origin": "https://evil.example", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 403)

    def test_upstream_error_maps_to_502(self):
        with patch.object(pm, "event_by_slug", side_effect=pm.UpstreamError("down")):
            self.assertEqual(self.call("/api/event?slug=x")[0], 502)
        with patch.object(pm, "event_by_slug", side_effect=LookupError("no")):
            self.assertEqual(self.call("/api/event?slug=x")[0], 404)

    def test_spa_and_image_guard(self):
        with urllib.request.urlopen(self.base + "/event/some-slug") as r:
            html = r.read().decode()
        self.assertIn(app.VERSION, html)
        self.assertNotIn("{{VERSION}}", html)
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(self.base + "/img?u=https%3A%2F%2Fevil.example%2Fa.png")
        self.assertEqual(e.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
