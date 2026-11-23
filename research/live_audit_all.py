"""Same audit as live_audit.py, but on the re-crawled bets of ALL profiled wallets, plus an honest walk-forward."""
import json
import math
import random
import statistics
import sys
from collections import Counter

from kontur import db as store
from kontur import scout

DB = sys.argv[1]
db = store.connect(DB)
NOW = float(db.execute("SELECT v FROM audit_meta WHERE k='now'").fetchone()[0])
DAY = 86400
rows = store.rows(db.execute("SELECT * FROM scout_wallets WHERE metrics IS NOT NULL"))
errs = db.execute("SELECT COUNT(*) FROM audit_done WHERE error!=''").fetchone()[0]
BETS = {r["wallet"]: scout.stored_bets(db, r["wallet"]) for r in rows}
out = {"wallets": len(rows), "with_bets": sum(1 for b in BETS.values() if b), "crawl_errors": errs,
       "partial": db.execute("SELECT COUNT(*) FROM audit_done WHERE partial=1").fetchone()[0]}


def pct(x):
    return None if x is None else round(100 * x, 1)


def slip_of(r):
    m = json.loads(r["metrics"])
    return max(scout.DEFAULT_SLIP, m.get("slip_measured") or 0), m.get("slip_measured")


def profile(r, as_of=None):
    """Metrics, kind and verdict with what was known at as_of (kind from bets only, no future trades)."""
    bets = BETS[r["wallet"]]
    end = as_of or NOW
    known = [b for b in bets if b["opened"] <= end]
    if as_of:  # hide outcomes and closes that happened later
        known = [dict(b, state="open", exit=None, closed=None) if b["closed"] and b["closed"] > end else b for b in known]
    slip, measured = slip_of(r)
    m = scout.metrics(known, slip=slip, now=end, as_of=as_of)
    # slippage is only measured today for promising wallets; assume the default for the rest (optimistic)
    m["slip_measured"] = measured if measured is not None else scout.DEFAULT_SLIP
    kind = scout.behaviour(known, [], m, end)[0] if as_of else r["kind"]
    return m, kind, scout.judge(m, kind, {}), slip


def luck(bets, slip, start, end):
    d = scout._decided(bets, slip, start, end)
    probs = [min(0.995, x["entry"] + slip) for x in d]
    var = sum(p * (1 - p) for p in probs)
    wins = sum(x["win"] for x in d)
    z = (wins - sum(probs)) / math.sqrt(var) if var else 0.0
    return len(d), z, d


# ---------------------------------------------------------------- 1. today's selection on full data
passed, fail, kinds = [], Counter(), Counter()
alls = []
for r in rows:
    m, kind, v, slip = profile(r)
    kinds[kind] += 1
    n, z, d = luck(BETS[r["wallet"]], slip, NOW - scout.LOOKBACK_DAYS * DAY, NOW)
    if kind == "directional" and n >= 40:
        alls.append({"z": z, "wr": m["wr"], "entry": m["avg_entry"], "roi": m["roi"], "passed": v["passed"], "n": n,
                     "name": r["name"], "wallet": r["wallet"]})
    if v["passed"]:
        rs = [x["r"] for x in d]
        rng = random.Random(1)
        boots = sorted(statistics.mean(rng.choices(rs, k=len(rs))) for _ in range(2000))
        passed.append({"name": (r["name"] or r["wallet"])[:18], "n": m["n"], "wr": pct(m["wr"]), "avg_entry": round(m["avg_entry"], 3),
                       "roi": pct(m["roi"]), "roi_lb5": pct(boots[100]), "z_vs_price": round(z, 2),
                       "p_luck": round(0.5 * math.erfc(z / math.sqrt(2)), 4), "score": v["score"],
                       "slip_measured": json.loads(r["metrics"]).get("slip_measured") is not None})
    elif kind == "directional":
        for f in v["failed"]:
            fail[f] += 1
passed.sort(key=lambda x: -x["score"])
out["kinds_today"] = dict(kinds)
out["passed_today"] = passed
out["directional_fail_reasons"] = dict(fail.most_common())
ps = sorted(0.5 * math.erfc(a["z"] / math.sqrt(2)) for a in alls)
k = max([i for i, p in enumerate(ps, 1) if p <= 0.05 * i / len(ps)], default=0)
out["directional_40plus"] = len(alls)
out["skill_significant_BH5"] = k
zcut = sorted(alls, key=lambda a: -a["z"])[:k]
out["skill_significant_and_passed"] = sum(a["passed"] for a in zcut)
out["skill_significant_rejected_wr_lt70"] = sum(1 for a in zcut if not a["passed"] and (a["wr"] or 0) < 0.7)
out["skill_significant_median_wr"] = pct(statistics.median(a["wr"] for a in zcut)) if zcut else None
out["skill_significant_median_entry"] = round(statistics.median(a["entry"] for a in zcut), 3) if zcut else None
out["high_wr_ge70_count"] = sum(1 for a in alls if a["wr"] >= 0.7)
out["high_wr_ge70_but_no_skill_z_lt1"] = sum(1 for a in alls if a["wr"] >= 0.7 and a["z"] < 1)
xs, ys = [a["wr"] for a in alls], [a["entry"] for a in alls]
mx, my = statistics.mean(xs), statistics.mean(ys)
out["corr_wr_vs_avg_entry"] = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) /
                                    math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)), 2)


# ---------------------------------------------------------------- 2. walk-forward: honest vs app-style
def summary(items):
    dec = [x for x in items if x["decided"]]
    if not dec:
        return None
    return {"bets": len(dec), "wr": pct(sum(x["r"] > 0 for x in dec) / len(dec)), "roi": pct(statistics.mean(x["r"] for x in dec)),
            "players_with_bets": None}


def walk(days, honest):
    as_of = NOW - days * DAY
    picks, pool = [], []
    for r in rows:
        bets = BETS[r["wallet"]]
        if not bets:
            continue
        slip = slip_of(r)[0]
        after = scout._after(bets, slip, as_of, NOW)
        if honest:
            m, kind, v, _ = profile(r, as_of)
            pool.extend(after)
        else:  # the app's backtest: today's kind, only wallets the app keeps bets for (promising today)
            cur = json.loads(r["metrics"])
            if not (r["kind"] == "directional" and cur["n"] >= 15 and (cur["wr"] or 0) >= 0.55):
                continue
            pool.extend(after)
            m = scout.metrics(bets, slip=slip, now=NOW, as_of=as_of)
            m["slip_measured"] = cur.get("slip_measured")
            kind = r["kind"]
            v = scout.judge(m, kind, {})
        if kind == "directional" and v["passed"]:
            picks.append((v["score"], r["name"] or r["wallet"][:10], m, after))
    picks.sort(key=lambda p: -p[0])
    roster = picks[:8]
    per = []
    for s, name, m, after in roster:
        sm = summary(after)
        per.append([name[:14], m["n"], pct(m["wr"]), round(m["avg_entry"], 2), sm and sm["bets"], sm and sm["wr"], sm and sm["roi"]])
    rs = summary([x for p in roster for x in p[3]])
    # robustness: roster result without its single best player
    best_wo = None
    if len(roster) > 1:
        contrib = [(sum(x["r"] for x in p[3] if x["decided"]), i) for i, p in enumerate(roster)]
        top = max(contrib)[1]
        best_wo = summary([x for i, p in enumerate(roster) if i != top for x in p[3]])
    return {"eligible": len(picks), "roster": rs, "roster_without_best_player": best_wo, "baseline_all": summary(pool),
            "pool_wallets": "all profiled" if honest else "promising today only", "players": per}


out["walk_forward"] = {f"{d}d": {"honest": walk(d, True), "app_style": walk(d, False)} for d in (30, 60, 90)}


# ---------------------------------------------------------------- 3. persistence on all directional (kind as of T)
def spearman(a, b):
    n = len(a)
    ra = {i: k for k, i in enumerate(sorted(range(n), key=lambda i: a[i]))}
    rb = {i: k for k, i in enumerate(sorted(range(n), key=lambda i: b[i]))}
    return round(1 - 6 * sum((ra[i] - rb[i]) ** 2 for i in range(n)) / (n * (n * n - 1)), 3)


as_of = NOW - 60 * DAY
pairs = []
for r in rows:
    bets = BETS[r["wallet"]]
    if not bets:
        continue
    m, kind, v, slip = profile(r, as_of)
    if kind != "directional":
        continue
    after = [x for x in scout._after(bets, slip, as_of, NOW) if x["decided"]]
    if m["n"] >= 20 and len(after) >= 10:
        pairs.append((m["roi"], statistics.mean(x["r"] for x in after), m["wr"], sum(x["r"] > 0 for x in after) / len(after),
                      m["wr"] - m["avg_entry"], sum(x["r"] > 0 for x in after) / len(after)))
out["persistence_pairs"] = len(pairs)
if pairs:
    out["spearman_roi"] = spearman([p[0] for p in pairs], [p[1] for p in pairs])
    out["spearman_wr"] = spearman([p[2] for p in pairs], [p[3] for p in pairs])
    top = sorted(pairs, key=lambda p: -p[2])[: max(1, len(pairs) // 5)]
    out["top20pct_by_wr_before_after"] = {"wr_before": pct(statistics.mean(p[2] for p in top)),
                                          "wr_after": pct(statistics.mean(p[3] for p in top)),
                                          "roi_after": pct(statistics.mean(p[1] for p in top))}
    top = sorted(pairs, key=lambda p: -p[0])[: max(1, len(pairs) // 5)]
    out["top20pct_by_roi_before_after"] = {"roi_before": pct(statistics.mean(p[0] for p in top)),
                                           "roi_after": pct(statistics.mean(p[1] for p in top)),
                                           "wr_after": pct(statistics.mean(p[3] for p in top))}
    out["all_pairs_roi_after"] = pct(statistics.mean(p[1] for p in pairs))

# ---------------------------------------------------------------- 4. data quality on full data
q = lambda sql, *a: db.execute(sql, a).fetchone()[0]  # noqa: E731
out["bets_total"] = q("SELECT COUNT(*) FROM scout_bets")
out["open_past_end_7d"] = q("SELECT COUNT(*) FROM scout_bets WHERE state='open' AND end_ts IS NOT NULL AND end_ts < ?", NOW - 7 * DAY)
out["open_past_end_mark_0_or_1"] = q("SELECT COUNT(*) FROM scout_bets WHERE state='open' AND end_ts < ? AND (mark<=0.01 OR mark>=0.99)", NOW - 7 * DAY)
print(json.dumps(out, ensure_ascii=False, indent=1, default=str))
