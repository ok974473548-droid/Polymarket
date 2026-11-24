"""Limit entry instead of a market copy: what fills, and what it earns (walk-forward, same windows as compare_logics).

Entry variants for every copy the chosen logic makes (leader's average entry L, minute prices from CLOB
/prices-history after the leader's first fill):
  market      buy at once: L + slippage measured on live ~1 s copies, taker fee            (as copies work now)
  cap         buy at once only if the price a minute later is at least $0.005 below L; pay L + taker fee, no slippage
  maker       limit at L for 30 min; filled only if the price traded through L by $0.01+; pay L, no fee
  maker-1c    limit at L - $0.01 for 30 min; filled if the price reached L - $0.02; pay L - $0.01, no fee
Unfilled copies are skipped. Exits are the same for every variant (with the taker fee on a sale before resolution).

Run: CMP_SLIP_MODEL=live PYTHONPATH=. python research/limit_entry.py data/audit.sqlite3 out.json [cache.json]
"""
import json
import os
import random
import statistics
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("CMP_SLIP_MODEL", "live")
import compare_logics as cl  # noqa: E402  (reads sys.argv[1] as the database)

from kontur import polymarket as pm  # noqa: E402

OUT = sys.argv[2]
CACHE = sys.argv[3] if len(sys.argv) > 3 else OUT + ".prices.json"
WAIT = 30 * 60
LOGICS = {"L3 Умение против цены (все прошедшие)": cl.logic_skill, "L1 Контур (70% выигрышей, топ-8)": cl.logic_kontur}


def load_cache():
    try:
        return {tuple(json.loads(k)): v for k, v in json.load(open(CACHE)).items()}
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    json.dump({json.dumps(list(k)): v for k, v in cache.items()}, open(CACHE, "w"))


def fetch_prices(copies, cache):
    todo = sorted({(c["token"], int(c["opened"])) for c in copies} - set(cache))
    lock, done = threading.Lock(), [0]

    def one(key):
        tok, t0 = key
        try:
            pts = pm.price_window(tok, t0, t0 + WAIT, fidelity=1, gap=0.06)
        except (pm.UpstreamError, LookupError, ValueError):
            pts = None
        with lock:
            cache[key] = pts
            done[0] += 1
            if done[0] % 1000 == 0:
                print(f"prices {done[0]}/{len(todo)}", flush=True)
                save_cache(cache)

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, todo))
    save_cache(cache)


def returns(c, entry_price, taker):
    """Copy return per $1 at a given entry price (fee on entry only for a taker)."""
    e = min(0.995, max(0.001, entry_price))
    fee_in = cl.fee(e) if taker else 0.0
    fee_out = 0.0 if c["resolved"] else cl.fee(c["exit"])
    return (c["exit"] - fee_out - e - fee_in) / (e + fee_in)


def variants(c, pts):
    """{variant: return or None (not filled)}."""
    L = c["entry"]
    out = {"market": c["r"]}
    after = [p for t, p in (pts or []) if t > c["opened"]]
    if not after:
        return {**out, "cap": None, "maker": None, "maker-1c": None, "no_prices": True}
    out["cap"] = returns(c, L, True) if after[0] <= L - 0.005 else None
    out["maker"] = returns(c, L, False) if min(after) <= L - 0.01 else None
    out["maker-1c"] = returns(c, L - 0.01, False) if L > 0.03 and min(after) <= L - 0.02 else None
    return out


def summary(rows, key, base_total):
    done = [(r[key], r["wt"], r["w"]) for r in rows if r[key] is not None]
    if not done:
        return {"filled": 0}
    wsum = sum(wt for _, wt, _ in done)
    pnl = sum(x * wt for x, wt, _ in done)
    by_p = defaultdict(lambda: [0.0, 0.0])
    for x, wt, w in done:
        by_p[w][0] += x * wt
        by_p[w][1] += wt
    ps = list(by_p.values())
    rng = random.Random(7)
    boots = sorted(sum(a for a, _ in s) / sum(b for _, b in s) for s in ([rng.choice(ps) for _ in ps] for _ in range(1000)))
    return {"filled": len(done), "fill_rate": round(len(done) / len(rows), 3), "roi": round(100 * pnl / wsum, 2),
            "ci95": [round(100 * boots[25], 1), round(100 * boots[975], 1)], "wr": round(100 * sum(1 for x, *_ in done if x > 0) / len(done), 1),
            "profit_vs_market": round(pnl / base_total, 2) if base_total > 0 else None, "profit_units": round(pnl, 1)}


def main():
    cl.prepare("all")
    cache = load_cache()
    result = {"wait_min": WAIT // 60, "logics": {}}
    for name, fn in LOGICS.items():
        per_window, pooled = [], []
        for days in cl.WINDOWS:
            _, copies, _ = fn(days)
            copies = [c for c in copies if c["decided"]]
            t = time.time()
            fetch_prices(copies, cache)
            rows = []
            for c in copies:
                v = variants(c, cache.get((c["token"], int(c["opened"]))))
                rows.append({**v, "wt": c["wt"], "w": c["w"], "days": days})
            base = sum(r["market"] * r["wt"] for r in rows)
            per_window.append({"T_days": days, "copies": len(rows), "no_prices": sum(1 for r in rows if r.get("no_prices")),
                               **{k: summary(rows, k, base) for k in ("market", "cap", "maker", "maker-1c")}})
            pooled += rows
            print(name[:12], days, f"{time.time() - t:.0f}s", json.dumps({k: per_window[-1][k].get("roi") for k in ("market", "cap", "maker", "maker-1c")}), flush=True)
        base = sum(r["market"] * r["wt"] for r in pooled)
        result["logics"][name] = {"windows": per_window, "pooled": {k: summary(pooled, k, base) for k in ("market", "cap", "maker", "maker-1c")},
                                  "copies": len(pooled), "no_prices": sum(1 for r in pooled if r.get("no_prices"))}
        print(name, json.dumps(result["logics"][name]["pooled"], ensure_ascii=False), flush=True)
    json.dump(result, open(OUT, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
