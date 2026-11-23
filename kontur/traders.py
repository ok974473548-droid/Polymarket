"""Trader rating built from Polymarket Data API v2.

The public leaderboard gives PnL and volume only. Positions, active positions, gains, losses,
win rate and net worth are computed per wallet by a background crawl of the top 200 wallets of
each tab (Leaderboard, Sports, Crypto, Politics). Results refresh once a day; PnL and volume
come straight from the leaderboard every 15 minutes.
"""

import json
import time

from . import db as store
from . import polymarket as pm

TOP = 200
BOARDS = ["overall", "sports", "crypto", "politics"]
POSITION_PAGES = {"OPEN": 10, "CLOSED": 25}
STATS_TTL = 24 * 3600
GAP = 0.35

TAGS = {
    "whale": "Кит",
    "sniper": "Снайпер",
    "active": "Активный",
    "rookie": "Новичок",
    "hot": "В ударе",
    "top10": "Топ-10",
    "loser": "Серия убытков",
}


def board_name(category, period="all"):
    return f"{category}:{period}"


def sync_board(db, category, period="all"):
    """Refresh one leaderboard (top 200 by PnL) into the board table."""
    res = pm.leaderboard(db, period=period, category=category, limit=TOP, ttl=600, gap=GAP)
    rows = (res["data"] or {}).get("rows") or []
    name, now = board_name(category, period), time.time()
    with store.tx(db):
        db.execute("DELETE FROM board WHERE board=?", (name,))
        for r in rows:
            db.execute("INSERT OR REPLACE INTO board VALUES(?,?,?,?,?,?,?,?)",
                       (name, r["rank"], r["wallet"], r["name"], r["image"], r["pnl"], r["volume"], now))
            db.execute("""INSERT INTO wallets(wallet, name, image, priority) VALUES(?,?,?,0)
                          ON CONFLICT(wallet) DO UPDATE SET name=CASE WHEN excluded.name!='' THEN excluded.name ELSE wallets.name END,
                          image=CASE WHEN excluded.image!='' THEN excluded.image ELSE wallets.image END""",
                       (r["wallet"], r["name"], r["image"]))
            if category == "overall" and period == "all":
                db.execute("UPDATE wallets SET pnl=?, volume=? WHERE wallet=?", (r["pnl"], r["volume"], r["wallet"]))
    return len(rows)


def ensure_board(db, category, period="all"):
    name = board_name(category, period)
    synced = db.execute("SELECT MAX(synced) FROM board WHERE board=?", (name,)).fetchone()[0]
    if not synced or time.time() - synced > 900:
        try:
            sync_board(db, category, period)
        except (pm.UpstreamError, LookupError, ValueError):
            if not synced:
                raise
    return name


def _all_positions(wallet, status):
    rows, cursor, pages = [], "", 0
    while pages < POSITION_PAGES[status]:
        page = pm.positions_page(user=wallet, status=status, sort="TIMESTAMP", limit=1000, cursor=cursor, gap=GAP)
        rows.extend(page["rows"])
        pages += 1
        cursor = page["next"]
        if not cursor:
            return rows, False
    return rows, True


def _series_stats(points):
    """15-day PnL change, 30-day capital efficiency and current daily streak from daily PnL points."""
    pts = [p for p in points if p.get("pnl") is not None]
    if len(pts) < 2:
        return None, None, None
    last, t_last = pts[-1], pts[-1]["t"]

    def at(days):
        target = t_last - days * 86400
        older = [p for p in pts if p["t"] <= target]
        return older[-1] if older else pts[0]
    p15, p30 = at(15), at(30)
    pnl15 = last["pnl"] - p15["pnl"]
    eff = None
    if last.get("volume") is not None and p30.get("volume") is not None:
        traded = last["volume"] - p30["volume"]
        if traded > 100:
            eff = (last["pnl"] - p30["pnl"]) / traded * 100
    streak, sign = 0, 0
    for a, b in zip(reversed(pts[:-1]), reversed(pts)):
        d = b["pnl"] - a["pnl"]
        s = 1 if d > 1e-6 else -1 if d < -1e-6 else 0
        if s == 0:
            break
        if sign == 0:
            sign = s
        if s != sign:
            break
        streak += 1
    return pnl15, eff, streak * sign


def compute_wallet(db, wallet):
    """Full statistics of one wallet. Large wallets are capped and marked partial."""
    wallet = pm.address(wallet)
    networth = pm.value(db, wallet, gap=GAP)["data"]
    stats = pm.user_stats(db, wallet, gap=GAP)["data"] or {}
    open_rows, open_capped = _all_positions(wallet, "OPEN")
    closed_rows, closed_capped = _all_positions(wallet, "CLOSED")
    every = open_rows + closed_rows
    gains = sum(r["total_pnl"] for r in every if (r["total_pnl"] or 0) > 0)
    losses = sum(r["total_pnl"] for r in every if (r["total_pnl"] or 0) < 0)
    wins = sum(1 for r in every if (r["total_pnl"] or 0) > 0)
    total = len(every)
    points = pm.user_pnl(db, wallet, interval="1m", fidelity="1d", gap=GAP)["data"] or []
    pnl15, eff, streak = _series_stats(points)
    base = networth if networth and networth > 1000 else None
    pct15 = (pnl15 / base * 100) if pnl15 is not None and base else None
    with store.tx(db):
        db.execute("INSERT OR IGNORE INTO wallets(wallet) VALUES(?)", (wallet,))
        db.execute("""UPDATE wallets SET positions=?, active=?, gains=?, losses=?, wins=?, win_rate=?, networth=?,
                      pnl=COALESCE(pnl, ?), volume=COALESCE(volume, ?), pnl_15d=?, pct_15d=?, cap_eff_30d=?, streak=?,
                      join_date=?, partial=?, stats_at=?, hot_at=?, error='', priority=0 WHERE wallet=?""",
                   (total, len(open_rows), gains, losses, wins, (wins / total * 100) if total else None, networth,
                    stats.get("pnl"), stats.get("volume"), pnl15, pct15, eff, streak, stats.get("join_date"),
                    int(open_capped or closed_capped), time.time(), time.time(), wallet))
    return wallet_row(db, wallet)


def next_wallet(db):
    """Wallet that most needs fresh statistics: requested ones first, then the oldest."""
    row = db.execute("""SELECT wallet FROM wallets WHERE (stats_at IS NULL OR stats_at < ? OR priority > 0)
                        AND (error = '' OR stats_at IS NULL OR stats_at < ?)
                        ORDER BY priority DESC, stats_at IS NOT NULL, stats_at LIMIT 1""",
                     (time.time() - STATS_TTL, time.time() - 3600)).fetchone()
    return row[0] if row else None


def request_stats(db, wallet):
    db.execute("INSERT INTO wallets(wallet, priority) VALUES(?,1) ON CONFLICT(wallet) DO UPDATE SET priority=1", (wallet,))


def tags_for(w, rank=None):
    tags = []
    if (w.get("networth") or 0) >= 1_000_000:
        tags.append("whale")
    if (w.get("win_rate") or 0) >= 70 and (w.get("positions") or 0) >= 50:
        tags.append("sniper")
    if (w.get("active") or 0) >= 100:
        tags.append("active")
    if w.get("join_date") and time.time() - w["join_date"] < 60 * 86400:
        tags.append("rookie")
    if (w.get("streak") or 0) >= 5:
        tags.append("hot")
    if (w.get("streak") or 0) <= -5:
        tags.append("loser")
    if rank is not None and rank <= 10:
        tags.append("top10")
    return tags


def wallet_row(db, wallet):
    w = store.one(db.execute("SELECT * FROM wallets WHERE wallet=?", (wallet,)))
    if not w:
        return None
    rank = db.execute("SELECT rank FROM board WHERE board='overall:all' AND wallet=?", (wallet,)).fetchone()
    w["tags"] = tags_for(w, rank[0] if rank else None)
    w["ready"] = w["stats_at"] is not None
    return w


SORTS = {"rank": "b.rank", "positions": "w.positions", "active": "w.active", "gains": "w.gains", "losses": "w.losses",
         "win": "w.win_rate", "networth": "w.networth", "pnl": "b.pnl", "volume": "b.volume", "name": "b.name"}
RANGES = {"pnl": "b.pnl", "networth": "w.networth", "active": "w.active", "gains": "w.gains",
          "losses": "w.losses", "positions": "w.positions", "win": "w.win_rate"}


def listing(db, q):
    """Rating table: tabs (overall/whales/sports/crypto/politics), category, sort, filters, paging."""
    tab = q.get("tab", "leaderboard")
    category = q.get("category") or ("overall" if tab in {"leaderboard", "whales"} else tab)
    if category not in pm.LEADER_CATEGORIES:
        category = "overall"
    name = ensure_board(db, category)
    if category not in BOARDS:
        db.execute("""UPDATE wallets SET priority=MAX(priority, 1) WHERE stats_at IS NULL AND wallet IN
                      (SELECT wallet FROM board WHERE board=?)""", (name,))
    default = "networth" if tab == "whales" else "pnl"
    sort = q.get("sort") if q.get("sort") in SORTS else default
    direction = "ASC" if q.get("dir") == "asc" else "DESC"
    where, params = ["b.board=?"], [name]
    if q.get("search"):
        where.append("(b.name LIKE ? OR b.wallet LIKE ?)")
        params += ["%" + q["search"][:60] + "%"] * 2
    if q.get("active") == "1":
        where.append("COALESCE(w.active, 1) > 0")
    for key, col in RANGES.items():
        lo, hi = pm.num(q.get(key + "_min")), pm.num(q.get(key + "_max"))
        if lo is not None:
            where.append(f"{col} >= ?")
            params.append(lo)
        if hi is not None:
            where.append(f"{col} <= ?")
            params.append(hi)
    size = int(q.get("size") or 25) if str(q.get("size") or "25").isdigit() else 25
    size = size if size in {10, 25, 50, 100} else 25
    page = max(1, int(q.get("page") or 1)) if str(q.get("page") or "1").isdigit() else 1
    sql = f"""SELECT b.rank, b.wallet, b.name, b.image, b.pnl, b.volume, w.positions, w.active, w.gains, w.losses, w.wins,
              w.win_rate, w.networth, w.pnl_15d, w.pct_15d, w.streak, w.join_date, w.partial, w.stats_at
              FROM board b LEFT JOIN wallets w ON w.wallet=b.wallet WHERE {' AND '.join(where)}"""
    rows = store.rows(db.execute(sql + f" ORDER BY {SORTS[sort]} IS NULL, {SORTS[sort]} {direction}, b.rank", params))
    tag = q.get("tag")
    for r in rows:
        r["tags"] = tags_for(r, r["rank"] if category == "overall" else None)
    if tag in TAGS:
        rows = [r for r in rows if tag in r["tags"]]
    total = len(rows)
    ranges = {k: [min((r[k2] for r in rows if r[k2] is not None), default=None), max((r[k2] for r in rows if r[k2] is not None), default=None)]
              for k, k2 in (("pnl", "pnl"), ("networth", "networth"), ("active", "active"), ("gains", "gains"),
                            ("losses", "losses"), ("positions", "positions"), ("win", "win_rate"))}
    ready = sum(1 for r in rows if r["stats_at"])
    return {"rows": rows[(page - 1) * size: page * size], "total": total, "page": page, "size": size, "sort": sort,
            "dir": direction.lower(), "category": category, "tab": tab, "ranges": ranges, "ready": ready,
            "tags": TAGS}


def hot(db, limit=10):
    rows = store.rows(db.execute("""SELECT b.rank, b.wallet, b.name, b.image, b.pnl, w.pnl_15d, w.pct_15d, w.networth
                                    FROM board b JOIN wallets w ON w.wallet=b.wallet
                                    WHERE b.board='overall:all' AND w.pct_15d IS NOT NULL AND w.pnl_15d > 0
                                    ORDER BY w.pnl_15d DESC LIMIT ?""", (limit,)))
    if len(rows) < limit:
        seen = {r["wallet"] for r in rows}
        extra = store.rows(db.execute("""SELECT b.rank, b.wallet, b.name, b.image, b.pnl, NULL AS pnl_15d, NULL AS pct_15d, NULL AS networth
                                         FROM board b WHERE b.board='overall:all' ORDER BY b.rank LIMIT ?""", (limit * 2,)))
        rows += [r for r in extra if r["wallet"] not in seen][: limit - len(rows)]
    for i, r in enumerate(rows):
        r["hot_rank"] = i + 1
    return rows


def categories(db, wallet):
    """Per-category rank, PnL and volume from the leaderboard; position counts when computed."""
    wallet = pm.address(wallet)
    out = []
    for cat in pm.LEADER_CATEGORIES[1:]:
        try:
            row = pm.leaderboard(db, period="all", category=cat, user=wallet, ttl=3600)["data"]
        except (pm.UpstreamError, LookupError, ValueError):
            row = None
        if row and (row.get("volume") or 0) > 0:
            out.append({"category": cat, **row})
    cached = db.execute("SELECT value FROM kv WHERE key=?", ("cats:" + wallet,)).fetchone()
    detail = json.loads(cached[0]) if cached else {}
    for r in out:
        r.update(detail.get(r["category"], {}))
    out.sort(key=lambda r: -(r.get("pnl") or 0))
    return {"rows": out, "detail_ready": bool(detail), "detail_at": detail.get("_at")}


def compute_categories(db, wallet, limit_events=150):
    """Group a wallet's recent positions by event category (positions, active, markets, win rate, gains/losses)."""
    rows = []
    for status in ("OPEN", "CLOSED"):
        page = pm.positions_page(user=wallet, status=status, sort="TIMESTAMP", limit=1000, gap=GAP)
        rows += page["rows"]
    slugs = []
    for r in rows:
        if r.get("event_slug") and r["event_slug"] not in slugs:
            slugs.append(r["event_slug"])
    tagmap = {s: pm.event_tags(db, s) for s in slugs[:limit_events]}
    groups = {}
    for r in rows:
        for cat in set(tagmap.get(r.get("event_slug"), [])) & set(pm.LEADER_CATEGORIES[1:]):
            g = groups.setdefault(cat, {"positions": 0, "active": 0, "markets": set(), "wins": 0, "gains": 0.0, "losses": 0.0})
            g["positions"] += 1
            g["active"] += 1 if r["status"] == "OPEN" else 0
            g["markets"].add(r.get("condition_id"))
            pnl = r.get("total_pnl") or 0
            g["wins"] += 1 if pnl > 0 else 0
            g["gains"] += max(pnl, 0)
            g["losses"] += min(pnl, 0)
    detail = {k: {"positions": g["positions"], "active": g["active"], "markets": len(g["markets"]),
                  "win_rate": g["wins"] / g["positions"] * 100 if g["positions"] else None,
                  "gains": g["gains"], "losses": g["losses"]} for k, g in groups.items()}
    detail["_at"] = time.time()
    store.kv_set(db, "cats:" + wallet, json.dumps(detail))
    return detail
