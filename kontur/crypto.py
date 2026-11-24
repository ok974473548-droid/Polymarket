"""Crypto "Up or Down" rounds: the live asset price (Chainlink, as Polymarket resolves them) and the round's data.

One WebSocket to Polymarket's real-time data service carries Chainlink prices of all supported coins about once a
second. It is opened only while someone looks at a round page (`want`) and closed ten minutes after the last one;
the last two hours stay in memory, so a page opened later gets the history at once. Prices reach open pages over
/api/live (bus event "crypto").

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

from . import bus
from . import polymarket as pm
from .ws import WebSocket, WsError

URL = "wss://ws-live-data.polymarket.com"
SITE = "https://polymarket.com/api"
SYMBOLS = {"btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "zec"}
NAMES = {"btc": "Биткоин", "eth": "Эфириум", "sol": "Солана", "xrp": "XRP", "doge": "Догикоин", "bnb": "BNB",
         "hype": "Hyperliquid", "zec": "Zcash"}
PERIODS = {"5m": (300, "fiveminute"), "15m": (900, "fifteenminute"), "1h": (3600, "hourly"),
           "4h": (14400, "fourhourly"), "1d": (86400, "daily")}
SLUG = re.compile(r"^([a-z]+)-updown-(5m|15m|1h|4h|1d)-(\d{9,11})$")
KEEP = 2 * 3600            # seconds of live prices kept per coin
IDLE_CLOSE = 600           # close the stream this long after the last viewer
STALE_AFTER = 20
MAX_POINTS = 900           # a round's history is thinned to this many points (a day has 86 400 seconds)
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
        self.wake = threading.Event()
        self.connected = False
        self.stats = {"connects": 0, "messages": 0, "errors": 0, "error": ""}

    def want(self):
        self.wanted_until = time.monotonic() + IDLE_CLOSE
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

    def _session(self):
        ws = WebSocket(URL, timeout=15)
        ws.settimeout(2)
        ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*"}]}))
        self.connected, last_rx, last_ping = True, time.monotonic(), time.monotonic()
        self.stats["connects"] += 1
        try:
            while time.monotonic() < self.wanted_until:
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
                    p = json.loads(msg).get("payload") or {}
                except ValueError:
                    continue
                self.stats["messages"] += 1
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


def want():
    """Someone looks at a round: keep (or start) the live prices."""
    global STREAM
    with _start_lock:
        if STREAM is None:
            STREAM = PriceStream()
            STREAM.start()
    STREAM.want()


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
    history = []
    try:
        raw = pm.fetch(SITE + "/crypto/price-history?" + urlencode({**q, "twapEnabled": "false", "historySeed": "true"}), gap=0.1)
        history = [[int(x["timestamp"] / 1000), float(x["value"])] for x in raw if isinstance(x, dict) and pm.num(x.get("value"))]
    except (pm.UpstreamError, LookupError, ValueError, TypeError):
        history = []
    # The server's own stream is the source; Polymarket's history only fills the time before its first point.
    # Mixing the two second by second draws spikes: their clocks differ by up to a minute.
    own = STREAM.series(sym, lo, min(end, now)) if STREAM else []
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
    return {**r, "name": NAMES.get(sym, sym.upper()), "target": target, "points": [[t, round(v, 6)] for t, v in points],
            "past": past, "now": now, "prev": f"{sym}-updown-{r['period']}-{start - span}",
            "next": f"{sym}-updown-{r['period']}-{start + span}"}
