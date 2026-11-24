"""SQLite storage. One file, created on first start; later versions add tables idempotently."""

import hashlib
import sqlite3
import time
from pathlib import Path

from . import packs

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  pw_salt TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created REAL NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invites(
  id INTEGER PRIMARY KEY,
  token_hash TEXT NOT NULL UNIQUE,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  note TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL,
  expires REAL NOT NULL,
  used_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  used_at REAL,
  revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS login_attempts(
  ip TEXT NOT NULL, at REAL NOT NULL
);

-- Virtual money. kind: main (one per user) or copy (copy-trading profile).
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('main','copy')),
  name TEXT NOT NULL,
  balance REAL NOT NULL DEFAULT 0,
  paused INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  amount REAL NOT NULL,
  balance_after REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  ref INTEGER,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  token_id TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  event_slug TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL CHECK(side IN ('buy','sell')),
  type TEXT NOT NULL CHECK(type IN ('market','limit')),
  amount REAL,
  shares REAL,
  limit_price REAL,
  fee_rate REAL NOT NULL DEFAULT 0,
  fee_exponent REAL NOT NULL DEFAULT 1,
  taker_only INTEGER NOT NULL DEFAULT 1,
  reserved REAL NOT NULL DEFAULT 0,
  filled_shares REAL NOT NULL DEFAULT 0,
  filled_cost REAL NOT NULL DEFAULT 0,
  fees REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual',
  copy_trade_id INTEGER,
  expires REAL,
  created REAL NOT NULL,
  updated REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS orders_open ON orders(status) WHERE status='open';
CREATE TABLE IF NOT EXISTS fills(
  id INTEGER PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  shares REAL NOT NULL,
  fee REAL NOT NULL,
  liquidity TEXT NOT NULL,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS positions(
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  token_id TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  event_slug TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  outcome_index INTEGER,
  image TEXT NOT NULL DEFAULT '',
  shares REAL NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  bought REAL NOT NULL DEFAULT 0,
  realized REAL NOT NULL DEFAULT 0,
  last_price REAL,
  price_at REAL,
  status TEXT NOT NULL DEFAULT 'open',
  payout REAL,
  updated REAL NOT NULL,
  PRIMARY KEY(account_id, token_id)
);
CREATE TABLE IF NOT EXISTS equity_history(
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  at REAL NOT NULL,
  equity REAL NOT NULL,
  PRIMARY KEY(account_id, at)
);

-- Copy trading.
CREATE TABLE IF NOT EXISTS copy_trades(
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  leader TEXT NOT NULL,
  nickname TEXT NOT NULL DEFAULT '',
  sizing TEXT NOT NULL CHECK(sizing IN ('fixed','percent')),
  amount REAL,
  percent REAL,
  max_per_trade REAL,
  daily_limit REAL,
  price_min REAL,
  price_max REAL,
  slippage REAL,
  notify INTEGER NOT NULL DEFAULT 1,
  paused INTEGER NOT NULL DEFAULT 0,
  cursor_ts INTEGER NOT NULL DEFAULT 0,
  cursor_keys TEXT NOT NULL DEFAULT '[]',
  last_check REAL,
  last_error TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS copy_events(
  id INTEGER PRIMARY KEY,
  copy_trade_id INTEGER NOT NULL REFERENCES copy_trades(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL,
  leader TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('filled','skipped','failed')),
  reason TEXT NOT NULL DEFAULT '',
  side TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  event_slug TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '',
  leader_price REAL,
  leader_size REAL,
  amount REAL,
  shares REAL,
  price REAL,
  order_id INTEGER,
  leader_tx TEXT NOT NULL DEFAULT '',
  at REAL NOT NULL
);

-- 1.3.0: what copy events show about an outcome (market title, outcome name, event, picture), stored once per token.
CREATE TABLE IF NOT EXISTS outcomes(
  id INTEGER PRIMARY KEY,
  token TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  event_slug TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT ''
);

-- Personal lists and messages.
CREATE TABLE IF NOT EXISTS watchlist(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK(kind IN ('trader','event','tag')),
  key TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '',
  created REAL NOT NULL,
  PRIMARY KEY(user_id, kind, key)
);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  read INTEGER NOT NULL DEFAULT 0,
  at REAL NOT NULL
);

-- Public data caches and background statistics.
CREATE TABLE IF NOT EXISTS cache(
  key TEXT PRIMARY KEY, stamp REAL NOT NULL, body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS board(
  board TEXT NOT NULL,
  rank INTEGER NOT NULL,
  wallet TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '',
  pnl REAL,
  volume REAL,
  synced REAL NOT NULL,
  PRIMARY KEY(board, wallet)
);
CREATE TABLE IF NOT EXISTS wallets(
  wallet TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '',
  positions INTEGER,
  active INTEGER,
  gains REAL,
  losses REAL,
  wins INTEGER,
  win_rate REAL,
  networth REAL,
  pnl REAL,
  volume REAL,
  pnl_15d REAL,
  pct_15d REAL,
  cap_eff_30d REAL,
  streak INTEGER,
  join_date REAL,
  partial INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 0,
  stats_at REAL,
  hot_at REAL,
  error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS event_tags(
  event_slug TEXT PRIMARY KEY, tags TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS series(
  name TEXT NOT NULL, day TEXT NOT NULL, value REAL NOT NULL,
  PRIMARY KEY(name, day)
);
CREATE TABLE IF NOT EXISTS census(
  id INTEGER PRIMARY KEY CHECK(id=1),
  cursor TEXT, seen INTEGER NOT NULL DEFAULT 0, started REAL,
  last_count INTEGER, last_finished REAL
);
INSERT OR IGNORE INTO census(id, seen) VALUES(1, 0);
CREATE TABLE IF NOT EXISTS kv(
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);

-- Autopilot: shared candidate pool and per-user roster (see AUTOPILOT.md).
CREATE TABLE IF NOT EXISTS scout_wallets(
  wallet TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '',
  sources TEXT NOT NULL DEFAULT '[]',
  added REAL NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  profiled REAL,
  kind TEXT,
  facts TEXT,
  metrics TEXT,
  score REAL,
  error TEXT NOT NULL DEFAULT ''
);
-- Closed positions of a rated trader for incremental statistics (1.3.0): token hash -> total P&L, last event.
CREATE TABLE IF NOT EXISTS wallet_closed(
  wallet TEXT PRIMARY KEY,
  synced REAL NOT NULL,
  capped INTEGER NOT NULL DEFAULT 0,
  n INTEGER NOT NULL DEFAULT 0,
  data BLOB NOT NULL
);
-- A wallet's scouted bets, packed column by column into one zlib record (kontur/packs.py; was scout_bets).
CREATE TABLE IF NOT EXISTS scout_packs(
  wallet TEXT PRIMARY KEY,
  n INTEGER NOT NULL,
  latest REAL,
  data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS ap_players(
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  wallet TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN ('active','bench','retired','banned')),
  since REAL NOT NULL,
  bench_since REAL,
  active_since REAL,
  pinned INTEGER NOT NULL DEFAULT 0,
  manual INTEGER NOT NULL DEFAULT 0,
  early INTEGER NOT NULL DEFAULT 0,
  copy_trade_id INTEGER,
  reason TEXT NOT NULL DEFAULT '',
  until REAL,
  checked REAL,
  shadow TEXT,
  PRIMARY KEY(user_id, wallet)
);
CREATE TABLE IF NOT EXISTS ap_bets(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  account_id INTEGER NOT NULL,
  wallet TEXT NOT NULL,
  copy_trade_id INTEGER,
  token_id TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  outcome TEXT NOT NULL DEFAULT '',
  event_slug TEXT NOT NULL DEFAULT '',
  image TEXT NOT NULL DEFAULT '',
  opened REAL NOT NULL,
  shares REAL NOT NULL DEFAULT 0,
  bought REAL NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  proceeds REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
  closed REAL,
  pnl REAL
);
CREATE INDEX IF NOT EXISTS ap_bets_open ON ap_bets(user_id, status);
CREATE TABLE IF NOT EXISTS ap_log(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  at REAL NOT NULL,
  kind TEXT NOT NULL,
  wallet TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL
);
"""

# Columns added after 1.0.0: (table, column, definition).
MIGRATIONS = [
    ("copy_trades", "autopilot", "INTEGER NOT NULL DEFAULT 0"),
    ("copy_trades", "mode", "TEXT NOT NULL DEFAULT 'normal'"),
    # 1.2.0: one identity per leader fill (stream and Data API), where it came from, and the copy delay
    ("copy_events", "trade_key", "TEXT NOT NULL DEFAULT ''"),
    ("copy_events", "source", "TEXT NOT NULL DEFAULT 'poll'"),
    ("copy_events", "leader_ts", "REAL"),
    ("copy_events", "detected", "REAL"),
    # 1.3.0: an 8-byte hash of the fill key instead of its ~150-character text, and a link to `outcomes`
    ("copy_events", "key_hash", "INTEGER"),
    ("copy_events", "outcome_id", "INTEGER"),
    # 1.5.0: several people in one app: a blocked person cannot sign in and their copying stops
    ("users", "blocked", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_login", "REAL"),
]

# Indexes for the hot queries (created after the column migrations above).
INDEXES = [
    "DROP INDEX IF EXISTS copy_events_key",  # 1.2.0 index on the text key; replaced by the hash below
    "CREATE INDEX IF NOT EXISTS copy_events_hash ON copy_events(copy_trade_id, key_hash)",
    "CREATE INDEX IF NOT EXISTS copy_events_account ON copy_events(account_id, id)",
    "CREATE INDEX IF NOT EXISTS copy_events_at ON copy_events(at)",
    "CREATE INDEX IF NOT EXISTS notifications_user ON notifications(user_id, id)",
    "CREATE INDEX IF NOT EXISTS positions_account ON positions(account_id, status)",
    "CREATE INDEX IF NOT EXISTS positions_token_open ON positions(token_id) WHERE status='open'",
    "CREATE INDEX IF NOT EXISTS orders_account ON orders(account_id, status)",
    "CREATE INDEX IF NOT EXISTS orders_token_open ON orders(token_id) WHERE status='open'",
    "CREATE INDEX IF NOT EXISTS fills_order ON fills(order_id)",
    "CREATE INDEX IF NOT EXISTS ledger_account ON ledger(account_id, id)",
    "CREATE INDEX IF NOT EXISTS copy_trades_leader ON copy_trades(leader)",
    "CREATE INDEX IF NOT EXISTS ap_bets_wallet ON ap_bets(user_id, wallet, status)",
    "CREATE INDEX IF NOT EXISTS cache_stamp ON cache(stamp)",
    "CREATE INDEX IF NOT EXISTS scout_wallets_profiled ON scout_wallets(profiled)",
]


def connect(path, shared=False):
    """Autocommit connection. shared=True lets a pool hand it to different threads (one at a time)."""
    db = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=not shared)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=15000")
    # WAL + synchronous=NORMAL: a commit no longer waits for the disk flush (safe in WAL: at worst the last
    # commits before a power cut are lost, the database is never corrupted). Bigger page cache, temp in memory.
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("PRAGMA cache_size=-32000")
    db.execute("PRAGMA temp_store=MEMORY")
    db.execute("PRAGMA mmap_size=268435456")
    return db


def init(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            have = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        _hash_copy_keys(db)
        for sql in INDEXES:
            db.execute(sql)
        if packs.migrate(db):        # 1.3.0: bets moved from one row per bet into packed records
            db.execute("VACUUM")     # give the freed space back to the disk
        db.execute("PRAGMA optimize")
    finally:
        db.close()


def key_hash(key):
    """Signed 64-bit hash of a copied fill's key (copytrade.trade_key), stored instead of the text."""
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), "big", signed=True)


def _hash_copy_keys(db):
    """1.2.0 rows kept the text key: hash it and free the text (once)."""
    rows = db.execute("SELECT id, trade_key FROM copy_events WHERE trade_key!='' AND key_hash IS NULL").fetchall()
    if rows:
        db.executemany("UPDATE copy_events SET key_hash=?, trade_key='' WHERE id=?", [(key_hash(k), i) for i, k in rows])


class tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK as a context manager (autocommit connection)."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, kind, value, trace):
        self.db.execute("COMMIT" if kind is None else "ROLLBACK")
        return False


def now():
    return time.time()


def rows(cursor):
    return [dict(r) for r in cursor]


def one(cursor):
    r = cursor.fetchone()
    return dict(r) if r else None


def kv_get(db, key, default=None):
    r = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return r[0] if r else default


def kv_set(db, key, value):
    db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
