"""Compact storage of a wallet's bets for the autopilot scouting: one zlib-packed record per wallet.

Every reader takes a wallet's bets as a whole, so instead of one row per bet (wallet, 77-digit token,
66-character market id, title and slug repeated in every row, plus an index copying them again) the bets
of a wallet are kept column by column in one compressed record: about 4–5 times less space, and reading
a wallet is faster than the row-by-row select it replaces. The unpacked list is exactly what the old
table returned: same fields, same number types, one bet per outcome token, ordered by token.
"""

import json
import math
import zlib

COLS = ("token", "condition", "oidx", "outcome", "title", "event_slug", "entry", "size", "opened", "closed", "exit",
        "state", "mark", "end", "short")
_REAL = {"entry", "size", "opened", "closed", "exit", "mark", "end"}
_INT = {"oidx", "short"}
VERSION = 1


def _norm(col, v):
    """The value as the old SQLite column returned it (REAL -> float, INTEGER -> int, TEXT -> str, NaN -> NULL)."""
    if v is None:
        return None
    if col in _REAL:
        v = float(v)
        return None if math.isnan(v) else v
    if col in _INT:
        return int(v)
    return str(v)


def pack(bets):
    """(blob, count, latest opening time) of a list of bet dicts."""
    by_token = {}
    for b in bets:
        by_token[str(b["token"])] = b           # one bet per outcome token; the last one wins
    order = sorted(by_token)
    cols = {c: [_norm(c, by_token[t].get(c)) for t in order] for c in COLS}
    cols["token"] = order
    raw = json.dumps({"v": VERSION, "cols": cols}, separators=(",", ":"), allow_nan=False).encode()
    latest = max((cols["opened"][i] for i in range(len(order)) if cols["opened"][i] is not None), default=None)
    return zlib.compress(raw, 9), len(order), latest


def unpack(blob):
    data = json.loads(zlib.decompress(blob))
    if data.get("v") != VERSION:
        raise ValueError("Неизвестный формат сохранённых ставок")
    cols = data["cols"]
    return [dict(zip(COLS, vals)) for vals in zip(*(cols[c] for c in COLS))]


def save(db, wallet, bets):
    if not bets:
        db.execute("DELETE FROM scout_packs WHERE wallet=?", (wallet,))
        return
    blob, n, latest = pack(bets)
    db.execute("""INSERT INTO scout_packs(wallet, n, latest, data) VALUES(?,?,?,?)
                  ON CONFLICT(wallet) DO UPDATE SET n=excluded.n, latest=excluded.latest, data=excluded.data""",
               (wallet, n, latest, blob))


def load(db, wallet):
    row = db.execute("SELECT data FROM scout_packs WHERE wallet=?", (wallet,)).fetchone()
    return unpack(row[0]) if row else []


def latest(db, wallet):
    row = db.execute("SELECT latest FROM scout_packs WHERE wallet=?", (wallet,)).fetchone()
    return row[0] if row else None


def migrate(db):
    """Move bets from the old one-row-per-bet table (before 1.3.0) into packs; returns the number of wallets moved."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scout_bets'").fetchone():
        return 0
    wallets = [r[0] for r in db.execute("SELECT DISTINCT wallet FROM scout_bets")]
    db.execute("BEGIN IMMEDIATE")
    try:
        for w in wallets:
            cur = db.execute("""SELECT token, condition, oidx, outcome, title, event_slug, entry, size, opened, closed, exit, state,
                                mark, end_ts AS end, short FROM scout_bets WHERE wallet=?""", (w,))
            names = [d[0] for d in cur.description]
            save(db, w, [dict(zip(names, r)) for r in cur.fetchall()])
        db.execute("DROP TABLE scout_bets")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return len(wallets)
