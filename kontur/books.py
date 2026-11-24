"""Live order books from the Polymarket CLOB market stream (WebSocket), kept in memory.

Tokens of open positions and orders, and outcomes the user has just quoted, are watched. A book is
served from memory only while the stream is connected and the book's snapshot came on the current
connection; otherwise callers fetch it over REST as before (`get`).
"""

import json
import logging
import threading
import time

from . import polymarket as pm
from .ws import WebSocket, WsError

URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_EVERY = 10
STALE_AFTER = 30          # seconds without any message from the stream -> books are not trusted
MAX_TOKENS = 800
LOG = logging.getLogger("kontur.books")


class BookStream(threading.Thread):
    def __init__(self):
        super().__init__(name="kontur-books", daemon=True)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.want = {}            # token -> watch until (monotonic)
        self.sent = set()         # tokens subscribed on the current connection
        self.books = {}           # token -> {"asks": {p: s}, "bids": {p: s}, "at": ts, "gen": n}
        self.gen = 0
        self.connected = False
        self.last_rx = 0.0
        self.stats = {"connects": 0, "messages": 0, "updates": 0, "errors": 0, "error": "", "since": None,
                      "wire": 0, "plain": 0}
        self.listeners = []

    # ---------------------------------------------------------------- public
    def watch(self, tokens, ttl=120):
        until = time.monotonic() + ttl
        with self.lock:
            for tok in map(str, tokens):
                if tok in self.want or len(self.want) < MAX_TOKENS:
                    self.want[tok] = max(self.want.get(tok, 0), until)

    def fresh(self, tok):
        tok = str(tok)
        with self.lock:
            b = self.books.get(tok)
            if not b or b["gen"] != self.gen or not self.connected or time.monotonic() - self.last_rx > STALE_AFTER:
                return None
            asks = sorted((p, s) for p, s in b["asks"].items() if s > 0)
            bids = sorted(((p, s) for p, s in b["bids"].items() if s > 0), reverse=True)
            at = b["at"]
        return {"token": tok, "asks": [[p, s] for p, s in asks], "bids": [[p, s] for p, s in bids],
                "min_size": None, "tick": None, "at": at, "live": True}

    def status(self):
        with self.lock:
            live = sum(1 for b in self.books.values() if b["gen"] == self.gen)
            ws = getattr(self, "_ws", None)
            return {"connected": self.connected and time.monotonic() - self.last_rx <= STALE_AFTER, "watched": len(self.want),
                    "books": live, **self.stats, "wire": self.stats["wire"] + (ws.rx_wire if ws else 0),
                    "plain": self.stats["plain"] + (ws.rx_plain if ws else 0)}

    # ---------------------------------------------------------------- stream
    def _apply(self, ev):
        kind = ev.get("event_type")
        if kind == "book":
            tok = str(ev.get("asset_id") or "")
            if tok not in self.want:
                return None
            side = {}
            for name in ("asks", "bids"):
                lv = {}
                for x in ev.get(name) or []:
                    p, s = pm.num(x.get("price")), pm.num(x.get("size"))
                    if p is not None and s and 0 < p < 1:
                        lv[p] = s
                side[name] = lv
            ts = (pm.num(ev.get("timestamp")) or time.time() * 1000) / 1000
            self.books[tok] = {"asks": side["asks"], "bids": side["bids"], "at": ts, "gen": self.gen}
            return tok
        if kind == "price_change":
            ts = (pm.num(ev.get("timestamp")) or time.time() * 1000) / 1000
            changed = None
            for ch in ev.get("price_changes") or []:
                tok = str(ch.get("asset_id") or "")
                b = self.books.get(tok)
                if not b or b["gen"] != self.gen:
                    continue
                p, s = pm.num(ch.get("price")), pm.num(ch.get("size"))
                if p is None or s is None:
                    continue
                levels = b["bids"] if str(ch.get("side")).upper() == "BUY" else b["asks"]
                if s > 0:
                    levels[p] = s
                else:
                    levels.pop(p, None)
                b["at"] = ts
                changed = tok
            return changed
        return None

    def _subscribe_new(self, ws):
        now = time.monotonic()
        with self.lock:
            for tok in [t for t, until in self.want.items() if until < now]:
                del self.want[tok]
                self.books.pop(tok, None)
            new = [t for t in self.want if t not in self.sent]
            gone = [t for t in self.sent if t not in self.want]
        if new:
            first = not self.sent
            msg = {"assets_ids": new, "type": "market"} if first else {"assets_ids": new, "operation": "subscribe"}
            ws.send(json.dumps(msg))
            self.sent.update(new)
        if gone:
            ws.send(json.dumps({"assets_ids": gone, "operation": "unsubscribe"}))
            self.sent.difference_update(gone)

    def _session(self):
        ws = WebSocket(URL, timeout=15)
        ws.settimeout(2)
        self._ws = ws
        with self.lock:
            self.gen += 1
            self.sent = set()
            self.connected = True
            self.last_rx = time.monotonic()
        self.stats["connects"] += 1
        self.stats["since"] = time.time()
        last_ping = last_sub = 0.0
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now - last_sub >= 1.0:
                    self._subscribe_new(ws)
                    last_sub = now
                if now - last_ping >= PING_EVERY:
                    ws.send("PING")
                    last_ping = now
                try:
                    msg = ws.recv()
                except TimeoutError:
                    if time.monotonic() - self.last_rx > STALE_AFTER:
                        raise WsError("Поток стаканов молчит") from None
                    continue
                self.last_rx = time.monotonic()
                if not msg or msg[0] not in "[{":
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                self.stats["messages"] += 1
                changed = []
                with self.lock:
                    for ev in data if isinstance(data, list) else [data]:
                        if isinstance(ev, dict):
                            tok = self._apply(ev)
                            if tok:
                                changed.append(tok)
                if changed:
                    self.stats["updates"] += len(changed)
                    for fn in self.listeners:
                        try:
                            fn(changed)
                        except Exception:  # noqa: BLE001 - a listener must not break the stream
                            LOG.exception("book listener failed")
        finally:
            with self.lock:
                self.connected = False
            ws.close()
            self.stats["wire"] += ws.rx_wire
            self.stats["plain"] += ws.rx_plain
            self._ws = None

    def run(self):
        delay = 1.0
        while not self.stop.is_set():
            with self.lock:
                idle = not self.want
            if idle:
                self.stop.wait(1.0)
                continue
            started = time.monotonic()
            try:
                self._session()
            except (OSError, WsError, ValueError) as exc:
                self.stats["errors"] += 1
                self.stats["error"] = str(exc)[:200]
                LOG.info("book stream: %s", exc)
            if time.monotonic() - started > 60:
                delay = 1.0
            self.stop.wait(delay)
            delay = min(30.0, delay * 2)


STREAM = None


def start():
    global STREAM
    if STREAM is None:
        STREAM = BookStream()
        STREAM.start()
    return STREAM


def watch(tokens, ttl=120):
    if STREAM is not None:
        STREAM.watch([str(t) for t in tokens if t], ttl)


def fresh(tok):
    return STREAM.fresh(tok) if STREAM is not None else None


_prefetched = {}             # token -> (monotonic time, book) fetched ahead by a copy that is being prepared
_prefetch_lock = threading.Lock()
PREFETCH_TTL = 1.5


def prefetch(tok):
    """Fetch a book over REST now, for a `get` that follows within PREFETCH_TTL seconds (copies fetch the book
    while the market terms are being read, instead of one after the other)."""
    bk = pm.book(tok)
    with _prefetch_lock:
        if len(_prefetched) > 200:
            _prefetched.clear()
        _prefetched[str(tok)] = (time.monotonic(), bk)
    return bk


def get(tok, watch_ttl=None):
    """Book from the live stream when trustworthy, a just-prefetched one, or a REST fetch.

    watch_ttl: keep the token on the live stream for that many seconds (the order ticket a person is looking at).
    """
    bk = fresh(tok)
    if bk:
        return bk
    with _prefetch_lock:
        hit = _prefetched.pop(str(tok), None)
    if hit and time.monotonic() - hit[0] <= PREFETCH_TTL:
        bk = hit[1]
    else:
        bk = pm.book(tok)
    if watch_ttl:
        watch([tok], ttl=watch_ttl)
    return bk


def status():
    return STREAM.status() if STREAM is not None else {"connected": False, "watched": 0, "books": 0}
