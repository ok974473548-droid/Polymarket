"""Read-only client for Polymarket Gamma, CLOB and Data API v2 plus DefiLlama volume.

Every reply is normalised to the small set of fields the interface uses. Successful reads
are cached in SQLite; a failed refresh serves the last good copy marked as stale.
"""

import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import net

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com/v2/"
LLAMA = "https://api.llama.fi/summary/dexs/polymarket-international"

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
CONDITION = re.compile(r"0x[0-9a-fA-F]{64}\Z")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,239}\Z")
TOKEN = re.compile(r"[0-9]{1,90}\Z")
MAX_READ = 16 * 1024 * 1024
USER_AGENT = "KonturMarket/1.0 (+local analytics)"

LEADER_CATEGORIES = ["overall", "politics", "sports", "crypto", "finance", "tech", "economy", "culture",
                     "weather", "mentions", "esports"]
MARKET_CATEGORIES = ["politics", "sports", "crypto", "esports", "finance", "geopolitics", "tech", "culture",
                     "economy", "weather", "ai", "elections", "mentions"]


class UpstreamError(RuntimeError):
    """The public source failed or answered in an unexpected format."""


_pace_lock = threading.Lock()
_last_call = {}


def _pace(host, gap):
    """Keep a minimum gap between calls to one host (background work is polite)."""
    with _pace_lock:
        wait = _last_call.get(host, 0) + gap - time.monotonic()
        _last_call[host] = max(time.monotonic(), _last_call.get(host, 0) + gap)
    if wait > 0:
        time.sleep(wait)


def _cool_down(host, seconds):
    """After HTTP 429 every paced caller of this host waits, not only the thread that got it."""
    with _pace_lock:
        _last_call[host] = max(_last_call.get(host, 0), time.monotonic() + seconds)


def fetch(url, *, accept="application/json", timeout=15, gap=0.0, raw=False, limit=MAX_READ):
    """GET a Polymarket (or other public) URL over a pooled keep-alive connection; JSON by default.

    Redirects are not followed. 404 raises LookupError, other failures UpstreamError; HTTP 429 waits
    (Retry-After) for every paced caller of the host and retries.
    """
    host = url.split("/")[2]
    if gap:
        _pace(host, gap)
    tries = 3 if gap else 2
    for attempt in range(tries):
        try:
            status, headers, body = net.get(url, headers={"Accept": accept, "User-Agent": USER_AGENT},
                                            timeout=timeout, limit=limit)
        except net.NetError as exc:
            raise UpstreamError("Источник недоступен: проверьте подключение к интернету") from exc
        if status == 429 and attempt < tries - 1:
            retry = headers.get("Retry-After")
            wait = min(6.0, float(retry)) if retry and retry.replace(".", "", 1).isdigit() else 2.0 * (attempt + 1)
            _cool_down(host, wait)
            time.sleep(wait)
            continue
        if status == 404:
            raise LookupError("Polymarket не нашёл запрошенный объект")
        if status >= 300:
            raise UpstreamError(f"Источник ответил HTTP {status}")
        break
    if len(body) > limit:
        raise UpstreamError("Слишком большой ответ источника")
    if raw:
        return body, headers.get("Content-Type", "")
    try:
        return json.loads(body)
    except ValueError as exc:
        raise UpstreamError("Источник вернул некорректный JSON") from exc


# ---------------------------------------------------------------- caching

def cached(db, key, ttl, loader):
    """Return {'data', 'fetched', 'stale', 'error'}; reuse a fresh copy or fall back to a stale one."""
    now = time.time()
    row = db.execute("SELECT stamp, body FROM cache WHERE key=?", (key,)).fetchone()
    if row and 0 <= now - row["stamp"] < ttl:
        return {"data": json.loads(row["body"]), "fetched": row["stamp"], "stale": False, "error": None}
    try:
        data = loader()
    except (UpstreamError, LookupError, ValueError, KeyError, TypeError) as exc:
        if row:
            return {"data": json.loads(row["body"]), "fetched": row["stamp"], "stale": True, "error": str(exc)}
        raise
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    db.execute("INSERT INTO cache VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET stamp=excluded.stamp, body=excluded.body",
               (key, now, body))
    if int(now) % 50 == 0:
        db.execute("DELETE FROM cache WHERE stamp < ?", (now - 3 * 86400,))
    return {"data": data, "fetched": now, "stale": False, "error": None}


# ---------------------------------------------------------------- helpers

def num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return n if math.isfinite(n) else None


def text(value, limit=300):
    return str(value or "")[:limit]


def array(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


def address(value):
    if not isinstance(value, str) or not ADDRESS.fullmatch(value.strip()):
        raise ValueError("Некорректный адрес кошелька: нужен 0x и 40 шестнадцатеричных символов")
    return value.strip().lower()


def condition(value):
    if not isinstance(value, str) or not CONDITION.fullmatch(value):
        raise ValueError("Некорректный идентификатор рынка")
    return value.lower()


def token(value):
    value = str(value or "")
    if not TOKEN.fullmatch(value):
        raise ValueError("Некорректный идентификатор исхода")
    return value


def slug(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError("Некорректный адрес события")
    return value


def iso_to_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def data_url(route, params):
    return DATA + route + "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})


def gamma_url(route, params=None):
    return GAMMA + route + ("?" + urlencode(params, doseq=True) if params else "")


# ---------------------------------------------------------------- Gamma

def fee_terms(market):
    """Fee rate, exponent and taker-only flag from a Gamma market (rate 0 when fees are off)."""
    schedule = market.get("feeSchedule")
    if market.get("feesEnabled") is True and isinstance(schedule, dict):
        rate = num(schedule.get("rate")) or 0.0
        exponent = num(schedule.get("exponent")) or 1.0
        return {"rate": max(0.0, min(rate, 1.0)), "exponent": exponent,
                "taker_only": schedule.get("takerOnly") is not False}
    return {"rate": 0.0, "exponent": 1.0, "taker_only": True}


def contract(item):
    cond = text(item.get("conditionId"), 80).lower()
    if not CONDITION.fullmatch(cond):
        return None
    names = array(item.get("outcomes"))
    prices = array(item.get("outcomePrices"))
    tokens = array(item.get("positionIds") if item.get("version") == "v2" else item.get("clobTokenIds"))
    sides = []
    for i, name in enumerate(names):
        tok = text(tokens[i] if i < len(tokens) else "", 100)
        price = num(prices[i]) if i < len(prices) else None
        sides.append({"name": text(name, 120), "price": price if price is not None and 0 <= price <= 1 else None,
                      "token": tok if TOKEN.fullmatch(tok) else None})
    fees = fee_terms(item)
    return {
        "condition": cond,
        "slug": text(item.get("slug"), 240),
        "question": text(item.get("question"), 700),
        "label": text(item.get("groupItemTitle"), 200),
        "image": text(item.get("image") or item.get("icon"), 600),
        "outcomes": sides,
        "volume": num(item.get("volume")),
        "volume24h": num(item.get("volume24hr")),
        "change24h": num(item.get("oneDayPriceChange")),
        "best_bid": num(item.get("bestBid")),
        "best_ask": num(item.get("bestAsk")),
        "last": num(item.get("lastTradePrice")),
        "closed": item.get("closed") is True,
        "active": item.get("active") is not False,
        "accepting": item.get("acceptingOrders") is not False and item.get("enableOrderBook") is not False,
        "end": text(item.get("endDate"), 40),
        "fee_rate": fees["rate"],
        "fee_exponent": fees["exponent"],
        "taker_only": fees["taker_only"],
        "min_size": num(item.get("orderMinSize")) or 5,
        "tick": num(item.get("orderPriceMinTickSize")) or 0.01,
        "neg_risk": item.get("negRisk") is True,
        "rules": text(item.get("description"), 4000),
        "resolution": text(item.get("resolutionSource"), 400),
    }


def event(raw, full=True):
    if not isinstance(raw, dict) or not SLUG.fullmatch(text(raw.get("slug"), 240)):
        raise ValueError("Событие без допустимого адреса")
    contracts = [c for c in (contract(m) for m in raw.get("markets") or [] if isinstance(m, dict)) if c]
    open_first = sorted(contracts, key=lambda c: (c["closed"], -(c["outcomes"][0]["price"] or 0) if c["outcomes"] else 0))
    obj = {
        "id": text(raw.get("id"), 30),
        "slug": raw["slug"],
        "title": text(raw.get("title"), 700),
        "image": text(raw.get("image") or raw.get("icon"), 600),
        "tags": [{"slug": text(t.get("slug"), 120), "label": text(t.get("label"), 120)}
                 for t in raw.get("tags") or [] if isinstance(t, dict) and t.get("slug")],
        "end": text(raw.get("endDate"), 40),
        "start": text(raw.get("startDate"), 40),
        "created": text(raw.get("createdAt"), 40),
        "volume": num(raw.get("volume")),
        "volume24h": num(raw.get("volume24hr")),
        "liquidity": num(raw.get("liquidity")),
        "oi": num(raw.get("openInterest")),
        "closed": raw.get("closed") is True,
        "live": raw.get("live") is True,          # a match being played right now
        "neg_risk": raw.get("negRisk") is True,
        "count": len(contracts),
        "contracts": open_first,
    }
    if full:
        obj["rules"] = text(raw.get("description"), 6000)
        obj["resolution"] = text(raw.get("resolutionSource"), 400)
    else:
        obj["contracts"] = [{k: v for k, v in c.items() if k not in {"rules", "resolution"}} for c in open_first[:2]]
    return obj


ORDERS = {"volume": "volume24hr", "oi": "openInterest", "new": "startDate", "liquidity": "liquidity"}


def catalog(db, *, order="oi", tag="", offset=0, limit=20):
    order = ORDERS.get(order, "openInterest")
    params = {"active": "true", "closed": "false", "archived": "false", "limit": limit, "offset": offset,
              "order": order, "ascending": "false"}
    if tag:
        params["tag_slug"] = slug(tag)
    url = gamma_url("/events", params)

    def load():
        rows = fetch(url)
        if not isinstance(rows, list):
            raise UpstreamError("Gamma изменила формат каталога")
        return [event(r, full=False) for r in rows if isinstance(r, dict) and r.get("closed") is not True]
    return cached(db, url, 60, load)


def event_by_slug(db, value):
    url = GAMMA + "/events/slug/" + slug(value)
    return cached(db, url, 30, lambda: event(fetch(url)))


def markets_by_condition(conditions, closed=False):
    """Fresh (uncached) state of up to 20 markets, used for settlement and order checks.

    Gamma returns either open or closed markets here, never both: without closed=true a resolved market is
    simply missing (until 1.4.4 settlement asked without it and never saw a resolved market).
    """
    params = [("condition_ids", c) for c in conditions] + [("limit", 50)] + ([("closed", "true")] if closed else [])
    rows = fetch(GAMMA + "/markets?" + urlencode(params), gap=0.2)
    return {text(r.get("conditionId"), 80).lower(): r for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}


_token_markets = {}


def market_by_token(tok):
    """Gamma market that an outcome token belongs to (kept in memory: a token never changes market)."""
    tok = token(tok)
    if tok in _token_markets:
        return _token_markets[tok]
    raw = None
    for closed in ((), (("closed", "true"),)):  # Gamma lists a closed market only when asked for closed ones
        rows = fetch(GAMMA + "/markets?" + urlencode((("clob_token_ids", tok),) + closed), gap=0.2)
        raw = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if raw is not None:
            break
    if raw is None:
        raise LookupError("Рынок этого исхода не найден")
    if len(_token_markets) > 5000:
        _token_markets.clear()
    _token_markets[tok] = raw
    return raw


def search(db, phrase, limit=8):
    phrase = phrase.strip()
    if not 1 <= len(phrase) <= 120:
        raise ValueError("Введите от 1 до 120 символов")
    url = gamma_url("/public-search", {"q": phrase, "events_status": "active", "limit_per_type": limit,
                                        "search_profiles": "true", "search_tags": "true", "keep_closed_markets": 0})

    def load():
        raw = fetch(url)
        return {
            "events": [event(r, full=False) for r in raw.get("events") or [] if isinstance(r, dict) and not r.get("closed")],
            "profiles": [{"wallet": text(p.get("proxyWallet"), 60).lower(), "name": text(p.get("name") or p.get("pseudonym"), 120),
                          "image": text(p.get("profileImage"), 600)}
                         for p in raw.get("profiles") or [] if isinstance(p, dict) and ADDRESS.fullmatch(text(p.get("proxyWallet"), 60))],
            "tags": [{"slug": text(t.get("slug"), 120), "label": text(t.get("label"), 120), "count": num(t.get("event_count"))}
                     for t in raw.get("tags") or [] if isinstance(t, dict) and t.get("slug")],
        }
    return cached(db, url, 120, load)


def tags(db, *, offset=0, limit=50):
    url = gamma_url("/tags", {"limit": limit, "offset": offset, "order": "label", "ascending": "true"})

    def load():
        rows = fetch(url)
        if not isinstance(rows, list):
            raise UpstreamError("Gamma изменила формат тегов")
        return [{"slug": text(t.get("slug"), 120), "label": text(t.get("label"), 120),
                 "count": num(t.get("event_count") or t.get("eventCount"))} for t in rows if isinstance(t, dict) and t.get("slug")]
    return cached(db, url, 3600, load)


TAGS_TTL = 7 * 86400


def _store_tags(db, event_slug, found):
    db.execute("INSERT INTO event_tags VALUES(?,?,?) ON CONFLICT(event_slug) DO UPDATE SET tags=excluded.tags, at=excluded.at",
               (event_slug, json.dumps(found), time.time()))


def event_tags(db, event_slug):
    """Tag slugs of an event, cached for a week (used to group a trader's positions by category)."""
    return events_tags(db, [event_slug]).get(event_slug, [])


def events_tags(db, slugs):
    """Tag slugs of many events at once: cached ones from the database, the rest 20 per request without
    their markets (`include_markets=false`: an event without its markets weighs ~5 KB instead of 30+ KB)."""
    out, missing, stale = {}, [], {}
    now = time.time()
    for s in dict.fromkeys(x for x in slugs if x):
        row = db.execute("SELECT tags, at FROM event_tags WHERE event_slug=?", (s,)).fetchone()
        if row and now - row["at"] < TAGS_TTL:
            out[s] = json.loads(row["tags"])
        else:
            missing.append(s)
            if row:
                stale[s] = json.loads(row["tags"])
    for i in range(0, len(missing), 20):
        part = [slug(s) for s in missing[i:i + 20]]
        try:
            rows = fetch(GAMMA + "/events?" + urlencode([("slug", s) for s in part] + [("include_markets", "false"), ("limit", 50)]),
                         gap=0.15)
        except (UpstreamError, LookupError, ValueError):
            rows = []
        got = {r.get("slug"): r for r in rows if isinstance(r, dict)} if isinstance(rows, list) else {}
        for s in part:
            if s in got:
                found = [text(t.get("slug"), 120) for t in got[s].get("tags") or [] if isinstance(t, dict)]
            else:  # not in the batch answer: the single request decides, exactly as before
                try:
                    raw = fetch(GAMMA + "/events/slug/" + s, gap=0.15)
                    found = [text(t.get("slug"), 120) for t in raw.get("tags") or [] if isinstance(t, dict)]
                except (UpstreamError, LookupError, ValueError):
                    if s in stale:
                        out[s] = stale[s]
                        continue
                    found = []
            _store_tags(db, s, found)
            out[s] = found
    return out


# ---------------------------------------------------------------- CLOB

def midpoints(tokens):
    """Mid prices of many outcome tokens in one small request (POST /midpoints, 100 per call): ~45 bytes a token
    instead of a whole order book. Tokens the CLOB has no mid for are simply absent."""
    out = {}
    toks = [token(t) for t in dict.fromkeys(tokens)]
    for i in range(0, len(toks), 100):
        try:
            status, _, body = net.post_json(CLOB + "/midpoints", [{"token_id": t} for t in toks[i:i + 100]], timeout=10)
        except net.NetError as exc:
            raise UpstreamError("Источник недоступен: проверьте подключение к интернету") from exc
        if status != 200:
            raise UpstreamError(f"Источник ответил HTTP {status}")
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise UpstreamError("Источник вернул некорректный JSON") from exc
        for t, v in (data.items() if isinstance(data, dict) else []):
            p = num(v)
            if p is not None and 0 <= p <= 1:
                out[str(t)] = p
    return out


def book(tok):
    """Current order book of one outcome token: asks ascending, bids descending."""
    raw = fetch(CLOB + "/book?token_id=" + token(tok), timeout=10)
    if not isinstance(raw, dict) or str(raw.get("asset_id")) != str(tok):
        raise UpstreamError("Стакан не соответствует запрошенному исходу")

    def levels(items, reverse):
        out = {}
        for level in items or []:
            price, size = num(level.get("price")), num(level.get("size"))
            if price is not None and size and 0 < price < 1 and size > 0:
                out[price] = out.get(price, 0) + size
        return [[p, out[p]] for p in sorted(out, reverse=reverse)]
    return {"token": str(tok), "asks": levels(raw.get("asks"), False), "bids": levels(raw.get("bids"), True),
            "min_size": num(raw.get("min_order_size")), "tick": num(raw.get("tick_size")),
            "at": (num(raw.get("timestamp")) or time.time() * 1000) / 1000}


PRICE_WINDOWS = {"1h": ("1h", 1), "6h": ("6h", 1), "1d": ("1d", 5), "1w": ("1w", 30), "1m": ("1m", 180), "3m": ("max", 720), "ytd": ("max", 720),
                 "1y": ("max", 1440), "max": ("max", 1440)}


def prices(db, tok, window="max", start=None, end=None):
    tok = token(tok)
    if start and end:
        span = max(1, (end - start) / 86400)
        params = {"market": tok, "startTs": int(start), "endTs": int(end), "fidelity": max(1, int(span * 1440 // 400))}
    else:
        interval, fidelity = PRICE_WINDOWS.get(window, PRICE_WINDOWS["max"])
        params = {"market": tok, "interval": interval, "fidelity": fidelity}
    url = CLOB + "/prices-history?" + urlencode(params)

    def load():
        raw = fetch(url)
        points = [[int(r["t"]), float(r["p"])] for r in raw.get("history") or []
                  if num(r.get("t")) is not None and num(r.get("p")) is not None]
        points.sort()
        if window in {"3m", "ytd", "1y"} and not start:
            now = time.time()
            first = {"3m": now - 92 * 86400, "1y": now - 366 * 86400,
                     "ytd": datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc).timestamp()}[window]
            points = [p for p in points if p[0] >= first]
        return points
    return cached(db, url, 60, load)


def price_window(tok, start, end, *, fidelity=1, gap=0.0):
    """Raw price points of one outcome between two moments (no cache), for slippage checks."""
    params = {"market": token(tok), "startTs": int(start), "endTs": int(end), "fidelity": int(fidelity)}
    raw = fetch(CLOB + "/prices-history?" + urlencode(params), gap=gap)
    points = [[int(r["t"]), float(r["p"])] for r in raw.get("history") or []
              if num(r.get("t")) is not None and num(r.get("p")) is not None]
    points.sort()
    return points


# ---------------------------------------------------------------- Data API v2

def leaderboard(db, *, period="all", category="overall", sort="PNL", limit=50, cursor="", user="", ttl=600, gap=0.0):
    if category not in LEADER_CATEGORIES:
        raise ValueError("Неизвестная категория рейтинга")
    params = {"time_period": period if period in {"day", "week", "month", "all"} else "all",
              "category": category, "sort_by": "VOLUME" if sort == "VOLUME" else "PNL",
              "limit": limit, "cursor": cursor, "user": address(user) if user else ""}
    url = data_url("leaderboard", params)

    def load():
        raw = fetch(url, gap=gap)
        data = raw.get("data")
        if user:
            if not isinstance(data, dict):
                return None
            return {"pnl": num(data.get("pnl")), "volume": num(data.get("volume")),
                    "rank": data.get("rank_pnl"), "rank_volume": data.get("rank_volume")}
        rows = [{"rank": r.get("rank"), "wallet": text(r.get("user_id"), 60).lower(), "name": text(r.get("user_name"), 120),
                 "image": text(r.get("profile_image"), 600), "pnl": num(r.get("pnl")), "volume": num(r.get("volume"))}
                for r in data or [] if isinstance(r, dict) and ADDRESS.fullmatch(text(r.get("user_id"), 60))]
        return {"rows": rows, "next": (raw.get("pagination") or {}).get("next_cursor")}
    return cached(db, url, ttl, load)


POSITION_FIELDS = ("token_id condition_id outcome outcome_index title slug event_slug icon avg_price current_price "
                   "current_size current_value total_cost_usdc total_size realized_pnl unrealized_pnl total_pnl "
                   "percent_pnl status redeemable end_date last_event_at first_entry_at").split()


def _position(r):
    out = {k: r.get(k) for k in POSITION_FIELDS}
    for k in ("avg_price", "current_price", "current_size", "current_value", "total_cost_usdc", "total_size",
              "realized_pnl", "unrealized_pnl", "total_pnl", "percent_pnl"):
        out[k] = num(out[k])
    return out


def positions_page(*, user="", cond="", status="OPEN", sort="CURRENT_VALUE", direction="DESC", limit=100, cursor="",
                   event_id="", title="", start=None, gap=0.0):
    """One page of positions. start: only positions whose last event is at or after this time (epoch seconds)."""
    params = {"user": address(user) if user else "", "condition": condition(cond) if cond else "",
              "status": status, "sort_by": sort, "sort_direction": direction, "limit": limit, "cursor": cursor,
              "event_id": event_id, "title": title[:100], "start": int(start) if start else None}
    raw = fetch(data_url("positions", params), gap=gap)
    rows = [_position(r) for r in raw.get("data") or [] if isinstance(r, dict)]
    return {"rows": rows, "next": (raw.get("pagination") or {}).get("next_cursor") if (raw.get("pagination") or {}).get("has_more") else None}


def positions(db, **kw):
    key = "positions:" + json.dumps(kw, sort_keys=True)
    return cached(db, key, 45, lambda: positions_page(**kw))


TRADE_FIELDS = ("proxy_wallet name pseudonym profile_image side token_id condition_id size price timestamp title slug "
                "event_slug icon outcome outcome_index transaction_hash").split()


def trades_page(*, user="", cond="", event_id="", side="", limit=50, cursor="", taker_only=True, gap=0.0):
    params = {"user": address(user) if user else "", "condition": condition(cond) if cond else "",
              "event_id": event_id, "side": side if side in {"BUY", "SELL"} else "", "limit": limit,
              "cursor": cursor, "taker_only": "true" if taker_only else "false"}
    raw = fetch(data_url("trades", params), gap=gap)
    rows = []
    for r in raw.get("data") or []:
        if not isinstance(r, dict):
            continue
        row = {k: r.get(k) for k in TRADE_FIELDS}
        row["size"], row["price"] = num(row["size"]), num(row["price"])
        row["proxy_wallet"] = text(row["proxy_wallet"], 60).lower()
        rows.append(row)
    pag = raw.get("pagination") or {}
    return {"rows": rows, "next": pag.get("next_cursor") if pag.get("has_more") else None}


def trades(db, **kw):
    key = "trades:" + json.dumps(kw, sort_keys=True)
    return cached(db, key, 20, lambda: trades_page(**kw))


def holders(db, cond, limit=20):
    url = data_url("holders", {"condition": condition(cond), "limit": limit, "include_pnl": "true"})

    def load():
        raw = fetch(url)
        out = []
        for group in raw.get("data") or []:
            rows = []
            for h in group.get("holders") or []:
                rows.append({"wallet": text(h.get("proxy_wallet"), 60).lower(),
                             "name": text(h.get("name") or h.get("pseudonym"), 120),
                             "image": text(h.get("profile_image_optimized") or h.get("profile_image"), 600),
                             "shares": num(h.get("amount")), "avg": num(h.get("avg_price")),
                             "value": num(h.get("current_value")), "pnl": num(h.get("total_pnl")),
                             "pnl_pct": num(h.get("percent_pnl")), "outcome_index": h.get("outcome_index")})
            out.append({"token": text(group.get("token_id") or (group.get("holders") or [{}])[0].get("token_id"), 100),
                        "rows": rows})
        return out
    return cached(db, url, 60, load)


def user_stats(db, user, gap=0.0):
    url = data_url("user-stats", {"user": address(user)})

    def load():
        data = fetch(url, gap=gap).get("data")
        if not isinstance(data, dict):
            return None
        totals = data.get("all_time_pnl") or {}
        return {"markets": data.get("trades"), "biggest_win": num(data.get("biggest_win")),
                "join_date": num(data.get("join_date")), "pnl": num(totals.get("economic_pnl")),
                "realized": num(totals.get("realized_pnl")), "unrealized": num(totals.get("unrealized_pnl")),
                "volume": num(totals.get("volume_usdc")), "fees": num(totals.get("fees_paid")),
                "trade_count": totals.get("trade_count")}
    return cached(db, url, 300, load)


def user_pnl(db, user, interval="max", fidelity="1d", gap=0.0):
    interval = interval if interval in {"max", "all", "1m", "1w", "1d", "12h", "6h"} else "max"
    fidelity = fidelity if fidelity in {"1d", "18h", "12h", "3h", "1h"} else "1d"
    url = data_url("user-pnl", {"user": address(user), "interval": interval, "fidelity": fidelity})

    def load():
        data = fetch(url, gap=gap).get("data") or {}
        return [{"t": int(p.get("timestamp") or 0), "pnl": num(p.get("economic_pnl")), "realized": num(p.get("realized_pnl")),
                 "volume": num(p.get("volume_usdc"))} for p in data.get("points") or [] if isinstance(p, dict)]
    return cached(db, url, 300, load)


def value(db, user, gap=0.0):
    url = data_url("value", {"user": address(user)})
    return cached(db, url, 120, lambda: num((fetch(url, gap=gap).get("data") or {}).get("value")))


def volume(db):
    url = LLAMA + "?excludeTotalDataChartBreakdown=true"

    def load():
        raw = fetch(url)
        chart = [[int(t), num(v)] for t, v in raw.get("totalDataChart") or [] if num(v) is not None][-30:]
        return {"total24h": num(raw.get("total24h")), "previous24h": num(raw.get("total48hto24h")), "chart": chart}
    return cached(db, url, 900, load)
