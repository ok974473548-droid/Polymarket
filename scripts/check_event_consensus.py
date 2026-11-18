"""Проверка: помогает ли консенсус другой площадки (Manifold) к цене Polymarket — на закрытых рынках.

Берёт набор из fit_event_calibration.py (цены Polymarket за 1/7/30 дней до закрытия и исходы), ищет на Manifold
закрытые вопросы с тем же смыслом (совпадение слов ≥ порога), берёт вероятность Manifold на тот же момент
(по истории ставок) и сравнивает Brier: цена Polymarket, Manifold, их смесь в логитах (вес Manifold 0,25/0,5).

Запуск: python scripts/check_event_consensus.py набор.jsonl [--max 1500] [--sim 0.6]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whalefollow import net
from whalefollow.estimator.external import similarity, words
from whalefollow.estimator.sources import keywords
from whalefollow.eventhub.calibration import brier, logloss
from whalefollow.ingest.polymarket import GammaClient
from whalefollow.util import pmap

M = "https://api.manifold.markets/v0"


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def questions(ids: list[str]) -> dict[str, str]:
    g = GammaClient()
    out = {}
    for i in range(0, len(ids), 50):
        for m in g.markets_by_ids(ids[i : i + 50]):
            out[m.get("conditionId")] = m.get("question") or ""
    return out


def match(q: str, sim_min: float) -> dict | None:
    s = net.client()
    r = s.get(
        f"{M}/search-markets",
        params={"term": keywords(q, 7), "limit": 8, "filter": "resolved", "contractType": "BINARY"},
        timeout=20,
    )
    if r.status_code != 200:
        return None
    best, bs = None, 0.0
    qw = words(q)
    for c in r.json():
        if c.get("resolution") not in ("YES", "NO"):
            continue
        sm = similarity(qw, words(c.get("question") or ""))
        if sm > bs:
            best, bs = c, sm
    return {**best, "_sim": bs} if best and bs >= sim_min else None


def prob_at(contract_id: str, t: int) -> float | None:
    # последняя ставка до момента t — вероятность Manifold на тот момент (рынка могло ещё не быть)
    r = net.client().get(
        f"{M}/bets", params={"contractId": contract_id, "limit": 1, "beforeTime": int(t) * 1000}, timeout=20
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    bets = [b for b in r.json() if b.get("probAfter") is not None]
    return float(bets[0]["probAfter"]) if bets else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("samples")
    ap.add_argument("--max", type=int, default=1500)
    ap.add_argument("--sim", type=float, default=0.6)
    a = ap.parse_args()
    samples = [json.loads(x) for x in Path(a.samples).read_text().splitlines() if x.strip()]
    ids = list(dict.fromkeys(s["market_id"] for s in samples))[: a.max]
    qs = questions(ids)
    found = {
        mid: res
        for mid, res, exc in pmap(lambda mid: match(qs.get(mid, ""), a.sim), ids, workers=4)
        if exc is None and res
    }
    print(f"рынков: {len(ids)}, похожих закрытых вопросов на Manifold: {len(found)}")
    rows = []

    def one(s: dict) -> dict | None:
        c = found.get(s["market_id"])
        if not c:
            return None
        t = s["close_ts"] - s["days_left"] * 86400
        pm = prob_at(c["id"], t)
        if pm is None:
            return None
        return {**s, "pm": pm, "agree": (c["resolution"] == "YES") == bool(s["y"]), "sim": c["_sim"]}

    errs = none = 0
    cand = [s for s in samples if s["market_id"] in found]
    for _, r, exc in pmap(one, cand, workers=4):
        if exc is not None:
            errs += 1
        elif r:
            rows.append(r)
        else:
            none += 1
    print(f"пар рынок × срок: {len(cand)}; на Manifold к тому моменту ещё не торговали: {none}; ошибок: {errs}")
    if not rows:
        print("пар нет — проверить нельзя")
        return
    agree = sum(r["agree"] for r in rows) / len(rows)
    print(f"пар (рынок × срок) с вероятностью Manifold на тот момент: {len(rows)}; исходы совпали: {agree:.0%}")
    Path("consensus_pairs.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    groups = {
        "все пары (без отбора по исходу — честно)": rows,
        "совпадение вопроса ≥ 0,7, без отбора по исходу": [r for r in rows if r["sim"] >= 0.7],
        "только где исходы совпали (завышает пользу Manifold)": [r for r in rows if r["agree"]],
    }
    for title, sel in groups.items():
        print(f"\n— {title}")
        report(sel)


def report(rows: list[dict]) -> None:
    for name, f in (
        ("Polymarket", lambda r: r["p"]),
        ("Manifold", lambda r: r["pm"]),
        ("смесь 25% Manifold", lambda r: sig(0.75 * logit(r["p"]) + 0.25 * logit(r["pm"]))),
        ("смесь 50%", lambda r: sig(0.5 * logit(r["p"]) + 0.5 * logit(r["pm"]))),
    ):
        pairs = [(f(r), int(r["y"])) for r in rows]
        print(f"{name:22s} n={len(pairs)}  Brier {brier(pairs):.4f}  log-loss {logloss(pairs):.4f}")


if __name__ == "__main__":
    main()
