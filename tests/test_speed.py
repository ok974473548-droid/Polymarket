"""1.2.0 speed work: WebSocket frames, live books, live-stream copies without duplicates, keep-alive server,
gzip, 304, server-sent events. No network: Polymarket calls are mocked."""
import gzip
import http.client
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import app
from kontur import books, bus, copytrade, net, notify, stream, trading, ws
from kontur import db as store
from kontur import polymarket as pm
from test_trading import COND, YES, Base, book

LEADER = "0x" + "ab" * 20


# ---------------------------------------------------------------- WebSocket frames
class FakeSock:
    """Feeds bytes in the chunks given; raises TimeoutError between them, like a socket with a timeout."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = b""

    def recv(self, n):
        if not self.chunks:
            return b""
        c = self.chunks.pop(0)
        if c is None:
            raise TimeoutError
        return c

    def sendall(self, b):
        self.sent += b

    def close(self):
        pass


def frame(opcode, payload, fin=True):
    n = len(payload)
    head = bytes([(0x80 if fin else 0) | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack("!H", n)
    else:
        head += bytes([127]) + struct.pack("!Q", n)
    return head + payload


def client(chunks):
    w = ws.WebSocket.__new__(ws.WebSocket)
    w._send_lock = threading.Lock()
    w._buf = bytearray()
    w._parts, w._kind = [], None
    w.sock = FakeSock(chunks)
    w.closed = False
    return w


class WebSocketFrames(unittest.TestCase):
    def test_timeout_mid_frame_keeps_data(self):
        data = frame(ws.OP_TEXT, "привет".encode())
        w = client([data[:3], None, data[3:]])
        with self.assertRaises(TimeoutError):
            w.recv()
        self.assertEqual(w.recv(), "привет")

    def test_fragments_ping_and_long_payload(self):
        long = b"x" * 70000
        w = client([frame(ws.OP_TEXT, b"ab", fin=False) + frame(ws.OP_PING, b"p") + frame(ws.OP_CONT, b"cd"),
                    frame(ws.OP_TEXT, long)])
        self.assertEqual(w.recv(), "abcd")
        self.assertEqual(w.recv(), long.decode())
        sent = w.sock.sent  # the ping was answered with a masked pong
        self.assertEqual(sent[0], 0x80 | ws.OP_PONG)
        self.assertEqual(ws._mask(ws._mask(sent[6:], sent[2:6]), sent[2:6]), sent[6:])
        self.assertEqual(ws._mask(sent[6:], sent[2:6]), b"p")

    def test_close_raises(self):
        w = client([frame(ws.OP_CLOSE, b"")])
        with self.assertRaises(ws.WsError):
            w.recv()


# ---------------------------------------------------------------- live books
class LiveBooks(unittest.TestCase):
    def test_snapshot_and_changes(self):
        s = books.BookStream()
        s.watch(["t1"])
        s.connected, s.last_rx = True, time.monotonic()
        s._apply({"event_type": "book", "asset_id": "t1", "timestamp": "1800000000000",
                  "bids": [{"price": "0.40", "size": "10"}, {"price": "0.41", "size": "5"}],
                  "asks": [{"price": "0.45", "size": "7"}, {"price": "0.44", "size": "3"}]})
        s._apply({"event_type": "price_change", "timestamp": "1800000001000", "price_changes": [
            {"asset_id": "t1", "price": "0.41", "size": "0", "side": "BUY"},
            {"asset_id": "t1", "price": "0.43", "size": "2", "side": "SELL"},
            {"asset_id": "other", "price": "0.5", "size": "9", "side": "SELL"}]})
        bk = s.fresh("t1")
        self.assertEqual(bk["bids"], [[0.40, 10.0]])
        self.assertEqual(bk["asks"], [[0.43, 2.0], [0.44, 3.0], [0.45, 7.0]])
        self.assertEqual(bk["at"], 1800000001.0)
        self.assertIsNone(s.fresh("other"))

    def test_untrusted_when_disconnected_or_old_connection(self):
        s = books.BookStream()
        s.watch(["t1"])
        s.connected, s.last_rx = True, time.monotonic()
        s._apply({"event_type": "book", "asset_id": "t1", "bids": [], "asks": [{"price": "0.5", "size": "1"}]})
        self.assertIsNotNone(s.fresh("t1"))
        s.gen += 1  # reconnected: the snapshot belongs to the previous connection
        self.assertIsNone(s.fresh("t1"))
        s.gen -= 1
        s.last_rx = time.monotonic() - books.STALE_AFTER - 1
        self.assertIsNone(s.fresh("t1"))

    def test_get_falls_back_to_rest(self):
        with patch.object(pm, "book", return_value={"asks": [[0.5, 1]], "bids": []}) as rest:
            self.assertEqual(books.get("t9")["asks"], [[0.5, 1]])
        rest.assert_called_once()


# ---------------------------------------------------------------- one key for stream and Data API
class TradeKeys(unittest.TestCase):
    def test_stream_row_matches_data_api_row(self):
        payload = {"proxyWallet": LEADER.upper().replace("0X", "0x"), "asset": YES, "conditionId": COND.upper().replace("0X", "0x"),
                   "side": "BUY", "size": 15, "price": 0.3, "timestamp": 1800000000, "transactionHash": "0xABC",
                   "title": "Will it rain?", "outcome": "Yes", "eventSlug": "rain"}
        row = stream.to_row(payload)
        api_row = {"transaction_hash": "0xabc", "token_id": YES, "side": "BUY", "size": 15.0, "price": 0.3}
        self.assertEqual(copytrade.trade_key(row), copytrade.trade_key(api_row))
        self.assertEqual(row["proxy_wallet"], LEADER)
        self.assertEqual(row["condition_id"], COND)
        self.assertEqual(stream.to_row({**payload, "timestamp": 1800000000123})["timestamp"], 1800000000)


class StreamRowWithoutMarket(unittest.TestCase):
    def test_market_filled_in_from_token(self):
        raw = {"conditionId": COND.upper().replace("0X", "0x"), "question": "Will it rain?", "slug": "rain-q", "outcomes": '["Yes", "No"]',
               "clobTokenIds": f'["{YES}", "222"]', "events": [{"slug": "rain"}]}
        row = stream.to_row({"proxyWallet": LEADER, "asset": YES, "side": "BUY", "size": 5, "price": 0.3, "timestamp": 1,
                             "transactionHash": "0x1"})
        self.assertEqual(row["condition_id"], "")
        with patch.object(pm, "market_by_token", return_value=raw):
            full = stream.TradeStream.complete(row)
        self.assertEqual((full["condition_id"], full["title"], full["event_slug"], full["outcome"], full["outcome_index"]),
                         (COND, "Will it rain?", "rain", "Yes", 0))
        self.assertEqual(copytrade.trade_key(full), copytrade.trade_key(row))


# ---------------------------------------------------------------- copies: stream first, then the Data API
class StreamCopies(Base):
    def setUp(self):
        super().setUp()
        self.aid = copytrade.create_profile(self.db, self.uid, "Скорость")["id"]
        trading.transfer_from_main(self.db, self.uid, self.aid, 500)
        with patch.object(pm, "trades_page", return_value={"rows": [], "next": None}):
            self.ct = copytrade.create(self.db, self.uid, self.aid, {"leader": LEADER, "sizing": "fixed", "amount": 10,
                                                                      "slippage": "any", "notify": False})
        self.fill = {"proxy_wallet": LEADER, "side": "BUY", "token_id": YES, "condition_id": COND, "size": 100.0, "price": 0.3,
                     "timestamp": int(time.time()), "title": "Will it rain?", "outcome": "Yes", "event_slug": "rain",
                     "transaction_hash": "0xfeed"}

    def row(self):
        return store.one(self.db.execute("SELECT * FROM copy_trades WHERE id=?", (self.ct["id"],)))

    def test_stream_copy_then_poll_skips_it(self):
        s = stream.TradeStream(self.path)
        seen = time.time()
        s.copy(self.db, dict(self.fill), seen)
        ev = store.rows(self.db.execute("SELECT * FROM copy_events"))
        self.assertEqual([(e["status"], e["source"]) for e in ev], [("filled", "stream")])
        self.assertAlmostEqual(ev[0]["detected"], seen)
        self.assertEqual(ev[0]["leader_ts"], self.fill["timestamp"])
        # minutes later the Data API shows the same fill (float size, mixed-case hash): no second copy
        api_fill = {**self.fill, "size": 100.0, "transaction_hash": "0xFEED"}
        with patch.object(pm, "trades_page", return_value={"rows": [api_fill], "next": None}):
            self.assertEqual(copytrade.poll(self.db, self.row()), 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_events").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM orders WHERE source='copy'").fetchone()[0], 1)

    def test_parallel_stream_and_poll_copy_once(self):
        path = self.path
        results = []

        def go(src):
            db = store.connect(path)
            ct = store.one(db.execute("SELECT * FROM copy_trades WHERE id=?", (self.ct["id"],)))
            results.append(copytrade.handle(db, ct, dict(self.fill), self.uid, source=src, detected=time.time()))
            db.close()
        ts = [threading.Thread(target=go, args=(src,)) for src in ("stream", "poll", "stream", "poll")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(results), [False, False, False, True])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_events").fetchone()[0], 1)

    def test_copy_before_profile_created_is_ignored(self):
        s = stream.TradeStream(self.path)
        s.copy(self.db, {**self.fill, "timestamp": int(self.ct["created"]) - 3600}, time.time())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_events").fetchone()[0], 0)

    def test_sell_fraction_when_api_position_is_stale(self):
        s = stream.TradeStream(self.path)
        s.copy(self.db, dict(self.fill), time.time())
        have = trading.available_shares(self.db, self.aid, YES)
        sell = {**self.fill, "side": "SELL", "size": 25.0, "transaction_hash": "0xsell", "timestamp": int(time.time()) + 1}
        # Data API still shows the leader's 100 shares from before the sale (last event older than the sale)
        stale = {"rows": [{"token_id": YES, "current_size": 100.0, "last_event_at": self.fill["timestamp"]}], "next": None}
        with patch.object(pm, "positions_page", return_value=stale):
            s.copy(self.db, sell, time.time())
        self.assertAlmostEqual(trading.available_shares(self.db, self.aid, YES), have * 0.75, places=4)

    def test_sell_fraction_when_api_position_is_updated(self):
        s = stream.TradeStream(self.path)
        s.copy(self.db, dict(self.fill), time.time())
        have = trading.available_shares(self.db, self.aid, YES)
        sell = {**self.fill, "side": "SELL", "size": 25.0, "transaction_hash": "0xsell", "timestamp": int(time.time()) + 1}
        fresh = {"rows": [{"token_id": YES, "current_size": 75.0, "last_event_at": sell["timestamp"]}], "next": None}
        with patch.object(pm, "positions_page", return_value=fresh):
            s.copy(self.db, sell, time.time())
        self.assertAlmostEqual(trading.available_shares(self.db, self.aid, YES), have * 0.75, places=4)

    def test_followed_wallets(self):
        self.assertEqual(stream.followed(self.db), {LEADER})
        copytrade.set_paused(self.db, self.uid, self.ct["id"], True)
        self.assertEqual(stream.followed(self.db), set())


# ---------------------------------------------------------------- resting orders checked often, liquidity counted once per 20 s
class RestingRefill(Base):
    def test_partial_fill_waits_before_counting_same_liquidity_again(self):
        trading.place(self.db, self.uid, {"side": "buy", "type": "limit", "condition": COND, "token": YES, "shares": 50, "price": 0.30})
        thin = book(asks=((0.29, 20),))
        now = time.time()
        trading.match_resting(self.db, {YES: thin}, now=now + 1)
        trading.match_resting(self.db, {YES: thin}, now=now + 3)   # 2 s later: same displayed 20 shares
        filled = self.db.execute("SELECT filled_shares FROM orders").fetchone()[0]
        self.assertAlmostEqual(filled, 20)
        trading.match_resting(self.db, {YES: thin}, now=now + 1 + trading.REFILL_AFTER + 1)
        self.assertAlmostEqual(self.db.execute("SELECT filled_shares FROM orders").fetchone()[0], 40)


# ---------------------------------------------------------------- local server
class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.path = str(Path(cls.dir.name) / "s.sqlite3")
        store.init(cls.path)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.server.db_path = cls.path
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.dir.cleanup()

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_keep_alive_gzip_and_304(self):
        c = self.conn()
        c.request("GET", "/static/js/core.js", headers={"Accept-Encoding": "gzip"})
        r = c.getresponse()
        body = r.read()
        self.assertEqual((r.status, r.version, r.getheader("Content-Encoding")), (200, 11, "gzip"))
        self.assertIn(b"export", gzip.decompress(body))
        etag = r.getheader("ETag")
        sock = c.sock
        c.request("GET", "/static/js/core.js", headers={"If-None-Match": etag})
        r = c.getresponse()
        self.assertEqual((r.status, r.read()), (304, b""))
        self.assertIs(c.sock, sock)  # same TCP connection
        c.request("GET", "/api/health")
        r = c.getresponse()
        self.assertEqual(json.loads(r.read())["version"], app.VERSION)
        self.assertIs(c.sock, sock)

    def test_keep_alive_requests_are_fast(self):
        # headers and body leave in separate writes: without TCP_NODELAY each request waits ~40 ms for a delayed ACK
        c = self.conn()
        c.request("GET", "/api/health")
        c.getresponse().read()
        t = time.perf_counter()
        for _ in range(20):
            c.request("GET", "/api/health")
            c.getresponse().read()
        self.assertLess((time.perf_counter() - t) / 20, 0.02)

    def test_rejected_post_does_not_spoil_connection(self):
        c = self.conn()
        c.request("POST", "/api/auth/login", body=b'{"username": "a"}', headers={"Content-Type": "text/plain"})
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 415)
        c.request("GET", "/api/health")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertTrue(json.loads(r.read())["ok"])

    def test_events_stream_pushes_own_notifications(self):
        c = self.conn()
        c.request("POST", "/api/auth/setup", body=json.dumps({"username": "owner", "password": "password123"}).encode(),
                  headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        cookie = r.getheader("Set-Cookie").split(";")[0]
        sse = self.conn()
        sse.request("GET", "/api/stream", headers={"Cookie": cookie})
        resp = sse.getresponse()
        self.assertEqual(resp.getheader("Content-Type").split(";")[0], "text/event-stream")
        self.assertEqual(resp.fp.readline(), b"retry: 3000\n")
        resp.fp.readline()
        deadline = time.time() + 3
        while bus.listeners() == 0 and time.time() < deadline:
            time.sleep(0.02)
        db = store.connect(self.path)
        uid = db.execute("SELECT id FROM users").fetchone()[0]
        bus.publish("notify", {"user_id": uid + 999})   # another user's event is not delivered
        notify.add(db, uid, "copy", "Скопирована покупка")
        db.close()
        self.assertEqual(resp.fp.readline(), b"event: notify\n")
        self.assertEqual(json.loads(resp.fp.readline().removeprefix(b"data: ")), {"user_id": uid})
        sse.close()

    def test_events_need_login(self):
        c = self.conn()
        c.request("GET", "/api/stream")
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 401)


# ---------------------------------------------------------------- HTTPS client helpers
class NetHelpers(unittest.TestCase):
    def test_inflate_gzip_and_limit(self):
        raw = json.dumps({"a": "x" * 5000}).encode()
        self.assertEqual(net._inflate(gzip.compress(raw), "gzip", 10 ** 6), raw)
        self.assertEqual(net._inflate(zlib.compress(raw), "deflate", 10 ** 6), raw)
        self.assertEqual(len(net._inflate(gzip.compress(raw), "gzip", 100)), 101)  # caller sees "too big"
        with self.assertRaises(net.NetError):
            net._inflate(b"not gzip", "gzip", 100)

    def test_no_server_is_upstream_error(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        with patch.object(net, "proxy_for", return_value=None):
            with self.assertRaises(pm.UpstreamError):
                pm.fetch(f"https://127.0.0.1:{port}/x", timeout=2)


if __name__ == "__main__":
    unittest.main()
