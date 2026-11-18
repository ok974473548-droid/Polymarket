"""Встроенные модели оценки модуля «События» (этап C).

Проверка на закрытых рынках Polymarket (scripts/fit_event_calibration.py, scripts/check_event_consensus.py):
  * поправка цены по истории («маловероятное переоценено») вне выборки точность НЕ улучшила — выключена по умолчанию;
  * консенсус другой площадки помогает чуть-чуть, только если отбрасывать прогнозы, расходящиеся с ценой больше
    чем на 30 п.п. (это почти всегда другой вопрос), — так здесь и сделано.
"""

from __future__ import annotations

import math
import re
import time
from statistics import NormalDist

from ...eventhub import calibration
from ..base import ComponentEstimate, EstimatorPlugin, EventInfo, PluginContext

N = NormalDist()
DAY = 86400


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


# ---------------------------------------------------------------- цена с поправкой (якорь)
class MarketCalibrated(EstimatorPlugin):
    name = "market_calibrated"
    title = "Цена с поправкой по истории"
    description = (
        "Поправляет цену Polymarket по закрытым рынкам («при цене 10¢ сбывалось в 7%»). На проверке вне выборки "
        "(2432 рынка) точность не улучшила — поэтому выключена; можно включить для опыта"
    )
    role = "anchor"
    default_enabled = False

    def estimate(
        self, ctx: PluginContext, ev: EventInfo, dossier: list[dict], market: dict[str, float]
    ) -> list[ComponentEstimate]:
        days = max(0.0, ((ev.end_ts or time.time()) - time.time()) / DAY)
        out = []
        for o in ev.outcomes:
            m = market.get(o.key)
            if m is None:
                continue
            p, label = calibration.baseline().apply(m, ev.topic, days)
            out.append(ComponentEstimate(o.key, p, 1.0, f"кривая: {label}"))
        return out


# ---------------------------------------------------------------- другие площадки
def _fkey(x: dict) -> str:
    return f"{x.get('url')}|{x.get('title')}|{x.get('value')}"


class Platforms(EstimatorPlugin):
    name = "platforms"
    title = "Другие площадки прогнозов"
    description = (
        "Kalshi, Manifold, Metaculus из досье; прогнозы, расходящиеся с ценой больше чем на 30 п.п., отбрасываются"
    )
    max_gap = 0.30
    min_similarity = 0.5

    def applies(self, ev: EventInfo, dossier: list[dict]) -> bool:
        return any(x["kind"] == "forecast" for x in dossier)

    def estimate(
        self, ctx: PluginContext, ev: EventInfo, dossier: list[dict], market: dict[str, float]
    ) -> list[ComponentEstimate]:
        # один и тот же прогноз, найденный к нескольким исходам («к 30 ноября», «к 31 декабря»), не говорит ни об одном
        # из них: у исходов разная вероятность, а у прогноза — одна. Такие не берём.
        seen: dict[str, set[str]] = {}
        for x in dossier:
            if x["kind"] == "forecast":
                seen.setdefault(_fkey(x), set()).add(str(x.get("outcome_key")))
        shared = {k for k, v in seen.items() if len(v) > 1}
        out = []
        for o in ev.outcomes:
            m = market.get(o.key)
            fs = [
                x
                for x in dossier
                if x["kind"] == "forecast"
                and x.get("outcome_key") == o.key
                and x.get("value") is not None
                and _fkey(x) not in shared
            ]
            used, dropped = [], 0
            for f in fs:
                sim = float((f.get("meta") or {}).get("similarity") or 0.5)
                if sim < self.min_similarity:
                    continue
                if m is not None and abs(float(f["value"]) - m) > self.max_gap:
                    dropped += 1  # почти наверняка другой вопрос (проверено на закрытых рынках)
                    continue
                used.append((float(f["value"]), sim, f.get("source")))
            if not used:
                continue
            w = sum(s for _, s, _ in used)
            p = _sig(sum(_logit(v) * s for v, s, _ in used) / w)
            q = min(1.0, 0.45 + 0.2 * len(used)) * (w / len(used))
            srcs = ", ".join(sorted({str(src) for _, _, src in used}))
            note = f"{srcs}: {len(used)} прогн." + (f"; отброшено как другой вопрос: {dropped}" if dropped else "")
            out.append(ComponentEstimate(o.key, p, round(q, 3), note))
        return out


# ---------------------------------------------------------------- крипто: порог цены к дате
_NUM = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)\s*([kmbt])?\b|([\d]{1,3}(?:,\d{3})+(?:\.\d+)?)", re.I)


def parse_threshold(text: str) -> dict | None:
    """«Will Bitcoin reach $100,000 in November?» → {"k": 100000, "dir": "up", "kind": "touch"}."""
    low = text.lower()
    nums = []
    for m in _NUM.finditer(text):
        raw = m.group(1) or m.group(3)
        v = float(raw.replace(",", ""))
        mult = {"k": 1e3, "m": 1e6, "b": 1e9, "t": 1e12}.get((m.group(2) or "").lower(), 1)
        nums.append(v * mult)
    if not nums:
        return None
    if "between" in low and len(nums) >= 2:
        return {"k": min(nums[:2]), "k2": max(nums[:2]), "dir": "range", "kind": "terminal"}
    up = any(w in low for w in ("reach", "hit", "above", "higher", "greater", "↑", "over "))
    down = any(w in low for w in ("dip", "below", "fall", "drop", "less than", "under", "↓"))
    if up == down:
        return None
    touch = any(w in low for w in ("reach", "hit", "dip", "fall to", "drop to", "↑", "↓"))
    return {"k": nums[0], "dir": "up" if up else "down", "kind": "touch" if touch else "terminal"}


def threshold_prob(
    spot: float, k: float, sigma: float, years: float, dir_: str, kind: str, k2: float | None = None
) -> float:
    """Логнормальная модель без сноса: вероятность быть выше/ниже порога к дате или коснуться его до даты."""
    s = sigma * math.sqrt(max(years, 1e-9))
    if kind == "touch":
        a = math.log(k / spot) / s
        if dir_ == "up":
            return 1.0 if k <= spot else min(1.0, 2 * (1 - N.cdf(a)))
        return 1.0 if k >= spot else min(1.0, 2 * N.cdf(a))

    def above(x: float) -> float:
        return N.cdf((math.log(spot / x) - 0.5 * s * s) / s)

    if dir_ == "range" and k2:
        return max(0.0, above(k) - above(k2))
    return above(k) if dir_ == "up" else 1 - above(k)


class CryptoThreshold(EstimatorPlugin):
    name = "crypto_threshold"
    title = "Крипто: порог цены к дате"
    description = "Вероятность коснуться порога или быть выше/ниже него к дате — по цене и волатильности из досье (логнормальная модель)"

    def applies(self, ev: EventInfo, dossier: list[dict]) -> bool:
        if any((x.get("meta") or {}).get("opt") for x in dossier):
            return False  # есть улыбка опционов — она точнее одной волатильности; крипто дважды не учитываем
        return any(x["kind"] == "data" and (x.get("meta") or {}).get("metric") == "spot" for x in dossier)

    def estimate(
        self, ctx: PluginContext, ev: EventInfo, dossier: list[dict], market: dict[str, float]
    ) -> list[ComponentEstimate]:
        data = {(x.get("meta") or {}).get("metric"): x for x in dossier if x["kind"] == "data"}
        spot = float(data["spot"]["value"])
        vol_src = data.get("implied_vol") or data.get("realized_vol_30d") or data.get("realized_vol")
        if not vol_src or not spot:
            return []
        sigma = float(vol_src["value"]) / 100
        years = ((ev.end_ts or 0) - time.time()) / (365 * DAY)
        if years <= 0:
            return []
        out = []
        for o in ev.outcomes:
            th = parse_threshold(o.question) or parse_threshold(o.title)
            if not th:
                continue
            m = market.get(o.key)
            if th["kind"] == "touch" and m is not None and m >= 0.9:
                continue  # порог, скорее всего, уже был достигнут раньше в окне события — модель пути не знает
            p = threshold_prob(spot, th["k"], sigma, years, th["dir"], th["kind"], th.get("k2"))
            # далеко от текущей цены логнормальная модель занижает «хвосты» (улыбка волатильности: опционы вдали
            # от цены дороже) — чем дальше порог, тем меньше доверия
            z = abs(math.log(th["k"] / spot)) / (sigma * math.sqrt(years))
            q = 0.6 if z <= 1.5 else round(0.6 * max(0.15, 1 - (z - 1.5) / 2), 3)
            kind = (
                "коснуться"
                if th["kind"] == "touch"
                else "быть " + ("выше" if th["dir"] == "up" else "ниже" if th["dir"] == "down" else "в диапазоне")
            )
            note = f"цена {spot:,.0f}, порог {th['k']:,.0f}, волатильность {sigma * 100:.0f}% ({vol_src['source']}), {kind}".replace(
                ",", " "
            )
            if q < 0.6:
                note += f"; порог далеко ({z:.1f} σ) — модель занижает хвосты, вес снижен"
            out.append(ComponentEstimate(o.key, min(max(p, 0.001), 0.999), q, note))
        return out


# ---------------------------------------------------------------- погода: доля вариантов прогноза в корзине
_BUCKET = re.compile(r"(-?\d+(?:\.\d+)?)(?:\s*[-–]\s*(-?\d+(?:\.\d+)?))?\s*°?\s*([CF])\b", re.I)


def parse_bucket(title: str) -> dict | None:
    """«21°C or below» → (-∞, 21.5); «22°C» → [21.5, 22.5); «80-81°F» → [79.5, 81.5)."""
    m = _BUCKET.search(title)
    if not m:
        return None
    a = float(m.group(1))
    b = float(m.group(2)) if m.group(2) else a
    unit = m.group(3).upper()
    low = title.lower()
    if "or below" in low or "or lower" in low or "or less" in low:
        return {"lo": -math.inf, "hi": b + 0.5, "unit": unit}
    if "or higher" in low or "or above" in low or "or more" in low:
        return {"lo": a - 0.5, "hi": math.inf, "unit": unit}
    return {"lo": a - 0.5, "hi": b + 0.5, "unit": unit}


class WeatherEnsemble(EstimatorPlugin):
    name = "weather_ensemble"
    title = "Погода: ансамбль прогнозов"
    description = "Доля вариантов прогноза, попадающих в каждый исход (с поправкой на ошибку прогноза ±0,5 °C); на истории не проверено — вес небольшой"
    spread_c = 0.5

    def applies(self, ev: EventInfo, dossier: list[dict]) -> bool:
        if any((x.get("meta") or {}).get("wx") for x in dossier):
            return False  # есть прогноз по станции рынка — он точнее; погоду дважды не учитываем
        return any((x.get("meta") or {}).get("members") for x in dossier if x["kind"] == "data")

    def estimate(
        self, ctx: PluginContext, ev: EventInfo, dossier: list[dict], market: dict[str, float]
    ) -> list[ComponentEstimate]:
        src = next(x for x in dossier if x["kind"] == "data" and (x.get("meta") or {}).get("members"))
        meta = src["meta"]
        members = [float(v) for v in meta["members"]]
        f_unit = "F" if "F" in str(meta.get("unit") or "") else "C"
        s = self.spread_c * (1.8 if f_unit == "F" else 1.0)
        # не проверено на истории: сырой ансамбль по точке сетки шире и смещён относительно метеостанции,
        # по которой решается рынок, — вес небольшой, пока самопроверка (этап E) не покажет пользу
        quality = 0.4 if "GFS" in str(src.get("source")) else 0.25
        raw = {}
        for o in ev.outcomes:
            b = parse_bucket(o.title) or parse_bucket(o.question)
            if not b:
                continue
            lo, hi = b["lo"], b["hi"]
            if b["unit"] != f_unit:  # корзина в другой шкале — переводим
                conv = (lambda x: x * 9 / 5 + 32) if f_unit == "F" else (lambda x: (x - 32) * 5 / 9)
                lo, hi = conv(lo) if lo != -math.inf else lo, conv(hi) if hi != math.inf else hi
            raw[o.key] = sum(
                (N.cdf((hi - x) / s) if hi != math.inf else 1.0) - (N.cdf((lo - x) / s) if lo != -math.inf else 0.0)
                for x in members
            ) / len(members)
        tot = sum(raw.values())
        if not raw or tot <= 0:
            return []
        note = f"{len(members)} вариантов прогноза ({src['source']}), медиана {sorted(members)[len(members) // 2]:.1f}°{f_unit}"
        # корзины покрывают все исходы события — нормируем, чтобы сумма была 100% (если разобрались все исходы)
        full = len(raw) == len(ev.outcomes)
        return [
            ComponentEstimate(k, min(max(v / tot if full else v, 0.001), 0.999), quality, note) for k, v in raw.items()
        ]


# ---------------------------------------------------------------- крипто по опционам Deribit (F4)
class CryptoOptionsModel(EstimatorPlugin):
    name = "crypto_options_model"
    title = "Крипто: опционы (улыбка волатильности)"
    description = (
        "Вероятность «выше / ниже / между / коснётся» к дате рынка по ценам опционов Deribit со всей улыбкой "
        "волатильности; если цена уже касалась порога с начала окна — исход почти решён"
    )
    quality = 0.7

    def applies(self, ev: EventInfo, dossier: list[dict]) -> bool:
        return any((x.get("meta") or {}).get("opt") for x in dossier)

    def estimate(
        self, ctx: PluginContext, ev: EventInfo, dossier: list[dict], market: dict[str, float]
    ) -> list[ComponentEstimate]:
        from ...eventhub import crypto as K

        opt = next(x["meta"]["opt"] for x in dossier if (x.get("meta") or {}).get("opt"))
        now = time.time()
        if not ev.end_ts or ev.end_ts <= now:
            return []
        out = []
        for o in ev.outcomes:
            th = parse_threshold(o.question) or parse_threshold(o.title)
            if not th:
                continue
            p = K.probability(
                opt["smiles"], float(opt["spot"]), float(ev.end_ts), th, now, opt.get("hi"), opt.get("lo")
            )
            if p is None:
                continue
            kind = (
                "коснуться"
                if th["kind"] == "touch"
                else "быть " + {"up": "выше", "down": "ниже"}.get(th["dir"], "между")
            )
            note = f"цена {opt['spot']:,.0f}, порог {th['k']:,.0f}, {kind}; по улыбке опционов".replace(",", " ")
            out.append(ComponentEstimate(o.key, min(max(p, 0.001), 0.999), self.quality, note))
        return out


# ---------------------------------------------------------------- погода по станции рынка (F2)
class WeatherStationModel(EstimatorPlugin):
    name = "weather_station_model"
    title = "Погода: станция рынка, 7 моделей"
    description = (
        "Вероятность каждой корзины температуры по прогнозам 7 моделей с поправкой на станцию из правил рынка; "
        "в день события — с учётом уже наблюдённого"
    )
    # проверено на истории закрытых погодных рынков (scripts/check_weather_model.py): за 1,5 суток модель точнее цены
    quality = 0.9

    def applies(self, ev: EventInfo, dossier: list[dict]) -> bool:
        return any((x.get("meta") or {}).get("wx") for x in dossier)

    def estimate(
        self, ctx: PluginContext, ev: EventInfo, dossier: list[dict], market: dict[str, float]
    ) -> list[ComponentEstimate]:
        from ...eventhub import weather as W

        wx = next(x["meta"]["wx"] for x in dossier if (x.get("meta") or {}).get("wx"))
        buckets = {}
        for o in ev.outcomes:
            b = parse_bucket(o.title) or parse_bucket(o.question)
            if b:
                buckets[o.key] = b
        if not buckets:
            return []
        probs = W.outcome_probs(wx, buckets)
        # проверено на истории только с поправкой на станцию; без неё (архив сводок недоступен) — малое доверие
        q = self.quality if int(wx.get("n") or 0) >= 20 and not wx.get("cal_note") else 0.25
        what = "макс." if wx["var"] == "max" else "мин."
        note = f"станция {wx['station']}: {what} {wx['mu']:.1f} ± {wx['sigma']:.1f} °C".replace(".", ",")
        if wx.get("seen") is not None:
            note += f", уже {wx['seen']:.1f} °C".replace(".", ",")
        if q < self.quality:
            note += "; без поправки на станцию — доверие снижено"
        return [
            ComponentEstimate(k, min(max(p, 0.001), 0.999), q, note, meta={"wx_mu": wx["mu"]}) for k, p in probs.items()
        ]


# ---------------------------------------------------------------- «умные деньги» (пока без веса)
class SmartMoney(EstimatorPlugin):
    name = "smart_money"
    title = "Перевес сделок за сутки"
    description = "Сдвиг от цены по перевесу покупок над продажами за сутки. Вес 0, пока не доказана польза (этап E)"
    weight = 0.0

    def applies(self, ev: EventInfo, dossier: list[dict]) -> bool:
        return any(x["kind"] == "flow" and (x.get("meta") or {}).get("yes_buy") is not None for x in dossier)

    def estimate(
        self, ctx: PluginContext, ev: EventInfo, dossier: list[dict], market: dict[str, float]
    ) -> list[ComponentEstimate]:
        out = []
        for x in dossier:
            meta = x.get("meta") or {}
            if x["kind"] != "flow" or meta.get("yes_buy") is None:
                continue
            m = market.get(x["outcome_key"])
            if m is None:
                continue
            yb, ys = float(meta.get("yes_buy") or 0), float(meta.get("yes_sell") or 0)
            if yb + ys < 1000:
                continue
            f = (yb - ys) / (yb + ys)
            out.append(
                ComponentEstimate(
                    x["outcome_key"],
                    _sig(_logit(m) + 0.3 * f),
                    0.3,
                    f"перевес {f * 100:+.0f}% от оборота ${yb + ys:,.0f}".replace(",", " "),
                )
            )
        return out


# ---------------------------------------------------------------- ИИ вслепую (результат готовит eventhub.ai)
class AIBlind(EstimatorPlugin):
    name = "ai_blind"
    title = "ИИ вслепую (Claude)"
    description = (
        "Claude оценивает исходы по правилам, новостям и данным досье, не видя цен; несколько ответов — середина"
    )
    needs_ai = True
    requires_env = ("ANTHROPIC_API_KEY",)

    def estimate(
        self, ctx: PluginContext, ev: EventInfo, dossier: list[dict], market: dict[str, float]
    ) -> list[ComponentEstimate]:
        from ...eventhub import ai

        res = ai.cached(ctx.db, ev.id)
        if not res or not res.get("outcomes"):
            return []
        q = {"high": 0.8, "medium": 0.6, "low": 0.4}.get(str(res.get("confidence")), 0.5)
        out = []
        for o in ev.outcomes:
            p = res["outcomes"].get(o.key)
            if p is not None:
                out.append(
                    ComponentEstimate(
                        o.key,
                        min(max(float(p), 0.001), 0.999),
                        q,
                        f"{res.get('samples', 1)} ответ(а), уверенность: {res.get('confidence', '—')}",
                    )
                )
        return out


PLUGINS = [
    MarketCalibrated(),
    Platforms(),
    CryptoOptionsModel(),
    CryptoThreshold(),
    WeatherStationModel(),
    WeatherEnsemble(),
    SmartMoney(),
    AIBlind(),
]
