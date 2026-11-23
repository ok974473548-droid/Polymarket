"""Synthetic check of the Kontur autopilot selection model (scout.metrics / behaviour / judge).

The world: markets with a true probability q, a near-efficient market price, outcomes drawn from q.
Traders of known types. We select a roster with data known 60 days ago (walk-forward, like
scout.backtest) and compare what the picks did afterwards with the ground truth.
"""
import json
import random
import statistics
import sys

from kontur import scout

DAY = 86400
NOW = 1_800_000_000.0
START = NOW - 240 * DAY
AS_OF = NOW - 60 * DAY


def clip(x, lo=0.01, hi=0.99):
    return max(lo, min(hi, x))


def make_bet(rng, t, side_price, win_prob, hold, i, title="Will it happen?"):
    """One position held to resolution. side_price = price paid for the chosen side."""
    win = rng.random() < win_prob
    closed = t + hold
    b = dict(token=f"tok{i}", condition=f"c{i}", oidx=0, outcome="Yes", title=title, event_slug=f"ev{i}",
             entry=round(side_price, 3), size=rng.uniform(20, 400) / side_price, opened=t, end=closed, short=0,
             state="", exit=None, closed=None, mark=None)
    if closed <= NOW:
        b.update(state="closed" if win else "resolved", exit=1.0 if win else 0.0, closed=closed)
    else:
        b.update(state="open", mark=clip(win_prob + rng.gauss(0, 0.05)))
    return b, win


def trader(rng, kind, idx):
    rate = rng.uniform(0.25, 1.2)  # bets per day
    bets, t, i = [], START + rng.uniform(0, 10) * DAY, 0
    truth = []  # (opened, true expected return per $1 of the leader)
    while t < NOW:
        t += rng.expovariate(rate) * DAY
        if t >= NOW:
            break
        q = rng.uniform(0.05, 0.95)                # true probability of YES
        price = clip(q + rng.gauss(0, 0.04))        # near-efficient market
        hold = rng.uniform(1, 20) * DAY
        skilled = kind in ("skilled", "longshot", "skilled_fav") or (kind == "fading" and t < NOW - 100 * DAY)
        if skilled:
            est = q + rng.gauss(0, 0.02)
            edge_yes, edge_no = est - price, (1 - est) - (1 - price)
            if kind == "longshot":  # only cheap sides
                cand = [(p, e, wp) for p, e, wp in ((price, edge_yes, q), (1 - price, edge_no, 1 - q)) if p < 0.35]
            elif kind == "skilled_fav":  # informed, but only on sides priced 0.60-0.94
                cand = [(p, e, wp) for p, e, wp in ((price, edge_yes, q), (1 - price, edge_no, 1 - q)) if 0.60 <= p <= 0.94]
            else:
                cand = [(price, edge_yes, q), (1 - price, edge_no, 1 - q)]
            cand = [c for c in cand if c[1] > 0.04]
            if not cand:
                continue
            p, _, wp = max(cand, key=lambda c: c[1])
        elif kind == "favorite":  # buys whatever side is priced 0.75–0.94, no information
            p, wp = (price, q) if price >= 0.5 else (1 - price, 1 - q)
            if not 0.75 <= p <= 0.94:
                continue
        else:  # noise
            p, wp = (price, q) if rng.random() < 0.5 else (1 - price, 1 - q)
        b, _ = make_bet(rng, t, p, wp, hold, f"{idx}-{i}")
        i += 1
        bets.append(b)
        truth.append((t, wp / p - 1))
    return bets, truth


def summary(rs):
    if not rs:
        return {"n": 0, "wr": None, "roi": None}
    return {"n": len(rs), "wr": sum(r > 0 for r in rs) / len(rs), "roi": statistics.mean(rs)}


def run(seed, mix):
    rng = random.Random(seed)
    players = []
    for kind, count in mix.items():
        for k in range(count):
            bets, truth = trader(rng, kind, f"{kind}{k}")
            slip = rng.uniform(0.004, 0.02)
            players.append({"kind": kind, "bets": bets, "slip": slip, "truth": truth})
    picks_all = []
    for p in players:
        past = scout.metrics(p["bets"], slip=p["slip"], now=NOW, as_of=AS_OF)
        past["slip_measured"] = p["slip"]
        btype, _ = scout.behaviour(p["bets"], [], past, now=AS_OF)
        v = scout.judge(past, btype, {})
        after = [x for x in scout._after(p["bets"], p["slip"], AS_OF, NOW) if x["decided"]]
        p.update(passed=v["passed"] and btype == "directional", score=v["score"], past=past, btype=btype,
                 after=[x["r"] for x in after], failed=v["failed"])
        if p["passed"]:
            picks_all.append(p)
    picks_all.sort(key=lambda p: -p["score"])
    roster = picks_all[:8]
    return players, picks_all, roster


def main(seeds=20):
    mix = {"skilled": 15, "skilled_fav": 10, "fading": 10, "longshot": 8, "favorite": 30, "noise": 150}
    agg = {"roster_after": [], "all_after": [], "pass_by_kind": {k: 0 for k in mix}, "roster_by_kind": {k: 0 for k in mix},
           "count_by_kind": {k: 0 for k in mix}, "fail_reasons": {}, "roster_sizes": [], "skilled_after": [],
           "pass_wr_before": [], "pass_wr_after": [], "seed_roster_roi": [], "seed_base_roi": []}
    for seed in range(seeds):
        players, picks, roster = run(seed, mix)
        for p in players:
            agg["count_by_kind"][p["kind"]] += 1
            agg["all_after"] += p["after"]
            if p["kind"] == "skilled":
                agg["skilled_after"] += p["after"]
            agg.setdefault("after_by_kind", {}).setdefault(p["kind"], []).extend(p["after"])
            if p["passed"]:
                agg.setdefault("passed_after_by_kind", {}).setdefault(p["kind"], []).extend(p["after"])
            if p["passed"]:
                agg["pass_by_kind"][p["kind"]] += 1
                agg["pass_wr_before"].append(p["past"]["wr"])
                s = summary(p["after"])
                if s["wr"] is not None:
                    agg["pass_wr_after"].append(s["wr"])
            elif p["kind"] in ("skilled", "fading"):
                for f in p["failed"]:
                    agg["fail_reasons"][f] = agg["fail_reasons"].get(f, 0) + 1
        for p in roster:
            agg["roster_by_kind"][p["kind"]] += 1
            agg["roster_after"] += p["after"]
        agg["roster_sizes"].append(len(roster))
        agg["seed_roster_roi"].append(summary([r for p in roster for r in p["after"]])["roi"])
        agg["seed_base_roi"].append(summary([r for p in players for r in p["after"]])["roi"])
    out = {
        "seeds": seeds, "mix_per_seed": mix,
        "passed_by_kind": agg["pass_by_kind"], "roster_by_kind": agg["roster_by_kind"],
        "roster_size_avg": statistics.mean(agg["roster_sizes"]),
        "roster_after": summary(agg["roster_after"]), "all_traders_after": summary(agg["all_after"]),
        "skilled_truth_after": summary(agg["skilled_after"]),
        "passed_wr_before_avg": statistics.mean(agg["pass_wr_before"]) if agg["pass_wr_before"] else None,
        "passed_wr_after_avg": statistics.mean(agg["pass_wr_after"]) if agg["pass_wr_after"] else None,
        "seeds_roster_beats_base": sum(1 for a, b in zip(agg["seed_roster_roi"], agg["seed_base_roi"]) if a is not None and a > b),
        "seeds_roster_roi_positive": sum(1 for a in agg["seed_roster_roi"] if a is not None and a > 0),
        "seeds_with_roster": sum(1 for a in agg["seed_roster_roi"] if a is not None),
        "after_by_kind_all": {k: summary(v) for k, v in agg["after_by_kind"].items()},
        "after_by_kind_passed": {k: summary(v) for k, v in agg.get("passed_after_by_kind", {}).items()},
        "skilled_fail_reasons": dict(sorted(agg["fail_reasons"].items(), key=lambda kv: -kv[1])),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
