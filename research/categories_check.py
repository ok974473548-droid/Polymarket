"""Copying by category (1.16.0) on history: off / soft / strict on the same wallets and dates.

Same walk-forward as compare_logics.py: players are judged at T only on what was known at T (bets opened before T,
outcomes settled before T); copies are their bets opened in [T, T+30d), judged now, with slippage and the
0.05·p·(1−p) fee. Skill and skill by category come from the app's own scout.metrics (category from slug and title,
as for events without cached tags). Benjamini–Hochberg runs over all players at T, and over all (player, category)
pairs at T. Every copy weighs the same; copies of bets resolving in more than 60 days are left out.

  off     players whose skill is proven as a whole; every bet copied
  soft    the same players plus specialists (a strong category); bets of a weak category skipped, and of a
          neutral/unknown category only for players proven as a whole
  strict  specialists only; only bets of their strong categories
  off_skip_weak    the players of "off", without bets of their weak categories (the category filter alone)
  off_market_cats  the players of "off", only in market categories whose copies earned over the 90 days before T
                   (all directional players, outcomes known at T)

Run: PYTHONPATH=. python research/categories_check.py data/audit.sqlite3 out.json
(CMP_SLIP_MODEL=live for the live slippage model; CMP_PAYOUT=1.5 to also copy only bets that pay ×1.5)
"""
import json
import multiprocessing as mp
import os
import sys
from collections import Counter, defaultdict

import compare_logics as cl
from kontur import categories, scout

PAYOUT = float(os.environ.get("CMP_PAYOUT", "0") or 0)
TOP = scout.payout_price(PAYOUT) if PAYOUT else 1.0
S = {**scout.DEFAULTS}


def per_wallet(item):
    w, name, metrics = item
    db = cl.store.connect(cl.DB)
    try:
        d = cl.load_wallet(db, w, name, metrics)
    finally:
        db.close()
    if not d:
        return None
    out = {}
    for days in cl.WINDOWS:
        T = cl.NOW - days * cl.DAY
        known = cl.known_at(d["bets"], T)
        if not known:
            continue
        m = scout.metrics(known, slip=d["slip"], now=T, as_of=T)
        kind = scout.behaviour(known, [], m, T)[0]
        if kind != "directional":
            continue
        fut = []
        for b in d["bets"]:
            if not T <= b["opened"] < T + cl.HORIZON or not cl.ok_bet(b):
                continue
            if ((b["end"] or b["opened"]) - b["opened"]) / cl.DAY > 60:
                continue
            if b["state"] == "open":
                if b["mark"] is None:
                    continue
                r = cl.copy_r(b["entry"], b["mark"], d["slip"], False)
            else:
                r = cl.copy_r(b["entry"], b["exit"], d["slip"], b["state"] == "resolved" or b["exit"] in (0.0, 1.0))
            fut.append({"r": r, "decided": b["state"] != "open", "entry": b["entry"],
                        "cat": categories.of_text(b.get("event_slug") or "", b.get("title") or "")})
        # copies of the bets of the 90 days before T whose outcome was known at T (category profitability at T)
        past = []
        for b in known:
            if not (T - 90 * cl.DAY <= b["opened"] < T) or b["state"] == "open" or not b["closed"] or b["closed"] > T:
                continue
            if not cl.ok_bet(b) or ((b["end"] or b["opened"]) - b["opened"]) / cl.DAY > 60:
                continue
            past.append((categories.of_text(b.get("event_slug") or "", b.get("title") or ""),
                         cl.copy_r(b["entry"], b["exit"], d["slip"], b["state"] == "resolved" or b["exit"] in (0.0, 1.0))))
        out[days] = {"skill": m.get("skill"), "cats": m.get("cats") or {}, "fut": fut, "past": past}
    return w, out


def market_cats(pre, days, min_n=200):
    """Categories whose copies (all directional players) earned on the bets of the 90 days before T, as known at T."""
    by = defaultdict(list)
    for per in pre.values():
        for cat, r in (per.get(days) or {}).get("past", []):
            by[cat].append(r)
    return {c for c, rs in by.items() if len(rs) >= min_n and sum(rs) / len(rs) > 0}, {c: (len(rs), round(100 * sum(rs) / len(rs), 1)) for c, rs in by.items()}


def run(pre, mode, days, cut, ccut):
    if mode in ("off_skip_weak", "off_market_cats"):
        s = {**S, "cat_mode": "soft"}
        good = market_cats(pre, days)[0] if mode == "off_market_cats" else None
        chosen, copies = [], []
        for w, per in pre.items():
            x = per.get(days)
            if not x:
                continue
            m = {"skill": x["skill"], "cats": x["cats"], "version": scout.METRICS_VERSION}
            if not scout.overall_ok(m, s, cut):
                continue
            chosen.append(w)
            for c in x["fut"]:
                if c["entry"] > TOP:
                    continue
                if good is not None and c["cat"] not in good:
                    continue
                if good is None and scout.cat_verdict(x["cats"].get(c["cat"]), s, ccut) == "weak":
                    continue
                copies.append(dict(c, w=w, wt=1.0))
        return chosen, copies
    s = {**S, "cat_mode": mode}
    chosen, copies = [], []
    for w, per in pre.items():
        x = per.get(days)
        if not x:
            continue
        m = {"skill": x["skill"], "cats": x["cats"], "version": scout.METRICS_VERSION}
        whole = scout.overall_ok(m, s, cut)
        strong = scout.strong_cats(m, s, ccut)
        if mode == "off" and not whole or mode == "soft" and not (whole or strong) or mode == "strict" and not strong:
            continue
        chosen.append(w)
        for c in x["fut"]:
            if c["entry"] > TOP:
                continue
            ok, _ = scout.cat_decision(m, c["cat"], s, cut, ccut) if mode != "off" else (True, None)
            if ok:
                copies.append(dict(c, w=w, wt=1.0))
    return chosen, copies


def main():
    cl.store.init(cl.DB)
    db = cl.store.connect(cl.DB)
    now = float(db.execute("SELECT v FROM audit_meta WHERE k='now'").fetchone()[0])
    items = [tuple(r) for r in db.execute("""SELECT u.wallet, COALESCE(s.name, ''), s.metrics FROM audit_universe u
                                              JOIN audit_done d ON d.wallet=u.wallet AND d.n>0
                                              LEFT JOIN scout_wallets s ON s.wallet=u.wallet""")]
    db.close()
    cl._init(now)
    pre = {}
    with mp.Pool(4, initializer=cl._init, initargs=(now,)) as pool:
        for i, res in enumerate(pool.imap_unordered(per_wallet, items, chunksize=8)):
            if res:
                pre[res[0]] = res[1]
            if (i + 1) % 300 == 0:
                print(f"{i + 1}/{len(items)}", flush=True)
    result = {"wallets": len(pre), "payout": PAYOUT or None, "modes": {}}
    for mode in ("off", "soft", "strict", "off_skip_weak", "off_market_cats"):
        pooled, wins = [], []
        for days in cl.WINDOWS:
            ps = [x[days]["skill"]["p"] for x in pre.values() if days in x and x[days]["skill"]]
            cps = [cs["p"] for x in pre.values() if days in x for cs in x[days]["cats"].values() if cs["n_ev"] >= S["cat_min_events"]]
            cut, ccut = scout.bh_cut(ps), scout.bh_cut(cps)
            chosen, copies = run(pre, mode, days, cut, ccut)
            st = cl.stats(copies)
            st.update({"T_days": days, "players": len(chosen), "cut": round(cut, 5), "cat_cut": round(ccut, 5),
                       "market_cats": sorted(market_cats(pre, days)[0]), "market_cat_roi": market_cats(pre, days)[1],
                       "by_cat": dict(Counter(c["cat"] for c in copies).most_common())})
            wins.append(st)
            pooled += copies
        entry = {"windows": wins, "pooled": cl.stats(pooled), "ci95": cl.boot_ci(pooled)}
        cat_roi = defaultdict(list)
        for c in pooled:
            cat_roi[c["cat"]].append(c["r"])
        entry["roi_by_cat"] = {k: [len(v), round(100 * sum(v) / len(v), 2)] for k, v in sorted(cat_roi.items(), key=lambda kv: -len(kv[1]))}
        result["modes"][mode] = entry
        print(mode, json.dumps(entry["pooled"], ensure_ascii=False), entry["ci95"],
              [(w["T_days"], w.get("roi"), w["players"], w.get("copies")) for w in wins], flush=True)
    json.dump(result, open(cl.OUT, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
