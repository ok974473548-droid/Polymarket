"""Candidate universe for the comparison, in table audit_universe(wallet, source).

source = 'leaderboard': every wallet the app found on today's leaderboards (scout_wallets).
source = 'markets':     wallets that bought on randomly sampled markets which closed 60–210 days ago,
                        regardless of their current rank (removes "today's winners" survivorship).

Run: PYTHONPATH=. python research/build_universe.py data/audit.sqlite3 [target_wallets=2500]
"""
import random
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from kontur import db as store
from kontur import polymarket as pm

DB = sys.argv[1]
TARGET = int(sys.argv[2]) if len(sys.argv) > 2 else 2500
DAY = 86400
GAP = 0.06
rng = random.Random(42)
db = store.connect(DB)
db.execute("CREATE TABLE IF NOT EXISTS audit_universe(wallet TEXT PRIMARY KEY, source TEXT, origin TEXT)")
db.execute("""INSERT OR IGNORE INTO audit_universe SELECT wallet, 'leaderboard', '' FROM scout_wallets""")
board = {r[0] for r in db.execute("SELECT wallet FROM scout_wallets")}
now = float(db.execute("SELECT v FROM audit_meta WHERE k='now'").fetchone()[0])


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# 1. markets: 15 ten-day slices of end dates, one market per event, random order inside the slice
markets = []
for k in range(15):
    lo, hi = now - (210 - 10 * k) * DAY, now - (200 - 10 * k) * DAY
    seen_ev, got = set(), []
    for offset in (0, 500):
        q = {"closed": "true", "end_date_min": iso(lo), "end_date_max": iso(hi), "volume_num_min": 20000,
             "limit": 500, "offset": offset}
        try:
            rows = pm.fetch("https://gamma-api.polymarket.com/markets?" + urlencode(q), gap=0.2)
        except pm.UpstreamError:
            break
        for m in rows:
            ev = (m.get("events") or [{}])[0].get("slug") or m.get("slug")
            if ev in seen_ev or not m.get("conditionId"):
                continue
            seen_ev.add(ev)
            got.append((m["conditionId"], ev))
        if len(rows) < 500:
            break
    rng.shuffle(got)
    markets += got[:40]
print("markets", len(markets), flush=True)

# 2. buyers on those markets (3 pages of buys each), not on today's leaderboards
found = {}
for i, (cond, ev) in enumerate(markets):
    cursor = ""
    for _ in range(3):
        try:
            page = pm.trades_page(cond=cond, side="BUY", limit=500, cursor=cursor, taker_only=False, gap=GAP)
        except (pm.UpstreamError, LookupError):
            break
        for t in page["rows"]:
            w = t["proxy_wallet"]
            if w and w not in board and t["price"] and 0.05 <= t["price"] <= 0.95 and t["price"] * (t["size"] or 0) >= 20:
                found.setdefault(w, ev)
        cursor = page["next"]
        if not cursor:
            break
    if (i + 1) % 50 == 0:
        print(f"{i + 1}/{len(markets)} markets, {len(found)} wallets", flush=True)

pick = rng.sample(sorted(found), min(TARGET, len(found)))
db.executemany("INSERT OR IGNORE INTO audit_universe VALUES(?, 'markets', ?)", [(w, found[w]) for w in pick])
print("leaderboard", len(board), "markets sampled", len(pick), "of", len(found), flush=True)
