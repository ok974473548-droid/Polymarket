"""Крипто по опционам Deribit (точность модели, F4).

Прежняя модель брала одну волатильность (индекс DVOL — «у денег») и логнормальное распределение. Опционы вдали от
текущей цены торгуются дороже (улыбка волатильности): рынок ждёт резких движений чаще, чем «нормальное»
распределение, — поэтому прежняя модель занижала вероятности дальних порогов.

Здесь — вся улыбка:
  1. по каждой дате экспирации Deribit — подразумеваемая волатильность по страйкам (σ от логарифма K/F);
  2. на срок рынка T — интерполяция полной дисперсии w = σ²·t по времени между соседними экспирациями;
  3. P(цена в момент T выше K) = −dC/dK (цифровой опцион из цен колл-опционов с улыбкой, конечная разность);
  4. «коснётся до T»: ≈ 2·P(выше K в момент T) (отражение; для «вниз» — 2·P(ниже));
     если с начала окна рынка цена уже касалась порога — 1;
  5. «между K1 и K2» — разность цифровых.
Цены — нейтральные к риску; на сроках до года снос мал по сравнению с разбросом.
"""

from __future__ import annotations

import math
import re
import time
from datetime import UTC, datetime
from statistics import NormalDist, median
from typing import Any

N = NormalDist()
YEAR = 365 * 86400
MONTHS = {
    m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)
}
# актив → (валюта запроса Deribit, префикс инструмента)
DERIBIT = {"BTC": ("BTC", "BTC"), "ETH": ("ETH", "ETH"), "SOL": ("USDC", "SOL_USDC"), "XRP": ("USDC", "XRP_USDC")}
MIN_STRIKES = 5
MAX_MONEYNESS = 1.2  # |ln(K/F)| — дальше точек мало и они шумные


def parse_instrument(name: str) -> dict | None:
    """«BTC-20NOV26-64000-P» → {prefix, exp (UTC 08:00), K, cp}; страйки «2d5» — 2,5."""
    m = re.match(r"^([A-Z_]+)-(\d{1,2})([A-Z]{3})(\d{2})-([\dd.]+)-([CP])$", name)
    if not m or m.group(3) not in MONTHS:
        return None
    exp = datetime(2000 + int(m.group(4)), MONTHS[m.group(3)], int(m.group(2)), 8, tzinfo=UTC).timestamp()
    return {"prefix": m.group(1), "exp": int(exp), "K": float(m.group(5).replace("d", ".")), "cp": m.group(6)}


def build_smiles(points: list[tuple[int, float, float, float]], now: float, max_days: int = 400) -> list[dict]:
    """points: (экспирация, страйк, σ (доля), форвард) → улыбки по экспирациям, отсортированы по сроку."""
    by: dict[int, dict] = {}
    for exp, k, iv, f in points:
        if exp <= now + 3600 or exp > now + max_days * 86400 or not (0.05 < iv < 5) or k <= 0 or f <= 0:
            continue
        by.setdefault(exp, {"F": [], "pts": {}})
        by[exp]["F"].append(f)
        by[exp]["pts"].setdefault(k, []).append(iv)
    out = []
    for exp, d in sorted(by.items()):
        f = median(d["F"])
        pts = sorted((math.log(k / f), median(v)) for k, v in d["pts"].items() if abs(math.log(k / f)) <= MAX_MONEYNESS)
        if len(pts) >= MIN_STRIKES:
            out.append(
                {"exp": exp, "F": round(f, 6), "k": [round(x, 5) for x, _ in pts], "iv": [round(v, 5) for _, v in pts]}
            )
    return out


def sigma_at(sm: dict, k: float) -> float:
    """σ на логарифмической удалённости k: линейно между страйками, за краями — как у крайнего."""
    ks, ivs = sm["k"], sm["iv"]
    if k <= ks[0]:
        return ivs[0]
    if k >= ks[-1]:
        return ivs[-1]
    for i in range(1, len(ks)):
        if k <= ks[i]:
            a = (k - ks[i - 1]) / (ks[i] - ks[i - 1])
            return ivs[i - 1] + a * (ivs[i] - ivs[i - 1])
    return ivs[-1]


def forward_and_var(smiles: list[dict], spot: float, T: float, K: float, now: float) -> tuple[float, float]:
    """Форвард на момент T и полная дисперсия w = σ²·t для страйка K (интерполяция по времени)."""
    t = max(T - now, 60.0) / YEAR
    sm = smiles
    t_of = [(s["exp"] - now) / YEAR for s in sm]
    if sm[0]["exp"] >= T:
        f = spot + (sm[0]["F"] - spot) * min(1.0, t / t_of[0])
        return f, sigma_at(sm[0], math.log(K / f)) ** 2 * t
    if sm[-1]["exp"] <= T:
        f = sm[-1]["F"]
        return f, sigma_at(sm[-1], math.log(K / f)) ** 2 * t
    for i in range(1, len(sm)):
        if sm[i]["exp"] >= T:
            a = (t - t_of[i - 1]) / (t_of[i] - t_of[i - 1])
            f = sm[i - 1]["F"] + a * (sm[i]["F"] - sm[i - 1]["F"])
            w1 = sigma_at(sm[i - 1], math.log(K / sm[i - 1]["F"])) ** 2 * t_of[i - 1]
            w2 = sigma_at(sm[i], math.log(K / sm[i]["F"])) ** 2 * t_of[i]
            return f, w1 + a * (w2 - w1)
    raise AssertionError("unreachable")


def _call(f: float, k: float, w: float) -> float:
    if w <= 1e-12:
        return max(f - k, 0.0)
    s = math.sqrt(w)
    d1 = (math.log(f / k) + w / 2) / s
    return f * N.cdf(d1) - k * N.cdf(d1 - s)


def prob_above(smiles: list[dict], spot: float, T: float, K: float, now: float) -> float:
    """P(цена в момент T > K) = −dC/dK по ценам колл-опционов с улыбкой (учитывает наклон улыбки)."""
    h = K * 0.005
    f1, w1 = forward_and_var(smiles, spot, T, K - h, now)
    f2, w2 = forward_and_var(smiles, spot, T, K + h, now)
    p = -(_call(f2, K + h, w2) - _call(f1, K - h, w1)) / (2 * h)
    return min(max(p, 0.0), 1.0)


def probability(
    smiles: list[dict],
    spot: float,
    T: float,
    th: dict,
    now: float,
    window_hi: float | None = None,
    window_lo: float | None = None,
) -> float | None:
    """th — разобранный порог (estimators.parse_threshold): k, k2?, dir up/down/range, kind touch/terminal."""
    if not smiles or now >= T:
        return None
    k = float(th["k"])
    if th["dir"] == "range" and th.get("k2"):
        return max(0.0, prob_above(smiles, spot, T, k, now) - prob_above(smiles, spot, T, float(th["k2"]), now))
    if th["kind"] == "touch":
        if th["dir"] == "up":
            if spot >= k or (window_hi is not None and window_hi >= k):
                return 1.0  # уже касалась в окне рынка
            return min(1.0, 2 * prob_above(smiles, spot, T, k, now))
        if spot <= k or (window_lo is not None and window_lo <= k):
            return 1.0
        return min(1.0, 2 * (1 - prob_above(smiles, spot, T, k, now)))
    p = prob_above(smiles, spot, T, k, now)
    return p if th["dir"] == "up" else 1 - p


# ---------------------------------------------------------------- данные Deribit
def chain_smiles(get: Any, asset: str, now: float | None = None) -> list[dict]:
    """Текущая улыбка: сводка по всем опционам актива (mark_iv, форвард экспирации). Берутся опционы «вне денег»."""
    now = now or time.time()
    cur, prefix = DERIBIT[asset]
    rows = (
        get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency", {"currency": cur, "kind": "option"})
        or {}
    ).get("result") or []
    pts = []
    for r in rows:
        ins = parse_instrument(str(r.get("instrument_name") or ""))
        if not ins or ins["prefix"] != prefix or not r.get("mark_iv") or not r.get("underlying_price"):
            continue
        f = float(r["underlying_price"])
        if (ins["cp"] == "C") != (ins["K"] >= f):
            continue  # «в деньгах» — у них хуже ликвидность; σ та же по паритету
        pts.append((ins["exp"], ins["K"], float(r["mark_iv"]) / 100, f))
    return build_smiles(pts, now)


def trades_smiles(get: Any, asset: str, t: float, hours: float = 6.0, max_pages: int = 6) -> list[dict]:
    """Улыбка на прошлый момент t по сделкам опционов за предыдущие часы (архив Deribit) — для проверки на истории."""
    cur, prefix = DERIBIT[asset]
    pts: list[tuple[int, float, float, float]] = []
    end = int(t * 1000)
    start = int((t - hours * 3600) * 1000)
    for _ in range(max_pages):  # по 1000 сделок
        r = (
            get(
                "https://history.deribit.com/api/v2/public/get_last_trades_by_currency_and_time",
                {
                    "currency": cur,
                    "kind": "option",
                    "start_timestamp": start,
                    "end_timestamp": end,
                    "count": 1000,
                    "sorting": "desc",
                },
            )
            or {}
        ).get("result") or {}
        trades = r.get("trades") or []
        for tr in trades:
            ins = parse_instrument(str(tr.get("instrument_name") or ""))
            if ins and ins["prefix"] == prefix and tr.get("iv") and tr.get("index_price"):
                pts.append((ins["exp"], ins["K"], float(tr["iv"]) / 100, float(tr["index_price"])))
        if not r.get("has_more") or not trades:
            break
        end = int(trades[-1]["timestamp"]) - 1
    return build_smiles(pts, t)


def window_extremes(candles: list[list[float]], start: float, end: float) -> tuple[float | None, float | None]:
    """Максимум и минимум цены в окне [start, end) по свечам Coinbase [время, low, high, open, close, объём]."""
    hi = lo = None
    for c in candles:
        if start <= c[0] < end:
            hi = c[2] if hi is None else max(hi, c[2])
            lo = c[1] if lo is None else min(lo, c[1])
    return hi, lo


_WINDOW = re.compile(
    r"\b(?:in|on|during)\s+("
    + "|".join(
        m.title()
        for m in (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        )
    )
    + r")(?:\s+(\d{1,2}))?(?:\s*[-–]\s*(\d{1,2}))?(?:,?\s*(\d{4}))?|\bin\s+(\d{4})\b",
    re.I,
)


def window_start(title: str, end_ts: int) -> float | None:
    """Начало окна «коснётся в …»: «in November» → 1 ноября, «November 16-22» → 16 ноября, «in 2026» → 1 января."""
    ref = datetime.fromtimestamp(end_ts, UTC)
    m = _WINDOW.search(title)
    if m is None:
        m2 = re.search(
            r"\b("
            + "|".join(
                k.title()
                for k in (
                    "january",
                    "february",
                    "march",
                    "april",
                    "may",
                    "june",
                    "july",
                    "august",
                    "september",
                    "october",
                    "november",
                    "december",
                )
            )
            + r")\s+(\d{1,2})\s*[-–]\s*\d{1,2}",
            title,
        )
        if not m2:
            return None
        mon = list(MONTHS)[
            [
                k.title()
                for k in (
                    "january",
                    "february",
                    "march",
                    "april",
                    "may",
                    "june",
                    "july",
                    "august",
                    "september",
                    "october",
                    "november",
                    "december",
                )
            ].index(m2.group(1).title())
        ]
        return datetime(ref.year, MONTHS[mon], int(m2.group(2)), tzinfo=UTC).timestamp()
    if m.group(5):
        return datetime(int(m.group(5)), 1, 1, tzinfo=UTC).timestamp()
    mon = m.group(1)[:3].upper()
    year = int(m.group(4)) if m.group(4) else ref.year
    day = int(m.group(2)) if m.group(2) else 1
    return datetime(year, MONTHS[mon], day, tzinfo=UTC).timestamp()
