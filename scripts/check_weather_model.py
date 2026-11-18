"""Проверка погодной модели (точность модели, F2) на закрытых погодных рынках Polymarket.

Для каждого закрытого события «Highest/Lowest temperature in … on …» за последние ~45 дней:
  * станция и правило — из описания рынка; корзины и исход — из рынков события;
  * два момента: (a) за 36 ч до закрытия — прогнозы, выпущенные за 2 дня до часа (точно раньше этого момента);
    (b) в момент закрытия — прогнозы за 1 день + фактические сводки станции до этого момента;
  * поправка на станцию — только по дням, завершённым до момента (окно 45 дней);
  * цена Polymarket в тот же момент (CLOB /prices-history).
Параметры дня события (погрешность наблюдения, разброс оставшихся часов) и множитель разброса подбираются на
ранней половине событий (по дате), а Brier цены, модели и смеси с ценой сообщается на поздней половине — честно.

Запуск: python scripts/check_weather_model.py [--max 250] [--days 45] [--cases кэш.json]
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whalefollow import net
from whalefollow.eventhub import weather as W
from whalefollow.eventhub.calibration import brier, logloss
from whalefollow.ingest.polymarket import ClobClient, GammaClient, _json_list, _ts
from whalefollow.plugins.builtin.estimators import parse_bucket
from whalefollow.util import pmap

H = 3600
GRID = {"obs_sd": [0.0, 0.3, 0.5, 0.7, 1.0], "factor": [0.6, 0.8, 1.0], "s_mult": [1.0, 1.25, 1.5]}


def get_json(url, params):
    return net.get_json(net.client(), url, params=params, timeout=40, retries=3)


def get_text(url, params):
    r = net.client().get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.text


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sig(x):
    return 1 / (1 + math.exp(-x))


def closed_events(g: GammaClient, days: int, cap: int) -> list[dict]:
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
    out, offset = [], 0
    while len(out) < cap and offset < 3000:
        evs = g.events(
            limit=100,
            offset=offset,
            closed="true",
            tag_slug="weather",
            end_date_min=since,
            order="endDate",
            ascending="false",
        )
        if not evs:
            break
        offset += 100
        out += [e for e in evs if "temperature" in (e.get("title") or "").lower()]
    return out[:cap]


def buckets_of(e: dict) -> list[dict]:
    rows = []
    for m in e.get("markets") or []:
        title = m.get("groupItemTitle") or ""
        b = parse_bucket(title)
        prices = [str(p) for p in _json_list(m.get("outcomePrices"))]
        toks = [str(t) for t in _json_list(m.get("clobTokenIds"))]
        if not b or prices[:2] not in (["1", "0"], ["0", "1"]) or not toks:
            continue
        rows.append(
            {
                "title": title,
                "lo": b["lo"],
                "hi": b["hi"],
                "unit": b["unit"],
                "y": 1 if prices[0] == "1" else 0,
                "token": toks[0],
            }
        )
    return rows


def price_at(c: ClobClient, token: str, t: int) -> float | None:
    pts = c.prices_history(token, start=t - 12 * H, end=t + 60, fidelity_min=10)
    before = [(ts, p) for ts, p in sorted(pts) if ts <= t]
    return before[-1][1] if before else None


def collect(a) -> list[dict]:
    """Случаи «событие × момент»: прогноз станции (μ, σ, наблюдённое, остаток дня) + корзины с ценой и исходом."""
    g, c = GammaClient(), ClobClient()
    evs = closed_events(g, a.days, a.max)
    print(f"закрытых температурных событий: {len(evs)}", flush=True)
    items = []
    for e in evs:
        desc = e.get("description") or (e.get("markets") or [{}])[0].get("description") or ""
        st, end = W.parse_station(desc), _ts(e.get("endDate"))
        tg = W.parse_target(e.get("title") or "", end)
        bk = buckets_of(e)
        if st and tg and end and len(bk) >= 3:
            items.append({"id": e["id"], "title": e["title"], "station": st, "target": tg, "end": end, "buckets": bk})
    print(f"разобрано (станция, дата, корзины): {len(items)}", flush=True)
    stations = {it["station"]["id"]: it["station"] for it in items}
    data: dict[str, dict] = {}

    def load(sid: str) -> dict:
        st = stations[sid]
        if st["kind"] == "metar":
            st = {**st, **(W.station_info(get_json, sid) or {})}
        resp = W.fetch_forecasts(get_json, st["lat"], st["lon"], past_days=92, forecast_days=1)
        tz = ZoneInfo(resp.get("timezone") or "UTC")
        start = date.today() - timedelta(days=95)
        if st["kind"] == "metar":
            obs = W.fetch_obs(get_text, sid, start, date.today())
            daily = {"max": W.obs_daily(obs, tz, "max"), "min": W.obs_daily(obs, tz, "min")}
        else:
            months = sorted({(d.year, d.month) for d in (start + timedelta(days=i) for i in range(96))})
            obs = []
            daily = {v: W.fetch_hko_daily(get_json, v, months) for v in ("max", "min")}
        return {"resp": resp, "tz": tz, "obs": obs, "daily": daily}

    for sid in stations:  # архив IEM ограничивает частоту — станции по очереди, с паузой и повтором
        for attempt in range(4):
            try:
                data[sid] = load(sid)
                break
            except Exception as exc:
                if "429" in str(exc) and attempt < 3:
                    time.sleep(15 * (attempt + 1))
                    continue
                print(f"  станция {sid}: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
                break
        time.sleep(1.5)
    print(f"станций с данными: {len(data)} из {len(stations)}", flush=True)

    def one(it: dict) -> list[dict]:
        d = data.get(it["station"]["id"])
        if not d:
            return []
        var, day, tz = it["target"]["var"], it["target"]["date"], d["tz"]
        cases = []
        for horizon, t, lead in (("за 36 ч", it["end"] - 36 * H, 2), ("при закрытии", it["end"] - 600, 1)):
            win = W.window(day, datetime.fromtimestamp(t, tz).date())
            cal = W.fit(W.model_history(d["resp"], win, var, lead), d["daily"][var], win)
            fc = W.model_daily(d["resp"], day, var, lead)
            mu = W.combine(fc, cal)
            if mu is None or cal.n < 15:
                continue
            seen = mu_rest = None
            if horizon == "при закрытии" and d["obs"]:
                day_obs = [
                    v for ts, v in d["obs"] if ts.timestamp() <= t and ts.astimezone(tz).date().isoformat() == day
                ]
                if day_obs:
                    seen = max(day_obs) if var == "max" else min(day_obs)
                    h, rest = d["resp"]["hourly"], {}
                    for m in fc:
                        vals = [
                            v
                            for tt, v in zip(h["time"], h.get(f"temperature_2m_previous_day1_{m}") or [], strict=False)
                            if v is not None
                            and tt.startswith(day)
                            and datetime.fromisoformat(tt).replace(tzinfo=tz).timestamp() > t
                        ]
                        if vals:
                            rest[m] = max(vals) if var == "max" else min(vals)
                    mu_rest = W.combine(rest, cal) if rest else (-99.0 if var == "max" else 99.0)
            conv = (lambda x: (x - 32) * 5 / 9) if it["buckets"][0]["unit"] == "F" else (lambda x: x)
            bks = []
            for b in it["buckets"]:
                px = price_at(c, b["token"], t)
                if px is None:
                    continue
                bks.append(
                    {
                        "title": b["title"],
                        "y": b["y"],
                        "p": px,
                        "lo": conv(b["lo"]) if b["lo"] != -math.inf else -1e9,
                        "hi": conv(b["hi"]) if b["hi"] != math.inf else 1e9,
                    }
                )
            if bks:
                cases.append(
                    {
                        "event": it["id"],
                        "title": it["title"],
                        "station": it["station"]["id"],
                        "day": day,
                        "var": var,
                        "horizon": horizon,
                        "t": t,
                        "tz": str(tz),
                        "mu": mu,
                        "sigma": cal.sigma,
                        "n": cal.n,
                        "seen": seen,
                        "mu_rest": mu_rest,
                        "buckets": bks,
                    }
                )
        return cases

    out: list[dict] = []
    for _, res, exc in pmap(one, items, workers=6):
        if exc is None and res:
            out += res
        elif exc is not None:
            print("  ошибка:", type(exc).__name__, str(exc)[:120], flush=True)
    return out


def model_rows(cases: list[dict], obs_sd: float, factor: float, s_mult: float) -> list[dict]:
    rows = []
    for cs in cases:
        sigma = cs["sigma"] * s_mult
        s_rest = W.rest_sigma(sigma, cs["t"], ZoneInfo(cs["tz"]), factor) if cs["seen"] is not None else None
        cdf = W.cdf_factory(cs["mu"], sigma, cs["seen"], cs["var"], cs["mu_rest"], s_rest, obs_sd=obs_sd)
        probs = W.bucket_probs({b["title"]: (b["lo"], b["hi"]) for b in cs["buckets"]}, cdf)
        for b in cs["buckets"]:
            rows.append(
                {"event": cs["event"], "horizon": cs["horizon"], "y": b["y"], "p": b["p"], "m": probs[b["title"]]}
            )
    return rows


def score(rows: list[dict]) -> float:
    return brier([(r["m"], r["y"]) for r in rows]) if rows else float("inf")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=250)
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--cases", default="weather_cases.json", help="кэш случаев (если есть — сеть не нужна)")
    a = ap.parse_args()
    path = Path(a.cases)
    if path.exists():
        cases = json.loads(path.read_text(encoding="utf-8"))
    else:
        cases = collect(a)
        path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    days = sorted({cs["day"] for cs in cases})
    cut = days[len(days) // 2]
    train = [cs for cs in cases if cs["day"] < cut]
    test = [cs for cs in cases if cs["day"] >= cut]
    print(f"случаев: {len(cases)}; подбор параметров — дни до {cut} ({len(train)}), проверка — с {cut} ({len(test)})")
    best = {}
    for horizon in ("за 36 ч", "при закрытии"):
        tr = [cs for cs in train if cs["horizon"] == horizon]
        grid = (
            itertools.product(GRID["obs_sd"], GRID["factor"], GRID["s_mult"])
            if horizon == "при закрытии"
            else ((W.OBS_SD, W.SAME_DAY_FACTOR, s) for s in GRID["s_mult"])
        )
        best[horizon] = min(grid, key=lambda g: score(model_rows(tr, *g)))
        print(
            f"  {horizon}: лучшие на ранней половине — погрешность наблюдения {best[horizon][0]}, "
            f"доля разброса к вечеру {best[horizon][1]}, множитель разброса {best[horizon][2]}"
        )
    rows = []
    for horizon in ("за 36 ч", "при закрытии"):
        rows += model_rows([cs for cs in test if cs["horizon"] == horizon], *best[horizon])
    report(rows)


def report(rows: list[dict]) -> None:
    for horizon in ("за 36 ч", "при закрытии"):
        rs = [r for r in rows if r["horizon"] == horizon]
        if not rs:
            continue
        print(f"\n— {horizon} (поздняя половина): событий {len({r['event'] for r in rs})}, корзин {len(rs)}")
        for name, f in (
            ("цена Polymarket", lambda r: r["p"]),
            ("модель", lambda r: r["m"]),
            ("смесь λ=0,25", lambda r: sig(logit(r["p"]) + 0.25 * (logit(r["m"]) - logit(r["p"])))),
            ("смесь λ=0,5", lambda r: sig(logit(r["p"]) + 0.5 * (logit(r["m"]) - logit(r["p"])))),
            ("смесь λ=0,75", lambda r: sig(logit(r["p"]) + 0.75 * (logit(r["m"]) - logit(r["p"])))),
        ):
            pairs = [(f(r), int(r["y"])) for r in rs]
            print(f"   {name:18s} Brier {brier(pairs):.4f}  log-loss {logloss(pairs):.4f}")
        per = defaultdict(float)
        for r in rs:
            per[r["event"]] += (r["m"] - r["y"]) ** 2 - (r["p"] - r["y"]) ** 2
        v = list(per.values())
        mean = sum(v) / len(v)
        se = math.sqrt(sum((x - mean) ** 2 for x in v) / max(len(v) - 1, 1) / len(v))
        print(f"   модель − цена по событиям: {mean:+.4f} ± {2 * se:.4f} (2 ошибки; минус — модель точнее)")


if __name__ == "__main__":
    main()
