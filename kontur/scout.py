"""Player scouting for the autopilot.

Finds candidates on Polymarket leaderboards, turns each wallet's history into "copy bets" (what a
copier would have earned entering a little later and paying fees), and measures how reliably those
copies win. The design is described in AUTOPILOT.md.

Key detail: losing positions are rarely redeemed, so they stay OPEN with redeemable=true and a price
of $0. Resolved OPEN positions are therefore counted together with CLOSED ones; otherwise the win
rate would be heavily inflated.
"""

import json
import math
import os
import re
import statistics
import time
from datetime import datetime, timezone

from . import db as store
from . import packs
from . import polymarket as pm

# Minimum gap between scouting calls to one host, shared by all scouting threads (~16 requests/s).
GAP = float(os.environ.get("KONTUR_SCOUT_GAP", "0.06"))
THREADS = max(1, int(os.environ.get("KONTUR_SCOUT_THREADS", "6")))
DAY = 86400
LOOKBACK_DAYS = 180          # profile window, by entry date
CRAWL_DAYS = LOOKBACK_DAYS + 60  # the extra 60 days feed the history check
DELAY = 60                   # seconds between the leader's fill and the copy, for slippage
PAGE = 500
MAX_PAGES = {"CLOSED": 12, "OPEN": 8}
MIN_COST = 5.0
PRICE_MIN, PRICE_MAX = 0.02, 0.97
PUSH = 0.002                 # |return| below this is neither a win nor a loss
DEFAULT_SLIP = 0.005
PROFILE_TTL = 72 * 3600
ROSTER_TTL = 6 * 3600
DISCOVER_EVERY = 6 * 3600
SHORT_TERM = re.compile(r"up or down|updown", re.I)

# Selection rules shared with the autopilot settings (values in the units shown in the UI).
DEFAULTS = {
    "min_bets": 40,       # decided copy bets in the window
    "min_wr": 70,         # %, copy win rate
    "min_lb": 60,         # %, Wilson lower bound of the win rate
    "min_roi": 3,         # %, average return of one copy
    "min_edge": 0.02,     # $ per share over the entry price
    "max_top_share": 40,  # %, share of profit from the best bet
    "min_recent": 2,      # new bets in the last 14 days
    "max_slip": 0.03,     # $, median price move 60 s after the leader's buys
    "selection": "skill",  # "skill": wins against the price paid; "winrate": the share of winning copies (until 1.10.0)
    "min_skill_edge": 2,  # %, return of a copy over what its price promised, shrunk by the amount of evidence
}
SELECTIONS = {"skill": "Умение против цены", "winrate": "Доля выигрышей"}
METRICS_VERSION = 2       # 2: fee 0.05 * p * (1 - p) and skill against the price (1.10.0)
SKILL_MIN_BETS, SKILL_MIN_EVENTS = 20, 15
K_SHRINK = 40             # events of evidence for half weight
BH_Q = 0.10               # false discovery rate across all candidates (Benjamini–Hochberg)

TYPES = {
    "directional": "Направленный",
    "short_crypto": "Краткосрочные крипторынки",
    "market_maker": "Маркетмейкер",
    "sniper": "Скупщик почти решённых",
    "hft": "Высокочастотный бот",
    "scalper": "Скальпер",
    "thin": "Мало истории",
}


# ---------------------------------------------------------------- maths

FEE_RATE = 0.05     # taker fee of the markets copies are made in: 0.05 * p * (1 - p) per share, as live copies pay
# (until 1.10.0 the estimate used 0.02, two and a half times less than the copies actually paid)


def fee_est(price):
    """Per-share taker fee of a copy (the same formula real copies pay)."""
    return FEE_RATE * price * (1 - price)


def wilson_lb(wins, n, z=1.96):
    if n <= 0:
        return 0.0
    p = wins / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, (centre - spread) / (1 + z2 / n))


def copy_result(entry, exit_price, slip, resolved):
    """Return per $1 and per-share edge of a copy that entered `slip` dollars worse than the leader."""
    e = min(0.995, max(0.001, entry + slip))
    fb = fee_est(e)
    fs = 0.0 if resolved else fee_est(exit_price)
    gain = exit_price - fs - e - fb
    return gain / (e + fb), gain


def _date_ts(value):
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


SETTLE_EDGE = 0.0005  # exits this close to $0 / $1 are payouts at resolution (the Data API rounds the average entry price)


def settled(x):
    """An exit price with the rounding noise of a payout removed: 0.99990 -> 1.0 (until 1.4.4 such exits stayed as they were)."""
    if x is None:
        return None
    return 1.0 if x >= 1 - SETTLE_EDGE else 0.0 if x <= SETTLE_EDGE else x


def decided_exit(b):
    """Exit price of a decided bet and whether it was paid out at resolution (no fee on the way out)."""
    x = settled(b["exit"])
    return x, b["state"] == "resolved" or x in (0.0, 1.0)


# ---------------------------------------------------------------- bets from positions

def bet_from_position(r, now):
    """Compact bet from a Data API v2 position, or None when it cannot be a copy bet."""
    entry, size = r.get("avg_price"), r.get("total_size")
    if entry is None or not size or size <= 0:
        return None
    opened = pm.num(r.get("first_entry_at")) or pm.num(r.get("last_event_at"))
    last = pm.num(r.get("last_event_at")) or opened
    if not opened:
        return None
    end_ts = _date_ts(r.get("end_date"))
    title = str(r.get("title") or "")[:160]
    bet = {"token": str(r.get("token_id") or ""), "condition": str(r.get("condition_id") or "").lower(),
           "oidx": r.get("outcome_index"), "outcome": str(r.get("outcome") or "")[:40], "title": title,
           "event_slug": str(r.get("event_slug") or "")[:240], "entry": float(entry), "size": float(size),
           "opened": float(opened), "end": end_ts,
           "short": int(bool(SHORT_TERM.search(title) or SHORT_TERM.search(str(r.get("slug") or "")))),
           "state": "open", "exit": None, "closed": None, "mark": r.get("current_price")}
    if str(r.get("status")).upper() == "CLOSED":
        realized = r.get("realized_pnl") or 0.0
        bet["state"] = "closed"
        bet["exit"] = settled(_clamp(entry + realized / size))
        bet["closed"] = float(last)
    elif r.get("redeemable"):
        price = r.get("current_price")
        if price is None:
            return None
        bet["state"] = "resolved"
        bet["exit"] = 1.0 if price >= 0.5 else 0.0
        bet["closed"] = float(end_ts if end_ts and end_ts <= now else max(last, opened))
    return bet


SYNC_MARGIN = 86400          # re-read a day of closed positions on top: the Data API indexes with a delay


def crawl(wallet, since, now=None, prev=None, prev_at=None, prev_partial=False):
    """Positions of a wallet active since `since`, newest first, both statuses.

    With `prev` (the bets stored at `prev_at`): open positions are read in full as before, closed ones only if
    their last event is after prev_at minus a day; closed positions do not change, so the result is the same
    as a full crawl, for a fraction of the traffic. A position bought again (open now) leaves the closed set.
    """
    now = now or time.time()
    incremental = prev is not None and bool(prev_at)
    bets, partial, open_tokens = [], False, set()
    closed_rows = 0
    for status in ("CLOSED", "OPEN"):
        cursor, pages = "", 0
        start = int(prev_at - SYNC_MARGIN) if incremental and status == "CLOSED" else None
        while True:
            page = pm.positions_page(user=wallet, status=status, sort="TIMESTAMP", direction="DESC", limit=PAGE,
                                     cursor=cursor, start=start, gap=GAP)
            rows = page["rows"]
            for r in rows:
                if status == "OPEN":
                    open_tokens.add(str(r.get("token_id") or ""))
                b = bet_from_position(r, now)
                if b and b["opened"] >= since - 30 * DAY:
                    bets.append(b)
            pages += 1
            closed_rows += len(rows) if status == "CLOSED" else 0
            cursor = page["next"]
            oldest = min((pm.num(r.get("last_event_at")) or now for r in rows), default=0)
            if not cursor or not rows or oldest < since:
                break
            if pages >= MAX_PAGES[status]:
                if incremental and status == "CLOSED":
                    # more changed than a full crawl would read: fall back to a full crawl
                    return crawl(wallet, since, now)
                partial = True
                break
    if not incremental:
        return bets, partial
    merged = {b["token"]: b for b in prev if b["state"] == "closed" and b["token"] not in open_tokens
              and b["opened"] >= since - 30 * DAY}
    for b in bets:  # fresh closed, then open/resolved: the newest record of a token wins, as in a full crawl
        merged[b["token"]] = b
    out = list(merged.values())
    closed = sorted((b for b in out if b["state"] == "closed"), key=lambda b: -(b["closed"] or 0))
    cap = MAX_PAGES["CLOSED"] * PAGE
    if len(closed) > cap or prev_partial:  # a full crawl keeps only the newest `cap` closed positions
        keep = {id(b) for b in closed[:cap]}
        out = [b for b in out if b["state"] != "closed" or id(b) in keep]
        partial = partial or prev_partial or len(closed) > cap
    return out, partial


def measure_slip(trades, now=None, samples=12):
    """Median price change DELAY seconds after the leader's recent buys (positive = the copy pays more)."""
    now = now or time.time()
    picked, seen = [], set()
    for t in trades:
        if (t.get("side") or "").upper() != "BUY" or not t.get("price") or not t.get("size"):
            continue
        ts = int(t.get("timestamp") or 0)
        tok = str(t.get("token_id") or "")
        if t["price"] * t["size"] < MIN_COST or tok in seen or ts > now - DELAY - 120:
            continue
        if not PRICE_MIN <= t["price"] <= PRICE_MAX:
            continue
        seen.add(tok)
        picked.append((tok, ts, t["price"]))
        if len(picked) >= samples:
            break
    moves = []
    for tok, ts, price in picked:
        try:
            pts = pm.price_window(tok, ts - 60, ts + DELAY + 900, fidelity=1, gap=GAP)
        except (pm.UpstreamError, LookupError, ValueError):
            continue
        after = next((p for t, p in pts if t >= ts + DELAY), None)
        if after is not None:
            moves.append(after - price)
    if not moves:
        return None
    return {"median": statistics.median(moves), "samples": len(moves),
            "over": sum(1 for m in moves if m > DEFAULTS["max_slip"]) / len(moves)}


# ---------------------------------------------------------------- profile

def _decided(bets, slip, start, end):
    out = []
    for b in bets:
        if b["state"] == "open" or b["closed"] is None or b["closed"] > end:
            continue
        if not start <= b["opened"] <= end:
            continue
        if not PRICE_MIN <= b["entry"] <= PRICE_MAX or b["entry"] * b["size"] < MIN_COST or b["short"]:
            continue
        x, paid = decided_exit(b)
        r, edge = copy_result(b["entry"], x, slip, paid)
        if abs(r) < PUSH:
            continue
        out.append({"closed": b["closed"], "opened": b["opened"], "r": r, "edge": edge, "entry": b["entry"],
                    "win": r > 0, "hold": max(0.0, b["closed"] - b["opened"]) / DAY,
                    "ev": b.get("event_slug") or b.get("condition") or b["token"]})
    out.sort(key=lambda x: x["closed"])
    return out


def skill_stats(d, slip):
    """How much better a player's copies did than their price promised (research/compare_logics.py, logic L3).

    A copy bought at p wins with probability p if the price is fair, so its "excess" is win - p. Bets of one event
    move together, so the evidence is counted by event: the mean copy return of each event, its t statistic over
    events and a one-sided p-value. Edge and excess are shrunk towards zero by n / (n + 40) events. None when the
    history is too short to say."""
    if len(d) < SKILL_MIN_BETS:
        return None
    by = {}
    for x in d:
        e = by.setdefault(x["ev"], [0.0, 0.0, 0])
        e[0] += (1.0 if x["win"] else 0.0) - min(0.995, x["entry"] + slip)
        e[1] += x["r"]
        e[2] += 1
    n_ev = len(by)
    if n_ev < SKILL_MIN_EVENTS:
        return None
    rets = [e[1] / e[2] for e in by.values()]
    mean = statistics.mean(rets)
    sd = statistics.pstdev(rets) or 1e-9
    t = mean / (sd / math.sqrt(n_ev))
    k = n_ev / (n_ev + K_SHRINK)
    excess = sum(e[0] for e in by.values()) / sum(e[2] for e in by.values())
    return {"t": t, "p": 0.5 * math.erfc(t / math.sqrt(2)), "edge": mean * k, "excess": excess * k, "n_ev": n_ev}


def bh_cut(pvalues, q=BH_Q):
    """Benjamini–Hochberg: the largest p-value still significant among all candidates (0 when none is)."""
    ps = sorted(p for p in pvalues if p is not None)
    m = len(ps)
    cut = 0.0
    for i, p in enumerate(ps, 1):
        if p <= q * i / m:
            cut = p
    return cut


def metrics(bets, *, slip=DEFAULT_SLIP, now=None, as_of=None, lookback=LOOKBACK_DAYS, target_wr=70):
    """Copy statistics of one wallet. With `as_of`, only what was known at that moment is used."""
    now = now or time.time()
    end = as_of or now
    start = end - lookback * DAY
    d = _decided(bets, slip, start, end)
    n = len(d)
    wins = sum(1 for x in d if x["win"])
    rs = [x["r"] for x in d]
    pos, neg = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    equity, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    months = {}
    for x in d:
        key = datetime.fromtimestamp(x["closed"], timezone.utc).strftime("%Y-%m")
        m = months.setdefault(key, [0, 0])
        m[0] += 1
        m[1] += int(x["win"])
    monthly = [{"month": k, "n": v[0], "wr": v[1] / v[0]} for k, v in sorted(months.items())]
    counted = [m for m in monthly if m["n"] >= 5]
    good = sum(1 for m in counted if m["wr"] >= target_wr / 100 - 0.10)
    recent = d[-20:]
    streak = 0
    for x in reversed(d):
        if streak == 0:
            streak = 1 if x["win"] else -1
        elif (streak > 0) == x["win"]:
            streak += 1 if x["win"] else -1
        else:
            break
    live = [b for b in bets if b["opened"] <= end and PRICE_MIN <= b["entry"] <= PRICE_MAX
            and b["entry"] * b["size"] >= MIN_COST and not b["short"]]
    open_now = [b for b in bets if b["state"] == "open" and as_of is None]
    open_r = [copy_result(b["entry"], b["mark"], slip, False)[0] for b in open_now
              if b["mark"] is not None and PRICE_MIN <= b["entry"] <= PRICE_MAX and not b["short"]]
    return {
        "n": n, "wins": wins, "losses": n - wins, "wr": wins / n if n else None, "lb": wilson_lb(wins, n),
        "roi": sum(rs) / n if n else None, "edge": sum(x["edge"] for x in d) / n if n else None,
        "avg_entry": sum(x["entry"] for x in d) / n if n else None,
        "pf": (pos / neg) if neg > 0 else (None if pos == 0 else 99.0), "max_dd": dd,
        "top_share": (max(rs) / pos) if pos > 0 else None,
        "monthly": monthly[-8:], "stability": (good / len(counted)) if counted else None,
        "recent_n": len(recent), "recent_wr": (sum(1 for x in recent if x["win"]) / len(recent)) if recent else None,
        "streak": streak,
        "new_14d": sum(1 for b in live if b["opened"] >= end - 14 * DAY),
        "new_30d": sum(1 for b in live if b["opened"] >= end - 30 * DAY),
        "hold_days": statistics.median([x["hold"] for x in d]) if d else None,
        "open_n": len(open_r), "open_r": (sum(open_r) / len(open_r)) if open_r else None,
        "first_seen": min((b["opened"] for b in bets), default=None),
        "slip": slip,
        "skill": skill_stats(d, slip),
        "version": METRICS_VERSION,
    }


def behaviour(bets, trades, m, now=None):
    """Classify how the wallet trades (only directional wallets are worth copying)."""
    now = now or time.time()
    total = len(bets)
    short = sum(b["short"] for b in bets) / total if total else 0.0
    sides = {}
    for b in bets:
        sides.setdefault(b["condition"], set()).add(b["oidx"])
    both = sum(1 for v in sides.values() if len(v) > 1) / len(sides) if sides else 0.0
    sniper = sum(1 for b in bets if b["entry"] >= 0.95) / total if total else 0.0
    per_day, sells = 0.0, 0.0
    if trades:
        stamps = [int(t.get("timestamp") or 0) for t in trades]
        span = max(1.0, (max(stamps) - min(stamps)) / DAY) if len(stamps) > 1 else 1.0
        per_day = len(trades) / span if len(trades) >= 20 else len(trades) / max(span, 7)
        sells = sum(1 for t in trades if (t.get("side") or "").upper() == "SELL") / len(trades)
    facts = {"short_share": short, "both_sides": both, "sniper_share": sniper, "trades_day": per_day,
             "sell_share": sells, "bets_total": total}
    age_days = (now - m["first_seen"]) / DAY if m.get("first_seen") else 0
    if short >= 0.4:
        kind = "short_crypto"
    elif per_day >= 300:
        kind = "hft"
    elif both >= 0.25 or (per_day >= 150 and sells >= 0.4):
        kind = "market_maker"
    elif sniper >= 0.5:
        kind = "sniper"
    elif m.get("hold_days") is not None and m["hold_days"] < 1 and sells >= 0.6:
        kind = "scalper"
    elif age_days < 45 or m["n"] < 15:
        kind = "thin"
    else:
        kind = "directional"
    return kind, facts


def gates(m, kind, settings, cut=None):
    """Selection checks with the user's thresholds. Each item: key, label, ok, value, need.

    `cut` is the Benjamini–Hochberg p-value threshold across all candidates (skill selection only)."""
    s = {**DEFAULTS, **{k: v for k, v in (settings or {}).items() if k in DEFAULTS}}
    pct = lambda v: None if v is None else round(v * 100, 1)  # noqa: E731
    if s.get("selection") == "skill":
        sk = m.get("skill")
        cut = 0.0 if cut is None else cut
        return [
            {"key": "type", "label": "Тип поведения", "ok": kind == "directional", "value": TYPES.get(kind, kind),
             "need": TYPES["directional"]},
            {"key": "history", "label": "История для оценки умения", "ok": sk is not None,
             "value": f"{m['n']} ставок, {sk['n_ev']} событий" if sk else f"{m['n']} ставок",
             "need": f"от {SKILL_MIN_BETS} ставок и {SKILL_MIN_EVENTS} событий"},
            {"key": "skill", "label": "Умение доказано (не удача)", "ok": bool(sk) and sk["p"] <= cut and cut > 0,
             "value": None if not sk else f"p = {sk['p']:.4f}", "need": f"p ≤ {cut:.4f} (поправка на {int(BH_Q * 100)}% ложных)"},
            {"key": "edge", "label": "Доходность сверх цены", "ok": bool(sk) and sk["edge"] * 100 >= s["min_skill_edge"],
             "value": None if not sk else pct(sk["edge"]), "need": f"от {s['min_skill_edge']}%"},
            {"key": "excess", "label": "Выигрывает чаще, чем обещала цена", "ok": bool(sk) and sk["excess"] > 0,
             "value": None if not sk else pct(sk["excess"]), "need": "больше 0 п. п."},
            {"key": "top", "label": "Доля лучшей ставки", "ok": m["top_share"] is not None and m["top_share"] * 100 <= s["max_top_share"],
             "value": pct(m["top_share"]), "need": f"до {s['max_top_share']}%"},
            {"key": "recent", "label": "Новых ставок за 14 дней", "ok": m["new_14d"] >= s["min_recent"], "value": m["new_14d"],
             "need": f"от {s['min_recent']}"},
            {"key": "slip", "label": "Проскальзывание", "ok": m.get("slip_measured") is not None and m["slip"] <= s["max_slip"],
             "value": None if m.get("slip_measured") is None else round(m["slip"], 3), "need": f"до ${s['max_slip']}"},
        ]
    out = [
        {"key": "type", "label": "Тип поведения", "ok": kind == "directional", "value": TYPES.get(kind, kind),
         "need": TYPES["directional"]},
        {"key": "bets", "label": "Решённых ставок", "ok": m["n"] >= s["min_bets"], "value": m["n"], "need": f"от {s['min_bets']}"},
        {"key": "wr", "label": "Доля выигрышей копий", "ok": (m["wr"] or 0) * 100 >= s["min_wr"], "value": pct(m["wr"]),
         "need": f"от {s['min_wr']}%"},
        {"key": "lb", "label": "Надёжная граница", "ok": m["lb"] * 100 >= s["min_lb"], "value": pct(m["lb"]),
         "need": f"от {s['min_lb']}%"},
        {"key": "roi", "label": "Доходность на ставку", "ok": (m["roi"] or -1) * 100 >= s["min_roi"], "value": pct(m["roi"]),
         "need": f"от {s['min_roi']}%"},
        {"key": "edge", "label": "Преимущество над ценой", "ok": (m["edge"] or -1) >= s["min_edge"],
         "value": None if m["edge"] is None else round(m["edge"], 3), "need": f"от ${s['min_edge']}"},
        {"key": "top", "label": "Доля лучшей ставки", "ok": m["top_share"] is not None and m["top_share"] * 100 <= s["max_top_share"],
         "value": pct(m["top_share"]), "need": f"до {s['max_top_share']}%"},
        {"key": "recent", "label": "Новых ставок за 14 дней", "ok": m["new_14d"] >= s["min_recent"], "value": m["new_14d"],
         "need": f"от {s['min_recent']}"},
        {"key": "slip", "label": "Проскальзывание", "ok": m.get("slip_measured") is not None and m["slip"] <= s["max_slip"],
         "value": None if m.get("slip_measured") is None else round(m["slip"], 3), "need": f"до ${s['max_slip']}"},
    ]
    return out


def score(m, selection="winrate"):
    """0–100. Skill: return over the price 50, evidence 20, stability 15, copyability 15.
    Win rate: reliability 40, return 25, stability 15, copyability 10, recent form 10."""
    if not m["n"]:
        return 0.0
    if selection == "skill":
        sk = m.get("skill")
        if not sk:
            return 0.0
        part = 50 * _clamp(sk["edge"] / 0.12)
        part += 20 * _clamp((sk["t"] - 2) / 4)
        part += 15 * (m["stability"] if m["stability"] is not None else 0.5)
        part += 15 * _clamp(1 - (m["slip"] or 0) / 0.04)
        return round(part, 1)
    part = 40 * _clamp((m["lb"] - 0.55) / 0.30)
    part += 25 * _clamp((m["roi"] or 0) / 0.25)
    part += 15 * (m["stability"] if m["stability"] is not None else 0.5)
    part += 10 * _clamp(1 - (m["slip"] or 0) / 0.04)
    part += 10 * _clamp(((m["recent_wr"] if m["recent_wr"] is not None else m["wr"] or 0) - 0.5) / 0.4)
    return round(part, 1)


def judge(m, kind, settings, cut=None):
    checks = gates(m, kind, settings, cut)
    mode = (settings or {}).get("selection", DEFAULTS["selection"])
    return {"passed": all(c["ok"] for c in checks), "checks": checks, "score": score(m, mode),
            "failed": [c["label"] for c in checks if not c["ok"]], "selection": mode}


_cut_cache = {"at": 0.0, "cut": 0.0}


def skill_cut(db, ttl=300):
    """The Benjamini–Hochberg threshold over every profiled directional candidate (cached for a few minutes)."""
    if time.time() - _cut_cache["at"] < ttl:
        return _cut_cache["cut"]
    ps = []
    for kind, raw in db.execute("SELECT kind, metrics FROM scout_wallets WHERE metrics IS NOT NULL"):
        if kind != "directional":
            continue
        sk = json.loads(raw).get("skill")
        if sk:
            ps.append(sk["p"])
    _cut_cache.update(at=time.time(), cut=bh_cut(ps))
    return _cut_cache["cut"]


# ---------------------------------------------------------------- storage and crawl jobs

def discover(db):
    """Add leaderboard wallets with positive PnL to the candidate pool (every 6 hours).

    Copies of most profitable traders win only 40–55% of the time, and about 3% reach 70%, so the
    pool has to be wide: top 200 of every category for the week, the month and all time.
    """
    last = float(store.kv_get(db, "scout:discovered", "0") or 0)
    if time.time() - last < DISCOVER_EVERY:
        return 0
    added = 0
    for category in pm.LEADER_CATEGORIES:
        for period in ("week", "month", "all"):
            res = pm.leaderboard(db, period=period, category=category, limit=200, ttl=3600, gap=GAP)
            for r in (res["data"] or {}).get("rows") or []:
                if (r["pnl"] or 0) <= 0:
                    continue
                row = db.execute("SELECT sources FROM scout_wallets WHERE wallet=?", (r["wallet"],)).fetchone()
                sources = set(json.loads(row[0])) if row else set()
                sources.add(f"{category}:{period}")
                if row:
                    db.execute("UPDATE scout_wallets SET name=CASE WHEN ?!='' THEN ? ELSE name END, image=CASE WHEN ?!='' THEN ? ELSE image END, sources=? WHERE wallet=?",
                               (r["name"], r["name"], r["image"], r["image"], json.dumps(sorted(sources)), r["wallet"]))
                else:
                    db.execute("INSERT INTO scout_wallets(wallet, name, image, sources, added) VALUES(?,?,?,?,?)",
                               (r["wallet"], r["name"], r["image"], json.dumps(sorted(sources)), time.time()))
                    added += 1
    store.kv_set(db, "scout:discovered", str(time.time()))
    return added


def add_wallet(db, wallet, source="manual", name="", image=""):
    wallet = pm.address(wallet).lower()
    row = db.execute("SELECT sources FROM scout_wallets WHERE wallet=?", (wallet,)).fetchone()
    if row:
        sources = sorted(set(json.loads(row[0])) | {source})
        db.execute("UPDATE scout_wallets SET sources=?, priority=1 WHERE wallet=?", (json.dumps(sources), wallet))
    else:
        db.execute("INSERT INTO scout_wallets(wallet, name, image, sources, added, priority) VALUES(?,?,?,?,?,1)",
                   (wallet, name, image, json.dumps([source]), time.time()))
    return wallet


def next_wallet(db, skip=()):
    """Roster wallets first (every 6 h), then requested and new ones, then the stalest (every 72 h).

    New wallets found on more leaderboards go first. `skip` holds wallets other threads are profiling.
    """
    now = time.time()
    skip = list(skip)
    holes = ",".join("?" * len(skip))
    not_busy = f" AND s.wallet NOT IN ({holes})" if skip else ""
    row = db.execute(f"""SELECT s.wallet FROM scout_wallets s JOIN ap_players p ON p.wallet=s.wallet
                        WHERE p.state IN ('active','bench') AND (s.profiled IS NULL OR s.profiled < ?){not_busy}
                        ORDER BY s.profiled IS NOT NULL, s.profiled LIMIT 1""", (now - ROSTER_TTL, *skip)).fetchone()
    if row:
        return row[0]
    row = db.execute(f"""SELECT s.wallet FROM scout_wallets s WHERE (s.priority>0 OR s.profiled IS NULL OR s.profiled < ?){not_busy}
                        ORDER BY s.priority DESC, s.profiled IS NOT NULL, s.profiled, length(s.sources) DESC, s.added
                        LIMIT 1""", (now - PROFILE_TTL, *skip)).fetchone()
    return row[0] if row else None


def profile_wallet(db, wallet, now=None):
    """Crawl a wallet, store its bets (for directional or roster wallets) and its metrics."""
    now = now or time.time()
    since = now - CRAWL_DAYS * DAY
    prev_row = db.execute("SELECT profiled, metrics FROM scout_wallets WHERE wallet=?", (wallet,)).fetchone()
    prev = packs.load(db, wallet) if prev_row and prev_row[0] else []
    if prev:  # bets kept from the last profile: only what changed since then is downloaded
        prev_partial = bool(json.loads(prev_row[1] or "{}").get("partial"))
        bets, partial = crawl(wallet, since, now, prev=prev, prev_at=prev_row[0], prev_partial=prev_partial)
    else:
        bets, partial = crawl(wallet, since, now)
    trades = pm.trades_page(user=wallet, limit=100, taker_only=False, gap=GAP)["rows"]
    m = metrics(bets, now=now)
    kind, facts = behaviour(bets, trades, m, now)
    in_roster = db.execute("SELECT 1 FROM ap_players WHERE wallet=? AND state IN ('active','bench')", (wallet,)).fetchone()
    # worth a closer look: wins often, or wins more often than its prices promised (a buyer of cheap outcomes
    # with 40% wins can be the better player; until 1.10.0 only wr >= 55% was kept)
    sk = m.get("skill")
    promising = kind == "directional" and m["n"] >= 15 and ((m["wr"] or 0) >= 0.55 or bool(sk and sk["edge"] > 0))
    slip = None
    if promising or in_roster:
        slip = measure_slip(trades, now)
    live = live_slip(db, wallet)
    used = copy_slip(slip["median"] if slip else None, live)
    if used is not None:
        m = metrics(bets, slip=used, now=now)
    m["slip_measured"] = slip["median"] if slip else (live["mean"] if live else None)
    m["slip_samples"] = slip["samples"] if slip else 0
    m["slip_over"] = slip["over"] if slip else None
    m["slip_live"] = live["mean"] if live else None
    m["slip_live_n"] = live["n"] if live else 0
    m["partial"] = partial
    keep = promising or in_roster
    with store.tx(db):
        packs.save(db, wallet, bets if keep else [])
        name = next((t.get("pseudonym") or t.get("name") for t in trades if t.get("pseudonym") or t.get("name")), "")
        db.execute("""UPDATE scout_wallets SET profiled=?, kind=?, facts=?, metrics=?, score=?, error='', priority=0,
                      name=CASE WHEN name='' THEN ? ELSE name END WHERE wallet=?""",
                   (now, kind, json.dumps(facts), json.dumps(m), score(m), name or "", wallet))
    return m


LIVE_SLIP_MIN = 5            # live market copies of a player before their price counts
LIVE_SLIP_DAYS = 60


def live_slip(db, wallet):
    """What copies of this player really paid over the player's price: market-order buys of every user's copies.

    The price a minute after the player's buy (measure_slip) misses that the player's own buy has just taken the
    cheap asks; live copies of thin markets paid about four times more (24.11.2026: $0.058 against $0.014)."""
    rows = db.execute("""SELECT e.price - e.leader_price FROM copy_events e JOIN orders o ON o.id=e.order_id
                         WHERE e.leader=? AND e.status='filled' AND e.side='buy' AND o.type='market'
                         AND e.leader_price > 0 AND e.price > 0 AND e.at > ?""",
                      (wallet, time.time() - LIVE_SLIP_DAYS * DAY)).fetchall()
    moves = [r[0] for r in rows if r[0] is not None]
    if len(moves) < LIVE_SLIP_MIN:
        return None
    return {"mean": sum(moves) / len(moves), "n": len(moves)}


def copy_slip(measured, live):
    """Slippage a copy is valued with: the worse of the minute-after price and what live copies paid."""
    vals = [v for v in (measured, live["mean"] if live else None) if v is not None]
    return max(DEFAULT_SLIP, *vals) if vals else None


def current(m):
    """Metrics counted by this version (older ones lack fields the rules use and are not judged until recomputed)."""
    return bool(m) and m.get("version") == METRICS_VERSION


def refresh_metrics(db, limit=200):
    """Recompute stored metrics of an older version from the bets kept for the wallet (no network).

    Players of an autopilot come first. Wallets whose bets were not kept stay as they are until their next profile."""
    done = 0
    rows = store.rows(db.execute("""SELECT wallet, metrics, profiled FROM scout_wallets WHERE metrics IS NOT NULL
                                    ORDER BY wallet IN (SELECT wallet FROM ap_players) DESC"""))
    for row in rows:
        old = json.loads(row["metrics"])
        if old.get("version") == METRICS_VERSION:
            continue
        bets = packs.load(db, row["wallet"])
        if not bets:
            continue
        # as of the stored profile: the bets kept end there, so "new bets in 14 days" stays what it was
        m = metrics(bets, slip=old.get("slip") or DEFAULT_SLIP, now=row["profiled"] or time.time())
        for k in ("slip_measured", "slip_samples", "slip_over", "slip_live", "slip_live_n", "partial"):
            if k in old:
                m[k] = old[k]
        db.execute("UPDATE scout_wallets SET metrics=?, score=? WHERE wallet=?", (json.dumps(m), score(m), row["wallet"]))
        done += 1
        if done >= limit:
            break
    if done:
        _cut_cache["at"] = 0.0
    return done


def stored_bets(db, wallet):
    return packs.load(db, wallet)


def candidate(db, wallet):
    row = store.one(db.execute("SELECT * FROM scout_wallets WHERE wallet=?", (wallet,)))
    if not row:
        return None
    row["metrics"] = json.loads(row["metrics"]) if row["metrics"] else None
    row["facts"] = json.loads(row["facts"]) if row["facts"] else None
    row["sources"] = json.loads(row["sources"] or "[]")
    return row


def candidates(db, settings):
    """All profiled wallets with the user's verdict, best first."""
    out = []
    cut = skill_cut(db)
    for row in store.rows(db.execute("SELECT * FROM scout_wallets WHERE metrics IS NOT NULL")):
        m = json.loads(row["metrics"])
        verdict = judge(m, row["kind"], settings, cut)
        sk = m.get("skill") or {}
        out.append({"wallet": row["wallet"], "name": row["name"], "image": row["image"], "kind": row["kind"],
                    "kind_label": TYPES.get(row["kind"], row["kind"]), "sources": json.loads(row["sources"] or "[]"),
                    "profiled": row["profiled"], "n": m["n"], "wr": m["wr"], "lb": m["lb"], "roi": m["roi"], "edge": m["edge"],
                    "avg_entry": m["avg_entry"], "new_14d": m["new_14d"], "slip": m.get("slip_measured"),
                    "recent_wr": m["recent_wr"], "skill_edge": sk.get("edge"), "skill_excess": sk.get("excess"),
                    "skill_p": sk.get("p"), "score": verdict["score"], "passed": verdict["passed"],
                    "failed": verdict["failed"]})
    out.sort(key=lambda r: (not r["passed"], -r["score"]))
    return out


def status(db):
    total = db.execute("SELECT COUNT(*) FROM scout_wallets").fetchone()[0]
    done = db.execute("SELECT COUNT(*) FROM scout_wallets WHERE profiled IS NOT NULL").fetchone()[0]
    return {"total": total, "profiled": done, "discovered": float(store.kv_get(db, "scout:discovered", "0") or 0) or None}


# ---------------------------------------------------------------- history check (walk-forward)

def backtest(db, settings, count, days_back=60, now=None):
    """Pick a roster with data known `days_back` days ago, then score what its bets did afterwards."""
    now = now or time.time()
    as_of = now - days_back * DAY
    picks, pool_after, known = [], [], []
    for row in store.rows(db.execute("SELECT wallet, name, image, kind, metrics FROM scout_wallets WHERE metrics IS NOT NULL")):
        bets = stored_bets(db, row["wallet"])
        if not bets:
            continue
        cur = json.loads(row["metrics"])
        slip = cur.get("slip") or DEFAULT_SLIP
        past = metrics(bets, slip=slip, now=now, as_of=as_of, target_wr=settings.get("min_wr", 70))
        past["slip_measured"] = cur.get("slip_measured")
        after = _after(bets, slip, as_of, now)
        pool_after.extend(after)
        known.append((row, past, after))
    # skill is judged against every candidate known then, as it is today
    cut = bh_cut([p["skill"]["p"] for r, p, _ in known if r["kind"] == "directional" and p.get("skill")])
    for row, past, after in known:
        if row["kind"] != "directional":
            continue
        verdict = judge(past, row["kind"], settings, cut)
        if verdict["passed"]:
            picks.append({"wallet": row["wallet"], "name": row["name"], "image": row["image"], "score": verdict["score"],
                          "before": {"n": past["n"], "wr": past["wr"], "roi": past["roi"]}, "after_bets": after})
    picks.sort(key=lambda p: -p["score"])
    chosen = picks[:count]
    picked_bets = [b for p in chosen for b in p["after_bets"]]
    for i, p in enumerate(picks):
        items = p.pop("after_bets")
        if i < count:
            p["after"] = _summary(items)
    return {"as_of": as_of, "days": days_back, "now": now, "count": count, "eligible": len(picks),
            "roster": _summary(picked_bets) if chosen else None, "baseline": _summary(pool_after),
            "players": chosen}


def _after(bets, slip, as_of, now):
    out = []
    for b in bets:
        if b["opened"] < as_of or b["short"] or not PRICE_MIN <= b["entry"] <= PRICE_MAX or b["entry"] * b["size"] < MIN_COST:
            continue
        if b["state"] == "open":
            if b["mark"] is None:
                continue
            r, _ = copy_result(b["entry"], b["mark"], slip, False)
            out.append({"r": r, "decided": False})
        else:
            x, paid = decided_exit(b)
            r, _ = copy_result(b["entry"], x, slip, paid)
            if abs(r) >= PUSH:
                out.append({"r": r, "decided": True})
    return out


def _summary(items):
    decided = [x for x in items if x["decided"]]
    wins = sum(1 for x in decided if x["r"] > 0)
    open_ = [x for x in items if not x["decided"]]
    return {"bets": len(decided), "wins": wins, "wr": wins / len(decided) if decided else None,
            "roi": sum(x["r"] for x in decided) / len(decided) if decided else None,
            "open": len(open_), "open_roi": sum(x["r"] for x in open_) / len(open_) if open_ else None}
