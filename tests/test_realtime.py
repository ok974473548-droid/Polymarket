"""Реальное время: клиент WebSocket, живые стаканы, поток сделок китов, отсев повторов, быстрый сервер интерфейса."""
import base64
import gzip
import hashlib
import json
import re
import socket
import struct
import threading
import time
import urllib.request

import pytest

from conftest import fast_params
from synthetic import FakeClob, FakeData, FakeGamma, SyntheticWorld, load_world
from whalefollow.agent import Agent
from whalefollow.api.server import serve
from whalefollow.db import DB
from whalefollow.estimator.base import FunctionEstimator
from whalefollow.ingest import pipeline
from whalefollow.ingest.polymarket import parse_market
from whalefollow.positions import assign_entities, build_positions
from whalefollow.scoring.scorer import run_scoring
from whalefollow.scoring.sybil import run_antisybil
from whalefollow.services import Services
from whalefollow.streams import LiveBooks, Streams, TradeStream, to_trade_row
from whalefollow.ws import WSClient

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _mask(data: bytes, key: bytes) -> bytes:
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def wait_for(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- маленький сервер WebSocket для тестов
class MiniWS:
    def __init__(self, on_text=None):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/ws"
        self.on_text = on_text
        self.conns, self.received, self.pongs = [], [], []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    @staticmethod
    def _read(c, n):
        buf = b""
        while len(buf) < n:
            chunk = c.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    def _serve(self, c):
        head = b""
        while b"\r\n\r\n" not in head:
            head += c.recv(1024)
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", head).group(1).decode()
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        c.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  f"Sec-WebSocket-Accept: {acc}\r\n\r\n".encode())
        self.conns.append(c)
        try:
            while True:
                b1, b2 = self._read(c, 2)
                op, n = b1 & 0x0F, b2 & 0x7F
                if n == 126:
                    n = struct.unpack("!H", self._read(c, 2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", self._read(c, 8))[0]
                assert b2 & 0x80, "кадры клиента обязаны быть замаскированы"
                mask = self._read(c, 4)
                data = _mask(self._read(c, n), mask)
                if op == 0x8:
                    return
                if op == 0xA:
                    self.pongs.append(data)
                    continue
                text = data.decode()
                self.received.append(text)
                if text == "PING":
                    self.send("PONG", c)
                elif self.on_text:
                    self.on_text(self, c, text)
        except (ConnectionError, OSError):
            return

    @staticmethod
    def frame(op, payload: bytes, fin=True):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | op])
        if n < 126:
            head += bytes([n])
        elif n < 65536:
            head += bytes([126]) + struct.pack("!H", n)
        else:
            head += bytes([127]) + struct.pack("!Q", n)
        return head + payload

    def send(self, text, c=None, fragmented=False):
        c = c or self.conns[-1]
        data = text.encode()
        if fragmented:
            half = len(data) // 2
            c.sendall(self.frame(0x1, data[:half], fin=False) + self.frame(0x0, data[half:], fin=True))
        else:
            c.sendall(self.frame(0x1, data))

    def ping(self, payload=b"hi"):
        self.conns[-1].sendall(self.frame(0x9, payload))

    def drop(self):
        for c in self.conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    def close(self):
        self.drop()
        self.srv.close()


def test_ws_client_frames_keepalive_and_reconnect():
    got, opens = [], []
    srv = MiniWS()
    cli = WSClient(srv.url, got.append, on_open=lambda c: (opens.append(c.generation), c.send({"type": "sub"})),
                   name="t", ping_interval=0.1, max_backoff=0.2).start()
    try:
        assert wait_for(lambda: '{"type": "sub"}' in srv.received)
        big = json.dumps({"x": "y" * 70_000})                    # длина в 8 байтах + фрагменты
        srv.send(big, fragmented=True)
        srv.send("PONG")                                         # служебный ответ не доходит до обработчика
        srv.send("hello")
        assert wait_for(lambda: len(got) == 2)
        assert got == [big, "hello"]
        assert wait_for(lambda: "PING" in srv.received)          # keepalive Polymarket — текстовый PING
        srv.ping(b"abc")
        assert wait_for(lambda: srv.pongs == [b"abc"])           # ответ на ping сервера
        assert cli.alive(5)
        srv.drop()                                               # обрыв → переподключение и повторная подписка
        assert wait_for(lambda: cli.stats["connects"] == 2 and len(opens) == 2, timeout=6)
        assert opens == [1, 2] and srv.received.count('{"type": "sub"}') == 2
    finally:
        cli.stop()
        srv.close()


# ---------------------------------------------------------------- живые стаканы
class FakeWS:
    instances = []

    def __init__(self, url, on_message, on_open=None, **kw):
        self.on_message, self.on_open = on_message, on_open
        self.generation, self.sent, self.ok = 1, [], True
        self.stats = {"connected": True, "connects": 1, "messages": 0, "last_msg_ts": None, "last_error": None,
                      "connect_ms": 40}
        FakeWS.instances.append(self)

    def start(self):
        return self

    def stop(self):
        pass

    @property
    def connected(self):
        return self.ok

    def alive(self, s):
        return self.ok

    def send(self, obj):
        self.sent.append(obj)
        return True


def test_live_books_snapshot_updates_and_freshness():
    lb = LiveBooks(client_cls=FakeWS)
    assert lb.get("t1") is None and lb.status()["started"] is False
    lb.want(["t1", "t2"])
    c = lb._client
    assert c.sent[-1] == {"assets_ids": ["t1", "t2"], "operation": "subscribe", "custom_feature_enabled": True}
    c.on_message(json.dumps([{"event_type": "book", "asset_id": "t1", "market": "m",
                              "bids": [{"price": "0.48", "size": "100"}, {"price": "0.50", "size": "20"}],
                              "asks": [{"price": "0.53", "size": "40"}, {"price": "0.52", "size": "10"}]}]))
    assert lb.get("t1") == ([(0.50, 20.0), (0.48, 100.0)], [(0.52, 10.0), (0.53, 40.0)])
    c.on_message(json.dumps({"event_type": "price_change", "market": "m", "price_changes": [
        {"asset_id": "t1", "price": "0.50", "size": "0", "side": "BUY"},          # уровень ушёл
        {"asset_id": "t1", "price": "0.51", "size": "7", "side": "SELL"},         # новая заявка на продажу
        {"asset_id": "t9", "price": "0.1", "size": "1", "side": "BUY"}]}))        # без снимка — пропускаем
    assert lb.get("t1") == ([(0.48, 100.0)], [(0.51, 7.0), (0.52, 10.0), (0.53, 40.0)])
    assert lb.get("t2") is None                                                   # снимка ещё нет
    c.ok = False
    assert lb.get("t1") is None                                                   # связь пропала — не верим
    c.ok, c.generation = True, 2
    assert lb.get("t1") is None                                                   # стакан из прошлого соединения
    c.on_open(c)                                                                  # переподключение: подписка заново
    assert c.sent[-1]["type"] == "market" and set(c.sent[-1]["assets_ids"]) == {"t1", "t2"}
    st = lb.status()
    assert st["subscribed"] == 2 and st["hits"] >= 2 and st["misses"] >= 3
    lb.MAX_ASSETS = 3
    lb.want(["t3", "t4"])                       # вытесняется давно не нужный t2; t1 только что читали — остаётся
    assert c.sent[-2] == {"assets_ids": ["t2"], "operation": "unsubscribe"}
    assert set(lb.wanted) == {"t1", "t3", "t4"}


def test_live_books_over_real_websocket():
    def on_text(srv, conn, text):
        msg = json.loads(text)
        if msg.get("type") == "market" or msg.get("operation") == "subscribe":
            srv.send(json.dumps([{"event_type": "book", "asset_id": a, "bids": [{"price": "0.4", "size": "5"}],
                                  "asks": [{"price": "0.6", "size": "3"}]} for a in msg["assets_ids"]]), conn)
    srv = MiniWS(on_text)
    lb = LiveBooks(url=srv.url, ping_interval=0.2)
    try:
        lb.want(["tok"])
        assert wait_for(lambda: lb.get("tok") is not None)
        t0 = time.perf_counter()
        bids, asks = lb.get("tok")
        assert (time.perf_counter() - t0) * 1000 < 5                  # из памяти — доли миллисекунды
        assert bids == [(0.4, 5.0)] and asks == [(0.6, 3.0)]
    finally:
        lb.stop()
        srv.close()


# ---------------------------------------------------------------- поток сделок
def _payload(wallet, token="tok", side="BUY", price="0.55", size="100.000000", ts=1_700_000_000, tx="0xAB"):
    return {"proxyWallet": wallet, "asset": token, "side": side, "price": price, "size": size,
            "timestamp": ts * 1000, "transactionHash": tx, "conditionId": "0xm", "title": "Q?", "outcome": "Yes"}


def test_trade_stream_keeps_only_tracked_wallets():
    got = []
    ts = TradeStream(got.extend, client_cls=FakeWS)
    assert ts._client is None
    ts.set_wallets(["0xWHALE"])                                         # поток стартует, когда есть кого слушать
    c = ts._client
    c.on_open(c)
    assert c.sent[-1] == {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}
    c.on_message(json.dumps({"topic": "activity", "type": "trades", "payload": _payload("0xother")}))
    c.on_message(json.dumps({"topic": "activity", "type": "trades", "payload": _payload("0xWhale")}))
    c.on_message("not json")
    assert len(got) == 1 and got[0]["proxy_wallet"] == "0xwhale" and got[0]["timestamp"] == 1_700_000_000
    assert ts.status()["matched"] == 1 and ts.stats["seen"] == 2
    assert to_trade_row({"proxyWallet": "0x1", "asset": "t", "side": "HOLD", "price": 1, "size": 1}) is None
    t = pipeline.parse_trade(got[0])
    assert t["size_shares"] == 100.0 and t["price"] == 0.55 and t["tx_hash"] == "0xab"


def test_store_trades_dedupes_stream_and_rest_copies(db):
    ws_row = to_trade_row(_payload("0xw", size="100.000000", price="0.5500", tx="0xABCDEF"))
    rest_row = {"transaction_hash": "0xabcdef", "proxy_wallet": "0xw", "token_id": "tok", "side": "BUY",
                "size": 100, "price": 0.55, "timestamp": 1_700_000_000, "condition_id": "0xm"}
    assert pipeline.parse_trade(ws_row)["trade_id"] != pipeline.parse_trade(rest_row)["trade_id"]
    assert len(pipeline.store_trades(db, [ws_row])) == 1
    assert pipeline.store_trades(db, [rest_row]) == []                    # та же сделка из опроса — не новая
    other = dict(rest_row, size=50)                                       # другая сделка в той же транзакции
    assert len(pipeline.store_trades(db, [other])) == 1
    assert db.scalar("SELECT COUNT(*) AS n FROM trades") == 2
    # поток и опрос одновременно: «новой» сделка оказывается ровно один раз
    rows = [dict(rest_row, transaction_hash=f"0x{i:04x}") for i in range(40)]
    ws_rows = [to_trade_row(_payload("0xw", size="100.0", price="0.550", tx=f"0x{i:04X}")) for i in range(40)]
    out = []
    th = [threading.Thread(target=lambda r=r: out.extend(pipeline.store_trades(db, r))) for r in (rows, ws_rows)]
    [t.start() for t in th]
    [t.join() for t in th]
    assert len(out) == 40 and db.scalar("SELECT COUNT(*) AS n FROM trades") == 42


# ---------------------------------------------------------------- агент: сигнал сразу из потока
@pytest.fixture(scope="module")
def env():
    world = SyntheticWorld(seed=4)
    db = DB("sqlite://:memory:")
    db.init_schema()
    load_world(db, world)
    params = fast_params()
    build_positions(db)
    run_antisybil(db, params)
    assign_entities(db)
    scores = run_scoring(db, params, now=world.now)
    agent = Agent(db, params, FakeData(world), FakeClob(world), FakeGamma(world),
                  estimator=FunctionEstimator(lambda ctx: 0.72), clock=lambda: world.now)
    agent.run_once()
    best = scores[scores.status == "approved"].sort_values("score", ascending=False)
    whale = cat = None
    for e in best["entity_id"]:
        cats = [r["category"] for r in db.query("SELECT category FROM wallet_category_scores WHERE entity_id=? "
                                                "AND status='approved'", (e,))
                if r["category"] in ("economics", "politics", "sports", "tech")]
        if cats:
            whale, cat = e, cats[0]
            break
    assert whale
    return {"world": world, "db": db, "agent": agent, "whale": whale, "cat": cat}


def test_stream_trade_becomes_signal_at_once_and_poll_does_not_repeat_it(env):
    world, db, agent = env["world"], env["db"], env["agent"]
    m = world.add_live_market(category=env["cat"], pi=0.75, price=0.55)
    mrow, toks, _ = parse_market(world.gamma_market(m))
    db.upsert("markets", mrow, ["market_id"])
    db.upsert_many("tokens", toks, ["token_id"])
    wallet = agent.wallets_of(env["whale"])[0]
    world.whale_trade_now(wallet, m, m.yes_token, 3000)
    rest = world.trades[-1]

    class Books:                                      # живой стакан есть — запрос к бирже не нужен
        def __init__(self):
            self.wanted = []

        def get(self, token):
            return world.book(token)

        def want(self, tokens):
            self.wanted += list(tokens)

    class _Trades:
        def live(self):
            return True
    agent.streams = Streams(db, agent, books=Books(), trades=TradeStream(lambda r: None, client_cls=FakeWS))
    agent.streams.trades = _Trades()
    try:
        before = db.scalar("SELECT COUNT(*) AS n FROM signals")
        row = to_trade_row({"proxyWallet": wallet.upper(), "asset": rest["token_id"], "side": "BUY",
                            "price": f"{rest['price']:.6f}", "size": f"{rest['size']:.6f}",
                            "timestamp": rest["timestamp"] * 1000, "transactionHash": rest["transaction_hash"].upper(),
                            "conditionId": rest["condition_id"]})
        t0 = time.perf_counter()
        res = agent.process_stream_trades([row])
        took = time.perf_counter() - t0
        assert res["new_trades"] == 1 and res["signals"] == 1, res
        assert took < 2.0
        assert agent.book_stats["ws"] >= 1 and agent.book_stats["http"] == 0
        assert db.scalar("SELECT COUNT(*) AS n FROM signals") == before + 1
        agent._last_poll = 0.0                                          # страховочный опрос
        again = agent.run_once()
        assert again["signals"] == 0 and db.scalar("SELECT COUNT(*) AS n FROM signals") == before + 1
        assert agent.process_stream_trades([row])["new_trades"] == 0     # повтор из потока
        # пока поток жив, опрос не идёт чаще раза в 20 с
        calls = {"n": 0}
        orig = agent._poll
        agent._poll = lambda w: calls.__setitem__("n", calls["n"] + 1) or []
        agent.run_once()
        assert calls["n"] == 0
        agent._last_poll = 0.0
        agent.run_once()
        assert calls["n"] == 1
        agent._poll = orig
    finally:
        agent.streams = None


def test_diagnostics_shows_stream_state(env):
    svc = Services(env["db"], fast_params(), agent=env["agent"])
    assert svc.diagnostics()["streams"] is None                         # потоки не запущены — блока нет
    env["agent"].streams = Streams(env["db"], env["agent"], books=LiveBooks(client_cls=FakeWS),
                                   trades=TradeStream(lambda r: None, client_cls=FakeWS))
    try:
        env["agent"].streams.trades.set_wallets(["0xa"])
        d = svc.diagnostics()
        s = d["streams"]
        assert s["trades"]["live"] and s["trades"]["wallets"] == 1 and not s["books"]["started"]
        assert "Поток сделок: работает" in d["report"] and "Поток стаканов: не запущен" in d["report"]
    finally:
        env["agent"].streams = None


# ---------------------------------------------------------------- сервер интерфейса
def test_ui_server_keepalive_gzip_etag(db):
    import http.client
    svc = Services(db, fast_params())
    httpd = serve(svc, port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/app.js", headers={"Accept-Encoding": "gzip"})
        r = conn.getresponse()
        body = r.read()
        etag = r.getheader("ETag")
        assert r.status == 200 and r.getheader("Content-Encoding") == "gzip" and etag
        assert b"viewDiagnostics" in gzip.decompress(body)
        conn.request("GET", "/app.js", headers={"If-None-Match": etag})      # то же соединение
        r2 = conn.getresponse()
        assert r2.status == 304 and r2.read() == b""
        # отклонённый POST с телом не портит следующий запрос на том же соединении
        conn.request("POST", "/api/settings", body="x=1", headers={"Content-Type": "text/plain"})
        r3 = conn.getresponse()
        assert r3.status == 415 and json.loads(r3.read())["error"]
        t0 = time.perf_counter()
        for _ in range(20):
            conn.request("GET", "/api/status", headers={"Accept-Encoding": "gzip"})
            r4 = conn.getresponse()
            data = r4.read()
            assert r4.status == 200
        per = (time.perf_counter() - t0) / 20 * 1000
        payload = gzip.decompress(data) if r4.getheader("Content-Encoding") == "gzip" else data
        assert "agent_ok" in json.loads(payload)
        assert per < 20, per          # по открытому соединению (без задержки Нейгла было бы ~40 мс)
        conn.request("OPTIONS", "/api/status")
        r5 = conn.getresponse()
        assert r5.status == 204 and r5.read() == b""
        conn.close()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:   # обычный клиент без gzip
            assert b"<html" in resp.read().lower()
    finally:
        httpd.shutdown()


def test_signal_card_has_live_decision_data(env):
    svc = Services(env["db"], fast_params(), agent=env["agent"], clock=lambda: env["world"].now)
    pend = env["db"].one("SELECT signal_id FROM signals WHERE status='pending' AND kind='entry' ORDER BY ts DESC")
    assert pend, "нужен ждущий сигнал из предыдущего теста"
    s = svc.signal_detail(pend["signal_id"])
    L = s["live"]
    assert L and L["ask"] and L["whale_price"] and 0 < L["q_eff"] < 1 and L["threshold"] == fast_params().edge_threshold
    assert L["window_s"] >= 1 and s["pending_ids"][0] == pend["signal_id"]
    d = svc.dashboard()["pending"][0]
    assert d["token_id"] == L["token_id"] and d["whale_cents"] is not None
