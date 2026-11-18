"""Погодная модель модуля «События» (точность модели, F2): станция из правил рынка, 7 моделей прогноза,
поправка на станцию по истории, в день события — фактические наблюдения.

Как решается рынок: «максимум (минимум) температуры за дату по почасовым сводкам METAR станции» (аэропорт из правил,
код ICAO в скобках), в Гонконге — «абсолютный максимум за сутки» обсерватории. Значения — в целых или десятых °C,
корзины рынка — целые градусы (°C или °F).

Модель:
  1. прогнозы 7 моделей (ECMWF, GFS, ICON, GEM, JMA, Météo-France, UKMO) на дату из архивного API Open-Meteo —
     он же даёт, что каждая модель предсказывала за 1 и 2 дня в прошлые 90 дней;
  2. поправка на станцию: за прошлые дни (только до момента анализа) сравниваем прогноз каждой модели со сводками
     станции → смещение и ошибка каждой модели; итог — среднее поправленных прогнозов с весами 1/ошибка²,
     разброс — по ошибкам этого среднего в прошлом (не меньше 0,6 °C);
  3. в день события: максимум уже не меньше наблюдённого; оставшиеся часы — по прогнозу с меньшим разбросом:
     P(итог ≤ x) = [наблюдённое ≤ x] · Φ((x − μ_ост)/σ_ост).
Вероятность корзины [a, b) = F(b) − F(a) с поправкой на округление до целого.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import NormalDist
from typing import Any
from zoneinfo import ZoneInfo

N = NormalDist()
MODELS = (
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
    "jma_seamless",
    "meteofrance_seamless",
    "ukmo_seamless",
)
MODEL_TITLES = {
    "ecmwf_ifs025": "ECMWF",
    "gfs_seamless": "GFS",
    "icon_seamless": "ICON",
    "gem_seamless": "GEM",
    "jma_seamless": "JMA",
    "meteofrance_seamless": "Météo-France",
    "ukmo_seamless": "UKMO",
}
PREV_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
AWC = "https://aviationweather.gov/api/data"
HKO = "https://data.weather.gov.hk/weatherAPI/opendata"
HISTORY_DAYS = 45  # окно для поправки на станцию
MIN_SIGMA = 0.6  # °C — меньше не бывает даже у лучших прогнозов на сутки
UNCALIBRATED_SIGMA = 2.0  # °C — разброс без поправки на станцию (осторожно)
SAME_DAY_FACTOR = 0.6  # разброс прогноза оставшихся часов к вечеру — доля от суточного (утром — почти весь)
# Подобрано на ранней половине закрытых погодных рынков, проверено на поздней (scripts/check_weather_model.py):
OBS_SD = 0.3  # °C — погрешность «уже наблюдённого»: архив METAR в целых градусах, рынок — по своему значению
DAY_OF_SIGMA_MULT = 1.25  # в день события прогноз оставшихся часов ошибается больше, чем ошибка «за сутки» по истории
HKO_STATION = {"id": "HKO", "name": "Hong Kong Observatory", "lat": 22.3019, "lon": 114.1742, "kind": "hko"}


# ---------------------------------------------------------------- правила рынка
def parse_station(text: str) -> dict | None:
    """Станция из правил: «… reported at London City Airport (EGLC) …» → {id: EGLC, kind: metar}; обсерватория
    Гонконга → особый случай (не METAR)."""
    if "Hong Kong Observatory" in text:
        return dict(HKO_STATION)
    m = re.search(r"\(([A-Z]{4})\)", text)
    if m:
        name = re.search(r"\b(?:at|for)\s+([^()]{3,80}?)\s*\(" + m.group(1), text)
        return {"id": m.group(1), "name": (name.group(1).strip() if name else m.group(1)), "kind": "metar"}
    return None


def parse_target(title: str, end_ts: int | None) -> dict | None:
    """«Highest temperature in London on November 18?» → {var: max, date: 2026-11-18}."""
    low = title.lower()
    if "temperature" not in low:
        return None
    var = "min" if any(k in low for k in ("lowest", "minimum", "min temp")) else "max"
    months = {
        m: i
        for i, m in enumerate(
            (
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
            ),
            1,
        )
    }
    d = re.search(r"\b(" + "|".join(months) + r")\s+(\d{1,2})\b", low)
    if not d:
        return None
    ref = datetime.fromtimestamp(end_ts or time.time(), UTC)
    year = ref.year
    if months[d.group(1)] == 12 and ref.month == 1:
        year -= 1
    return {"var": var, "date": date(year, months[d.group(1)], int(d.group(2))).isoformat()}


# ---------------------------------------------------------------- суточные значения
def daily_extreme(times: list[str], values: list[float | None], day: str, var: str = "max") -> float | None:
    """Максимум/минимум за местные сутки по почасовому ряду (время — местное, как отдаёт Open-Meteo с timezone=auto)."""
    vals = [v for t, v in zip(times, values, strict=False) if v is not None and t.startswith(day)]
    if len(vals) < 18:
        return None  # суток не хватает — не считаем
    return max(vals) if var == "max" else min(vals)


def obs_daily(obs: list[tuple[datetime, float]], tz: ZoneInfo, var: str = "max") -> dict[str, float]:
    """Суточные максимум/минимум по сводкам станции (время сводки — UTC) в местных сутках."""
    by: dict[str, list[float]] = {}
    for t, v in obs:
        by.setdefault(t.astimezone(tz).date().isoformat(), []).append(v)
    return {d: (max(v) if var == "max" else min(v)) for d, v in by.items() if len(v) >= 18}


# ---------------------------------------------------------------- поправка на станцию
@dataclass
class Calibration:
    bias: dict[str, float] = field(default_factory=dict)  # модель → среднее (прогноз − факт)
    weight: dict[str, float] = field(default_factory=dict)  # модель → 1/ошибка²
    sigma: float = 1.5  # ошибка поправленного среднего, °C
    n: int = 0  # дней в окне

    def to_json(self) -> dict:
        return {"bias": self.bias, "weight": self.weight, "sigma": self.sigma, "n": self.n}

    @classmethod
    def from_json(cls, d: dict) -> Calibration:
        return cls(
            dict(d.get("bias") or {}), dict(d.get("weight") or {}), float(d.get("sigma", 1.5)), int(d.get("n", 0))
        )


def fit(forecasts: dict[str, dict[str, float]], observed: dict[str, float], days: list[str]) -> Calibration:
    """forecasts: модель → {дата: прогноз на эту дату, сделанный заранее}; observed: {дата: факт}; days — окно
    (только прошлое относительно момента анализа)."""
    cal = Calibration()
    errs: dict[str, list[float]] = {}
    for m, fc in forecasts.items():
        e = [fc[d] - observed[d] for d in days if d in fc and d in observed]
        if len(e) >= 7:
            errs[m] = e
    if not errs:
        return cal
    for m, e in errs.items():
        b = sum(e) / len(e)
        var = sum((x - b) ** 2 for x in e) / max(len(e) - 1, 1)
        cal.bias[m] = round(b, 3)
        cal.weight[m] = round(1 / max(var, 0.25), 4)
    # ошибка поправленного взвешенного среднего — по тем же дням (оценка разброса прогноза)
    resid = []
    for d in days:
        if d not in observed:
            continue
        mu = combine({m: forecasts[m][d] for m in errs if d in forecasts[m]}, cal)
        if mu is not None:
            resid.append(mu - observed[d])
    cal.n = len(resid)
    if len(resid) >= 7:
        # среднее поправленных прогнозов уже без смещения: разброс — корень из среднего квадрата
        cal.sigma = round(max(MIN_SIGMA, math.sqrt(sum(r * r for r in resid) / len(resid))), 3)
    return cal


def combine(fc: dict[str, float], cal: Calibration) -> float | None:
    """Поправленное среднее прогнозов моделей с весами 1/ошибка² (модели без истории — средний вес)."""
    if not fc:
        return None
    wdef = (sum(cal.weight.values()) / len(cal.weight)) if cal.weight else 1.0
    tot = sw = 0.0
    for m, v in fc.items():
        w = cal.weight.get(m, wdef)
        tot += w * (v - cal.bias.get(m, 0.0))
        sw += w
    return tot / sw if sw else None


# ---------------------------------------------------------------- распределение и корзины
def cdf_factory(
    mu: float,
    sigma: float,
    observed_so_far: float | None = None,
    var: str = "max",
    mu_rest: float | None = None,
    sigma_rest: float | None = None,
    obs_sd: float = OBS_SD,
):
    """F(x) = P(итог ≤ x). В день события: максимум не меньше наблюдённого, минимум — не больше.
    Наблюдённое — с погрешностью obs_sd (значение, по которому решается рынок, может отличаться на градус)."""
    if observed_so_far is None:
        return lambda x: N.cdf((x - mu) / sigma)
    m_r = mu_rest if mu_rest is not None else mu
    s_r = sigma_rest if sigma_rest is not None else sigma * SAME_DAY_FACTOR

    def seen_le(x: float) -> float:  # P(наблюдённое на самом деле ≤ x)
        if obs_sd <= 0:
            return 1.0 if observed_so_far <= x else 0.0
        return N.cdf((x - observed_so_far) / obs_sd)

    if var == "max":
        return lambda x: seen_le(x) * N.cdf((x - m_r) / s_r)
    # минимум: итог ≤ x, если уже было ≤ x или оставшиеся часы опустятся ≤ x
    return lambda x: 1.0 - (1.0 - seen_le(x)) * (1.0 - N.cdf((x - m_r) / s_r))


def rest_sigma(sigma: float, now: float, tz: Any, factor: float | None = None) -> float:
    """Разброс прогноза оставшихся часов: утром впереди почти весь день (как суточный), к вечеру — меньше."""
    f = SAME_DAY_FACTOR if factor is None else factor
    loc = datetime.fromtimestamp(now, tz)
    left = max(0.0, 24 - (loc.hour + loc.minute / 60)) / 24
    return sigma * (f + (1 - f) * left)


def bucket_probs(buckets: dict[str, tuple[float, float]], cdf) -> dict[str, float]:
    """Корзины [a, b) уже с учётом округления (parse_bucket); сумма по полному набору корзин = 1."""
    out = {}
    for k, (a, b) in buckets.items():
        hi = 1.0 if b == math.inf else cdf(b)
        lo = 0.0 if a == -math.inf else cdf(a)
        out[k] = min(max(hi - lo, 0.0), 1.0)
    return out


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


# ---------------------------------------------------------------- данные (сеть)
def fetch_forecasts(get: Any, lat: float, lon: float, past_days: int = 60, forecast_days: int = 4) -> dict:
    """Почасовые прогнозы 7 моделей: текущий, за 1 день и за 2 дня до часа. Один запрос."""
    return get(
        PREV_RUNS,
        {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,temperature_2m_previous_day1,temperature_2m_previous_day2",
            "models": ",".join(MODELS),
            "past_days": past_days,
            "forecast_days": forecast_days,
            "timezone": "auto",
        },
    )


def model_daily(resp: dict, day: str, var: str, lead: int) -> dict[str, float]:
    """Прогноз каждой модели на дату: lead = 0 — текущий прогноз, 1/2 — сделанный за 1/2 дня."""
    h = resp.get("hourly") or {}
    times = h.get("time") or []
    suffix = "" if lead <= 0 else f"_previous_day{min(lead, 2)}"
    out = {}
    for m in MODELS:
        v = daily_extreme(times, h.get(f"temperature_2m{suffix}_{m}") or [], day, var)
        if v is not None:
            out[m] = v
    return out


def model_history(resp: dict, days: list[str], var: str, lead: int) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {m: {} for m in MODELS}
    for d in days:
        for m, v in model_daily(resp, d, var, max(lead, 1)).items():
            out[m][d] = v
    return {m: v for m, v in out.items() if v}


def station_info(get: Any, icao: str) -> dict | None:
    rows = get(f"{AWC}/stationinfo", {"ids": icao, "format": "json"})
    if not rows:
        return None
    r = rows[0]
    return {"id": icao, "name": r.get("site") or icao, "lat": float(r["lat"]), "lon": float(r["lon"]), "kind": "metar"}


def parse_iem(text: str) -> list[tuple[datetime, float]]:
    """CSV IEM (station,valid,tmpf) → [(время UTC, °C)]. tmpf — с десятыми, если они есть в сводке."""
    out = []
    for line in text.splitlines()[1:]:
        p = line.split(",")
        if len(p) < 3 or p[2] in ("M", ""):
            continue
        try:
            t = datetime.strptime(p[1], "%Y-%m-%d %H:%M").replace(tzinfo=UTC)
            out.append((t, round((float(p[2]) - 32) * 5 / 9, 2)))
        except ValueError:
            continue
    return out


def fetch_obs(get_text: Any, icao: str, start: date, end: date) -> list[tuple[datetime, float]]:
    """Плановые сводки METAR станции (без SPECI — как в правилах рынка) из архива IEM; end — включительно
    (у IEM конец диапазона не включается — прибавляем день)."""
    end = end + timedelta(days=1)
    txt = get_text(
        IEM,
        {
            "station": icao,
            "data": "tmpf",
            "year1": start.year,
            "month1": start.month,
            "day1": start.day,
            "year2": end.year,
            "month2": end.month,
            "day2": end.day,
            "tz": "Etc/UTC",
            "format": "onlycomma",
            "latlon": "no",
            "missing": "M",
            "trace": "T",
            "direct": "no",
            "report_type": "3",
        },
    )
    return parse_iem(txt)


def fetch_metar_today(get: Any, icao: str, hours: int = 30) -> list[tuple[datetime, float]]:
    """Свежие сводки (последние сутки с запасом) — для дня события. SPECI не берём."""
    rows = get(f"{AWC}/metar", {"ids": icao, "hours": hours, "format": "json"}) or []
    out = []
    for r in rows:
        if str(r.get("metarType") or "METAR") == "SPECI" or r.get("temp") is None:
            continue
        out.append((datetime.fromtimestamp(int(r["obsTime"]), UTC), float(r["temp"])))
    return out


def fetch_hko_daily(get: Any, var: str, months: list[tuple[int, int]]) -> dict[str, float]:
    """Суточные максимум/минимум обсерватории Гонконга (публикуются помесячно, с задержкой)."""
    dtype = "CLMMAXT" if var == "max" else "CLMMINT"
    out = {}
    for y, m in months:
        d = get(f"{HKO}/opendata.php", {"dataType": dtype, "station": "HKO", "rformat": "json", "year": y, "month": m})
        for row in (d or {}).get("data") or []:
            try:
                if row[4] == "C":
                    out[date(int(row[0]), int(row[1]), int(row[2])).isoformat()] = float(row[3])
            except (ValueError, IndexError):
                continue
    return out


def window(target: str, now_local: date, n: int = HISTORY_DAYS) -> list[str]:
    """Дни окна поправки: только завершённые до «сейчас» (вчера и раньше) и не позже, чем за 2 дня до цели."""
    last = min(now_local - timedelta(days=1), date.fromisoformat(target) - timedelta(days=1))
    return [(last - timedelta(days=i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------- прогноз для анализа (досье)
_FC_CACHE: dict[tuple, tuple[float, dict]] = {}
FC_TTL_S = 3600.0


def _cached_forecasts(get: Any, lat: float, lon: float) -> dict:
    key = (round(lat, 3), round(lon, 3))
    hit = _FC_CACHE.get(key)
    if hit and time.time() - hit[0] < FC_TTL_S:
        return hit[1]
    resp = fetch_forecasts(get, lat, lon, past_days=HISTORY_DAYS + 5, forecast_days=4)
    _FC_CACHE[key] = (time.time(), resp)
    return resp


def station_forecast(
    get: Any, get_text: Any, state: Any, station: dict, target: dict, now: float | None = None
) -> dict | None:
    """Всё для оценки корзин на момент «сейчас»: прогноз станции (μ, σ), а в день события — наблюдённое и
    прогноз оставшихся часов. state — get_state/set_state базы (кэш координат и поправки на сутки)."""
    now = now or time.time()
    var, day = target["var"], target["date"]
    st = dict(station)
    if st["kind"] == "metar" and "lat" not in st:
        key = f"wx_station:{st['id']}"
        info = state.get(key) or station_info(get, st["id"])
        if not info:
            return None
        state.set(key, info)
        st.update(info)
    resp = _cached_forecasts(get, st["lat"], st["lon"])
    tz = ZoneInfo(resp.get("timezone") or "UTC")
    today = datetime.fromtimestamp(now, tz).date()
    lead = (date.fromisoformat(day) - today).days
    if lead < 0:
        return None  # дата уже прошла — ждём исхода
    # поправка на станцию: по завершённым дням окна, раз в сутки (кэш в базе)
    base = f"wx_cal:{st['id']}:{var}:{min(max(lead, 1), 2)}"
    ck = f"{base}:{today.isoformat()}"
    cached = state.get(ck)
    cal_note = ""
    if cached:
        cal = Calibration.from_json(cached)
    else:
        win = window(day, today)
        try:
            if st["kind"] == "metar":
                obs = fetch_obs(get_text, st["id"], date.fromisoformat(win[-1]), today)
                observed = obs_daily(obs, tz, var)
            else:
                months = sorted({(d[:4], d[5:7]) for d in win})
                observed = fetch_hko_daily(get, var, [(int(y), int(m)) for y, m in months])
            cal = fit(model_history(resp, win, var, max(lead, 1)), observed, win)
            state.set(ck, cal.to_json())
            state.set(f"{base}:last", cal.to_json())
        except Exception as exc:
            # архив сводок недоступен (лимит частоты, сеть): последняя удачная поправка этой станции,
            # иначе — без поправки и с широким разбросом (модель осторожнее, но не выключается)
            last = state.get(f"{base}:last")
            cal = Calibration.from_json(last) if last else Calibration(sigma=UNCALIBRATED_SIGMA)
            cal_note = ("поправка за прошлые дни" if last else "без поправки на станцию") + f" ({type(exc).__name__})"
    fc = model_daily(resp, day, var, 0)  # текущий прогноз каждой модели на дату
    mu = combine(fc, cal)
    if mu is None:
        return None
    sigma = cal.sigma * (1 + 0.15 * max(lead - 1, 0))  # дальше дата — шире разброс
    out = {
        "station": st["id"],
        "name": st.get("name"),
        "var": var,
        "date": day,
        "lead": lead,
        "mu": round(mu, 2),
        "sigma": round(sigma, 2),
        "models": {MODEL_TITLES[m]: round(v, 1) for m, v in fc.items()},
        "bias": {MODEL_TITLES[m]: b for m, b in cal.bias.items()},
        "n": cal.n,
        "tz": str(tz),
        "seen": None,
        "cal_note": cal_note,
    }
    if lead == 0 and st["kind"] == "metar":
        obs = [(t, v) for t, v in fetch_metar_today(get, st["id"]) if t.astimezone(tz).date() == today]
        if obs:
            seen = max(v for _, v in obs) if var == "max" else min(v for _, v in obs)
            h = resp.get("hourly") or {}
            rest = {}
            for m in MODELS:
                vals = [
                    v
                    for tt, v in zip(h.get("time") or [], h.get(f"temperature_2m_{m}") or [], strict=False)
                    if v is not None
                    and tt.startswith(day)
                    and datetime.fromisoformat(tt).replace(tzinfo=tz).timestamp() > now
                ]
                if vals:
                    rest[m] = max(vals) if var == "max" else min(vals)
            mu_rest = combine(rest, cal) if rest else None
            out.update(
                seen=seen,
                obs_n=len(obs),
                mu_rest=round(mu_rest, 2) if mu_rest is not None else (-99.0 if var == "max" else 99.0),
                sigma_rest=round(rest_sigma(cal.sigma * DAY_OF_SIGMA_MULT, now, tz), 2),
            )
    return out


def outcome_probs(wx: dict, buckets: dict[str, dict]) -> dict[str, float]:
    """buckets: ключ исхода → parse_bucket (границы в своих единицах). Вероятности по прогнозу станции."""
    cdf = cdf_factory(wx["mu"], wx["sigma"], wx.get("seen"), wx["var"], wx.get("mu_rest"), wx.get("sigma_rest"))
    conv = {k: ((lambda x: (x - 32) * 5 / 9) if b["unit"] == "F" else (lambda x: x)) for k, b in buckets.items()}
    rng = {
        k: (conv[k](b["lo"]) if b["lo"] != -math.inf else b["lo"], conv[k](b["hi"]) if b["hi"] != math.inf else b["hi"])
        for k, b in buckets.items()
    }
    return bucket_probs(rng, cdf)
