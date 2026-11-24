"""Контур рынка: local web server. Public Polymarket analytics plus a password-protected
virtual account (orders, portfolio, copy trading, watchlist)."""

import argparse
import gzip
import hashlib
import json
import logging
import mimetypes
import os
import queue
import re
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from kontur import VERSION, accounts, auth, autopilot, books, bus, copytrade, crypto, net, news, notify, scout, stream, traders, trading, watchlist
from kontur import db as store
from kontur import polymarket as pm
from kontur.worker import FAST, SLOW, ScoutPool, WalletPool, Worker

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
LOG = logging.getLogger("kontur")
IMAGE_HOSTS = re.compile(r"(polymarket-upload\.s3[\w.-]*\.amazonaws\.com|[\w-]+\.polymarket\.com)\Z")
WORKERS = []


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def q1(query, key, default=""):
    return query.get(key, [default])[0]


def need(user):
    if not user:
        raise HttpError(401, "Войдите, чтобы продолжить")
    return user


# ---------------------------------------------------------------- public views

def copy_delay(db, last=200):
    """Copy delay over the latest copies: leader's fill -> seen by us -> copy placed (seconds, medians)."""
    out = {}
    for source in ("stream", "poll"):
        rows = db.execute("""SELECT leader_ts, detected, at FROM copy_events WHERE source=? AND leader_ts IS NOT NULL
                             AND detected IS NOT NULL ORDER BY id DESC LIMIT ?""", (source, last)).fetchall()
        if not rows:
            continue
        seen = sorted(max(0.0, r[1] - r[0]) for r in rows)
        done = sorted(max(0.0, r[2] - r[1]) for r in rows)
        total = sorted(max(0.0, r[2] - r[0]) for r in rows)
        out[source] = {"copies": len(rows), "seen_s": round(seen[len(seen) // 2], 2), "placed_s": round(done[len(done) // 2], 3),
                       "total_s": round(total[len(total) // 2], 2), "total_p90_s": round(total[int(len(total) * 0.9)], 2)}
    return out


def home(db):
    out = {}
    try:
        out["volume"] = pm.volume(db)
    except (pm.UpstreamError, LookupError, ValueError) as exc:
        out["volume"] = {"error": str(exc)}
    census = store.one(db.execute("SELECT * FROM census WHERE id=1"))
    out["markets"] = {"count": census["last_count"], "finished": census["last_finished"],
                      "scanning": bool(census["started"]), "seen": census["seen"],
                      "series": [[r[0], r[1]] for r in db.execute("SELECT day, value FROM series WHERE name='markets' ORDER BY day DESC LIMIT 30")][::-1]}
    out["hot"] = traders.hot(db, 10)
    out["featured"] = pm.catalog(db, order="oi", limit=5)
    return out


def trade_rows(db, rows):
    wallets = {r["proxy_wallet"] for r in rows}
    known = {}
    if wallets:
        marks = ",".join("?" * len(wallets))
        for w in db.execute(f"SELECT * FROM wallets WHERE wallet IN ({marks})", list(wallets)):
            w = dict(w)
            rank = db.execute("SELECT rank FROM board WHERE board='overall:all' AND wallet=?", (w["wallet"],)).fetchone()
            known[w["wallet"]] = traders.tags_for(w, rank[0] if rank else None)
    for r in rows:
        tags = list(known.get(r["proxy_wallet"], []))
        if (r["size"] or 0) * (r["price"] or 0) >= 10_000:
            tags.insert(0, "big")
        r["tags"] = tags
    return rows


def trader_view(db, wallet):
    wallet = pm.address(wallet)
    w = traders.wallet_row(db, wallet)
    if not w or not w["ready"]:
        traders.request_stats(db, wallet)
        w = traders.wallet_row(db, wallet)
    overall = None
    try:
        overall = pm.leaderboard(db, period="all", category="overall", user=wallet, ttl=900)["data"]
    except (pm.UpstreamError, LookupError, ValueError):
        pass
    stats = None
    try:
        stats = pm.user_stats(db, wallet)["data"]
    except (pm.UpstreamError, LookupError, ValueError):
        pass
    if not w.get("name"):
        try:
            prof = pm.cached(db, "profile:" + wallet, 86400,
                             lambda: pm.fetch(pm.gamma_url("/public-profile", {"address": wallet})))["data"] or {}
            name = pm.text(prof.get("name") or prof.get("pseudonym"), 120)
            image = pm.text(prof.get("profileImage"), 600)
            if name:
                db.execute("UPDATE wallets SET name=?, image=COALESCE(NULLIF(image,''), ?) WHERE wallet=?", (name, image, wallet))
                w["name"], w["image"] = name, w.get("image") or image
        except (pm.UpstreamError, LookupError, ValueError):
            pass
    try:
        value = pm.value(db, wallet)["data"]
    except (pm.UpstreamError, LookupError, ValueError):
        value = w.get("networth")
    return {"wallet": wallet, "stats": w, "overall": overall, "profile": stats, "networth": value}


def trader_positions(db, query):
    wallet = pm.address(q1(query, "wallet"))
    status = q1(query, "status", "OPEN").upper()
    status = status if status in {"OPEN", "CLOSED"} else "OPEN"
    sort = q1(query, "sort", "CURRENT_VALUE")
    sort = sort if sort in {"CURRENT_VALUE", "TOTAL_PNL", "TIMESTAMP", "TOKENS", "REALIZED_PNL"} else "CURRENT_VALUE"
    return pm.positions(db, user=wallet, status=status, sort=sort, direction="ASC" if q1(query, "dir") == "asc" else "DESC",
                        limit=int(q1(query, "limit", "50")) if q1(query, "limit", "50").isdigit() else 50,
                        cursor=q1(query, "cursor"), title=q1(query, "title"))


# ---------------------------------------------------------------- routing

def get_routes(db, path, query, user):
    if path == "/api/health":
        return {"ok": True, "version": VERSION}
    if path == "/api/session":
        if not user:
            return {"user": None, "setup": not auth.has_users(db)}
        acc = trading.main_account(db, user["id"])
        return {"user": user, "setup": False, "account": trading.summary(db, user["id"], acc["id"]),
                "unread": db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0", (user["id"],)).fetchone()[0]}
    if path == "/api/auth/invite":
        return accounts.check_invite(db, q1(query, "token"))
    if path == "/api/admin/users":
        return accounts.users(db, need(user)["id"])
    if path == "/api/admin/invites":
        return accounts.invites(db, need(user)["id"])
    if path == "/api/crypto-round":
        return crypto.round_info(q1(query, "slug"))
    if path == "/api/home":
        return home(db)
    if path == "/api/markets":
        offset = q1(query, "offset", "0")
        return pm.catalog(db, order=q1(query, "order", "oi"), tag=q1(query, "tag"),
                          offset=int(offset) if offset.isdigit() else 0, limit=20)
    if path == "/api/event":
        return pm.event_by_slug(db, q1(query, "slug"))
    if path == "/api/prices":
        start, end = pm.num(q1(query, "start")), pm.num(q1(query, "end"))
        return pm.prices(db, q1(query, "token"), q1(query, "window", "max"), start, end)
    if path == "/api/holders":
        return pm.holders(db, q1(query, "condition"))
    if path == "/api/event-trades":
        res = pm.trades(db, event_id=re.sub(r"\D", "", q1(query, "event_id"))[:20], side=q1(query, "side"),
                        cursor=q1(query, "cursor"), limit=25)
        res["data"]["rows"] = trade_rows(db, res["data"]["rows"])
        return res
    if path == "/api/news":
        return news.for_event(db, q1(query, "title")[:300])
    if path == "/api/search":
        return pm.search(db, q1(query, "q"))
    if path == "/api/tags":
        data = json.loads(store.kv_get(db, "tags", '{"rows": [], "events": 0, "at": null}'))
        phrase = q1(query, "q").strip().lower()
        rows = [r for r in data["rows"] if not phrase or phrase in r["label"].lower() or phrase in r["slug"]]
        if phrase and len(rows) < 5:
            try:
                found = pm.search(db, phrase)["data"]["tags"]
                known = {r["slug"] for r in rows}
                rows += [{"slug": t["slug"], "label": t["label"], "count": None} for t in found if t["slug"] not in known]
            except (pm.UpstreamError, LookupError, ValueError):
                pass
        return {"rows": rows, "events": data["events"], "at": data["at"]}
    if path == "/api/traders":
        return traders.listing(db, {k: v[0] for k, v in query.items()})
    if path == "/api/hot":
        return traders.hot(db, 10)
    if path == "/api/trader":
        return trader_view(db, q1(query, "wallet"))
    if path == "/api/trader/pnl":
        window = q1(query, "window", "max")
        interval = {"7d": "1w", "1m": "1m"}.get(window, "max")
        fidelity = "1h" if window == "7d" else "1d"
        return pm.user_pnl(db, q1(query, "wallet"), interval=interval, fidelity=fidelity)
    if path == "/api/trader/positions":
        return trader_positions(db, query)
    if path == "/api/trader/trades":
        res = pm.trades(db, user=q1(query, "wallet"), side=q1(query, "side"), cursor=q1(query, "cursor"), limit=25)
        return res
    if path == "/api/trader/categories":
        wallet = pm.address(q1(query, "wallet"))
        data = traders.categories(db, wallet)
        if not data["detail_ready"] or time.time() - (data["detail_at"] or 0) > 86400:
            store.kv_set(db, "catreq:" + wallet, "1")
        return data
    if path == "/api/status":
        return {"version": VERSION,
                "wallets": db.execute("SELECT COUNT(*), SUM(stats_at IS NOT NULL) FROM wallets").fetchone()[:],
                "jobs": {k: v for w in WORKERS for k, v in w.errors.items()},
                "trades_stream": stream.status(), "books_stream": books.status(), "copy_delay": copy_delay(db),
                "network": dict(net.STATS), "routes": net.top_routes(), "live_clients": bus.listeners()}
    # ------------------------------------------------ personal
    if path == "/api/me":
        user = need(user)
        acc = trading.main_account(db, user["id"])
        return {"account": trading.summary(db, user["id"], acc["id"]), "profiles": copytrade.profiles(db, user["id"])}
    if path.startswith("/api/account"):
        user = need(user)
        aid = q1(query, "id") or trading.main_account(db, user["id"])["id"]
        if path == "/api/account":
            return trading.summary(db, user["id"], aid)
        if path == "/api/account/history":
            return trading.history(db, user["id"], aid, q1(query, "window", "1w"))
        if path == "/api/account/positions":
            trading.owned_account(db, user["id"], aid)
            return trading.position_rows(db, int(aid), q1(query, "status", "open"))
        if path == "/api/account/orders":
            return trading.orders(db, user["id"], aid, q1(query, "status", "open"))
        if path == "/api/account/activity":
            return trading.activity(db, user["id"], aid)
    if path == "/api/copy/profiles":
        return copytrade.profiles(db, need(user)["id"])
    if path == "/api/copy/trades":
        return copytrade.listing(db, need(user)["id"], q1(query, "account"))
    if path == "/api/copy/trade":
        return copytrade.get(db, need(user)["id"], q1(query, "id"))
    if path == "/api/copy/events":
        return copytrade.events(db, need(user)["id"], q1(query, "account"), q1(query, "status"))
    if path.startswith("/api/autopilot"):
        return autopilot_get(db, path, query, need(user)["id"])
    if path == "/api/watchlist":
        return watchlist.listing(db, need(user)["id"])
    if path == "/api/notifications":
        return notify.listing(db, need(user)["id"])
    raise HttpError(404, "Нет такого запроса")


def user_message(exc):
    """Our own errors are written in Russian for the person; Python's own (int('x'), a None) are not shown as they are."""
    text = str(exc)
    if re.search("[а-яА-ЯёЁ]", text):
        return text
    LOG.info("bad request data: %s", text)
    return "Некорректные данные в запросе"


def autopilot_get(db, path, query, uid):
    if path == "/api/autopilot":
        store.kv_set(db, "scout:wanted", "1")
        return autopilot.overview(db, uid)
    if path == "/api/autopilot/candidates":
        s = autopilot.settings(db, uid)
        states = {r[0]: r[1] for r in db.execute("SELECT wallet, state FROM ap_players WHERE user_id=?", (uid,))}
        rows = scout.candidates(db, s)
        phrase = q1(query, "q").strip().lower()
        if phrase:
            rows = [r for r in rows if phrase in (r["name"] or "").lower() or phrase in r["wallet"]]
        if q1(query, "passed") == "1":
            rows = [r for r in rows if r["passed"]]
        for r in rows:
            r["state"] = states.get(r["wallet"])
        return {"rows": rows[:500], "total": len(rows), "scout": scout.status(db)}
    if path == "/api/autopilot/player":
        return autopilot.player(db, uid, q1(query, "wallet"))
    if path == "/api/autopilot/bets":
        return autopilot.bets(db, uid, q1(query, "status"), q1(query, "wallet"))
    if path == "/api/autopilot/log":
        return autopilot.log_rows(db, uid)
    if path == "/api/autopilot/events":
        aid = autopilot.account_id(db, uid)
        return copytrade.events(db, uid, aid, q1(query, "status")) if aid else []
    if path == "/api/autopilot/backtest":
        return {"result": autopilot.backtest_result(db, uid)}
    raise HttpError(404, "Нет такого запроса")


def post_routes(db, path, body, user, handler):
    if path == "/api/auth/setup":
        handler.new_session = auth.setup(db, body)
        return {"ok": True}
    if path == "/api/auth/login":
        handler.new_session = auth.login(db, body, handler.client_address[0])
        return {"ok": True}
    if path == "/api/auth/register":
        handler.new_session = accounts.register(db, body)
        return {"ok": True}
    if path.startswith("/api/admin/"):
        uid = need(user)["id"]
        if path == "/api/admin/invite":
            return accounts.create_invite(db, uid, body.get("note"), body.get("days") or accounts.INVITE_DAYS)
        if path == "/api/admin/invite/revoke":
            return accounts.revoke_invite(db, uid, body.get("id"))
        if path == "/api/admin/user/block":
            return accounts.set_blocked(db, uid, body.get("id"), bool(body.get("blocked")))
        if path == "/api/admin/user/delete":
            return accounts.delete_user(db, uid, body.get("id"))
    if path == "/api/auth/logout":
        auth.logout(db, handler.session_token)
        handler.new_session = ""
        return {"ok": True}
    user = need(user)
    uid = user["id"]
    if path == "/api/auth/password":
        handler.new_session = auth.change_password(db, uid, body)
        return {"ok": True}
    if path == "/api/account/deposit":
        return trading.deposit(db, uid, body.get("id") or trading.main_account(db, uid)["id"], body.get("amount"))
    if path == "/api/account/withdraw":
        return trading.withdraw(db, uid, body.get("id") or trading.main_account(db, uid)["id"], body.get("amount"))
    if path == "/api/account/transfer":
        return trading.transfer_from_main(db, uid, body.get("id"), body.get("amount"))
    if path == "/api/quote":
        return trading.quote(db, uid, body)
    if path == "/api/order":
        return trading.place(db, uid, body)
    if path == "/api/order/cancel":
        return trading.cancel(db, uid, body.get("id"))
    if path == "/api/copy/profile":
        return copytrade.create_profile(db, uid, body.get("name"))
    if path == "/api/copy/profile/rename":
        return copytrade.rename_profile(db, uid, body.get("id"), body.get("name"))
    if path == "/api/copy/profile/pause":
        return copytrade.pause_profile(db, uid, body.get("id"), body.get("paused"))
    if path == "/api/copy/profile/delete":
        return copytrade.delete_profile(db, uid, body.get("id"))
    if path == "/api/copy/trade":
        return copytrade.create(db, uid, body.get("account"), body)
    if path == "/api/copy/trade/update":
        return copytrade.update(db, uid, body.get("id"), body)
    if path == "/api/copy/trade/pause":
        return copytrade.set_paused(db, uid, body.get("id"), body.get("paused"))
    if path == "/api/copy/trade/delete":
        return copytrade.remove(db, uid, body.get("id"))
    if path == "/api/autopilot/enable":
        budget = pm.num(body.get("budget"))
        return autopilot.enable(db, uid, bool(body.get("on")), budget if budget and budget > 0 else None)
    if path == "/api/autopilot/settings":
        return autopilot.update_settings(db, uid, body)
    if path == "/api/autopilot/action":
        return autopilot.action(db, uid, body.get("wallet"), body.get("action"))
    if path == "/api/autopilot/backtest":
        return {"result": autopilot.run_backtest(db, uid)}
    if path == "/api/autopilot/review":
        store.kv_set(db, f"ap:{uid}:review_now", "1")
        return {"ok": True}
    if path == "/api/watchlist":
        return watchlist.toggle(db, uid, body)
    if path == "/api/notifications/read":
        return notify.mark_read(db, uid)
    raise HttpError(404, "Нет такого действия")


# ---------------------------------------------------------------- HTTP

COMPRESSIBLE = ("application/json", "text/", "application/javascript", "image/svg+xml")
_static = {}                 # path -> (mtime, body, gzipped body, etag, content type)
_static_lock = threading.Lock()
_db_pools = {}                # database path -> idle connections (one request uses one at a time)
_db_pools_lock = threading.Lock()


def _pool(path):
    with _db_pools_lock:
        return _db_pools.setdefault(path, queue.LifoQueue(maxsize=16))


def db_take(path):
    try:
        return _pool(path).get_nowait()
    except queue.Empty:
        return store.connect(path, shared=True)


def db_give(path, db):
    if db.in_transaction:
        db.execute("ROLLBACK")
    try:
        _pool(path).put_nowait(db)
    except queue.Full:
        db.close()


def static_file(file, transform=None):
    """File bytes, gzip copy and ETag, kept in memory until the file changes on disk."""
    mtime = file.stat().st_mtime_ns
    with _static_lock:
        hit = _static.get(file)
        if hit and hit[0] == mtime:
            return hit
    body = file.read_bytes()
    if transform:
        body = transform(body)
    ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype == "application/javascript":
        ctype += "; charset=utf-8"
    packed = gzip.compress(body, 6) if ctype.startswith(COMPRESSIBLE) and len(body) > 512 else None
    entry = (mtime, body, packed, '"' + hashlib.sha1(body).hexdigest()[:20] + '"', ctype)
    with _static_lock:
        _static[file] = entry
    return entry


class Handler(BaseHTTPRequestHandler):
    server_version = "Kontur/" + VERSION
    protocol_version = "HTTP/1.1"     # keep-alive: the browser reuses one connection for many requests
    timeout = 60                      # idle keep-alive connections are closed after a minute
    disable_nagle_algorithm = True    # TCP_NODELAY: headers and body go out at once (no 40 ms delayed-ACK stall)

    def log_message(self, fmt, *args):
        LOG.debug(fmt, *args)

    def gzip_ok(self):
        return "gzip" in (self.headers.get("Accept-Encoding") or "")

    def send(self, status, body, ctype, extra=(), packed=None):
        if packed is None and len(body) > 1024 and ctype.startswith(COMPRESSIBLE) and self.gzip_ok():
            packed = gzip.compress(body, 5)
        if packed is not None and self.gzip_ok() and len(packed) < len(body):
            body = packed
            extra = [*extra, ("Content-Encoding", "gzip")]
        if ctype.startswith(COMPRESSIBLE):
            extra = [*extra, ("Vary", "Accept-Encoding")]
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in extra:
            self.send_header(k, v)
        if getattr(self, "new_session", None) is not None:
            token = self.new_session
            age = auth.SESSION_DAYS * 86400 if token else 0
            self.send_header("Set-Cookie", f"{auth.COOKIE}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def json(self, status, obj):
        self.send(status, json.dumps(obj, ensure_ascii=False, allow_nan=False, default=str).encode(),
                  "application/json; charset=utf-8", [("Cache-Control", "no-store")])

    def do_GET(self):
        self.handle_any()

    def do_HEAD(self):
        self.handle_any()

    def do_POST(self):
        self.handle_any()

    def handle_any(self):
        self.new_session = None
        url = urlsplit(self.path)
        path = unquote(url.path)
        if path.startswith("/api/"):
            return self.api(path, parse_qs(url.query))
        if self.command == "POST":
            self.close_connection = True  # the body was not read
            return self.json(405, {"error": "Метод не поддерживается"})
        if path == "/img":
            return self.image(parse_qs(url.query))
        if path.startswith("/static/"):
            file = (STATIC / path.removeprefix("/static/")).resolve()
            if not file.is_relative_to(STATIC) or not file.is_file():
                return self.json(404, {"error": "Файл не найден"})
            versioned = "/fonts/" in path or "/vendor/" in path or "v=" in url.query
            cache = "public, max-age=31536000, immutable" if versioned else "no-cache"
            return self.static(file, cache)
        if path in {"/favicon.ico"}:
            return self.static(STATIC / "favicon.svg", "max-age=86400")
        return self.static(STATIC / "index.html", "no-cache", lambda b: b.replace(b"{{VERSION}}", VERSION.encode()))

    def static(self, file, cache, transform=None):
        """Static file from memory; a repeat visit gets 304 Not Modified instead of the whole file."""
        _, body, packed, etag, ctype = static_file(file, transform)
        extra = [("Cache-Control", cache), ("ETag", etag)]
        if etag in (self.headers.get("If-None-Match") or ""):
            self.send_response(304)
            for k, v in extra:
                self.send_header(k, v)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return self.send(200, body, ctype, extra, packed=packed)

    @property
    def session_token(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[auth.COOKIE].value if auth.COOKIE in cookie else ""

    def api(self, path, query):
        if path == "/api/stream" and self.command == "GET":
            return self.events()
        if path == "/api/live" and self.command == "GET":
            return self.live(query)
        raw = b""
        if self.command == "POST":  # read the body first: on keep-alive, unread bytes would spoil the next request
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= 65536:
                self.close_connection = True
                return self.json(413, {"error": "Слишком большой запрос"})
            raw = self.rfile.read(length) if length else b""
        db = db_take(self.server.db_path)
        try:
            user = auth.user_for(db, self.session_token)
            if self.command == "POST":
                origin = self.headers.get("Origin")
                if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                    raise HttpError(403, "Запрос с другого сайта отклонён")
                if self.headers.get_content_type() != "application/json":
                    raise HttpError(415, "Нужен JSON")
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    raise HttpError(400, "Запрос не читается: ожидается JSON") from None
                if not isinstance(body, dict):
                    raise HttpError(400, "Ожидается объект JSON")
                result = post_routes(db, path, body, user, self)
            else:
                result = get_routes(db, path, query, user)
            self.json(200, result)
        except HttpError as exc:
            self.json(exc.status, {"error": str(exc)})
        except PermissionError as exc:
            self.json(401, {"error": str(exc)})
        except LookupError as exc:
            self.json(404, {"error": str(exc) or "Не найдено"})
        except pm.UpstreamError as exc:
            self.json(502, {"error": str(exc)})
        except (ValueError, TypeError) as exc:
            self.json(400, {"error": user_message(exc)})
        except Exception:  # noqa: BLE001
            LOG.exception("request failed: %s", path)
            self.json(500, {"error": "Внутренняя ошибка сервера"})
        finally:
            db_give(self.server.db_path, db)

    def events(self):
        """Server-sent events: tells the page that its data changed (notifications, copies, fills) — no polling."""
        db = db_take(self.server.db_path)
        try:
            user = auth.user_for(db, self.session_token)
            accounts = {r[0] for r in db.execute("SELECT id FROM accounts WHERE user_id=?", (user["id"],))} if user else set()
        finally:
            db_give(self.server.db_path, db)
        if not user:
            return self.json(401, {"error": "Войдите, чтобы продолжить"})
        q = bus.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self.wfile.flush()
            until = time.time() + 3600   # the browser reconnects by itself; bounded so threads do not pile up
            while time.time() < until:
                try:
                    kind, data, _ = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if kind not in USER_EVENTS:
                    continue  # live prices of event pages go over /api/live
                if kind in ("notify", "autopilot") and data.get("user_id") != user["id"]:
                    continue
                if kind in ("copy", "fill", "account", "prices"):
                    ids = set(data.get("accounts") or [data.get("account_id")])
                    if not ids & accounts:
                        accounts = own_accounts(self.server.db_path, user["id"])  # a profile created since the page opened
                        if not ids & accounts:
                            continue
                    data = {}  # only a hint: which accounts of other people changed is not this page's business
                self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(q)
        return None

    def live(self, query):
        """Server-sent live prices for an open event page (no sign-in needed): outcome prices from the order book
        stream ("price") and, on a crypto round, the coin price from Chainlink ("crypto")."""
        toks = list(dict.fromkeys(t for t in q1(query, "tokens").split(",") if pm.TOKEN.fullmatch(t)))[:40]
        sym = q1(query, "crypto").lower()
        sym = sym if sym in crypto.SYMBOLS else ""
        if not toks and not sym:
            return self.json(400, {"error": "Нужны исходы или монета"})
        with _live_lock:
            if _live_count[0] >= LIVE_MAX:
                return self.json(503, {"error": "Слишком много открытых живых графиков, попробуйте позже"})
            _live_count[0] += 1
            for t in toks:
                LIVE_TOKENS[t] = LIVE_TOKENS.get(t, 0) + 1
        books.start()
        if _on_books not in books.STREAM.listeners:
            books.STREAM.listeners.append(_on_books)
        books.watch(toks, ttl=120)
        if sym:
            crypto.want()
        q = bus.subscribe(maxsize=512)
        self.close_connection = True
        sent = {}

        def push(kind, data):
            self.wfile.write(f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode())

        def refresh():  # the current price of every outcome, when it changed since it was last sent
            for t in toks:
                cur = books.shown(t)
                if cur and cur["p"] is not None and sent.get(t) != cur["p"]:
                    sent[t] = cur["p"]
                    push("price", {"t": t, **cur})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            refresh()
            self.wfile.flush()
            until, last_tick, last_watch = time.time() + 1800, time.monotonic(), time.monotonic()
            while time.time() < until:
                try:
                    kind, data, _ = q.get(timeout=1)
                except queue.Empty:
                    kind = None
                if kind == "book" and data.get("t") in toks:
                    sent[data["t"]] = data.get("p")
                    push("price", data)
                elif kind == "crypto" and data.get("s") == sym:
                    push("crypto", data)
                now = time.monotonic()
                if now - last_tick >= 2:          # catches anything the throttle held back, and keeps the line alive
                    refresh()
                    self.wfile.write(b": ping\n\n")
                    last_tick = now
                if now - last_watch >= 30:
                    books.watch(toks, ttl=120)
                    if sym:
                        crypto.want()
                    last_watch = now
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            bus.unsubscribe(q)
            with _live_lock:
                _live_count[0] -= 1
                for t in toks:
                    LIVE_TOKENS[t] -= 1
                    if LIVE_TOKENS[t] <= 0:
                        del LIVE_TOKENS[t]
        return None

    def image(self, query):
        """Same-origin proxy and disk cache for Polymarket pictures (event, outcome and avatar images)."""
        url = q1(query, "u")
        parts = urlsplit(url)
        if parts.scheme != "https" or not IMAGE_HOSTS.fullmatch(parts.netloc or ""):
            return self.json(400, {"error": "Недопустимый адрес картинки"})
        folder = Path(self.server.db_path).parent / "img"
        folder.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(url.encode()).hexdigest()
        file = folder / key
        meta = folder / (key + ".type")
        if not file.exists():
            try:
                body, ctype = pm.fetch(url, accept="image/*", raw=True, limit=3_000_000, timeout=10)
            except (pm.UpstreamError, LookupError):
                return self.json(502, {"error": "Картинка недоступна"})
            ctype = ctype.split(";")[0].strip()
            if ctype not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif", "image/svg+xml"}:
                return self.json(415, {"error": "Неизвестный формат картинки"})
            file.write_bytes(body)
            meta.write_text(ctype)
        ctype = meta.read_text() if meta.exists() else "image/png"
        extra = [("Cache-Control", "public, max-age=604800")]
        if ctype == "image/svg+xml":
            extra.append(("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"))
        return self.send(200, file.read_bytes(), ctype, extra)


USER_EVENTS = {"notify", "autopilot", "copy", "fill", "account", "prices"}
LIVE_TOKENS = {}             # outcome token -> open event pages showing it live
LIVE_MAX = 300               # open live connections at most (each holds a server thread)
_live_lock = threading.Lock()
_live_count = [0]
_book_sent = {}


def _on_books(changed):
    """The order book stream changed these outcomes: tell open event pages their new price (at most 4 times a second;
    the page's own 2-second refresh catches anything held back here)."""
    now = time.monotonic()
    with _live_lock:
        toks = [t for t in changed if t in LIVE_TOKENS and now - _book_sent.get(t, 0) >= 0.25]
        for t in toks:
            _book_sent[t] = now
        if len(_book_sent) > 5000:
            _book_sent.clear()
    for t in toks:
        cur = books.shown(t)
        if cur and cur["p"] is not None:
            bus.publish("book", {"t": t, **cur})


def own_accounts(path, uid):
    db = db_take(path)
    try:
        return {r[0] for r in db.execute("SELECT id FROM accounts WHERE user_id=?", (uid,))}
    finally:
        db_give(path, db)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A browser or phone dropping a keep-alive connection is normal, not a failure worth a traceback in the log.
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    parser = argparse.ArgumentParser(description="Контур рынка")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8787")))
    parser.add_argument("--db", default=os.environ.get("KONTUR_DB", str(ROOT / "data" / "kontur-v1.sqlite3")))
    parser.add_argument("--no-worker", action="store_true", help="не запускать фоновые задачи")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store.init(args.db)
    server = Server((args.host, args.port), Handler)
    server.db_path = args.db
    if not args.no_worker:
        for jobs, name in ((FAST, "fast"), (SLOW, "slow")):
            w = Worker(args.db, jobs, name)
            w.start()
            WORKERS.append(w)
        for pool in (ScoutPool(args.db), WalletPool(args.db)):
            pool.start()
            WORKERS.append(pool)
        books.start()
        stream.start(args.db)
    print(f"Контур рынка {VERSION} · http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for w in WORKERS:
            w.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
