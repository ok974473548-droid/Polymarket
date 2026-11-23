"""Walk-forward comparison of three player-selection logics on the same data.

L1  Kontur 1.1.1 as is: copy win rate >= 70%, Wilson bound, score, top 8 (scout.judge).
L2  Whalidate-like: wallet screen from the public methodology (ROI on money put in >= 15%, >= 30 settled
    trades, drawdown <= 25%, best trade <= 50% of profit) + preset rules on trades (price band, resolves
    within N days, whale size). All passing wallets are copied, as in their presets.
L3  Skill vs price: copy return measured against what the entry price promised, clustered by event,
    Benjamini–Hochberg across all candidates, shrunk edge; quarter-Kelly sizing; consensus of players.

Everything a logic uses at date T is known at T: only bets opened before T, outcomes only if settled before T,
behaviour type from bets known at T. Copies are judged on bets opened in [T, T+30d).
Same copy cost for every logic: leader price + slippage, real-size taker fee 0.05 * p * (1 - p) on entry and
on a sale before resolution. Copies follow the leader's exit (data has no separate resolution price for sold
positions).

Run: PYTHONPATH=. python research/compare_logics.py data/audit.sqlite3
"""
import json
import math
import random
import statistics
import sys
from collections import defaultdict

from kontur import db as store
from kontur import scout

DAY = 86400
FEE = 0.05
DB = sys.argv[1]
db = store.connect(DB)
NOW = float(db.execute("SELECT v FROM audit_meta WHERE k='now'").fetchone()[0])
WINDOWS = [150, 120, 90, 60]  # selection date T = NOW - d days; copies judged on bets opened in [T, T+30d)
HORIZON = 30 * DAY
ROSTER = 8

rows = store.rows(db.execute("SELECT wallet, name, metrics FROM scout_wallets WHERE metrics IS NOT NULL"))
WAL = {}
for r in rows:
    bets = scout.stored_bets(db, r["wallet"])
    if not bets:
        continue
    m = json.loads(r["metrics"])
    for b in bets:  # data fix applied to every logic: finished markets with a 0/1 price are resolved
        if b["state"] == "open" and b["end"] and b["end"] < NOW - 7 * DAY and b["mark"] is not None and (b["mark"] <= 0.01 or b["mark"] >= 0.99):
            b["state"], b["exit"], b["closed"] = "resolved", 1.0 if b["mark"] >= 0.5 else 0.0, b["end"]
    WAL[r["wallet"]] = {"name": r["name"] or r["wallet"][:10], "bets": bets,
                        "slip": max(scout.DEFAULT_SLIP, m.get("slip_measured") or 0.01) if m.get("slip_measured") is not None else 0.01,
                        "slip_measured": m.get("slip_measured")}


def fee(p, rate=FEE):
    return rate * p * (1 - p)


def copy_r(entry, exit_price, slip, resolved, rate=FEE):
    e = min(0.995, max(0.001, entry + slip))
    gain = exit_price - (0.0 if resolved else fee(exit_price, rate)) - e - fee(e, rate)
    return gain / (e + fee(e, rate))


def ok_bet(b):
    return scout.PRICE_MIN <= b["entry"] <= scout.PRICE_MAX and b["entry"] * b["size"] >= scout.MIN_COST and not b["short"]


def known_at(bets, T):
    """Bets as they looked at T: later outcomes hidden."""
    out = []
    for b in bets:
        if b["opened"] > T:
            continue
        if b["closed"] and b["closed"] > T:
            b = dict(b, state="open", exit=None, closed=None)
        out.append(b)
    return out


def settled(bets, T, lookback=scout.LOOKBACK_DAYS):
    return [b for b in bets if b["state"] != "open" and b["closed"] and b["closed"] <= T
            and b["opened"] >= T - lookback * DAY and ok_bet(b)]


def kind_at(known, T):
    m = scout.metrics(known, now=T)
    return scout.behaviour(known, [], m, T)[0]


def future(w, T):
    """Copy results of the leader's bets opened in [T, T+30d), judged at NOW."""
    out = []
    for b in WAL[w]["bets"]:
        if not T <= b["opened"] < T + HORIZON or not ok_bet(b):
            continue
        if b["state"] == "open":
            if b["mark"] is None:
                continue
            r, decided = copy_r(b["entry"], b["mark"], WAL[w]["slip"], False), False
        else:
            r, decided = copy_r(b["entry"], b["exit"], WAL[w]["slip"], b["state"] == "resolved" or b["exit"] in (0.0, 1.0)), True
        out.append({"w": w, "r": r, "decided": decided, "entry": b["entry"], "cond": b["condition"], "oidx": b["oidx"],
                    "event": b["event_slug"], "days": ((b["end"] or b["opened"]) - b["opened"]) / DAY,
                    "cost": b["entry"] * b["size"], "opened": b["opened"]})
    return out


# ---------------------------------------------------------------- L1: Kontur as is
def logic_kontur(T):
    picks = []
    for w, d in WAL.items():
        known = known_at(d["bets"], T)
        m = scout.metrics(known, slip=d["slip"], now=T, as_of=T)
        m["slip_measured"] = d["slip_measured"] if d["slip_measured"] is not None else scout.DEFAULT_SLIP
        k = scout.behaviour(known, [], m, T)[0]
        v = scout.judge(m, k, {})
        if k == "directional" and v["passed"]:
            picks.append((v["score"], w))
    picks.sort(reverse=True)
    chosen = [w for _, w in picks[:ROSTER]]
    copies = []
    for w in chosen:  # Kontur copies: price 0.02-0.97, $5+, market resolves within 60 days
        copies += [dict(c, wt=1.0) for c in future(w, T) if c["days"] <= 60]
    return chosen, copies, len(picks)


# ---------------------------------------------------------------- L2: Whalidate-like
PRESETS = {  # name: (price lo, price hi, max days to resolution, min whale buy $)
    "40-80¢, до 30 дн. (как Helm/Beacon)": (0.40, 0.80, 30, 0),
    "80-98¢, до 30 дн. (как Anchor)": (0.80, 0.98, 30, 0),
    "60-90¢, до 7 дн., кит от $2000 (как Current)": (0.60, 0.90, 7, 2000),
    "40-98¢, до 30 дн. (как Reef)": (0.40, 0.98, 30, 0),
}


def whalidate_screen(w, T):
    d = WAL[w]
    known = known_at(d["bets"], T)
    st = sorted(settled(known, T, lookback=10_000), key=lambda b: b["closed"])
    if len(st) < 30:
        return False
    pnl = [(b["exit"] - b["entry"]) * b["size"] for b in st]
    cost = sum(b["entry"] * b["size"] for b in st)
    roi = sum(pnl) / cost if cost else 0
    pos = sum(p for p in pnl if p > 0)
    top = max(pnl) / pos if pos > 0 else 1
    # drawdown of a $1000 bankroll copying every trade with $25 (their app example: "One copy $25")
    eq, peak, dd = 1000.0, 1000.0, 0.0
    for b in st:
        eq += 25 * (b["exit"] - b["entry"]) / b["entry"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 1)
    return roi >= 0.15 and dd <= 0.25 and top <= 0.5


def logic_whalidate(T, preset):
    lo, hi, days, min_usd = PRESETS[preset]
    chosen = [w for w in WAL if whalidate_screen(w, T)]
    copies = []
    for w in chosen:
        copies += [dict(c, wt=1.0) for c in future(w, T) if lo <= c["entry"] <= hi and c["days"] <= days and c["cost"] >= min_usd]
    return chosen, copies, len(chosen)


# ---------------------------------------------------------------- L3: skill vs price
K_SHRINK = 40      # events of evidence for half weight
BH_Q = 0.10


def skill(w, T):
    d = WAL[w]
    known = known_at(d["bets"], T)
    if kind_at(known, T) != "directional":
        return None
    st = settled(known, T)
    if len(st) < 20:
        return None
    by_ev = defaultdict(lambda: [0.0, 0.0, 0])  # excess wins, return sum, count
    for b in st:
        p = min(0.995, b["entry"] + d["slip"])
        r = copy_r(b["entry"], b["exit"], d["slip"], b["state"] == "resolved" or b["exit"] in (0.0, 1.0))
        win = 1.0 if r > 0 else 0.0
        e = by_ev[b["event_slug"] or b["condition"]]
        e[0] += win - p
        e[1] += r
        e[2] += 1
    ev = list(by_ev.values())
    n_ev = len(ev)
    if n_ev < 15:
        return None
    rets = [e[1] / e[2] for e in ev]   # one number per event: no double counting of one situation
    mean = statistics.mean(rets)
    sd = statistics.pstdev(rets) or 1e-9
    t = mean / (sd / math.sqrt(n_ev))
    p_val = 0.5 * math.erfc(t / math.sqrt(2))
    shrunk = mean * n_ev / (n_ev + K_SHRINK)
    excess = sum(e[0] for e in ev) / sum(e[2] for e in ev)  # win rate above the price paid
    return {"w": w, "t": t, "p": p_val, "edge": shrunk, "excess": excess * n_ev / (n_ev + K_SHRINK), "n_ev": n_ev}


def logic_skill(T, top=None):
    cands = [s for s in (skill(w, T) for w in WAL) if s]
    cands.sort(key=lambda s: s["p"])
    m = len(cands)
    k = max([i for i, s in enumerate(cands, 1) if s["p"] <= BH_Q * i / m], default=0)
    passed = [s for s in cands[:k] if s["edge"] > 0.02 and s["excess"] > 0]
    passed.sort(key=lambda s: -s["edge"])
    chosen = passed[:top] if top else passed
    info = {s["w"]: s for s in chosen}
    copies = []
    for w in info:
        for c in future(w, T):
            if c["days"] > 60:
                continue
            p = min(0.99, c["entry"] + WAL[w]["slip"])
            q = min(0.99, p + info[w]["excess"])
            kelly = max(0.0, (q - p) / (1 - p)) * 0.25 if q > p else 0.0
            if kelly <= 0 or info[w]["edge"] <= fee(p):  # expected edge must beat the fee
                continue
            copies.append(dict(c, wt=min(kelly, 0.05)))
    # consensus: same market and side by 2+ chosen players -> double weight; opposite sides -> skip both
    sides = defaultdict(set)
    for c in copies:
        sides[c["cond"]].add(c["oidx"])
    buyers = defaultdict(set)
    for c in copies:
        buyers[(c["cond"], c["oidx"])].add(c["w"])
    out = []
    for c in copies:
        if len(sides[c["cond"]]) > 1:
            continue
        out.append(dict(c, wt=c["wt"] * (2 if len(buyers[(c["cond"], c["oidx"])]) > 1 else 1)))
    return [s["w"] for s in chosen], out, len(passed)


# ---------------------------------------------------------------- baselines
def logic_all(T):
    ws = [w for w in WAL if kind_at(known_at(WAL[w]["bets"], T), T) == "directional"]
    return ws, [dict(c, wt=1.0) for w in ws for c in future(w, T) if c["days"] <= 60], len(ws)


def logic_random(T, seed):
    ws, _, _ = logic_all(T)
    rng = random.Random(seed)
    pick = rng.sample(ws, min(ROSTER, len(ws)))
    return pick, [dict(c, wt=1.0) for w in pick for c in future(w, T) if c["days"] <= 60], len(ws)


# ---------------------------------------------------------------- scoring of copies
def stats(copies):
    if not copies:
        return {"copies": 0}
    wsum = sum(c["wt"] for c in copies)
    by_p = defaultdict(list)
    for c in copies:
        by_p[c["w"]].append(c)
    p_ret = {w: sum(c["r"] * c["wt"] for c in cs) / sum(c["wt"] for c in cs) for w, cs in by_p.items()}
    contrib = {w: sum(c["r"] * c["wt"] for c in cs) for w, cs in by_p.items()}
    best = max(contrib, key=contrib.get)
    rest = [c for c in copies if c["w"] != best]
    return {
        "copies": len(copies), "players_traded": len(by_p),
        "decided_share": round(sum(c["decided"] for c in copies) / len(copies), 2),
        "wr": round(100 * sum(c["wt"] for c in copies if c["r"] > 0) / wsum, 1),
        "roi": round(100 * sum(c["r"] * c["wt"] for c in copies) / wsum, 2),
        "roi_player_equal": round(100 * statistics.mean(p_ret.values()), 2),
        "roi_without_best": round(100 * sum(c["r"] * c["wt"] for c in rest) / sum(c["wt"] for c in rest), 2) if rest else None,
        "best_share": round(contrib[best] / sum(abs(v) for v in contrib.values()), 2) if contrib else None,
    }


def boot_ci(copies, n=1000):
    """95% interval of the weighted ROI, resampling players (copies of one player are not independent)."""
    by_p = defaultdict(list)
    for c in copies:
        by_p[c["w"]].append(c)
    ps = list(by_p.values())
    if len(ps) < 2:
        return None
    rng = random.Random(7)
    vals = []
    for _ in range(n):
        sample = [c for _ in ps for c in rng.choice(ps)]
        ws = sum(c["wt"] for c in sample)
        vals.append(sum(c["r"] * c["wt"] for c in sample) / ws)
    vals.sort()
    return [round(100 * vals[int(0.025 * n)], 1), round(100 * vals[int(0.975 * n)], 1)]


LOGICS = {
    "L1 Контур (70% выигрышей, топ-8)": logic_kontur,
    **{f"L2 Whalidate: {p}": (lambda T, p=p: logic_whalidate(T, p)) for p in PRESETS},
    "L3 Умение против цены (все прошедшие)": logic_skill,
    "L3 Умение против цены (топ-8)": lambda T: logic_skill(T, ROSTER),
    "База: все направленные игроки": logic_all,
    "База: случайные 8 (среднее 20 выборок)": None,
}

result = {"now": NOW, "wallets": len(WAL), "windows_days_before_now": WINDOWS, "logics": {}}
for name, fn in LOGICS.items():
    per, pooled = [], []
    for d in WINDOWS:
        T = NOW - d * DAY
        if fn is None:
            runs = [logic_random(T, s) for s in range(20)]
            rois = [stats(c)["roi"] for _, c, _ in runs if c]
            per.append({"T_days": d, "roi_mean": round(statistics.mean(rois), 2), "roi_range": [min(rois), max(rois)]})
            continue
        chosen, copies, eligible = fn(T)
        s = stats(copies)
        s.update({"T_days": d, "eligible": eligible, "chosen": len(chosen),
                  "names": [WAL[w]["name"][:14] for w in chosen[:8]]})
        per.append(s)
        pooled += copies
    entry = {"windows": per}
    if fn is not None:
        entry["pooled"] = stats(pooled)
        entry["pooled_ci95_roi"] = boot_ci(pooled)
    result["logics"][name] = entry
    print(name, json.dumps(entry.get("pooled", per), ensure_ascii=False), flush=True)

json.dump(result, open(sys.argv[2] if len(sys.argv) > 2 else "compare_result.json", "w"), ensure_ascii=False, indent=1)
