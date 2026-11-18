"""Базовые кривые поправки цены (модуль «События»): по настоящим закрытым рынкам Polymarket.

Что делает:
  1. берёт закрытые события по темам (Gamma /events?closed=true), рынки с однозначным исходом и объёмом от $5 000;
  2. по каждому рынку — цена «Да» за 1, 7 и 30 дней до закрытия (CLOB /prices-history);
  3. проверка вне выборки: кривые подгоняются на 70% более ранних рынков, проверяются на 30% поздних
     (Brier и log-loss «цена как есть» против «цена с поправкой»);
  4. итоговые кривые по всем данным — в whalefollow/eventhub/calibration_baseline.json (с отчётом проверки).

Запуск (нужна сеть): python scripts/fit_event_calibration.py [--per-topic 250] [--out путь_к_набору.jsonl]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from whalefollow.eventhub import calibration as cal
from whalefollow.eventhub.catalog import TOPIC_TAG
from whalefollow.ingest.polymarket import ClobClient, GammaClient, _json_list, _ts
from whalefollow.util import pmap

DAY = 86400
HORIZONS_D = (1, 7, 30)


def closed_markets(g: GammaClient, tag: str, topic: str, per_topic: int, since_ts: int) -> list[dict]:
    out, offset = [], 0
    since = datetime.fromtimestamp(since_ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    while len(out) < per_topic and offset < 3000:
        evs = g.events(
            limit=100, offset=offset, closed="true", order="volume", ascending="false", tag_slug=tag, end_date_min=since
        )
        if not evs:
            break
        offset += 100
        for e in evs:
            for m in e.get("markets") or []:
                prices = [str(p) for p in _json_list(m.get("outcomePrices"))]
                toks = [str(t) for t in _json_list(m.get("clobTokenIds"))]
                if prices[:2] not in (["1", "0"], ["0", "1"]) or len(toks) < 2:
                    continue  # исход не однозначный (50/50, спор) — пропускаем
                if float(m.get("volumeNum") or 0) < 5000:
                    continue
                close = _ts(m.get("closedTime")) or _ts(m.get("endDate"))
                start = _ts(m.get("startDate")) or _ts(m.get("createdAt"))
                if not close or close < since_ts:
                    continue
                out.append(
                    {
                        "market_id": m.get("conditionId"),
                        "token": toks[0],
                        "y": 1 if prices[0] == "1" else 0,
                        "close_ts": close,
                        "start_ts": start or 0,
                        "topic": topic,
                        "question": m.get("question"),
                    }
                )
    return out[:per_topic]


def samples_for(c: ClobClient, m: dict) -> list[dict]:
    # у закрытых рынков окно startTs–endTs ограничено (~2 недели): вся история — по 12 ч, последние 2 дня — по 30 мин
    pts = c.prices_history(m["token"], fidelity_min=720)
    pts += c.prices_history(m["token"], start=m["close_ts"] - 2 * DAY, end=m["close_ts"], fidelity_min=30)
    pts.sort()
    out = []
    for h in HORIZONS_D:
        t = m["close_ts"] - h * DAY
        if m["start_ts"] and m["start_ts"] > t - DAY:
            continue  # рынка ещё не было (или только открылся) — цена не показательна
        before = [(ts, p) for ts, p in pts if ts <= t]
        if not before or t - before[-1][0] > max(12 * 3600, h * DAY // 4):
            continue  # цены около этого момента нет
        p = before[-1][1]
        if p <= 0.0 or p >= 1.0:
            continue
        out.append({**{k: m[k] for k in ("market_id", "y", "topic", "close_ts")}, "p": p, "days_left": h})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-topic", type=int, default=250)
    ap.add_argument("--days", type=int, default=540, help="рынки, закрытые за последние N дней")
    ap.add_argument("--out", default="event_calibration_samples.jsonl")
    ap.add_argument("--from-file", default="", help="не скачивать, взять готовый набор")
    a = ap.parse_args()
    if a.from_file:
        samples = [json.loads(x) for x in Path(a.from_file).read_text().splitlines() if x.strip()]
    else:
        g, c = GammaClient(), ClobClient()
        since = int(time.time()) - a.days * DAY
        markets: list[dict] = []
        for topic, tag in TOPIC_TAG.items():
            got = closed_markets(g, tag, topic, a.per_topic, since)
            print(f"{topic}: {len(got)} рынков", flush=True)
            markets += got
        seen: set[str] = set()
        markets = [m for m in markets if not (m["market_id"] in seen or seen.add(m["market_id"]))]
        samples = []
        for _m, res, exc in pmap(lambda m: samples_for(c, m), markets, workers=8):
            if exc is None and res:
                samples += res
        Path(a.out).write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples), encoding="utf-8")
        print(f"примеров: {len(samples)} из {len(markets)} рынков → {a.out}")
    samples.sort(key=lambda s: s["close_ts"])
    cut = int(len(samples) * 0.7)
    report = cal.evaluate(samples[:cut], samples[cut:])
    for g, r in sorted(report.items(), key=lambda kv: (kv[0] != "all", kv[0])):
        d = r["brier_cal"] - r["brier_raw"]
        print(
            f"{g:28s} n={r['n']:5d}  Brier {r['brier_raw']:.4f} → {r['brier_cal']:.4f} ({'лучше' if d < 0 else 'хуже' if d > 0 else '='} {d:+.4f})"
            f"  log-loss {r['logloss_raw']:.4f} → {r['logloss_cal']:.4f}"
        )
    full = cal.Calibration.fit(samples)
    full.meta = {
        "fitted": datetime.now(UTC).strftime("%Y-%m-%d"),
        "markets": len({s["market_id"] for s in samples}),
        "samples": len(samples),
        "out_of_sample": report,
        "split": "70% ранних по дате закрытия → 30% поздних",
    }
    cal.BASELINE.write_text(json.dumps(full.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"кривые сохранены: {cal.BASELINE}")


if __name__ == "__main__":
    main()
