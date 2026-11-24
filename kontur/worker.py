"""Background jobs, each on its own schedule, sharing one thread and one database connection."""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import autopilot
from . import books
from . import copytrade
from . import db as store
from . import polymarket as pm
from . import scout
from . import stream
from . import traders
from . import trading

LOG = logging.getLogger("kontur.worker")


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def job_copy(db):
    """Data API polling: the fallback for fills the live stream missed (the API itself lags by minutes)."""
    every = 120 if stream.connected() else 30
    due = store.rows(db.execute("""SELECT c.* FROM copy_trades c JOIN accounts a ON a.id=c.account_id JOIN users u ON u.id=a.user_id
                                   WHERE c.paused=0 AND a.paused=0 AND u.blocked=0 AND (c.last_check IS NULL OR c.last_check < ?)
                                   ORDER BY c.last_check LIMIT 20""", (time.time() - every,)))
    for ct in due:
        try:
            copytrade.poll(db, ct)
        except (pm.UpstreamError, LookupError, ValueError) as exc:
            db.execute("UPDATE copy_trades SET last_check=?, last_error=? WHERE id=?", (time.time(), str(exc)[:200], ct["id"]))


_BOOK_POOL = ThreadPoolExecutor(8, thread_name_prefix="kontur-books")


def _one_book(tok):
    try:
        return tok, pm.book(tok)
    except (pm.UpstreamError, LookupError, ValueError):
        return tok, None


def _books(tokens):
    """Order books of many tokens: fresh ones from the live stream, the rest fetched 8 at a time."""
    out = {}
    missing = []
    for tok in tokens:
        bk = books.fresh(tok)
        if bk:
            out[tok] = bk
        else:
            missing.append(tok)
    for tok, bk in _BOOK_POOL.map(_one_book, missing):
        if bk:
            out[tok] = bk
    return out


_last_rest = {}


def _due_without_stream(name, every):
    """With the live book stream up, book jobs run on every tick (memory reads); without it, every `every` seconds."""
    if books.status().get("connected"):
        return True
    now = time.time()
    if now - _last_rest.get(name, 0) < every:
        return False
    _last_rest[name] = now
    return True


def job_watch(db):
    """Keep the live book stream subscribed to the outcomes with resting limit orders (they need depth and an
    instant reaction). Positions are valued from batch mid prices instead: a busy market sends its whole book
    after every trade, which for a few dozen positions is gigabytes a day."""
    tokens = [r[0] for r in db.execute("SELECT DISTINCT token_id FROM orders WHERE status='open'")]
    books.watch(tokens, ttl=120)


def job_orders(db):
    trading.expire_orders(db)
    if not _due_without_stream("orders", 20):
        return
    tokens = [r[0] for r in db.execute("SELECT DISTINCT token_id FROM orders WHERE status='open' LIMIT 200")]
    if tokens:
        trading.match_resting(db, _books(tokens))


def _mid(bk):
    bid = bk["bids"][0][0] if bk["bids"] else None
    ask = bk["asks"][0][0] if bk["asks"] else None
    return (bid + ask) / 2 if bid is not None and ask is not None else bid if bid is not None else ask


def job_marks(db):
    """Value open positions at the mid price: books already in memory, then one batch request of mids
    (~45 bytes a token instead of a whole book), and a full book only where the CLOB has no mid."""
    tokens = [r[0] for r in db.execute("""SELECT token_id FROM positions WHERE status='open' AND shares>0.000001
                                          GROUP BY token_id ORDER BY MIN(COALESCE(price_at,0)) LIMIT 500""")]
    marks, rest = {}, []
    for tok in tokens:
        bk = books.fresh(tok)
        if bk:
            marks[tok] = _mid(bk)
        else:
            rest.append(tok)
    if rest:
        try:
            mids = pm.midpoints(rest)
        except (pm.UpstreamError, ValueError):
            mids = {}
        marks.update(mids)
        missing = [t for t in rest if t not in mids]
        for tok, bk in _books(missing[:40]).items():
            marks[tok] = _mid(bk)
    trading.mark_prices(db, marks)


def job_settle(db):
    conds = [r[0] for r in db.execute("""SELECT DISTINCT condition_id FROM positions WHERE status='open'
                                         UNION SELECT DISTINCT condition_id FROM orders WHERE status='open'""")]
    for i in range(0, len(conds), 20):
        markets = pm.markets_by_condition(conds[i:i + 20], closed=True)
        for cond, raw in markets.items():
            if raw.get("closed"):
                trading.settle(db, cond, raw)


def job_equity(db):
    trading.snapshot_equity(db)


def job_boards(db):
    for cat in traders.BOARDS:
        traders.sync_board(db, cat)


def _wallet_stats(db, wallet):
    try:
        traders.compute_wallet(db, wallet)
    except (pm.UpstreamError, LookupError, ValueError) as exc:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.execute("UPDATE wallets SET error=?, stats_at=COALESCE(stats_at, ?), priority=0 WHERE wallet=?",
                   (str(exc)[:200], time.time(), wallet))


def job_wallet(db):
    wallet = traders.next_wallet(db)
    if wallet:
        _wallet_stats(db, wallet)


def job_categories(db):
    row = db.execute("SELECT key FROM kv WHERE key LIKE 'catreq:%' LIMIT 1").fetchone()
    if not row:
        return
    wallet = row[0].split(":", 1)[1]
    db.execute("DELETE FROM kv WHERE key=?", (row[0],))
    traders.compute_categories(db, wallet)


def job_census(db):
    """Count active, open markets with a keyset scan (one page per run); once a day."""
    st = store.one(db.execute("SELECT * FROM census WHERE id=1"))
    if not st["started"]:
        if st["last_finished"] and time.time() - st["last_finished"] < 86400:
            return
        db.execute("UPDATE census SET started=?, cursor=NULL, seen=0 WHERE id=1", (time.time(),))
        st = store.one(db.execute("SELECT * FROM census WHERE id=1"))
    params = {"active": "true", "closed": "false", "limit": 500, "order": "id", "ascending": "true"}
    if st["cursor"]:
        params["after_cursor"] = st["cursor"]
    raw = pm.fetch(pm.GAMMA + "/markets/keyset?" + urlencode(params), gap=0.3)
    rows = raw.get("markets") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise pm.UpstreamError("Gamma изменила формат списка рынков")
    seen = st["seen"] + sum(1 for r in rows if r.get("active") is True and r.get("closed") is False)
    cursor = raw.get("next_cursor")
    if cursor and rows:
        db.execute("UPDATE census SET cursor=?, seen=? WHERE id=1", (cursor, seen))
    else:
        db.execute("UPDATE census SET cursor=NULL, seen=0, started=NULL, last_count=?, last_finished=? WHERE id=1",
                   (seen, time.time()))
        db.execute("INSERT OR REPLACE INTO series VALUES('markets', ?, ?)", (_today(), seen))


def job_tags(db):
    """Count tags over the top 2,000 active events by 24h volume (one page of 100 per run, daily)."""
    state = json.loads(store.kv_get(db, "tagscan", "{}"))
    if not state.get("running"):
        if state.get("finished") and time.time() - state["finished"] < 86400:
            return
        state = {"running": True, "cursor": None, "pages": 0, "counts": {}, "labels": {}}
    # only the tags are needed: events without their markets weigh ~75 times less (30 MB -> 0.4 MB a page)
    params = {"active": "true", "closed": "false", "limit": 100, "order": "volume24hr", "ascending": "false",
              "include_markets": "false"}
    if state["cursor"]:
        params["after_cursor"] = state["cursor"]
    raw = pm.fetch(pm.GAMMA + "/events/keyset?" + urlencode(params), gap=0.5)
    events = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(events, list):
        raise pm.UpstreamError("Gamma изменила формат списка событий")
    for ev in events:
        for t in ev.get("tags") or []:
            if isinstance(t, dict) and t.get("slug"):
                slug = pm.text(t["slug"], 120)
                state["counts"][slug] = state["counts"].get(slug, 0) + 1
                state["labels"][slug] = pm.text(t.get("label"), 120)
    state["pages"] += 1
    state["cursor"] = raw.get("next_cursor")
    if not state["cursor"] or state["pages"] >= 20 or not events:
        rows = sorted(({"slug": k, "label": state["labels"].get(k, k), "count": v} for k, v in state["counts"].items()),
                      key=lambda r: -r["count"])
        store.kv_set(db, "tags", json.dumps({"rows": rows, "events": state["pages"] * 100, "at": time.time()}))
        state = {"running": False, "finished": time.time()}
    store.kv_set(db, "tagscan", json.dumps(state))


def job_autopilot(db):
    """Review each user's autopilot every 10 minutes, or right away after a change."""
    for uid in autopilot.users(db):
        due = store.kv_get(db, f"ap:{uid}:review_now", "") == "1"
        last = float(store.kv_get(db, f"ap:{uid}:reviewed", "0") or 0)
        if due or time.time() - last >= autopilot.REVIEW_EVERY:
            autopilot.review(db, uid)
        else:
            autopilot.sync_bets(db, uid)


def _scouting(db):
    return store.kv_get(db, "scout:wanted", "") == "1"


def job_discover(db):
    if _scouting(db):
        scout.discover(db)


def job_scout(db, skip=()):
    """Profile one wallet; returns it (None when there is nothing to do)."""
    if not _scouting(db):
        return None
    wallet = scout.next_wallet(db, skip)
    if not wallet:
        return None
    _profile(db, wallet)
    return wallet


def _profile(db, wallet):
    try:
        scout.profile_wallet(db, wallet)
    except (pm.UpstreamError, LookupError, ValueError) as exc:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.execute("UPDATE scout_wallets SET error=?, profiled=?, priority=0 WHERE wallet=?", (str(exc)[:200], time.time(), wallet))


class JobPool:
    """Several threads of their own for one kind of slow per-wallet work, so it neither waits for nor slows
    the other jobs. Threads share the per-host request pace in polymarket.fetch: more threads hide network
    latency without raising the request rate above 1 / gap per host.

    claim(db, busy) -> item or None picks the next item not held by another thread; work(db, item) does it.
    """

    def __init__(self, path, name, claim, work, threads):
        self.path = path
        self.name = name
        self.claim, self.work = claim, work
        self.count = threads
        self.stop = threading.Event()
        self.errors = {}
        self._busy = set()
        self._lock = threading.Lock()
        self.threads = [threading.Thread(target=self._run, name=f"kontur-{name}-{i}", daemon=True) for i in range(self.count)]

    def start(self):
        for t in self.threads:
            t.start()

    def _claim(self, db):
        with self._lock:
            item = self.claim(db, self._busy)
            if item:
                self._busy.add(item)
            return item

    def _run(self):
        db = store.connect(self.path)
        while not self.stop.is_set():
            item = None
            try:
                item = self._claim(db)
                if item:
                    self.work(db, item)
                    self.errors.pop(self.name, None)
            except Exception:  # noqa: BLE001 - a background job must never kill the thread
                LOG.exception("job %s failed", self.name)
                self.errors[self.name] = "внутренняя ошибка"
                if db.in_transaction:
                    db.execute("ROLLBACK")
            finally:
                if item:
                    with self._lock:
                        self._busy.discard(item)
            if not item:
                self.stop.wait(2.0)
        db.close()


def ScoutPool(path, threads=None):
    """Player scouting for the autopilot (only once the autopilot page was opened)."""
    return JobPool(path, "scout", lambda db, busy: scout.next_wallet(db, busy) if _scouting(db) else None,
                   _profile, threads or scout.THREADS)


def WalletPool(path, threads=None):
    """Statistics of the top-200 traders of every leaderboard tab."""
    return JobPool(path, "wallet", traders.next_wallet, _wallet_stats, threads or traders.THREADS)


def job_backtest(db):
    """History check for each autopilot user: on request, otherwise twice a day once enough players are profiled."""
    for uid in autopilot.users(db):
        res = autopilot.backtest_result(db, uid)
        asked = store.kv_get(db, f"ap:{uid}:backtest_req", "") == "1"
        enough = db.execute("SELECT COUNT(*) FROM scout_wallets WHERE metrics IS NOT NULL").fetchone()[0] >= 20
        if asked or (enough and (not res or time.time() - res["now"] > 12 * 3600)):
            autopilot.run_backtest(db, uid)


FAST = [
    ("copy", job_copy, 10),
    ("watch", job_watch, 15),
    ("orders", job_orders, 2),
    ("marks", job_marks, 15),
    ("settle", job_settle, 600),
    ("equity", job_equity, 300),
    ("autopilot", job_autopilot, 30),
]
SLOW = [
    ("boards", job_boards, 900),
    ("categories", job_categories, 15),
    ("census", job_census, 4),
    ("tags", job_tags, 8),
    ("discover", job_discover, 300),
    ("backtest", job_backtest, 120),
]


class Worker(threading.Thread):
    def __init__(self, path, jobs, name):
        super().__init__(name="kontur-" + name, daemon=True)
        self.path = path
        self.jobs = jobs
        self.stop = threading.Event()
        self.last = {}
        self.errors = {}

    def run(self):
        db = store.connect(self.path)
        while not self.stop.is_set():
            ran = False
            for name, job, every in self.jobs:
                if self.stop.is_set():
                    break
                if time.time() - self.last.get(name, 0) < every:
                    continue
                self.last[name] = time.time()
                try:
                    job(db)
                    self.errors.pop(name, None)
                except (pm.UpstreamError, LookupError, ValueError) as exc:
                    self.errors[name] = str(exc)[:200]
                    LOG.info("job %s: %s", name, exc)
                except Exception:  # noqa: BLE001 - a background job must never kill the thread
                    LOG.exception("job %s failed", name)
                    self.errors[name] = "внутренняя ошибка"
                ran = True
            if db.in_transaction:
                db.execute("ROLLBACK")
            self.stop.wait(0.5 if ran else 1.0)
        db.close()
