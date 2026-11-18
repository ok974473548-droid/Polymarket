"""Источники досье события (модуль «События», этап B): данные по теме события.

  * crypto_market — цена монеты и волатильность (Deribit: индекс и DVOL для BTC/ETH; Coinbase — остальные);
  * weather — ансамблевый прогноз погоды на дату события (Open-Meteo, 31 вариант модели GFS);
  * fred — последние значения экономических показателей США (FRED: CSV без ключа; с WF_FRED_API_KEY — и дата
    следующей публикации);
  * metaculus — похожие вопросы на Metaculus (нужен ключ WF_METACULUS_TOKEN);
  * polymarket_flow — крупные сделки за сутки и крупнейшие держатели на самом Polymarket.
Числа сохраняются как есть (value + unit), сырые данные — в meta: из них модель на этапе C считает вероятности.
"""

from __future__ import annotations

import csv
import io
import math
import re
import statistics
import time
from datetime import UTC, datetime
from typing import Any

from ... import net
from ...estimator.external import similarity, words
from ...estimator.sources import Forecast, keywords
from ..base import EventInfo, Evidence, PluginContext, SourcePlugin

DAY = 86400


def _get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 15) -> Any:
    """GET → JSON с повторами на 429/5xx (общий клиент приложения)."""
    s = net.client()
    if headers:
        r = s.get(url, params=params, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()
    # досье не должно ждать минутами: паузы между повторами — не больше 3 с
    return net.get_json(s, url, params=params, timeout=timeout, retries=3, sleep=lambda d: time.sleep(min(d, 3.0)))


def _text(ev: EventInfo) -> str:
    return " ".join([ev.title] + [o.title for o in ev.outcomes[:12]]).lower()


# ---------------------------------------------------------------- крипто
ASSETS = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "ether": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "xrp": "XRP",
    "ripple": "XRP",
    "dogecoin": "DOGE",
    "doge": "DOGE",
    "bnb": "BNB",
    "cardano": "ADA",
    "hyperliquid": "HYPE",
    "litecoin": "LTC",
}
DERIBIT = "https://www.deribit.com/api/v2/public"


def detect_asset(text: str) -> str | None:
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in ASSETS:
            return ASSETS[w]
    return None


class CryptoMarket(SourcePlugin):
    name = "crypto_market"
    title = "Крипторынок (Deribit, Coinbase)"
    description = "Цена монеты сейчас и волатильность: ожидаемая по опционам (DVOL) и фактическая за 30 дней"
    topics = ("crypto",)

    def applies(self, ev: EventInfo) -> bool:
        return ev.topic == "crypto" or detect_asset(ev.title) is not None

    def dossier(self, ctx: PluginContext, ev: EventInfo) -> list[Evidence]:
        asset = detect_asset(_text(ev))
        if not asset:
            return []
        now = int(time.time())
        out: list[Evidence] = []
        if asset in ("BTC", "ETH"):
            px = _get(f"{DERIBIT}/get_index_price", {"index_name": f"{asset.lower()}_usd"})["result"]["index_price"]
            out.append(
                Evidence(
                    "data",
                    "Deribit",
                    f"Цена {asset} сейчас",
                    "https://www.deribit.com",
                    now,
                    float(px),
                    "USD",
                    meta={"asset": asset, "metric": "spot"},
                )
            )
            dv = _get(
                f"{DERIBIT}/get_volatility_index_data",
                {
                    "currency": asset,
                    "start_timestamp": (now - 8 * DAY) * 1000,
                    "end_timestamp": now * 1000,
                    "resolution": "3600",
                },
            )["result"]["data"]
            if dv:
                last, week = float(dv[-1][4]), float(dv[0][4])
                out.append(
                    Evidence(
                        "data",
                        "Deribit DVOL",
                        f"Ожидаемая волатильность {asset} по опционам",
                        "https://www.deribit.com/statistics/BTC/volatility-index",
                        int(dv[-1][0] / 1000),
                        round(last, 1),
                        "% годовых",
                        note=f"неделю назад {week:.1f}%".replace(".", ","),
                        meta={"asset": asset, "metric": "implied_vol", "week_ago": week},
                    )
                )
            hv = _get(f"{DERIBIT}/get_historical_volatility", {"currency": asset})["result"]
            if hv:
                out.append(
                    Evidence(
                        "data",
                        "Deribit",
                        f"Фактическая волатильность {asset}",
                        "https://www.deribit.com",
                        int(hv[-1][0] / 1000),
                        round(float(hv[-1][1]), 1),
                        "% годовых",
                        meta={"asset": asset, "metric": "realized_vol"},
                    )
                )
        else:
            spot = _get(f"https://api.coinbase.com/v2/prices/{asset}-USD/spot")["data"]["amount"]
            out.append(
                Evidence(
                    "data",
                    "Coinbase",
                    f"Цена {asset} сейчас",
                    "https://www.coinbase.com",
                    now,
                    float(spot),
                    "USD",
                    meta={"asset": asset, "metric": "spot"},
                )
            )
        # фактическая волатильность за 30 дней по дневным свечам (для всех монет)
        try:
            candles = _get(f"https://api.exchange.coinbase.com/products/{asset}-USD/candles", {"granularity": 86400})
            closes = [float(c[4]) for c in sorted(candles, key=lambda c: c[0])][-31:]
            rets = [math.log(b / a) for a, b in zip(closes, closes[1:], strict=False) if a > 0 and b > 0]
            if len(rets) >= 20:
                vol = statistics.pstdev(rets) * math.sqrt(365) * 100
                out.append(
                    Evidence(
                        "data",
                        "Coinbase",
                        f"Фактическая волатильность {asset} за 30 дней",
                        "https://www.coinbase.com",
                        now,
                        round(vol, 1),
                        "% годовых",
                        note="по дневным ценам закрытия",
                        meta={"asset": asset, "metric": "realized_vol_30d", "closes": closes},
                    )
                )
        except Exception:
            pass  # свечей нет (монета не торгуется на Coinbase) — достаточно цены
        return out


# ---------------------------------------------------------------- погода
MONTHS = {
    m: i
    for i, m in enumerate(
        [
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
        ],
        1,
    )
}
VARS = [
    (("lowest", "low temp", "minimum"), "temperature_2m_min", "мин. температура"),
    (("rain", "precipitation"), "precipitation_sum", "осадки"),
    (("snow",), "snowfall_sum", "снег"),
    (("temperature", "temp", "hottest", "highest"), "temperature_2m_max", "макс. температура"),
]


def parse_weather(title: str, end_ts: int | None) -> dict | None:
    """«Highest temperature in Hong Kong on November 19?» → город, дата, величина."""
    m = re.search(r"\bin ([A-Z][\w.'-]*(?: [A-Z][\w.'-]*)*)", title)
    d = re.search(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2})\b", title, re.I)
    if not m or not d:
        return None
    year = datetime.fromtimestamp(end_ts or time.time(), UTC).year
    month, day = MONTHS[d.group(1).lower()], int(d.group(2))
    low = title.lower()
    var, label = next(
        ((v, lb) for keys, v, lb in VARS if any(k in low for k in keys)), ("temperature_2m_max", "макс. температура")
    )
    return {
        "city": m.group(1).strip(),
        "date": f"{year:04d}-{month:02d}-{day:02d}",
        "var": var,
        "label": label,
        "fahrenheit": "°f" in low or "fahrenheit" in low,
    }


_WEATHER_CACHE: dict[tuple, tuple[float, Any, str]] = {}


class Weather(SourcePlugin):
    name = "weather"
    title = "Прогноз погоды (Open-Meteo)"
    description = (
        "Ансамблевый прогноз на дату события: разброс 31 варианта модели GFS (если ансамбль недоступен — "
        "прогнозы шести моделей) — основа вероятностей по погоде"
    )
    topics = ("weather",)

    def dossier(self, ctx: PluginContext, ev: EventInfo) -> list[Evidence]:
        w = parse_weather(ev.title, ev.end_ts)
        if not w:
            return []  # нет одного города и даты (например, «где пойдёт дождь») — источник здесь не помогает
        geo = (
            _get("https://geocoding-api.open-meteo.com/v1/search", {"name": w["city"], "count": 1}).get("results")
            or [None]
        )[0]
        if not geo:
            return [Evidence("data", "Open-Meteo", f"Город «{w['city']}» не найден", note="прогноз не получен")]
        params = {
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "daily": w["var"],
            "timezone": "auto",
            "start_date": w["date"],
            "end_date": w["date"],
            "models": "gfs_seamless",
        }
        if w["fahrenheit"]:
            params["temperature_unit"] = "fahrenheit"
        key = (round(geo["latitude"], 2), round(geo["longitude"], 2), w["date"], w["var"], w["fahrenheit"])
        hit = _WEATHER_CACHE.get(key)
        if hit and time.time() - hit[0] < 3600:
            j, src = hit[1], hit[2]
        else:
            try:
                j, src = _get("https://ensemble-api.open-meteo.com/v1/ensemble", params), "Open-Meteo GFS"
            except Exception:
                # ансамбль недоступен (лимит частоты) — обычные прогнозы нескольких моделей как маленький ансамбль
                p2 = {k: v for k, v in params.items() if k != "models"} | {
                    "models": "best_match,gfs_seamless,ecmwf_ifs025,icon_seamless,gem_seamless,jma_seamless"
                }
                j, src = _get("https://api.open-meteo.com/v1/forecast", p2), "Open-Meteo (модели)"
            _WEATHER_CACHE[key] = (time.time(), j, src)
        daily = j.get("daily") or {}
        vals = [float(v[0]) for k, v in daily.items() if k.startswith(w["var"]) and v and v[0] is not None]
        if not vals:
            return [
                Evidence(
                    "data", "Open-Meteo", f"Прогноза на {w['date']} ещё нет", note="ансамбль строится на 16 дней вперёд"
                )
            ]
        unit = (j.get("daily_units") or {}).get(w["var"], "")
        vals.sort()
        q = lambda f: vals[min(len(vals) - 1, max(0, round(f * (len(vals) - 1))))]
        med = statistics.median(vals)
        place = f"{geo['name']}, {geo.get('country', '')}".strip(", ")
        return [
            Evidence(
                "data",
                src,
                f"Прогноз: {w['label']} {w['date']}, {place}",
                "https://open-meteo.com",
                int(time.time()),
                round(med, 1),
                unit,
                note=f"{len(vals)} вариантов прогноза: 10–90% — {q(0.1):.1f}…{q(0.9):.1f} {unit}".replace(".", ","),
                meta={"members": vals, "var": w["var"], "date": w["date"], "city": place, "unit": unit, "kind": src},
            )
        ]


# ---------------------------------------------------------------- экономика США (FRED)
# слова в событии → ряд FRED, подпись, единица, как считать (level — значение, yoy — рост за год, %)
FRED_SERIES = [
    (("core cpi", "core inflation"), "CPILFESL", "Базовая инфляция США (без еды и энергии), за год", "%", "yoy"),
    (("cpi", "inflation"), "CPIAUCSL", "Инфляция США (CPI), за год", "%", "yoy"),
    (("unemployment", "jobless rate"), "UNRATE", "Безработица США", "%", "level"),
    (
        ("payroll", "jobs report", "nonfarm", "jobs added"),
        "PAYEMS",
        "Занятость вне сельского хозяйства, тыс. чел.",
        "тыс.",
        "level",
    ),
    (
        ("fed", "fomc", "interest rate", "rate cut", "rate hike", "bps"),
        "DFEDTARU",
        "Ставка ФРС (верхняя граница)",
        "%",
        "level",
    ),
    (("gdp",), "A191RL1Q225SBEA", "Рост ВВП США, % в год (квартал)", "%", "level"),
    (("gas price", "gas prices", "gasoline"), "GASREGW", "Цена бензина в США, $ за галлон", "$", "level"),
    (("10-year", "10 year", "treasury yield"), "DGS10", "Доходность 10-летних облигаций США", "%", "level"),
    (("oil", "wti", "crude"), "DCOILWTICO", "Нефть WTI, $ за баррель", "$", "level"),
    (("mortgage",), "MORTGAGE30US", "Ипотека 30 лет, ставка", "%", "level"),
]


def fred_matches(text: str) -> list[tuple]:
    low = text.lower()
    found, used = [], set()
    for keys, sid, label, unit, how in FRED_SERIES:
        if sid not in used and any(re.search(r"\b" + re.escape(k) + r"\b", low) for k in keys):
            found.append((sid, label, unit, how))
            used.add(sid)
            if sid == "CPILFESL":
                used.add("CPIAUCSL")
    return found[:3]


_fred_http: Any = None


def fred_csv(series: str) -> list[tuple[str, float]]:
    """Ряд FRED из открытого CSV (без ключа). Сервер FRED рвёт соединения HTTP/2 — здесь HTTP/1.1 и повтор."""
    global _fred_http
    if _fred_http is None:
        _fred_http = net.make_client(timeout=20, http2=False)
    last: Exception | None = None
    for attempt in range(3):
        try:
            r = _fred_http.get("https://fred.stlouisfed.org/graph/fredgraph.csv", params={"id": series})
            r.raise_for_status()
            break
        except Exception as e:  # обрыв или 5xx — ещё раз через секунду
            last = e
            time.sleep(1 + attempt)
    else:
        raise RuntimeError(f"FRED не ответил: {last}")
    out = []
    for row in list(csv.reader(io.StringIO(r.text)))[1:]:
        if len(row) >= 2 and row[1] not in ("", "."):
            try:
                out.append((row[0], float(row[1])))
            except ValueError:
                continue
    return out


class Fred(SourcePlugin):
    name = "fred"
    title = "Экономика США (FRED)"
    description = (
        "Последние значения показателей: инфляция, безработица, ставка ФРС, ВВП, бензин и др.; "
        "с ключом WF_FRED_API_KEY — и дата следующей публикации"
    )
    topics = ("economics", "finance")

    def applies(self, ev: EventInfo) -> bool:
        return bool(fred_matches(_text(ev))) and (ev.topic in ("economics", "finance", "politics", "other"))

    def dossier(self, ctx: PluginContext, ev: EventInfo) -> list[Evidence]:
        out: list[Evidence] = []
        for sid, label, unit, how in fred_matches(_text(ev)):
            obs = fred_csv(sid)
            if not obs:
                continue
            date, val = obs[-1]
            prev = obs[-2][1] if len(obs) > 1 else None
            if how == "yoy" and len(obs) > 12:
                val = (obs[-1][1] / obs[-13][1] - 1) * 100
                prev = (obs[-2][1] / obs[-14][1] - 1) * 100 if len(obs) > 13 else None
            note = f"данные на {date}" + (f", предыдущее {prev:.2f}".replace(".", ",") if prev is not None else "")
            nxt = self._next_release(ctx, sid)
            if nxt:
                note += f"; следующая публикация {nxt}"
            out.append(
                Evidence(
                    "data",
                    "FRED",
                    label,
                    f"https://fred.stlouisfed.org/series/{sid}",
                    int(datetime.fromisoformat(date).replace(tzinfo=UTC).timestamp()),
                    round(val, 3),
                    unit,
                    note=note,
                    meta={"series": sid, "how": how, "last": obs[-24:]},
                )
            )
        return out

    def _next_release(self, ctx: PluginContext, sid: str) -> str | None:
        key = ctx.env("WF_FRED_API_KEY")
        if not key:
            return None
        try:
            base = "https://api.stlouisfed.org/fred"
            rel = _get(f"{base}/series/release", {"series_id": sid, "api_key": key, "file_type": "json"})["releases"][
                0
            ]["id"]
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            dates = _get(
                f"{base}/release/dates",
                {
                    "release_id": rel,
                    "api_key": key,
                    "file_type": "json",
                    "realtime_start": today,
                    "include_release_dates_with_no_data": "true",
                    "sort_order": "asc",
                    "limit": 3,
                },
            )["release_dates"]
            future = [d["date"] for d in dates if d["date"] >= today]
            return future[0] if future else None
        except Exception:
            return None


# ---------------------------------------------------------------- Metaculus
class Metaculus(SourcePlugin):
    name = "metaculus"
    title = "Metaculus"
    description = "Прогнозы сообщества Metaculus по похожим вопросам (нужен бесплатный ключ WF_METACULUS_TOKEN)"
    requires_env = ("WF_METACULUS_TOKEN",)

    def forecasts(self, ctx: PluginContext, question: str, market: dict | None = None) -> list:
        q = keywords(question, 6)
        if not q:
            return []
        j = _get(
            "https://www.metaculus.com/api/posts/",
            {"search": q, "statuses": "open", "forecast_type": "binary", "limit": 5},
            headers={"Authorization": f"Token {ctx.env('WF_METACULUS_TOKEN')}"},
        )
        qw = words(question)
        out = []
        for post in j.get("results") or []:
            qq = post.get("question") or {}
            agg = (qq.get("aggregations") or {}).get("recency_weighted") or {}
            latest = agg.get("latest") or {}
            centers = latest.get("centers") or latest.get("forecast_values") or []
            p = centers[0] if centers else None
            if p is None or not (0 <= float(p) <= 1):
                continue
            title = post.get("title") or qq.get("title") or ""
            if similarity(qw, words(title)) < 0.35:
                continue
            out.append(
                Forecast(
                    source="Metaculus",
                    title=title,
                    url=f"https://www.metaculus.com/questions/{post.get('id')}/",
                    probability=float(p),
                )
            )
        return out[:2]


# ---------------------------------------------------------------- сделки и держатели Polymarket
class PolymarketFlow(SourcePlugin):
    name = "polymarket_flow"
    title = "Сделки и держатели (Polymarket)"
    description = "Крупные сделки за сутки, перевес покупок или продаж и крупнейшие держатели главных исходов"
    big_usd = 2000.0

    def available(self, ctx: PluginContext) -> str | None:
        return None if ctx.clients.get("data") is not None else "Нет подключения к данным Polymarket"

    def dossier(self, ctx: PluginContext, ev: EventInfo) -> list[Evidence]:
        data = ctx.clients["data"]
        now = int(time.time())
        out: list[Evidence] = []
        big: list[tuple[float, Evidence]] = []
        for o in ev.outcomes[:4]:
            if not o.market_id:
                continue
            flows = {o.yes_token: [0.0, 0.0], o.no_token: [0.0, 0.0]}
            for t in data.trades(condition=o.market_id, limit=500, max_pages=1):
                ts = int(t.get("timestamp") or 0)
                if ts < now - DAY:
                    continue
                usd = float(t.get("size") or 0) * float(t.get("price") or 0)
                tok = str(t.get("token_id") or t.get("asset") or "")
                side = str(t.get("side") or "").upper()
                if tok in flows:
                    flows[tok][0 if side == "BUY" else 1] += usd
                if usd >= self.big_usd:
                    lbl = "«Да»" if tok == o.yes_token else "«Нет»"
                    act = "Покупка" if side == "BUY" else "Продажа"
                    big.append(
                        (
                            usd,
                            Evidence(
                                "flow",
                                "Polymarket",
                                f"{act} {lbl} «{o.title}» на ${usd:,.0f} по {float(t.get('price') or 0) * 100:.1f}¢".replace(
                                    ",", " "
                                ),
                                ts=ts,
                                value=round(usd, 2),
                                unit="USD",
                                outcome_key=o.key,
                                meta={
                                    "wallet": t.get("proxy_wallet"),
                                    "side": side,
                                    "token": tok,
                                    "price": t.get("price"),
                                },
                            ),
                        )
                    )
            yb, ys = flows.get(o.yes_token, [0.0, 0.0])
            if yb or ys:
                out.append(
                    Evidence(
                        "flow",
                        "Polymarket",
                        f"«{o.title}» за сутки: «Да» купили на ${yb:,.0f}, продали на ${ys:,.0f}".replace(",", " "),
                        ts=now,
                        value=round(yb - ys, 2),
                        unit="USD",
                        outcome_key=o.key,
                        note="перевес покупок «Да» минус продаж",
                        meta={
                            "yes_buy": yb,
                            "yes_sell": ys,
                            "no_buy": flows.get(o.no_token, [0, 0])[0],
                            "no_sell": flows.get(o.no_token, [0, 0])[1],
                        },
                    )
                )
            if hasattr(data, "holders"):
                hs = data.holders(o.market_id, limit=3, include_pnl=True)
                for tok, lbl in ((o.yes_token, "«Да»"), (o.no_token, "«Нет»")):
                    top = (hs.get(tok) or [None])[0]
                    if not top:
                        continue
                    val = float(top.get("current_value") or 0)
                    pnl = top.get("total_pnl")
                    name = top.get("name") or top.get("pseudonym") or str(top.get("proxy_wallet") or "")[:10]
                    out.append(
                        Evidence(
                            "flow",
                            "Polymarket",
                            f"Крупнейший держатель {lbl} «{o.title}»: {name} — ${val:,.0f}".replace(",", " "),
                            ts=now,
                            value=round(val, 2),
                            unit="USD",
                            outcome_key=o.key,
                            note=(
                                f"его прибыль по рынку ${float(pnl):,.0f}".replace(",", " ") if pnl is not None else ""
                            ),
                            meta={
                                "wallet": top.get("proxy_wallet"),
                                "avg_price": top.get("avg_price"),
                                "pnl": pnl,
                                "side": lbl,
                            },
                        )
                    )
        big.sort(key=lambda x: -x[0])
        return out + [e for _, e in big[:6]]


class _StateKV:
    """Кэш погодной модели в состоянии базы (координаты станций, поправка на станцию на сутки)."""

    def __init__(self, db: Any):
        self.db = db

    def get(self, key: str) -> Any:
        return self.db.get_state(key) if self.db is not None else None

    def set(self, key: str, value: Any) -> None:
        if self.db is not None:
            self.db.set_state(key, value)


def _get_text(url: str, params: dict | None = None) -> str:
    r = net.client().get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.text


class WeatherStation(SourcePlugin):
    name = "weather_station"
    title = "Погода по станции рынка"
    description = (
        "Станция из правил рынка (аэропорт METAR или обсерватория Гонконга), прогнозы 7 моделей (ECMWF, GFS, ICON, "
        "GEM, JMA, Météo-France, UKMO) с поправкой на станцию по истории, в день события — фактические сводки"
    )
    topics = ("weather",)

    def dossier(self, ctx: PluginContext, ev: EventInfo) -> list[Evidence]:
        from ...eventhub import weather as W

        target = W.parse_target(ev.title, ev.end_ts)
        station = W.parse_station(ev.description or "")
        if not target or not station:
            return []  # не температура или в правилах нет станции — источник здесь не помогает
        wx = W.station_forecast(_get, _get_text, _StateKV(ctx.db), station, target)
        if not wx:
            return []
        what = "макс." if wx["var"] == "max" else "мин."
        note = f"{len(wx['models'])} моделей, " + (wx.get("cal_note") or f"поправка по {wx['n']} дням станции")
        if wx.get("seen") is not None:
            note += f"; уже наблюдалось {wx['seen']:.1f} °C ({wx.get('obs_n', 0)} сводок)".replace(".", ",", 1)
        return [
            Evidence(
                "data",
                f"Станция {wx['station']}",
                f"Прогноз станции {wx['name'] or wx['station']}: {what} {wx['mu']:.1f} °C ± {wx['sigma']:.1f} на {wx['date']}".replace(
                    ".", ","
                ),
                f"https://aviationweather.gov/data/metar/?ids={wx['station']}" if wx["station"] != "HKO" else W.HKO,
                value=wx["mu"],
                unit="°C",
                note=note,
                meta={"wx": wx},
            )
        ]


PLUGINS = [CryptoMarket(), Weather(), WeatherStation(), Fred(), Metaculus(), PolymarketFlow()]
