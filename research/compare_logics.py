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

Run: PYTHONPATH=. python research/compare_logics.py data/audit.sqlite3 out.json [leaderboard|markets|all]
"""
import json
import math
import multiprocessing as mp
import os
import random
import statistics
import sys
from collections import defaultdict

from kontur import db as store
from kontur import scout

DAY = 86400
FEE = float(os.environ.get("CMP_FEE", "0.05"))       # taker fee rate used for copies
NO_SLIP = os.environ.get("CMP_NO_SLIP") == "1"     # 1 = copies at the leader's own price (gross of costs)
WINDOWS = [150, 120, 90, 60]  # selection date T = NOW - d days; copies judged on bets opened in [T, T+30d)
HORIZON = 30 * DAY
ROSTER = 8
DB = sys.argv[1]
OUT = sys.argv[2] if len(sys.argv) > 2 else "compare_result.json"
UNIVERSE = sys.argv[3] if len(sys.argv) > 3 else "all"   # leaderboard | markets | all
NOW = 0.0
WAL = {}   # wallet -> {"name", "slip", "slip_measured", "bets"} (bets only while precomputing one wallet)


def load_wallet(db, w, name, metrics):
    bets = scout.stored_bets(db, w)
    if not bets:
        return None
    m = json.loads(metrics) if metrics else {}
    for b in bets:  # data fix applied to every logic: finished markets with a 0/1 price are resolved
        if b["state"] == "open" and b["end"] and b["end"] < NOW - 7 * DAY and b["mark"] is not None and (b["mark"] <= 0.01 or b["mark"] >= 0.99):
            b["state"], b["exit"], b["closed"] = "resolved", 1.0 if b["mark"] >= 0.5 else 0.0, b["end"]
    measured = m.get("slip_measured")
    slip = 0.0 if NO_SLIP else (max(scout.DEFAULT_SLIP, measured) if measured is not None else 0.01)
    return {"name": name or w[:10], "bets": bets, "slip": slip,
            "slip_measured": measured}


def fee(p, rate=FEE):
    return rate * p * (1 - p)


# CMP_SLIP_MODEL=live: the copy is placed ~1 s after the leader (1.2.0+), and pays what live copies paid on average
# over the leader's price (436 stream copies, Nov 2026): +$0.023 below $0.30, +$0.007 above.
LIVE_SLIP = os.environ.get("CMP_SLIP_MODEL") == "live"


def live_slip(entry):
    return 0.023 if entry < 0.30 else 0.007


def copy_r(entry, exit_price, slip, resolved, rate=FEE):
    if LIVE_SLIP and slip > 0:
        slip = live_slip(entry)
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


def future(d, T):
    """Copy results of the leader's bets opened in [T, T+30d), judged at NOW."""
    out = []
    for b in d["bets"]:
        if not T <= b["opened"] < T + HORIZON or not ok_bet(b):
            continue
        if b["state"] == "open":
            if b["mark"] is None:
                continue
            r, decided = copy_r(b["entry"], b["mark"], d["slip"], False), False
        else:
            r, decided = copy_r(b["entry"], b["exit"], d["slip"], b["state"] == "resolved" or b["exit"] in (0.0, 1.0)), True
        out.append({"r": r, "decided": decided, "entry": b["entry"], "cond": b["condition"], "oidx": b["oidx"],
                    "days": ((b["end"] or b["opened"]) - b["opened"]) / DAY, "cost": b["entry"] * b["size"]})
    return out


# ---------------------------------------------------------------- per-wallet facts at each date T (one pass per wallet)
def kontur_verdict(d, known, T):
    """L1: Kontur 1.1.1 as is (scout.metrics / behaviour / judge with the app's own fee estimate)."""
    m = scout.metrics(known, slip=d["slip"], now=T, as_of=T)
    m["slip_measured"] = d["slip_measured"] if d["slip_measured"] is not None else scout.DEFAULT_SLIP
    k = scout.behaviour(known, [], m, T)[0]
    v = scout.judge(m, k, {})
    return k == "directional" and v["passed"], v["score"], k


def whalidate_screen(known, T):
    """L2 wallet screen from Whalidate's public methodology (drawdown on a $1000 bankroll with $25 copies)."""
    st = sorted(settled(known, T, lookback=10_000), key=lambda b: b["closed"])
    if len(st) < 30:
        return False
    pnl = [(b["exit"] - b["entry"]) * b["size"] for b in st]
    cost = sum(b["entry"] * b["size"] for b in st)
    roi = sum(pnl) / cost if cost else 0
    pos = sum(p for p in pnl if p > 0)
    top = max(pnl) / pos if pos > 0 else 1
    eq, peak, dd = 1000.0, 1000.0, 0.0
    for b in st:
        eq += 25 * (b["exit"] - b["entry"]) / b["entry"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak if peak > 0 else 1)
    return roi >= 0.15 and dd <= 0.25 and top <= 0.5


K_SHRINK = 40      # events of evidence for half weight
BH_Q = 0.10


def skill(d, known, T, kind):
    """L3: copy return against the price paid, one number per event, shrunk to zero by the amount of evidence."""
    if kind != "directional":
        return None
    st = settled(known, T)
    if len(st) < 20:
        return None
    by_ev = defaultdict(lambda: [0.0, 0.0, 0])  # excess wins, return sum, count
    for b in st:
        p = min(0.995, b["entry"] + (live_slip(b["entry"]) if LIVE_SLIP else d["slip"]))
        r = copy_r(b["entry"], b["exit"], d["slip"], b["state"] == "resolved" or b["exit"] in (0.0, 1.0))
        e = by_ev[b["event_slug"] or b["condition"]]
        e[0] += (1.0 if r > 0 else 0.0) - p
        e[1] += r
        e[2] += 1
    ev = list(by_ev.values())
    n_ev = len(ev)
    if n_ev < 15:
        return None
    rets = [e[1] / e[2] for e in ev]
    mean = statistics.mean(rets)
    sd = statistics.pstdev(rets) or 1e-9
    t = mean / (sd / math.sqrt(n_ev))
    excess = sum(e[0] for e in ev) / sum(e[2] for e in ev)
    return {"t": t, "p": 0.5 * math.erfc(t / math.sqrt(2)), "edge": mean * n_ev / (n_ev + K_SHRINK),
            "excess": excess * n_ev / (n_ev + K_SHRINK), "n_ev": n_ev}


def _init(now):
    global NOW
    NOW = now


def precompute(item):
    w, name, metrics = item
    db = store.connect(DB)
    try:
        d = load_wallet(db, w, name, metrics)
    finally:
        db.close()
    if not d:
        return None
    per = {}
    for days in WINDOWS:
        T = NOW - days * DAY
        known = known_at(d["bets"], T)
        if not known:
            continue
        k_pass, k_score, kind = kontur_verdict(d, known, T)
        per[days] = {"kontur": (k_pass, k_score), "whal": whalidate_screen(known, T), "kind": kind,
                     "skill": skill(d, known, T, kind), "fut": future(d, T)}
    return w, {"name": d["name"], "slip": d["slip"], "per": per}


# ---------------------------------------------------------------- logics (read the precomputed facts)
def logic_kontur(days):
    picks = sorted(((P[days]["kontur"][1], w) for w, P in PRE.items() if days in P and P[days]["kontur"][0]), reverse=True)
    chosen = [w for _, w in picks[:ROSTER]]
    return chosen, [dict(c, w=w, wt=1.0) for w in chosen for c in PRE[w]["per"][days]["fut"] if c["days"] <= 60], len(picks)


PRESETS = {  # name: (price lo, price hi, max days to resolution, min whale buy $)
    "40-80¢, до 30 дн. (как Helm/Beacon)": (0.40, 0.80, 30, 0),
    "80-98¢, до 30 дн. (как Anchor)": (0.80, 0.98, 30, 0),
    "60-90¢, до 7 дн., кит от $2000 (как Current)": (0.60, 0.90, 7, 2000),
    "40-98¢, до 30 дн. (как Reef)": (0.40, 0.98, 30, 0),
}


def logic_whalidate(days, preset):
    lo, hi, mx, min_usd = PRESETS[preset]
    chosen = [w for w, P in PRE.items() if days in P and P[days]["whal"]]
    copies = [dict(c, w=w, wt=1.0) for w in chosen for c in PRE[w]["per"][days]["fut"]
              if lo <= c["entry"] <= hi and c["days"] <= mx and c["cost"] >= min_usd]
    return chosen, copies, len(chosen)


def logic_skill(days, top=None):
    cands = sorted(((P[days]["skill"], w) for w, P in PRE.items() if days in P and P[days]["skill"]), key=lambda x: x[0]["p"])
    m = len(cands)
    k = max([i for i, (s, _) in enumerate(cands, 1) if s["p"] <= BH_Q * i / m], default=0)
    passed = [(s, w) for s, w in cands[:k] if s["edge"] > 0.02 and s["excess"] > 0]
    passed.sort(key=lambda x: -x[0]["edge"])
    chosen = passed[:top] if top else passed
    copies = []
    for s, w in chosen:
        for c in PRE[w]["per"][days]["fut"]:
            if c["days"] > 60:
                continue
            p = min(0.99, c["entry"] + (live_slip(c["entry"]) if LIVE_SLIP else PRE[w]["slip"]))
            q = min(0.99, p + s["excess"])
            kelly = max(0.0, (q - p) / (1 - p)) * 0.25 if q > p else 0.0
            if kelly <= 0 or s["edge"] <= fee(p):  # expected edge must beat the fee
                continue
            copies.append(dict(c, w=w, wt=min(kelly, 0.05)))
    # consensus: same market and side by 2+ chosen players -> double weight; opposite sides -> skip both
    sides, buyers = defaultdict(set), defaultdict(set)
    for c in copies:
        sides[c["cond"]].add(c["oidx"])
        buyers[(c["cond"], c["oidx"])].add(c["w"])
    out = [dict(c, wt=c["wt"] * (2 if len(buyers[(c["cond"], c["oidx"])]) > 1 else 1)) for c in copies if len(sides[c["cond"]]) == 1]
    return [w for _, w in chosen], out, len(passed)


def logic_all(days):
    ws = [w for w, P in PRE.items() if days in P and P[days]["kind"] == "directional"]
    return ws, [dict(c, w=w, wt=1.0) for w in ws for c in PRE[w]["per"][days]["fut"] if c["days"] <= 60], len(ws)


def logic_random(days, seed):
    ws, _, _ = logic_all(days)
    pick = random.Random(seed).sample(ws, min(ROSTER, len(ws)))
    return pick, [dict(c, w=w, wt=1.0) for w in pick for c in PRE[w]["per"][days]["fut"] if c["days"] <= 60], len(ws)


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
        by_p[c["w"]].append((c["r"] * c["wt"], c["wt"]))
    ps = [(sum(a for a, _ in v), sum(b for _, b in v)) for v in by_p.values()]
    if len(ps) < 2:
        return None
    rng = random.Random(7)
    vals = []
    for _ in range(n):
        sample = [rng.choice(ps) for _ in ps]
        vals.append(sum(a for a, _ in sample) / sum(b for _, b in sample))
    vals.sort()
    return [round(100 * vals[int(0.025 * n)], 1), round(100 * vals[int(0.975 * n)], 1)]


LOGICS = {
    "L1 Контур (70% выигрышей, топ-8)": logic_kontur,
    **{f"L2 Whalidate: {p}": (lambda days, p=p: logic_whalidate(days, p)) for p in PRESETS},
    "L3 Умение против цены (все прошедшие)": logic_skill,
    "L3 Умение против цены (топ-8)": lambda days: logic_skill(days, ROSTER),
    "База: все направленные игроки": logic_all,
    "База: случайные 8 (среднее 20 выборок)": None,
}


def main():
    global PRE
    store.init(DB)  # also moves bets of an older database into packed records
    db = store.connect(DB)
    now = float(db.execute("SELECT v FROM audit_meta WHERE k='now'").fetchone()[0])
    _init(now)
    has_u = db.execute("SELECT 1 FROM sqlite_master WHERE name='audit_universe'").fetchone()
    if UNIVERSE == "profiled":  # the 919 wallets of the first comparison (regression check)
        items = db.execute("SELECT wallet, name, metrics FROM scout_wallets WHERE metrics IS NOT NULL").fetchall()
    elif has_u:
        where = "" if UNIVERSE == "all" else f"WHERE u.source='{'markets' if UNIVERSE == 'markets' else 'leaderboard'}'"
        items = db.execute(f"""SELECT u.wallet, COALESCE(s.name, ''), s.metrics FROM audit_universe u
                               JOIN audit_done d ON d.wallet=u.wallet AND d.n>0
                               LEFT JOIN scout_wallets s ON s.wallet=u.wallet {where}""").fetchall()
    else:
        items = db.execute("SELECT wallet, name, metrics FROM scout_wallets WHERE metrics IS NOT NULL").fetchall()
    db.close()
    items = [tuple(r) for r in items]
    PRE = {}
    with mp.Pool(4, initializer=_init, initargs=(now,)) as pool:
        for i, res in enumerate(pool.imap_unordered(precompute, items, chunksize=8)):
            if res:
                PRE[res[0]] = res[1]
            if (i + 1) % 500 == 0:
                print(f"precomputed {i + 1}/{len(items)}", flush=True)
    for v in PRE.values():  # logics index per-window facts directly
        for days, x in v["per"].items():
            v[days] = x
    result = {"now": now, "universe": UNIVERSE, "wallets": len(PRE), "windows_days_before_now": WINDOWS, "logics": {}}
    for name, fn in LOGICS.items():
        per, pooled = [], []
        for days in WINDOWS:
            if fn is None:
                runs = [logic_random(days, s) for s in range(20)]
                rois = [stats(c)["roi"] for _, c, _ in runs if c]
                per.append({"T_days": days, "roi_mean": round(statistics.mean(rois), 2), "roi_range": [min(rois), max(rois)]})
                continue
            chosen, copies, eligible = fn(days)
            s = stats(copies)
            s.update({"T_days": days, "eligible": eligible, "chosen": len(chosen), "names": [PRE[w]["name"][:14] for w in chosen[:8]]})
            per.append(s)
            pooled += copies
        entry = {"windows": per}
        if fn is not None:
            entry["pooled"] = stats(pooled)
            entry["pooled_ci95_roi"] = boot_ci(pooled)
        result["logics"][name] = entry
        print(name, json.dumps(entry.get("pooled", per), ensure_ascii=False), entry.get("pooled_ci95_roi"), flush=True)
    json.dump(result, open(OUT, "w"), ensure_ascii=False, indent=1)


PRE = {}
if __name__ == "__main__":
    main()
