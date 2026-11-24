"""Crypto "Up or Down" rounds: the live asset price (Chainlink, as Polymarket resolves them) and the round's data.

One WebSocket to Polymarket's real-time data service carries Chainlink prices of all supported coins about once a
second. It is opened only while someone looks at a round page (`want`) and closed ten minutes after the last one;
the last two hours stay in memory, so a page opened later gets the history at once. Prices reach open pages over
/api/live (bus event "crypto").

The same connection carries the trades of the rounds being looked at (RTDS activity/trades, filtered by the round's
event slug as polymarket.com does), so a round page shows the buys floating up ("+ $5", bus event "trade").

Round data (target price, history, past rounds) comes from the same endpoints polymarket.com uses for its chart.
"""

import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import bus, views
from . import polymarket as pm
from .ws import WebSocket, WsError

URL = "wss://ws-live-data.polymarket.com"
SITE = "https://polymarket.com/api"
SYMBOLS = views.ROUND_SYMBOLS
NAMES = {"btc": "Биткоин", "eth": "Эфириум", "sol": "Солана", "xrp": "XRP", "doge": "Догикоин", "bnb": "BNB",
         "hype": "Hyperliquid", "zec": "Zcash"}
PERIODS = {"5m": (300, "fiveminute"), "15m": (900, "fifteenminute"), "1h": (3600, "hourly"),
           "4h": (14400, "fourhourly"), "1d": (86400, "daily")}
SLUG = re.compile(r"^([a-z]+)-updown-(5m|15m|1h|4h|1d)-(\d{9,11})$")
KEEP = 2 * 3600            # seconds of live prices kept per coin
IDLE_CLOSE = 600           # close the stream this long after the last viewer
STALE_AFTER = 20
MAX_POINTS = 900           # a round's history is thinned to this many points (a day has 86 400 seconds)
ROUND_TRADES_FOR = 90      # seconds a round's trades stay subscribed after the last viewer asked (pages re-ask every 30)
MIN_TRADE = 1.0            # dollars: smaller buys are not shown
LOG = logging.getLogger("kontur.crypto")


def parse(slug):
    """{symbol, period, start, end, variant} of a round slug such as btc-updown-5m-1795519500, or None."""
    m = SLUG.match(str(slug or ""))
    if not m or m.group(1) not in SYMBOLS:
        return None
    span, variant = PERIODS[m.group(2)]
    start = int(m.group(3))
    return {"symbol": m.group(1), "period": m.group(2), "start": start, "end": start + span, "variant": variant}


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PriceStream(threading.Thread):
    def __init__(self):
        super().__init__(name="kontur-crypto", daemon=True)
        self.lock = threading.Lock()
        self.prices = {s: deque(maxlen=KEEP + 60) for s in SYMBOLS}   # (second, price)
        self.wanted_until = 0.0
        self.rounds = {}              # round slug -> monotonic time its trades are wanted until
        self.wake = threading.Event()
        self.connected = False
        self.stats = {"connects": 0, "messages": 0, "trades": 0, "errors": 0, "error": ""}

    def want(self, round_slug=None):
        self.wanted_until = time.monotonic() + IDLE_CLOSE
        if round_slug and parse(round_slug):
            self.rounds[round_slug] = time.monotonic() + ROUND_TRADES_FOR
        self.wake.set()

    def series(self, symbol, start, end):
        with self.lock:
            return [[t, v] for t, v in self.prices.get(symbol, ()) if start <= t <= end]

    def last(self, symbol):
        with self.lock:
            q = self.prices.get(symbol)
            return list(q[-1]) if q else None

    def _add(self, symbol, ts, value):
        t = int(ts)
        with self.lock:
            q = self.prices[symbol]
            if q and q[-1][0] >= t:
                if q[-1][0] == t:
                    q[-1] = (t, value)
                return
            q.append((t, value))

    def _follow_rounds(self, ws, subscribed):
        """Subscribe to the trades of newly watched rounds and drop the ones nobody looks at any more. Only this
        thread writes to the socket (it also reads from it)."""
        now = time.monotonic()
        for slug in [s for s, until in list(self.rounds.items()) if until <= now]:
            self.rounds.pop(slug, None)
            if slug in subscribed:
                subscribed.discard(slug)
                ws.send(json.dumps({"action": "unsubscribe", "subscriptions": [trades_subscription(slug)]}))
        new = [s for s in list(self.rounds) if s not in subscribed]
        if new:
            ws.send(json.dumps({"action": "subscribe", "subscriptions": [trades_subscription(s) for s in new]}))
            subscribed.update(new)

    def _trade(self, p):
        """A buy in a watched round -> bus "trade" {e: slug, o: up|down, u: dollars, t: seconds}."""
        slug = str(p.get("eventSlug") or "")
        if slug not in self.rounds or str(p.get("side") or "").upper() != "BUY":
            return
        size, price = pm.num(p.get("size")), pm.num(p.get("price"))
        outcome = str(p.get("outcome") or "").lower()
        if not size or price is None or outcome not in ("up", "down"):
            return
        usd = size * price
        if usd < MIN_TRADE:
            return                      # the other half of a matched pair ($0.05 at 0.1¢) says nothing
        ts = pm.num(p.get("timestamp")) or time.time()
        self.stats["trades"] += 1
        bus.publish("trade", {"e": slug, "o": outcome, "u": round(usd, 2), "t": int(ts / 1000 if ts > 1e12 else ts)})

    def _session(self):
        ws = WebSocket(URL, timeout=15)
        ws.settimeout(2)
        ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*"}]}))
        self.connected, last_rx, last_ping = True, time.monotonic(), time.monotonic()
        self.stats["connects"] += 1
        subscribed = set()
        try:
            while time.monotonic() < self.wanted_until:
                self._follow_rounds(ws, subscribed)
                if time.monotonic() - last_ping >= 5:
                    ws.send("PING")
                    last_ping = time.monotonic()
                try:
                    msg = ws.recv()
                except TimeoutError:
                    if time.monotonic() - last_rx > STALE_AFTER:
                        raise WsError("Поток цен Chainlink молчит") from None
                    continue
                last_rx = time.monotonic()
                if not msg or msg[0] != "{":
                    continue
                try:
                    d = json.loads(msg)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                p = d.get("payload") or {}
                self.stats["messages"] += 1
                if d.get("topic") == "activity":
                    self._trade(p)
                    continue
                if isinstance(p.get("data"), list):          # the snapshot sent on subscribe: the last minute
                    sym = str(p.get("symbol") or "").split("/")[0]
                    for x in p["data"]:
                        if sym in SYMBOLS and pm.num(x.get("value")):
                            self._add(sym, x["timestamp"] / 1000, float(x["value"]))
                    continue
                sym = str(p.get("symbol") or "").split("/")[0]
                v, ts = pm.num(p.get("value")), pm.num(p.get("timestamp"))
                if sym in SYMBOLS and v and ts:
                    self._add(sym, ts / 1000, v)
                    bus.publish("crypto", {"s": sym, "t": int(ts / 1000), "v": v})
        finally:
            self.connected = False
            ws.close()

    def run(self):
        delay = 1.0
        while True:
            if time.monotonic() >= self.wanted_until:
                self.wake.wait(60)
                self.wake.clear()
                continue
            started = time.monotonic()
            try:
                self._session()
            except (OSError, WsError, ValueError) as exc:
                self.stats["errors"] += 1
                self.stats["error"] = str(exc)[:200]
                LOG.info("crypto stream: %s", exc)
            if time.monotonic() - started > 60:
                delay = 1.0
            time.sleep(delay)
            delay = min(30.0, delay * 2)


STREAM = None
_start_lock = threading.Lock()


def trades_subscription(slug):
    # the filter is a JSON string and RTDS only understands it written without spaces (as polymarket.com sends it)
    return {"topic": "activity", "type": "trades", "filters": json.dumps({"event_slug": slug}, separators=(",", ":"))}


def want(round_slug=None):
    """Someone looks at a round: keep (or start) the live prices, and the round's trades when it is given."""
    global STREAM
    with _start_lock:
        if STREAM is None:
            STREAM = PriceStream()
            STREAM.start()
    STREAM.want(round_slug)


def status():
    return {"connected": STREAM.connected if STREAM else False, **(STREAM.stats if STREAM else {})}


# ---------------------------------------------------------------- round data

_targets = {}            # (symbol, start) -> open price; fixed once published
_past = {}               # (symbol, variant, start) -> (monotonic, results)
_cache_lock = threading.Lock()


def _thin(points, limit=MAX_POINTS):
    if len(points) <= limit:
        return points
    step = len(points) / limit
    out = [points[int(i * step)] for i in range(limit)]
    out[-1] = points[-1]
    return out


def round_info(slug):
    """Everything the round chart needs: target price, price history since the start, past rounds."""
    r = parse(slug)
    if not r:
        raise LookupError("Это не раунд «вверх или вниз» по криптовалюте")
    want()
    sym, start, end, variant = r["symbol"], r["start"], r["end"], r["variant"]
    q = {"symbol": sym.upper(), "eventStartTime": _iso(start), "variant": variant, "endDate": _iso(end),
         "twapEnabled": "true", "twapLookbackSeconds": 60}
    key = (sym, start)
    target = _targets.get(key)
    if target is None and time.time() >= start:
        try:
            got = pm.fetch(SITE + "/crypto/crypto-price?" + urlencode(q), gap=0.1)
            target = pm.num(got.get("openPrice"))
            if target:
                with _cache_lock:
                    if len(_targets) > 5000:
                        _targets.clear()
                    _targets[key] = target
        except (pm.UpstreamError, LookupError, ValueError, AttributeError):
            target = None
    now = time.time()
    lo = start - 30
    finished = now >= end
    history = []
    try:
        # a round being played: the last couple of minutes ("history seed"); a finished one: the whole round
        # (Polymarket answers with 61 points from start to end, as its own chart of a finished round shows)
        extra = {"twapEnabled": "false"} if finished else {"twapEnabled": "false", "historySeed": "true"}
        raw = pm.fetch(SITE + "/crypto/price-history?" + urlencode({**q, **extra}), gap=0.1)
        history = [[int(x["timestamp"] / 1000), float(x["value"])] for x in raw if isinstance(x, dict) and pm.num(x.get("value"))]
    except (pm.UpstreamError, LookupError, ValueError, TypeError):
        history = []
    # The server's own stream is the source; Polymarket's history only fills the time before its first point.
    # Mixing the two second by second draws spikes: their clocks differ by up to a minute.
    own = STREAM.series(sym, lo, min(end, now)) if STREAM else []
    if finished and own and own[0][0] > start + 5:
        own = []          # the stream saw only part of a finished round: Polymarket's whole round reads better
    first = own[0][0] if own else float("inf")
    merged = dict((t, v) for t, v in history if lo <= t < first)
    merged.update(own)
    points = _thin(sorted(merged.items()))
    pkey = (sym, variant, start)
    with _cache_lock:
        hit = _past.get(pkey)
    past = hit[1] if hit and time.monotonic() - hit[0] < 60 else None
    if past is None:
        try:
            got = pm.fetch(SITE + "/past-results?" + urlencode({"symbol": sym.upper(), "variant": variant, "assetType": "crypto",
                                                                  "currentEventStartTime": _iso(start), "count": 6,
                                                                  "endDate": _iso(end), "twapEnabled": "true",
                                                                  "twapLookbackSeconds": 60}), gap=0.1)
            past = [{"start": x.get("startTime"), "outcome": x.get("outcome"), "open": pm.num(x.get("openPrice")),
                     "close": pm.num(x.get("closePrice"))} for x in (got.get("data") or {}).get("results") or []]
        except (pm.UpstreamError, LookupError, ValueError, AttributeError):
            past = []
        with _cache_lock:
            _past[pkey] = (time.monotonic(), past)
    span = PERIODS[r["period"]][0]
    current = start + max(0, int((now - start) // span)) * span      # the round being played now (same grid)
    return {**r, "name": NAMES.get(sym, sym.upper()), "target": target, "points": [[t, round(v, 6)] for t, v in points],
            "past": past, "now": now, "finished": finished, "prev": f"{sym}-updown-{r['period']}-{start - span}",
            "next": f"{sym}-updown-{r['period']}-{start + span}", "live": f"{sym}-updown-{r['period']}-{current}"}
