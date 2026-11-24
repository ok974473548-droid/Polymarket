"""Live trade stream for copy trading: Polymarket RTDS (`activity/trades`) over WebSocket.

The Data API shows a fill only minutes after it happens; the live stream shows it in about a second.
Every fill of a followed wallet is handed to copy workers at once. The Data API polling in the worker
stays as a fallback: a fill copied from the stream is recognised by its key and never copied twice.
"""

import collections
import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import books
from . import copytrade
from . import db as store
from . import polymarket as pm
from . import trading
from .ws import WebSocket, WsError

URL = "wss://ws-live-data.polymarket.com"
PING_EVERY = 5
STALE_AFTER = 20          # seconds without messages -> reconnect (the feed carries dozens of fills a second)
LEADERS_EVERY = 2.0       # how often the followed wallets are re-read from the database
WORKERS = 3
PREFETCH = ThreadPoolExecutor(6, thread_name_prefix="kontur-prefetch")
LOG = logging.getLogger("kontur.stream")


def to_row(p):
    """RTDS trade payload -> the Data API trade row the copy code works with."""
    ts = p.get("timestamp") or 0
    ts = ts / 1000 if ts > 1e12 else ts
    return {"proxy_wallet": str(p.get("proxyWallet") or "").lower(), "name": p.get("name") or "",
            "pseudonym": p.get("pseudonym") or "", "profile_image": p.get("profileImage") or "",
            "side": str(p.get("side") or "").upper(), "token_id": str(p.get("asset") or ""),
            "condition_id": str(p.get("conditionId") or "").lower(),
            "size": float(p["size"]) if p.get("size") is not None else None,
            "price": float(p["price"]) if p.get("price") is not None else None,
            "timestamp": int(ts), "title": p.get("title") or "", "slug": p.get("slug") or "",
            "event_slug": p.get("eventSlug") or "", "icon": p.get("icon") or "", "outcome": p.get("outcome") or "",
            "outcome_index": p.get("outcomeIndex"), "transaction_hash": str(p.get("transactionHash") or "").lower()}


def followed(db):
    """Wallets with an active copy (manual or autopilot) on an active profile."""
    return {r[0].lower() for r in db.execute("""SELECT DISTINCT c.leader FROM copy_trades c JOIN accounts a ON a.id=c.account_id
                                                WHERE c.paused=0 AND a.paused=0""")}


class TradeStream(threading.Thread):
    def __init__(self, path):
        super().__init__(name="kontur-trades", daemon=True)
        self.path = path
        self.stop = threading.Event()
        self.queues = [queue.Queue(maxsize=2000) for _ in range(WORKERS)]  # one per worker: a leader's fills stay in order
        self.leaders = set()
        self.connected = False
        self.last_rx = 0.0
        self.lags = collections.deque(maxlen=500)   # seconds from the leader's fill to our receipt
        self.stats = {"connects": 0, "messages": 0, "matched": 0, "copied": 0, "errors": 0, "error": "", "since": None,
                      "wire": 0, "plain": 0}
        self.workers = [threading.Thread(target=self._work, args=(q,), name=f"kontur-copy-{i}", daemon=True)
                        for i, q in enumerate(self.queues)]

    # ---------------------------------------------------------------- copy workers
    def _work(self, q):
        db = store.connect(self.path)
        while not self.stop.is_set():
            try:
                row, seen_at = q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self.copy(db, row, seen_at)
            except Exception:  # noqa: BLE001 - one bad fill must not stop copying
                LOG.exception("stream copy failed")
                if db.in_transaction:
                    db.execute("ROLLBACK")
        db.close()

    @staticmethod
    def complete(row):
        """Some stream fills come without the market id and title: fill them in from the outcome token."""
        if row["condition_id"] and row["title"]:
            return row
        raw = pm.market_by_token(row["token_id"])
        c = pm.contract(raw)
        events = raw.get("events") if isinstance(raw.get("events"), list) else []
        ev = events[0] if events and isinstance(events[0], dict) else {}
        idx = next((i for i, o in enumerate(c["outcomes"]) if o["token"] == row["token_id"]), None)
        return {**row, "condition_id": row["condition_id"] or str(raw.get("conditionId") or "").lower(),
                "title": row["title"] or pm.text(raw.get("question"), 300), "slug": row["slug"] or pm.text(raw.get("slug"), 240),
                "event_slug": row["event_slug"] or pm.text(ev.get("slug"), 240), "icon": row["icon"] or pm.text(raw.get("icon"), 600),
                "outcome": row["outcome"] or (c["outcomes"][idx]["name"] if idx is not None else ""),
                "outcome_index": row["outcome_index"] if row["outcome_index"] is not None else idx}

    def copy(self, db, row, seen_at):
        try:
            row = self.complete(row)
        except (pm.UpstreamError, LookupError, ValueError):
            pass  # the copy is then recorded as failed with the reason, as before
        self._prefetch(row)
        cts = store.rows(db.execute("""SELECT c.*, a.user_id AS _uid FROM copy_trades c JOIN accounts a ON a.id=c.account_id
                                       WHERE lower(c.leader)=? AND c.paused=0 AND a.paused=0 AND c.created <= ?""",
                                    (row["proxy_wallet"], row["timestamp"] + 5)))
        infos = {}
        for ct in cts:
            uid = ct.pop("_uid")
            if copytrade.handle(db, ct, row, uid, infos=infos, source="stream", detected=seen_at):
                self.stats["copied"] += 1

    def _prefetch(self, row):
        """Read the market terms and the book at the same time (two network calls in parallel, not in a row)."""
        tok, cond = row.get("token_id"), row.get("condition_id")
        if not tok or not cond:
            return
        try:
            cached = trading.market_info_cached(cond, tok)
        except ValueError:
            return  # malformed ids: the copy records the error itself
        jobs = [PREFETCH.submit(books.prefetch, tok)]
        if not cached:
            jobs.append(PREFETCH.submit(trading.market_info, cond, tok))
        for j in jobs:
            try:
                j.result(timeout=15)
            except Exception:  # noqa: BLE001 - the copy itself fetches again and records any error
                pass

    # ---------------------------------------------------------------- stream
    def _refresh_leaders(self, db):
        self.leaders = followed(db)

    def _session(self, db):
        ws = WebSocket(URL, timeout=15)
        ws.settimeout(2)
        self._ws = ws
        ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}))
        self.connected, self.last_rx = True, time.monotonic()
        self.stats["connects"] += 1
        self.stats["since"] = time.time()
        last_ping = last_leaders = time.monotonic()
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if now - last_ping >= PING_EVERY:
                    ws.send("PING")
                    last_ping = now
                if now - last_leaders >= LEADERS_EVERY:
                    self._refresh_leaders(db)
                    last_leaders = now
                try:
                    msg = ws.recv()
                except TimeoutError:
                    if time.monotonic() - self.last_rx > STALE_AFTER:
                        raise WsError("Поток сделок молчит") from None
                    continue
                self.last_rx = time.monotonic()
                if not msg or msg[0] != "{":
                    continue
                self.stats["messages"] += 1
                # cheap pre-filter before parsing: most of the feed is other wallets
                if not self.leaders or '"proxyWallet"' not in msg:
                    continue
                try:
                    payload = json.loads(msg).get("payload") or {}
                except ValueError:
                    continue
                wallet = str(payload.get("proxyWallet") or "").lower()
                if wallet not in self.leaders:
                    continue
                seen_at = time.time()
                row = to_row(payload)
                self.lags.append(max(0.0, seen_at - row["timestamp"]))
                self.stats["matched"] += 1
                try:
                    self.queues[int(wallet[-6:], 16) % WORKERS].put_nowait((row, seen_at))
                except queue.Full:
                    self.stats["errors"] += 1
                    self.stats["error"] = "Очередь копий переполнена"
        finally:
            self.connected = False
            ws.close()
            self.stats["wire"] += ws.rx_wire
            self.stats["plain"] += ws.rx_plain
            self._ws = None

    def run(self):
        for w in self.workers:
            w.start()
        db = store.connect(self.path)
        delay = 1.0
        while not self.stop.is_set():
            self._refresh_leaders(db)
            if not self.leaders:            # nobody to follow: no connection at all
                self.stop.wait(LEADERS_EVERY)
                continue
            started = time.monotonic()
            try:
                self._session(db)
            except (OSError, WsError, ValueError) as exc:
                self.stats["errors"] += 1
                self.stats["error"] = str(exc)[:200]
                LOG.info("trade stream: %s", exc)
            if time.monotonic() - started > 60:
                delay = 1.0
            self.stop.wait(delay)
            delay = min(30.0, delay * 2)
        db.close()

    def status(self):
        lags = sorted(self.lags)
        return {"connected": self.connected and time.monotonic() - self.last_rx <= STALE_AFTER, "leaders": len(self.leaders),
                "queue": sum(q.qsize() for q in self.queues), "lag_median": round(lags[len(lags) // 2], 2) if lags else None,
                "lag_p90": round(lags[int(len(lags) * 0.9)], 2) if lags else None, **self.stats, **self._live_bytes()}

    def _live_bytes(self):
        ws = getattr(self, "_ws", None)
        return {"wire": self.stats["wire"] + (ws.rx_wire if ws else 0), "plain": self.stats["plain"] + (ws.rx_plain if ws else 0),
                "compressed": bool(ws and ws.deflate)}


STREAM = None


def start(path):
    global STREAM
    if STREAM is None:
        STREAM = TradeStream(path)
        STREAM.start()
    return STREAM


def connected():
    return STREAM is not None and STREAM.status()["connected"]


def status():
    return STREAM.status() if STREAM is not None else {"connected": False, "leaders": 0}
