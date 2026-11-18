"""Скан согласованности живых рынков Polymarket (точность модели, F3).

По всем темам (объём за сутки и «скоро закроются») ищет связанные исходы — лестницы порогов и дат, взаимоисключающие
исходы — и сообщает: сколько лестниц, где порядок нарушен (только рынки с живым двусторонним стаканом) и какие
комбинации дают прибыль без риска по лучшим ценам с комиссией.

Запуск: python scripts/scan_consistency.py [--min-gap 0.01]
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whalefollow.eventhub import consistency as C
from whalefollow.eventhub.advisor import liquid_keys, market_quotes
from whalefollow.eventhub.catalog import TOPIC_TAG, EventsCatalog
from whalefollow.fees import rate_for
from whalefollow.ingest.polymarket import ClobClient, GammaClient


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-gap", type=float, default=C.MIN_GAP)
    a = ap.parse_args()
    C.MIN_GAP = a.min_gap
    cat = EventsCatalog(GammaClient(), ClobClient(), None)
    n_ev = n_viol = 0
    kinds: Counter = Counter()
    viols, arbs, seen = [], [], set()
    t0 = time.time()
    for topic in TOPIC_TAG:
        for sort in ("volume24", "ending"):
            for it in cat.browse(topic, sort)["items"]:
                if it["id"] in seen or it["outcomes_n"] < 2:
                    continue
                seen.add(it["id"])
                ev = cat.get(it["id"])
                rows = [r for r in ev["outcomes"] if not r["closed"]]
                lads = C.ladders(rows)
                if not lads and not ev["neg_risk"]:
                    continue
                n_ev += 1
                for lad in lads:
                    kinds[lad.kind] += 1
                market, qs = market_quotes(cat, ev)
                pr = C.projected(rows, market, liquid_keys(ev, qs))
                if pr:
                    n_viol += 1
                    w = max(pr.values(), key=lambda v: abs(v["gap"]))
                    viols.append((abs(w["gap"]), ev["title"][:60], w["ladder"]))
                for x in C.arbitrage(ev, qs, rate_for(ev["topic"], ev.get("tag_slugs"))):
                    arbs.append((x["profit"], x["shares"], ev["title"][:60], x["kind"]))
    print(
        f"событий со связанными исходами: {n_ev}; лестниц: {sum(kinds.values())} {dict(kinds)}; "
        f"с нарушением порядка ≥ {a.min_gap * 100:.1f} п.п. (живой стакан): {n_viol}; {time.time() - t0:.0f} с"
    )
    for v in sorted(viols, reverse=True)[:10]:
        print(f"  нарушение {v[0] * 100:.1f} п.п. — {v[1]} | {v[2]}")
    print(f"без риска (по лучшим ценам, с комиссией, выгода > {C.ARB_BUFFER * 100:.1f}¢): {len(arbs)}")
    for x in sorted(arbs, reverse=True)[:10]:
        print(f"  +{x[0] * 100:.1f}¢ на комплект, до {x[1]} компл. — {x[2]} ({x[3]})")


if __name__ == "__main__":
    main()
