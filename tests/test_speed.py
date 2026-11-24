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
from kontur import books, bus, copytrade, net, notify, packs, scout, stream, traders, trading, ws
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


def client(chunks, deflate=None):
    w = ws.WebSocket.__new__(ws.WebSocket)
    w._init_state(deflate)
    w.sock = FakeSock(chunks)
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

    def test_permessage_deflate_with_shared_context(self):
        comp = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)

        def packed(text):
            data = comp.compress(text.encode()) + comp.flush(zlib.Z_SYNC_FLUSH)
            return data[:-4]  # RFC 7692: the 00 00 ff ff tail is removed

        msgs = ['{"topic":"activity","payload":{"proxyWallet":"0x1","price":0.5}}',
                '{"topic":"activity","payload":{"proxyWallet":"0x2","price":0.6}}']
        first, second = packed(msgs[0]), packed(msgs[1])
        self.assertLess(len(second), len(msgs[1]) / 2)  # the second message reuses the first one's words
        rsv1 = 0x40
        w = client([bytes([0x80 | rsv1 | ws.OP_TEXT, len(first)]) + first,
                    bytes([rsv1 | ws.OP_TEXT, 3]) + second[:3] + bytes([0x80 | ws.OP_CONT, len(second) - 3]) + second[3:]],
                   deflate={"reset": False})
        self.assertEqual(w.recv(), msgs[0])
        self.assertEqual(w.recv(), msgs[1])
        self.assertEqual(w.rx_plain, len(msgs[0]) + len(msgs[1]))
        self.assertLess(w.rx_wire, w.rx_plain)

    def test_deflate_without_context_takeover_and_plain_frames(self):
        def packed(text):
            c = zlib.compressobj(6, zlib.DEFLATED, -zlib.MAX_WBITS)
            return (c.compress(text.encode()) + c.flush(zlib.Z_SYNC_FLUSH))[:-4]
        a, b = packed("один"), packed("два")
        w = client([bytes([0xC1, len(a)]) + a + frame(ws.OP_TEXT, b"plain") + bytes([0xC1, len(b)]) + b], deflate={"reset": True})
        self.assertEqual([w.recv(), w.recv(), w.recv()], ["один", "plain", "два"])

    def test_compressed_frame_without_agreement_is_an_error(self):
        w = client([bytes([0xC1, 2]) + b"ab"])
        with self.assertRaises(ws.WsError):
            w.recv()

    def test_handshake_reply_parsing(self):
        self.assertEqual(ws._deflate_params("HTTP/1.1 101 OK\r\nsec-websocket-extensions: permessage-deflate\r\n"), {"reset": False})
        self.assertEqual(ws._deflate_params("HTTP/1.1 101 OK\r\nSec-WebSocket-Extensions: permessage-deflate; server_no_context_takeover"),
                         {"reset": True})
        self.assertIsNone(ws._deflate_params("HTTP/1.1 101 OK\r\nUpgrade: websocket"))

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

    def test_events_show_market_texts_stored_once(self):
        s = stream.TradeStream(self.path)
        for i in range(3):
            s.copy(self.db, {**self.fill, "transaction_hash": f"0x{i}", "icon": "https://polymarket-upload.s3.amazonaws.com/r.png"},
                   time.time())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_events WHERE title!='' OR trade_key!=''").fetchone()[0], 0)
        ev = copytrade.events(self.db, self.uid, self.aid)
        self.assertEqual(len(ev), 3)
        self.assertEqual({(e["title"], e["outcome"], e["event_slug"], e["image"]) for e in ev},
                         {("Will it rain?", "Yes", "rain", "https://polymarket-upload.s3.amazonaws.com/r.png")})
        self.assertEqual(ev[0]["source"], "stream")

    def test_rows_from_1_2_keep_texts_and_keys(self):
        # a 1.2.0 row: texts in the row, text key, no hash
        self.db.execute("""INSERT INTO copy_events(copy_trade_id, account_id, leader, status, reason, side, title, outcome, event_slug,
                           image, leader_tx, at, trade_key) VALUES(?,?,?,'filled','','buy','Старый рынок','Да','old','',?,?,?)""",
                        (self.ct["id"], self.aid, LEADER, "0xfeed", time.time(), copytrade.trade_key(self.fill)))
        store.init(self.path)                     # next start hashes the old key
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM copy_events WHERE trade_key!=''").fetchone()[0], 0)
        self.assertFalse(copytrade.handle(self.db, self.row(), dict(self.fill), self.uid, source="poll"))  # still known
        ev = copytrade.events(self.db, self.uid, self.aid)
        self.assertEqual((ev[0]["title"], ev[0]["outcome"], ev[0]["event_slug"]), ("Старый рынок", "Да", "old"))

    def test_followed_wallets(self):
        self.assertEqual(stream.followed(self.db), {LEADER})
        copytrade.set_paused(self.db, self.uid, self.ct["id"], True)
        self.assertEqual(stream.followed(self.db), set())


# ---------------------------------------------------------------- 1.3.0: light marks, books only where needed
class LightMarks(Base):
    def test_positions_valued_from_batch_mids_and_books_only_where_missing(self):
        from kontur import worker
        trading.place(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES, "amount": 20})
        self.db.execute("""INSERT INTO positions(account_id, token_id, condition_id, outcome, title, event_slug, image, shares,
                           cost, bought, realized, status, updated) SELECT account_id, '333', condition_id, outcome, title, event_slug,
                           image, shares, cost, bought, realized, status, updated FROM positions WHERE token_id=?""", (YES,))
        with patch.object(pm, "midpoints", return_value={YES: 0.47}) as mids, \
                patch.object(pm, "book", return_value=book(asks=((0.62, 5),), bids=((0.58, 5),))) as rest:
            worker.job_marks(self.db)
        mids.assert_called_once()
        self.assertEqual(rest.call_count, 1)                        # only the token without a mid
        prices = dict(self.db.execute("SELECT token_id, last_price FROM positions"))
        self.assertAlmostEqual(prices[YES], 0.47)
        self.assertAlmostEqual(prices["333"], 0.60)

    def test_live_stream_watches_only_resting_orders(self):
        from kontur import worker
        trading.place(self.db, self.uid, {"side": "buy", "type": "market", "condition": COND, "token": YES, "amount": 20})
        with patch.object(books, "watch") as watch:
            worker.job_watch(self.db)
        self.assertEqual(list(watch.call_args[0][0]), [])            # a position alone is not streamed
        trading.place(self.db, self.uid, {"side": "buy", "type": "limit", "condition": COND, "token": YES, "shares": 10, "price": 0.2})
        with patch.object(books, "watch") as watch:
            worker.job_watch(self.db)
        self.assertEqual(list(watch.call_args[0][0]), [YES])

    def test_prefetched_book_is_used_once(self):
        with patch.object(pm, "book", return_value={"asks": [[0.5, 1]], "bids": []}) as rest:
            books.prefetch("777")
            books.get("777")           # served from the prefetch
            books.get("777")           # the prefetch is spent: fetched again
        self.assertEqual(rest.call_count, 2)


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

    def test_bad_requests_answer_in_russian(self):
        c = self.conn()
        c.request("POST", "/api/auth/login", body=b"not json", headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        self.assertIn("JSON", json.loads(r.read())["error"])  # was Python's "Expecting value: line 1 column 1"
        self.assertEqual(app.user_message(ValueError("invalid literal for int() with base 10: 'x'")), "Некорректные данные в запросе")
        self.assertEqual(app.user_message(ValueError("Рынок закрыт для торговли")), "Рынок закрыт для торговли")

    def test_dropped_connection_is_not_logged(self):
        srv = app.Server.__new__(app.Server)
        with patch("socketserver.BaseServer.handle_error") as base:
            try:
                raise ConnectionResetError(104, "Connection reset by peer")
            except ConnectionResetError:
                srv.handle_error(None, ("127.0.0.1", 1))
            base.assert_not_called()  # a phone closing its connection is not a server error
            try:
                raise KeyError("real bug")
            except KeyError:
                srv.handle_error(None, ("127.0.0.1", 1))
            base.assert_called_once()

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


# ---------------------------------------------------------------- packed bets (1.3.0)
OLD_TABLE = """CREATE TABLE scout_bets(wallet TEXT NOT NULL, token TEXT NOT NULL, condition TEXT NOT NULL, oidx INTEGER,
  outcome TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', event_slug TEXT NOT NULL DEFAULT '', entry REAL NOT NULL,
  size REAL NOT NULL, opened REAL NOT NULL, closed REAL, exit REAL, state TEXT NOT NULL, mark REAL, end_ts REAL,
  short INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(wallet, token))"""
OLD_SELECT = """SELECT token, condition, oidx, outcome, title, event_slug, entry, size, opened, closed, exit, state, mark,
  end_ts AS end, short FROM scout_bets WHERE wallet=?"""


def sample_bets():
    return [{"token": "900", "condition": COND, "oidx": 1, "outcome": "No", "title": "Дождь?", "event_slug": "rain", "entry": 1,
             "size": 10, "opened": 1800000000, "closed": None, "exit": None, "state": "open", "mark": 0.42, "end": None, "short": 0},
            {"token": "111", "condition": COND, "oidx": 0, "outcome": "Yes", "title": "Дождь?", "event_slug": "rain", "entry": 0.31,
             "size": 12.5, "opened": 1800000100.25, "closed": 1800090000, "exit": 1.0, "state": "closed", "mark": None, "end": 1.8e9,
             "short": 1},
            {"token": "111", "condition": COND, "oidx": 0, "outcome": "Yes", "title": "Дождь?", "event_slug": "rain", "entry": 0.35,
             "size": 3, "opened": 1800000200, "closed": None, "exit": 0, "state": "resolved", "mark": float("nan"), "end": None,
             "short": 0}]


class PackedBets(unittest.TestCase):
    def old_table(self):
        db = store.connect(":memory:")
        db.execute(OLD_TABLE)
        db.executemany("""INSERT OR REPLACE INTO scout_bets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       [("w", b["token"], b["condition"], b["oidx"], b["outcome"], b["title"], b["event_slug"], b["entry"], b["size"],
                         b["opened"], b["closed"], b["exit"], b["state"], b["mark"], b["end"], b["short"]) for b in sample_bets()])
        return db

    def test_same_as_the_old_table(self):
        old = store.rows(self.old_table().execute(OLD_SELECT, ("w",)))
        blob, n, latest = packs.pack(sample_bets())
        self.assertEqual(packs.unpack(blob), old)      # values, number types, NaN -> NULL, last bet per token, token order
        self.assertEqual((n, latest), (2, 1800000200.0))
        self.assertEqual([type(x) for x in packs.unpack(blob)[1].values()], [type(x) for x in old[1].values()])

    def test_migration_moves_bets_and_drops_old_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "old.sqlite3")
            store.init(path)
            db = store.connect(path)
            db.execute(OLD_TABLE)
            db.executemany("""INSERT OR REPLACE INTO scout_bets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                           [(w, b["token"], b["condition"], b["oidx"], b["outcome"], b["title"], b["event_slug"], b["entry"], b["size"],
                             b["opened"], b["closed"], b["exit"], b["state"], b["mark"], b["end"], b["short"])
                            for w in ("w1", "w2") for b in sample_bets()])
            before = {w: store.rows(db.execute(OLD_SELECT, (w,))) for w in ("w1", "w2")}
            db.close()
            store.init(path)                           # the next start moves the data
            db = store.connect(path)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='scout_bets'").fetchone())
            self.assertEqual({w: scout.stored_bets(db, w) for w in ("w1", "w2")}, before)
            self.assertEqual(packs.latest(db, "w1"), 1800000200.0)
            store.init(path)                           # later starts do nothing
            self.assertEqual(scout.stored_bets(db, "w2"), before["w2"])
            db.close()

    def test_empty_list_removes_the_record(self):
        db = store.connect(":memory:")
        db.executescript(store.SCHEMA)
        packs.save(db, "w", sample_bets())
        self.assertEqual(len(packs.load(db, "w")), 2)
        packs.save(db, "w", [])
        self.assertEqual(packs.load(db, "w"), [])
        self.assertIsNone(packs.latest(db, "w"))


# ---------------------------------------------------------------- incremental positions (1.3.0)
NOW_TS = 1_800_000_000


def position(tok, status, last, pnl=1.0, entry=0.5, size=100, price=None, redeemable=False):
    return {"token_id": tok, "condition_id": "0x" + tok.rjust(64, "0"), "outcome": "Yes", "outcome_index": 0, "title": "Q " + tok,
            "slug": "", "event_slug": "ev", "avg_price": entry, "current_price": price, "total_size": size, "current_size": size,
            "realized_pnl": pnl, "total_pnl": pnl, "status": status, "redeemable": redeemable, "end_date": None,
            "first_entry_at": last - 86400, "last_event_at": last}


class FakePositions:
    """positions_page that honours status, start and paging like the Data API."""

    def __init__(self, rows, page=2):
        self.rows, self.page, self.calls = rows, page, []

    def __call__(self, **kw):
        self.calls.append({k: kw.get(k) for k in ("status", "start", "cursor")})
        rows = sorted((r for r in self.rows if r["status"] == kw["status"] and (not kw.get("start") or r["last_event_at"] >= kw["start"])),
                      key=lambda r: -r["last_event_at"])
        i = int(kw.get("cursor") or 0)
        chunk = rows[i:i + self.page]
        return {"rows": chunk, "next": str(i + self.page) if i + self.page < len(rows) else None}


class IncrementalPositions(unittest.TestCase):
    def test_scout_crawl_reads_only_new_closed_and_matches_full(self):
        day = 86400
        rows = [position("1", "CLOSED", NOW_TS - 10 * day), position("2", "CLOSED", NOW_TS - 9 * day),
                position("3", "CLOSED", NOW_TS - 5 * day), position("4", "OPEN", NOW_TS - 4 * day, price=0.6)]
        since = NOW_TS - 240 * day
        fake = FakePositions(rows)
        with patch.object(pm, "positions_page", side_effect=fake):
            prev, _ = scout.crawl("0x" + "1" * 40, since, NOW_TS)
        prev_at = NOW_TS
        # later: a new closed position (5), token 2 bought again (now open)
        later = NOW_TS + 3 * day
        rows += [position("5", "CLOSED", later - day)]
        rows[1] = position("2", "OPEN", later - 2 * day, price=0.7)
        fake = FakePositions(rows)
        with patch.object(pm, "positions_page", side_effect=fake):
            inc, partial = scout.crawl("0x" + "1" * 40, since, later, prev=prev, prev_at=prev_at)
        closed_calls = [c for c in fake.calls if c["status"] == "CLOSED"]
        self.assertTrue(all(c["start"] == prev_at - scout.SYNC_MARGIN for c in closed_calls))
        self.assertTrue(all(c["start"] is None for c in fake.calls if c["status"] == "OPEN"))
        with patch.object(pm, "positions_page", side_effect=FakePositions(rows)):
            full, _ = scout.crawl("0x" + "1" * 40, since, later)
        key = lambda bets: sorted((b["token"], b["state"], b["entry"], b["exit"], b["mark"]) for b in bets)  # noqa: E731
        self.assertEqual(key(inc), key(full))
        self.assertFalse(partial)

    def test_scout_falls_back_to_full_crawl_when_too_much_changed(self):
        day = 86400
        rows = [position(str(i), "CLOSED", NOW_TS - i * 60) for i in range(1, 40)]
        with patch.object(scout, "MAX_PAGES", {"CLOSED": 3, "OPEN": 3}), patch.object(pm, "positions_page", side_effect=FakePositions(rows)):
            inc, partial = scout.crawl("0x" + "1" * 40, NOW_TS - 240 * day, NOW_TS, prev=[], prev_at=NOW_TS - day)
            full, partial_full = scout.crawl("0x" + "1" * 40, NOW_TS - 240 * day, NOW_TS)
        self.assertEqual(sorted(b["token"] for b in inc), sorted(b["token"] for b in full))
        self.assertTrue(partial and partial_full)

    def test_trader_closed_positions_incremental_with_reopen_and_cap(self):
        db = store.connect(":memory:")
        db.executescript(store.SCHEMA)
        w = "0x" + "2" * 40
        rows = [position("1", "CLOSED", NOW_TS - 900, pnl=5), position("2", "CLOSED", NOW_TS - 800, pnl=-3),
                position("3", "OPEN", NOW_TS - 100, pnl=1)]
        fake = FakePositions(rows)
        with patch.object(pm, "positions_page", side_effect=fake):
            closed, capped = traders.closed_positions(db, w, [r for r in rows if r["status"] == "OPEN"], now=NOW_TS)
        self.assertEqual(sorted(p for p, _ in closed.values()), [-3, 5])
        rows.append(position("4", "CLOSED", NOW_TS + 50, pnl=7))
        rows[0] = position("1", "OPEN", NOW_TS + 60, pnl=6)          # bought again: no longer closed
        fake = FakePositions(rows)
        with patch.object(pm, "positions_page", side_effect=fake):
            closed, capped = traders.closed_positions(db, w, [r for r in rows if r["status"] == "OPEN"], now=NOW_TS + 100)
        self.assertEqual(sorted(p for p, _ in closed.values()), [-3, 7])
        self.assertEqual([c["start"] for c in fake.calls], [NOW_TS - traders.SYNC_MARGIN])
        with patch.object(traders, "CLOSED_CAP", 1):                 # a history capped at one position
            with patch.object(pm, "positions_page", side_effect=FakePositions(rows)):
                closed, capped = traders.closed_positions(db, w, [], now=NOW_TS + 200)
        self.assertEqual((len(closed), capped), (1, True))
        self.assertEqual(list(closed.values())[0][0], 7)            # the newest one is kept


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
