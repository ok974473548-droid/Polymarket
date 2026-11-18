"""Проверка крипто-модели по опционам (точность модели, F4) на закрытых крипто-рынках Polymarket.

Закрытые события про Bitcoin/Ethereum с порогами («hit», «above», «between» …) за последние ~60 дней.
Для каждого — моменты за 1 и 7 дней до закрытия; на каждый момент t, только по данным до t:
  * цена актива — свеча Coinbase (Polymarket решает по Binance; разница цен бирж мала по сравнению с порогами);
  * максимум/минимум с начала окна рынка до t («коснётся в ноябре» — уже касалась?);
  * улыбка волатильности — сделки опционов Deribit за 6 часов до t (архив history.deribit.com);
  * индекс DVOL на t — для прежней модели (одна волатильность, логнормальное распределение);
  * цена Polymarket на t.
Сравнение Brier: цена, опционная модель, прежняя модель, смеси с ценой. λ смеси выбирается на ранней половине
событий (по дате закрытия), результат — на поздней.

Запуск: python scripts/check_crypto_model.py [--max 150] [--days 60] [--cases кэш.json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whalefollow import net
from whalefollow.eventhub import crypto as K
from whalefollow.eventhub.calibration import brier, logloss
from whalefollow.ingest.polymarket import ClobClient, GammaClient, _json_list, _ts
from whalefollow.plugins.builtin.estimators import parse_threshold, threshold_prob
from whalefollow.util import pmap

DAY = 86400
ASSET = {"bitcoin": "BTC", "btc": "BTC", "ethereum": "ETH", "eth": "ETH"}
LAMS = [0.25, 0.5, 0.75]


def get(url, params=None):
    return net.get_json(net.client(), url, params=params, timeout=40, retries=3)


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sig(x):
    return 1 / (1 + math.exp(-x))


def asset_of(text: str) -> str | None:
    low = text.lower()
    return next((v for k, v in ASSET.items() if re.search(rf"\b{k}\b", low)), None)


def closed_events(g: GammaClient, days: int, cap: int) -> list[dict]:
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * DAY))
    out, offset = [], 0
    while len(out) < cap and offset < 6000:
        evs = g.events(
            limit=100,
            offset=offset,
            closed="true",
            tag_slug="crypto",
            end_date_min=since,
            order="volume",
            ascending="false",
        )
        if not evs:
            break
        offset += 100
        for e in evs:
            t = (e.get("title") or "").lower()
            if "up or down" in t or not asset_of(t) or len(e.get("markets") or []) < 2:
                continue
            if any(w in t for w in ("hit", "above", "below", "price", "between", "reach", "dip")):
                out.append(e)
    return out[:cap]


def candles(asset: str, start: float, end: float) -> list[list[float]]:
    """Часовые свечи Coinbase [время, low, high, open, close, объём] за период (по 300 за запрос)."""
    out, t = [], start
    while t < end:
        t2 = min(t + 300 * 3600, end)
        rows = (
            get(
                f"https://api.exchange.coinbase.com/products/{asset}-USD/candles",
                {
                    "granularity": 3600,
                    "start": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                    "end": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t2)),
                },
            )
            or []
        )
        out += rows
        t = t2
        time.sleep(0.35)
    return sorted({int(r[0]): r for r in out}.values())


def dvol(asset: str, start: float, end: float) -> list[tuple[int, float]]:
    out, s = [], int(start * 1000)
    while s < end * 1000:
        e = min(s + 40 * DAY * 1000, int(end * 1000))
        r = (
            get(
                "https://www.deribit.com/api/v2/public/get_volatility_index_data",
                {"currency": asset, "start_timestamp": s, "end_timestamp": e, "resolution": 3600},
            )
            or {}
        ).get("result") or {}
        out += [(int(x[0] / 1000), float(x[4])) for x in r.get("data") or []]
        s = e
    return sorted(out)


def at(series: list, t: float, idx: int = 0):
    """Последняя точка ряда с временем ≤ t."""
    best = None
    for row in series:
        if row[idx] <= t:
            best = row
        else:
            break
    return best


def price_at(c: ClobClient, token: str, t: int) -> float | None:
    pts = c.prices_history(token, start=t - 12 * 3600, end=t + 60, fidelity_min=30)
    before = [(ts, p) for ts, p in sorted(pts) if ts <= t]
    return before[-1][1] if before else None


def collect(a) -> list[dict]:
    g, c = GammaClient(), ClobClient()
    evs = closed_events(g, a.days, a.max)
    print(f"закрытых крипто-событий с порогами: {len(evs)}", flush=True)
    now = time.time()
    start = now - (a.days + 40) * DAY
    series = {}
    for asset in ("BTC", "ETH"):
        series[asset] = {"candles": candles(asset, start, now), "dvol": dvol(asset, start, now)}
        print(f"  {asset}: свечей {len(series[asset]['candles'])}, DVOL {len(series[asset]['dvol'])}", flush=True)
    # улыбки на нужные моменты — заранее, параллельно, с сохранением на диск (сбор долгий, обрыв не страшен)
    cache_dir = Path(a.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    keys = set()
    for e in evs:
        asset, end = asset_of(e.get("title") or ""), _ts(e.get("endDate"))
        if asset and end:
            for h in (1, 7):
                t = end - h * DAY
                if (_ts(e.get("startDate")) or 0) + DAY <= t <= now:
                    keys.add((asset, t // 3600 * 3600))
    todo = [k for k in keys if not (cache_dir / f"{k[0]}_{k[1]}.json").exists()]
    print(f"моментов для улыбки: {len(keys)}, загрузить: {len(todo)}", flush=True)

    def fetch(k):
        sm = K.trades_smiles(get, k[0], k[1], 4.0, max_pages=3)
        (cache_dir / f"{k[0]}_{k[1]}.json").write_text(json.dumps(sm), encoding="utf-8")
        return len(sm)

    done = 0
    for _, _, exc in pmap(fetch, todo, workers=6):
        done += 1
        if exc is not None:
            print("  улыбка:", type(exc).__name__, str(exc)[:100], flush=True)
        if done % 25 == 0:
            print(f"  улыбок загружено {done}/{len(todo)}", flush=True)

    def smile(asset: str, t: int) -> list[dict]:
        f = cache_dir / f"{asset}_{t // 3600 * 3600}.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []

    cases = []

    def one(e: dict) -> list[dict]:
        asset = asset_of(e.get("title") or "")
        end = _ts(e.get("endDate"))
        if not asset or not end:
            return []
        wstart = K.window_start(e.get("title") or "", end) or _ts(e.get("startDate"))
        rows = []
        for h in (1, 7):
            t = end - h * DAY
            if t < (_ts(e.get("startDate")) or 0) + DAY or t > now:
                continue
            cnd = at(series[asset]["candles"], t)
            dv = at(series[asset]["dvol"], t)
            if not cnd or not dv:
                continue
            spot = cnd[4]
            hi, lo = K.window_extremes(series[asset]["candles"], wstart or t, t) if wstart else (None, None)
            sm = smile(asset, t)
            for m in e.get("markets") or []:
                q = m.get("question") or ""
                th = parse_threshold(q)
                prices = [str(p) for p in _json_list(m.get("outcomePrices"))]
                toks = [str(x) for x in _json_list(m.get("clobTokenIds"))]
                if not th or prices[:2] not in (["1", "0"], ["0", "1"]) or not toks:
                    continue
                mend = _ts(m.get("endDate")) or end
                px = price_at(c, toks[0], t)
                if px is None:
                    continue
                p_opt = K.probability(sm, spot, mend, th, t, hi, lo) if sm else None
                yrs = max(mend - t, 3600) / (365 * DAY)
                if th["kind"] == "touch" and (
                    (th["dir"] == "up" and hi is not None and hi >= th["k"])
                    or (th["dir"] == "down" and lo is not None and lo <= th["k"])
                ):
                    p_dv = 1.0
                else:
                    p_dv = threshold_prob(spot, th["k"], dv[1] / 100, yrs, th["dir"], th["kind"], th.get("k2"))
                rows.append(
                    {
                        "event": str(e["id"]),
                        "end": end,
                        "h": h,
                        "kind": th["kind"],
                        "dir": th["dir"],
                        "q": q[:90],
                        "y": 1 if prices[0] == "1" else 0,
                        "p": px,
                        "opt": None if p_opt is None else round(min(max(p_opt, 0.001), 0.999), 5),
                        "dvol": round(min(max(p_dv, 0.001), 0.999), 5),
                        "n_exp": len(sm),
                    }
                )
        return rows

    for _, res, exc in pmap(one, evs, workers=4):
        if exc is None and res:
            cases += res
        elif exc is not None:
            print("  ошибка:", type(exc).__name__, str(exc)[:150], flush=True)
    return cases


def blend(rows, key, lam):
    return [(sig(logit(r["p"]) + lam * (logit(r[key]) - logit(r["p"]))), r["y"]) for r in rows]


def report(title: str, rows: list[dict], lam_opt: float, lam_dv: float) -> None:
    if not rows:
        return
    print(f"\n— {title}: событий {len({r['event'] for r in rows})}, исходов {len(rows)}")
    for name, pairs in (
        ("цена Polymarket", [(r["p"], r["y"]) for r in rows]),
        ("опционы (улыбка)", [(r["opt"], r["y"]) for r in rows]),
        ("прежняя (DVOL)", [(r["dvol"], r["y"]) for r in rows]),
        (f"смесь опционы λ={lam_opt}", blend(rows, "opt", lam_opt)),
        (f"смесь прежняя λ={lam_dv}", blend(rows, "dvol", lam_dv)),
    ):
        print(f"   {name:24s} Brier {brier(pairs):.4f}  log-loss {logloss(pairs):.4f}")
    per = defaultdict(float)
    for r, (pb, _) in zip(rows, blend(rows, "opt", lam_opt), strict=True):
        per[r["event"]] += (pb - r["y"]) ** 2 - (r["p"] - r["y"]) ** 2
    v = list(per.values())
    mean = sum(v) / len(v)
    se = math.sqrt(sum((x - mean) ** 2 for x in v) / max(len(v) - 1, 1) / len(v))
    print(f"   смесь опционы − цена по событиям: {mean:+.4f} ± {2 * se:.4f} (2 ошибки; минус — точнее рынка)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=150)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--cases", default="crypto_cases.json")
    ap.add_argument("--cache-dir", default="crypto_smiles", help="улыбки по моментам (JSON) — сбор можно продолжить")
    a = ap.parse_args()
    path = Path(a.cases)
    if path.exists():
        cases = json.loads(path.read_text(encoding="utf-8"))
    else:
        cases = collect(a)
        path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    cases = [r for r in cases if r["opt"] is not None]
    ends = sorted({r["end"] for r in cases})
    cut = ends[len(ends) // 2]
    train, test = [r for r in cases if r["end"] < cut], [r for r in cases if r["end"] >= cut]
    lam_opt = min(LAMS, key=lambda lam: brier(blend(train, "opt", lam)))
    lam_dv = min(LAMS, key=lambda lam: brier(blend(train, "dvol", lam)))
    print(
        f"исходов: {len(cases)}; λ выбран на ранней половине ({len(train)}): опционы {lam_opt}, прежняя {lam_dv}; "
        f"проверка — поздняя половина ({len(test)})"
    )
    for h in (1, 7):
        report(f"за {h} дн. до закрытия (поздняя половина)", [r for r in test if r["h"] == h], lam_opt, lam_dv)
    for kind in ("touch", "terminal"):
        report(f"вид «{kind}» (поздняя половина, оба срока)", [r for r in test if r["kind"] == kind], lam_opt, lam_dv)


if __name__ == "__main__":
    main()
