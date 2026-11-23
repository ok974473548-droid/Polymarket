"""Background jobs, each on its own schedule, sharing one thread and one database connection."""

import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from . import autopilot
from . import copytrade
from . import db as store
from . import polymarket as pm
from . import scout
from . import traders
from . import trading

LOG = logging.getLogger("kontur.worker")


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def job_copy(db):
    due = store.rows(db.execute("""SELECT c.* FROM copy_trades c JOIN accounts a ON a.id=c.account_id
                                   WHERE c.paused=0 AND a.paused=0 AND (c.last_check IS NULL OR c.last_check < ?)
                                   ORDER BY c.last_check LIMIT 20""", (time.time() - 30,)))
    for ct in due:
        try:
            copytrade.poll(db, ct)
        except (pm.UpstreamError, LookupError, ValueError) as exc:
            db.execute("UPDATE copy_trades SET last_check=?, last_error=? WHERE id=?", (time.time(), str(exc)[:200], ct["id"]))


def _books(tokens):
    out = {}
    for tok in tokens:
        try:
            out[tok] = pm.book(tok)
        except (pm.UpstreamError, LookupError, ValueError):
            continue
    return out


def job_orders(db):
    trading.expire_orders(db)
    tokens = [r[0] for r in db.execute("SELECT DISTINCT token_id FROM orders WHERE status='open' LIMIT 60")]
    if tokens:
        trading.match_resting(db, _books(tokens))


def job_marks(db):
    tokens = [r[0] for r in db.execute("""SELECT token_id FROM positions WHERE status='open' AND shares>0.000001
                                          GROUP BY token_id ORDER BY MIN(COALESCE(price_at,0)) LIMIT 40""")]
    marks = {}
    for tok, bk in _books(tokens).items():
        bid = bk["bids"][0][0] if bk["bids"] else None
        ask = bk["asks"][0][0] if bk["asks"] else None
        marks[tok] = (bid + ask) / 2 if bid is not None and ask is not None else bid if bid is not None else ask
    trading.mark_prices(db, marks)


def job_settle(db):
    conds = [r[0] for r in db.execute("""SELECT DISTINCT condition_id FROM positions WHERE status='open'
                                         UNION SELECT DISTINCT condition_id FROM orders WHERE status='open'""")]
    for i in range(0, len(conds), 20):
        markets = pm.markets_by_condition(conds[i:i + 20])
        for cond, raw in markets.items():
            if raw.get("closed"):
                trading.settle(db, cond, raw)


def job_equity(db):
    trading.snapshot_equity(db)


def job_boards(db):
    for cat in traders.BOARDS:
        traders.sync_board(db, cat)


def job_wallet(db):
    wallet = traders.next_wallet(db)
    if not wallet:
        return
    try:
        traders.compute_wallet(db, wallet)
    except (pm.UpstreamError, LookupError, ValueError) as exc:
        db.execute("UPDATE wallets SET error=?, stats_at=COALESCE(stats_at, ?), priority=0 WHERE wallet=?",
                   (str(exc)[:200], time.time(), wallet))


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
    params = {"active": "true", "closed": "false", "limit": 100, "order": "volume24hr", "ascending": "false"}
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


class ScoutPool:
    """Several scouting threads of their own, so profiling neither waits for nor slows the other jobs.

    Threads share the per-host request pace in polymarket.fetch, so more threads hide network latency
    without raising the request rate above 1 / scout.GAP per host.
    """

    def __init__(self, path, threads=None):
        self.path = path
        self.count = threads or scout.THREADS
        self.stop = threading.Event()
        self.errors = {}
        self._busy = set()
        self._lock = threading.Lock()
        self.threads = [threading.Thread(target=self._run, name=f"kontur-scout-{i}", daemon=True) for i in range(self.count)]

    def start(self):
        for t in self.threads:
            t.start()

    def _claim(self, db):
        with self._lock:
            if not _scouting(db):
                return None
            wallet = scout.next_wallet(db, self._busy)
            if wallet:
                self._busy.add(wallet)
            return wallet

    def _run(self):
        db = store.connect(self.path)
        while not self.stop.is_set():
            wallet = None
            try:
                wallet = self._claim(db)
                if wallet:
                    _profile(db, wallet)
                    self.errors.pop("scout", None)
            except Exception:  # noqa: BLE001 - a background job must never kill the thread
                LOG.exception("job scout failed")
                self.errors["scout"] = "внутренняя ошибка"
                if db.in_transaction:
                    db.execute("ROLLBACK")
            finally:
                if wallet:
                    with self._lock:
                        self._busy.discard(wallet)
            if not wallet:
                self.stop.wait(2.0)
        db.close()


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
    ("orders", job_orders, 20),
    ("marks", job_marks, 60),
    ("settle", job_settle, 600),
    ("equity", job_equity, 300),
    ("autopilot", job_autopilot, 30),
]
SLOW = [
    ("boards", job_boards, 900),
    ("categories", job_categories, 15),
    ("census", job_census, 4),
    ("tags", job_tags, 8),
    ("wallet", job_wallet, 1),
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
