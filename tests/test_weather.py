"""Точность модели, F2: погода по станции рынка — правила, суточные значения, поправка на станцию, день события,
корзины, источник и оценщик (без сети)."""

from __future__ import annotations

import math
import random
from datetime import UTC, date, datetime, timedelta

import pytest

from whalefollow import plugins
from whalefollow.db import DB
from whalefollow.eventhub import weather as W
from whalefollow.plugins import EventInfo, OutcomeInfo
from whalefollow.plugins.builtin import estimators as es
from whalefollow.plugins.builtin import event_sources as src

LONDON_RULES = (
    "This market will resolve to the temperature range that contains the highest temperature reported at London City "
    "Airport (EGLC) on 18 Nov '26. The qualifying value is the highest temperature reported in the hourly temperature "
    "remark of any routine (METAR) observation for London City Airport (EGLC) on that date."
)
HK_RULES = "the highest temperature recorded by the Hong Kong Observatory in degrees Celsius on 19 Nov '26."


def test_parse_station_and_target():
    assert W.parse_station(LONDON_RULES) == {"id": "EGLC", "name": "London City Airport", "kind": "metar"}
    assert W.parse_station(HK_RULES)["kind"] == "hko"
    assert W.parse_station("Where will it rain?") is None
    end = int(datetime(2026, 11, 18, 12, tzinfo=UTC).timestamp())
    assert W.parse_target("Highest temperature in London on November 18?", end) == {"var": "max", "date": "2026-11-18"}
    assert W.parse_target("Lowest temperature in Karachi on November 18?", end)["var"] == "min"
    jan = int(datetime(2027, 1, 1, 12, tzinfo=UTC).timestamp())
    assert W.parse_target("Highest temperature in NYC on December 31?", jan)["date"] == "2026-12-31"
    assert W.parse_target("Where will it rain on November 20?", end) is None


def test_daily_extreme_and_obs_daily():
    times = [f"2026-11-18T{h:02d}:00" for h in range(24)] + ["2026-11-19T00:00"]
    vals = [10 + math.sin(h / 24 * math.pi) * 5 for h in range(24)] + [30.0]
    assert W.daily_extreme(times, vals, "2026-11-18", "max") == pytest.approx(max(vals[:24]))
    assert W.daily_extreme(times[:10], vals[:10], "2026-11-18") is None  # суток не хватает
    tz = W.ZoneInfo("Asia/Shanghai")  # +8: местные сутки 18-го — с 16:00 UTC 17-го
    obs = [(datetime(2026, 11, 17, 16, tzinfo=UTC) + timedelta(hours=h), float(h)) for h in range(24)]
    obs.append((datetime(2026, 11, 17, 15, tzinfo=UTC), 99.0))  # это ещё 17-е по местному
    assert W.obs_daily(obs, tz, "max") == {"2026-11-18": 23.0}


def _history(days, true_bias, noise, seed=1):
    rnd = random.Random(seed)
    observed, fc = {}, {m: {} for m in W.MODELS[:3]}
    for d in days:
        t = rnd.uniform(5, 20)
        observed[d] = t
        for i, m in enumerate(W.MODELS[:3]):
            fc[m][d] = t + true_bias[i] + rnd.gauss(0, noise[i])
    return fc, observed


def test_fit_learns_bias_and_weights():
    days = [(date(2026, 11, 1) - timedelta(days=i)).isoformat() for i in range(45)]
    fc, obs = _history(days, true_bias=(1.5, -0.5, 0.0), noise=(0.5, 0.5, 2.0))
    cal = W.fit(fc, obs, days)
    assert cal.bias["ecmwf_ifs025"] == pytest.approx(1.5, abs=0.3)
    assert cal.bias["gfs_seamless"] == pytest.approx(-0.5, abs=0.3)
    assert cal.weight["ecmwf_ifs025"] > 5 * cal.weight["icon_seamless"]  # шумная модель — меньший вес
    assert W.MIN_SIGMA <= cal.sigma < 1.0 and cal.n == 45
    mu = W.combine({"ecmwf_ifs025": 21.5, "gfs_seamless": 19.5, "icon_seamless": 25.0}, cal)
    assert mu == pytest.approx(20.0, abs=0.5)  # смещения сняты, шумная модель почти не влияет
    assert W.Calibration.from_json(cal.to_json()).bias == cal.bias
    assert W.fit({}, obs, days).n == 0


def test_window_no_lookahead():
    w = W.window("2026-11-18", date(2026, 11, 17))
    assert w[0] == "2026-11-16" and len(w) == W.HISTORY_DAYS  # вчера и раньше: 17-е ещё не закончилось
    assert W.window("2026-11-18", date(2026, 11, 18))[0] == "2026-11-17"


def test_buckets_sum_and_day_of_constraint():
    titles = ["11°C or below", "12°C", "13°C", "14°C", "15°C or higher"]
    buckets = {t: es.parse_bucket(t) for t in titles}
    probs = W.outcome_probs({"mu": 13.2, "sigma": 0.9, "var": "max"}, buckets)
    assert sum(probs.values()) == pytest.approx(1.0)
    assert max(probs, key=probs.get) == "13°C"
    # день события: уже наблюдалось 14 °C — максимум не меньше, корзины ниже невозможны
    day = W.outcome_probs(
        {"mu": 13.2, "sigma": 0.9, "var": "max", "seen": 14.0, "mu_rest": 12.0, "sigma_rest": 0.5}, buckets
    )
    # с погрешностью наблюдения (±0,5 °C) соседняя корзина ещё возможна, дальние — нет
    assert day["11°C or below"] < 1e-6 and day["12°C"] < 0.01 and day["13°C"] < 0.2
    assert day["14°C"] > 0.6 and sum(day.values()) == pytest.approx(1.0)
    exact = W.bucket_probs(
        {t: (b["lo"], b["hi"]) for t, b in buckets.items()}, W.cdf_factory(13.2, 0.9, 14.0, "max", 12.0, 0.5, obs_sd=0)
    )
    assert exact["13°C"] == 0.0 and exact["14°C"] > 0.99  # без погрешности — ровно «не меньше наблюдённого»
    # минимум: уже было 3 °C — итог не выше
    mins = {t: es.parse_bucket(t) for t in ["2°C or below", "3°C", "4°C or higher"]}
    p = W.outcome_probs({"mu": 4.0, "sigma": 1.0, "var": "min", "seen": 3.0, "mu_rest": 3.5, "sigma_rest": 0.5}, mins)
    assert p["4°C or higher"] < 0.1 and sum(p.values()) == pytest.approx(1.0) and p["3°C"] > p["2°C or below"]


def test_rest_sigma_shrinks_through_the_day():
    tz = W.ZoneInfo("Europe/London")
    morning = datetime(2026, 11, 18, 6, tzinfo=UTC).timestamp()
    evening = datetime(2026, 11, 18, 21, tzinfo=UTC).timestamp()
    assert W.rest_sigma(1.0, morning, tz) > 0.85 > W.rest_sigma(1.0, evening, tz) >= W.SAME_DAY_FACTOR


def test_fahrenheit_buckets():
    b = {"lo": es.parse_bucket("76-77°F"), "hi": es.parse_bucket("78-79°F")}
    p = W.outcome_probs({"mu": (76.5 - 32) * 5 / 9, "sigma": 0.5, "var": "max"}, b)
    assert p["lo"] > 0.7 and p["hi"] < 0.3


def test_parse_iem_csv():
    txt = "station,valid,tmpf\nEGLC,2026-11-10 00:50,44.60\nEGLC,2026-11-10 01:50,M\nbad\n"
    assert W.parse_iem(txt) == [(datetime(2026, 11, 10, 0, 50, tzinfo=UTC), 7.0)]


class FakeState(dict):
    def set(self, k, v):
        self[k] = v


def _resp(day: str, base: float, days_back: int = 50):
    """Ответ архивного API: 7 моделей, текущий прогноз и за 1–2 дня; у всех моделей смещение +1 °C."""
    start = date.fromisoformat(day) - timedelta(days=days_back)
    times, cols = [], {}
    for i in range(days_back + 3):
        d = start + timedelta(days=i)
        for h in range(24):
            times.append(f"{d.isoformat()}T{h:02d}:00")
    for m in W.MODELS:
        for suf in ("", "_previous_day1", "_previous_day2"):
            cols[f"temperature_2m{suf}_{m}"] = [
                base + 1.0 + 4 * math.sin((int(t[11:13]) - 6) / 24 * 2 * math.pi) for t in times
            ]
    return {"timezone": "Europe/London", "hourly": {"time": times, **cols}}


def test_station_forecast_and_fallback(monkeypatch):
    day = "2026-11-20"
    now = datetime(2026, 11, 19, 12, tzinfo=UTC).timestamp()
    resp = _resp(day, 10.0)
    monkeypatch.setattr(W, "_FC_CACHE", {})
    calls = {"obs": 0}

    def get(url, params=None):
        if "stationinfo" in url:
            return [{"site": "London City Arpt", "lat": 51.5, "lon": 0.05}]
        if "previous-runs" in url:
            return resp
        raise AssertionError(url)

    def get_text(url, params=None):
        calls["obs"] += 1
        start = date(params["year1"], params["month1"], params["day1"])
        lines = ["station,valid,tmpf"]
        for i in range(60):
            d = start + timedelta(days=i)
            for h in range(24):  # факт: модели без смещения → у прогнозов смещение +1 °C
                c = 10.0 + 4 * math.sin((h - 6) / 24 * 2 * math.pi)
                lines.append(f"EGLC,{d.isoformat()} {h:02d}:00,{c * 9 / 5 + 32:.2f}")
        return "\n".join(lines)

    state = FakeState()
    wx = W.station_forecast(get, get_text, state, {"id": "EGLC", "kind": "metar"}, {"var": "max", "date": day}, now)
    assert wx["lead"] == 1 and wx["n"] >= 30
    assert wx["mu"] == pytest.approx(14.0, abs=0.2)  # прогноз 15 °C минус смещение +1
    assert wx["bias"]["ECMWF"] == pytest.approx(1.0, abs=0.05) and wx["sigma"] == W.MIN_SIGMA
    assert state["wx_station:EGLC"]["lat"] == 51.5
    W.station_forecast(get, get_text, state, {"id": "EGLC", "kind": "metar"}, {"var": "max", "date": day}, now)
    assert calls["obs"] == 1  # поправка — раз в сутки

    def broken(url, params=None):
        raise RuntimeError("HTTP 429")

    state2 = FakeState({"wx_station:EGLC": state["wx_station:EGLC"]})
    wx2 = W.station_forecast(get, broken, state2, {"id": "EGLC", "kind": "metar"}, {"var": "max", "date": day}, now)
    assert wx2["sigma"] == W.UNCALIBRATED_SIGMA and "без поправки" in wx2["cal_note"]
    assert (
        W.station_forecast(
            get, get_text, state, {"id": "EGLC", "kind": "metar"}, {"var": "max", "date": "2026-11-18"}, now
        )
        is None
    )  # дата прошла


def test_source_and_estimator(monkeypatch):
    db = DB("sqlite://:memory:")
    db.init_schema()
    wx = {
        "station": "EGLC",
        "name": "London City",
        "var": "max",
        "date": "2026-11-20",
        "lead": 1,
        "mu": 13.2,
        "sigma": 0.9,
        "models": {"ECMWF": 13.0},
        "bias": {},
        "n": 40,
        "seen": None,
        "cal_note": "",
    }
    monkeypatch.setattr(W, "station_forecast", lambda *a, **k: wx)
    outs = [OutcomeInfo(t, t, t, None, market_id=t) for t in ["12°C", "13°C", "14°C"]]
    ev = EventInfo(
        id="w",
        title="Highest temperature in London on November 20?",
        topic="weather",
        description=LONDON_RULES,
        end_ts=int(datetime(2026, 11, 20, 12, tzinfo=UTC).timestamp()),
        outcomes=outs,
    )
    items = src.WeatherStation().dossier(plugins.context(db), ev)
    assert len(items) == 1 and items[0].meta["wx"] is wx and "13,2" in items[0].title
    dossier = [
        {"kind": "data", "meta": {"wx": wx}, "source": "s"},
        {"kind": "data", "meta": {"members": [13.0, 14.0]}, "source": "Open-Meteo GFS"},
    ]
    est = es.WeatherStationModel()
    assert est.applies(ev, dossier) and not es.WeatherEnsemble().applies(ev, dossier)  # погоду дважды не учитываем
    got = {c.outcome_key: c.p for c in est.estimate(plugins.context(db), ev, dossier, {})}
    assert max(got, key=got.get) == "13°C" and got["13°C"] > 0.35
    assert {c.quality for c in est.estimate(plugins.context(db), ev, dossier, {})} == {est.quality}
    weak = [{"kind": "data", "meta": {"wx": {**wx, "n": 0, "sigma": 2.0, "cal_note": "без поправки на станцию"}}}]
    assert {c.quality for c in est.estimate(plugins.context(db), ev, weak, {})} == {0.25}  # не проверено — мало доверия
    no_station = EventInfo(id="r", title="Where will it rain on November 20?", topic="weather")
    assert src.WeatherStation().dossier(plugins.context(db), no_station) == []


def test_validated_weather_lambda_is_topic_prior(tmp_path, monkeypatch):
    from whalefollow.eventhub import selfcheck as S

    assert S.prior_for("weather") == pytest.approx(0.45)  # проверено на истории закрытых погодных рынков
    assert S.prior_for("politics") == S.LAMBDA_PRIOR
    assert S.lambda_for({"lambda": 0.3, "by_topic": {}}, "weather") == pytest.approx(0.45)
    # выученное по закрытым событиям темы важнее проверенного на истории
    assert S.lambda_for({"lambda": 0.3, "by_topic": {"weather": {"lambda": 0.2}}}, "weather") == 0.2
    monkeypatch.setattr(S, "VALIDATED", tmp_path / "nope.json")
    assert S.prior_for("weather") == S.LAMBDA_PRIOR  # файла нет — как раньше
