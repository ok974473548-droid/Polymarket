"""Модуль «События», этап C: модель оценки — сведение оценщиков, сжатие к цене, нормировка, оценщики, ИИ вслепую."""

from __future__ import annotations

import json
import math
import time
from types import SimpleNamespace

import pytest
from events_fake import EventsClob, EventsGamma, make_events

from whalefollow import plugins
from whalefollow.db import DB
from whalefollow.estimator.sources import Forecast
from whalefollow.eventhub import ai, calibration, model
from whalefollow.eventhub import dossier as dz
from whalefollow.eventhub.catalog import detail
from whalefollow.plugins import EventInfo, OutcomeInfo, SourcePlugin
from whalefollow.plugins.builtin import estimators as es
from whalefollow.services import Services

from conftest import fast_params
from synthetic import SyntheticWorld

OFF = {"google_news": False, "manifold": False, "kalshi": False, "bookmakers": False, "reddit": False}


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("WF_PLUGINS_DIR", str(tmp_path / "none"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    plugins.reset_registry()
    yield
    plugins.reset_registry()


@pytest.fixture
def world():
    w = SyntheticWorld(seed=3, n_events=40, n_random=6, n_skilled=2)
    return w, make_events(w)


@pytest.fixture
def db():
    d = DB("sqlite://:memory:")
    d.init_schema()
    d.set_setting("news_sources", OFF)
    return d


class Kalshi(SourcePlugin):
    """Прогноз «другой площадки» к каждому исходу — одно и то же число p."""

    name = "fake_platform"
    title = "Тестовая площадка"
    p = 0.37

    def forecasts(self, ctx, question, market=None):
        return [Forecast("Kalshi", question, "https://k/1", self.p)]


def _logit(p):
    return math.log(p / (1 - p))


def _sig(x):
    return 1 / (1 + math.exp(-x))


def _with_dossier(db, ev, p=0.37):
    src = Kalshi()
    src.p = p
    plugins.registry().register(src, "тест")
    dz.collect(db, ev)


# ---------------------------------------------------------------- сведение
def test_no_dossier_final_equals_market(world, db):
    _, evs = world
    ev = detail(evs[1])
    res = model.analyze(db, ev, "manual")
    r = res["outcomes"][0]
    assert r["final"] == pytest.approx(r["market"]) and r["model"] is None and r["confidence"] == "только цена"
    assert "Досье ещё не собрано" in res["explanation"][0]


def test_shrinks_toward_market_by_lambda(world, db):
    _, evs = world
    ev = detail(evs[1])  # одно «Да/Нет», цена 0,62
    _with_dossier(db, ev, 0.37)
    res = model.analyze(db, ev, "manual", {ev["outcomes"][0]["key"]: 0.62})
    r = res["outcomes"][0]
    comp = next(c for c in r["components"] if c["name"] == "platforms")
    assert comp["p"] == pytest.approx(0.37, abs=1e-4)
    lam = model.LAMBDA0 * min(1.0, comp["w"])
    want = _sig(_logit(0.62) + lam * (_logit(comp["p"]) - _logit(0.62)))
    assert r["final"] == pytest.approx(want, abs=1e-6)
    assert 0.37 < r["final"] < 0.62  # между ценой и оценкой, ближе к цене
    assert abs(r["final"] - 0.62) <= model.LAMBDA0 * (0.62 - 0.37) + 1e-9
    assert r["lo"] < r["final"] < r["hi"] and r["diff"] == pytest.approx(r["final"] - 0.62)
    assert any("ниже цены" in x for x in res["explanation"])


def test_exclusive_outcomes_keep_market_total(world, db):
    _, evs = world
    ev = detail(evs[0])  # 4 кандидата, negRisk
    _with_dossier(db, ev, 0.30)
    res = model.analyze(db, ev, "manual")
    assert len(res["outcomes"]) == 4
    # сумма итогов = сумме цен (здесь 95%): оценки перераспределяют вероятность, но не создают её
    assert sum(r["final"] for r in res["outcomes"]) == pytest.approx(sum(r["market"] for r in res["outcomes"]))
    assert (
        res["outcomes"][0]["final"] < res["outcomes"][0]["market"]
        and res["outcomes"][3]["final"] > res["outcomes"][3]["market"]
    )
    # без независимых оценок — ровно цены, без нормировки (иначе «выгода» из ничего)
    db.execute("DELETE FROM evidence")
    db.execute("DELETE FROM event_dossiers")
    plain = model.analyze(db, ev, "manual", save=False)
    assert all(r["final"] == pytest.approx(r["market"]) for r in plain["outcomes"])


def test_partial_estimates_move_others_opposite(world, db):
    _, evs = world
    ev = detail(evs[0])
    first = ev["outcomes"][0]["key"]

    class One(plugins.EstimatorPlugin):
        name = "one_est"
        title = "Оценка одного исхода"

        def estimate(self, ctx, ev, dossier, market):
            return [plugins.ComponentEstimate(first, 0.6, 1.0, "")]

    plugins.registry().register(One(), "тест")
    _with_dossier(db, ev, 0.9)  # прогноз площадки далёк от цен — отброшен, остаётся один оценщик
    res = model.analyze(db, ev, "manual", save=False)
    rows = {r["key"]: r for r in res["outcomes"]}
    assert rows[first]["final"] > rows[first]["market"]
    others = [r for k, r in rows.items() if k != first]
    assert all(r["final"] < r["market"] and r["model"] is None for r in others)  # уступили место, пропорционально
    ratios = {round(r["final"] / r["market"], 9) for r in others}
    assert len(ratios) == 1
    assert sum(r["final"] for r in rows.values()) == pytest.approx(sum(r["market"] for r in rows.values()))


def test_same_forecast_for_many_outcomes_is_dropped(world, db):
    _, evs = world
    ev = detail(evs[0])

    class Generic(SourcePlugin):
        name = "generic_fc"
        title = "Один прогноз на всё"

        def forecasts(self, ctx, question, market=None):
            # «Кто победит на выборах» — тот же вопрос площадки найден к каждому кандидату
            return [
                Forecast("Kalshi", "Победит ли Анна Смирнова, Борис Ким, Вера Ли или Глеб Орлов?", "https://k/all", 0.3)
            ]

    plugins.registry().register(Generic(), "тест")
    dz.collect(db, ev)
    assert len([x for x in dz.load(db, ev["id"])["items"] if x["kind"] == "forecast"]) >= 2
    res = model.analyze(db, ev, "manual", save=False)
    assert all(r["components"] == [] for r in res["outcomes"])


def test_platform_far_from_price_is_dropped(world, db):
    _, evs = world
    ev = detail(evs[1])
    _with_dossier(db, ev, 0.05)  # 5% при цене 62% — почти наверняка другой вопрос
    res = model.analyze(db, ev, "manual", {ev["outcomes"][0]["key"]: 0.62})
    r = res["outcomes"][0]
    assert r["components"] == [] and r["final"] == pytest.approx(0.62)


def test_saved_and_throttled_history(world, db):
    _, evs = world
    ev = detail(evs[1])
    _with_dossier(db, ev)
    model.analyze(db, ev, "manual")
    model.analyze(db, ev, "manual")  # те же оценщики, 10 мин не прошло — новую точку не пишем
    assert db.scalar("SELECT COUNT(*) AS n FROM event_analyses", (), 0) == 1
    row = db.one("SELECT * FROM event_analyses")
    assert row["mode"] == "manual" and row["dossier_ts"] and json.loads(row["components"])[0]["name"] == "platforms"
    line = model.history(db, ev["id"], "manual")
    assert list(line) == [ev["outcomes"][0]["key"]] and len(line[ev["outcomes"][0]["key"]]) == 1


def test_estimator_error_is_recorded(world, db):
    _, evs = world
    ev = detail(evs[1])
    _with_dossier(db, ev)

    class Bad(plugins.EstimatorPlugin):
        name = "bad_est"
        title = "Сломанный"

        def estimate(self, ctx, ev, dossier, market):
            raise ValueError("нет данных")

    plugins.registry().register(Bad(), "тест")
    res = model.analyze(db, ev, "manual", save=False)
    assert res["estimators"]["bad_est"]["error"] == "нет данных"
    assert "нет данных" in plugins.status(db, "bad_est")["error"]
    assert res["outcomes"][0]["model"] is not None  # остальные оценщики работают


# ---------------------------------------------------------------- оценщики по темам
def test_parse_threshold():
    assert es.parse_threshold("Will Bitcoin reach $100,000 in November?") == {"k": 100000, "dir": "up", "kind": "touch"}
    assert es.parse_threshold("Will ETH dip to $3k?") == {"k": 3000, "dir": "down", "kind": "touch"}
    assert es.parse_threshold("Bitcoin above $95,000 on Nov 30?") == {"k": 95000, "dir": "up", "kind": "terminal"}
    assert es.parse_threshold("BTC between $90,000 and $92,000?")["dir"] == "range"
    assert es.parse_threshold("Will Bitcoin be good?") is None


def test_threshold_prob_math():
    # без сноса медиана логнормального ниже текущей цены: «выше текущей» чуть меньше 50%
    p = es.threshold_prob(100, 100, 0.6, 0.25, "up", "terminal")
    assert p == pytest.approx(es.N.cdf(-0.5 * 0.6 * 0.5), abs=1e-9) and p < 0.5
    assert es.threshold_prob(100, 100, 0.6, 0.25, "down", "terminal") == pytest.approx(1 - p)
    # касание сверху: 2·P(конец выше порога без поправки), всегда ≥ вероятности быть выше в конце
    t = es.threshold_prob(100, 120, 0.6, 0.25, "up", "touch")
    assert t == pytest.approx(2 * (1 - es.N.cdf(math.log(1.2) / 0.3)))
    assert t > es.threshold_prob(100, 120, 0.6, 0.25, "up", "terminal")
    assert es.threshold_prob(100, 90, 0.6, 0.25, "up", "touch") == 1.0  # уже выше порога
    rng = es.threshold_prob(100, 95, 0.6, 0.25, "range", "terminal", 105)
    assert 0 < rng < 1


def test_crypto_estimator(db):
    ev = EventInfo(
        id="c",
        title="What price will Bitcoin hit in December?",
        topic="crypto",
        end_ts=int(time.time()) + 30 * 86400,
        outcomes=[OutcomeInfo("a", "↑ $120,000", "Will Bitcoin reach $120,000 in December?", None, market_id="m1")],
    )
    dossier = [
        {"kind": "data", "source": "Binance", "value": 100000.0, "meta": {"metric": "spot"}},
        {"kind": "data", "source": "Deribit DVOL", "value": 50.0, "meta": {"metric": "implied_vol"}},
    ]
    est = es.CryptoThreshold()
    assert est.applies(ev, dossier)
    got = est.estimate(plugins.context(db), ev, dossier, {"a": 0.1})
    years = 30 / 365
    want = 2 * (1 - es.N.cdf(math.log(1.2) / (0.5 * math.sqrt(years))))
    assert got[0].p == pytest.approx(want, rel=1e-3) and "Deribit" in got[0].note
    assert est.estimate(plugins.context(db), ev, dossier, {"a": 0.95}) == []  # почти наверняка уже касался
    far = EventInfo(
        id="c2",
        title="BTC",
        topic="crypto",
        end_ts=ev.end_ts,
        outcomes=[OutcomeInfo("b", "↓ $40,000", "Will Bitcoin dip to $40,000 in December?", None, market_id="m2")],
    )
    tail = est.estimate(plugins.context(db), far, dossier, {"b": 0.05})[0]
    assert tail.quality < 0.6 and "хвосты" in tail.note  # далеко от цены — модель занижает хвосты, доверие ниже


def test_weather_buckets(db):
    assert es.parse_bucket("21°C or below") == {"lo": -math.inf, "hi": 21.5, "unit": "C"}
    assert es.parse_bucket("22°C") == {"lo": 21.5, "hi": 22.5, "unit": "C"}
    assert es.parse_bucket("80-81°F") == {"lo": 79.5, "hi": 81.5, "unit": "F"}
    assert es.parse_bucket("25°C or higher")["hi"] == math.inf
    titles = ["21°C or below", "22°C", "23°C", "24°C or higher"]
    ev = EventInfo(
        id="w",
        title="Highest temperature in Hong Kong on November 19?",
        topic="weather",
        outcomes=[OutcomeInfo(str(i), t, t, None, market_id=f"m{i}") for i, t in enumerate(titles)],
        exclusive=True,
    )
    dossier = [
        {
            "kind": "data",
            "source": "Open-Meteo GFS",
            "value": 22.5,
            "meta": {"members": [22.0, 22.4, 23.1], "unit": "°C"},
        }
    ]
    got = es.WeatherEnsemble().estimate(plugins.context(db), ev, dossier, {})
    assert len(got) == 4 and sum(g.p for g in got) == pytest.approx(1.0, abs=0.01)
    assert max(got, key=lambda g: g.p).outcome_key in ("1", "2") and got[0].quality == 0.4


# ---------------------------------------------------------------- ИИ вслепую
class FakeClient:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        assert kw["output_config"]["format"]["type"] == "json_schema"
        a = self.answers.pop(0)
        if a == "refusal":
            return SimpleNamespace(stop_reason="refusal", content=[])
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(a))])


def _answer(p1, p2, conf="medium"):
    return {
        "outcomes": [{"id": "o1", "p": p1}, {"id": "o2", "p": p2}],
        "base_rate": "обычно 30%",
        "key_factors": ["фактор"],
        "what_would_change": ["опрос"],
        "confidence": conf,
    }


def test_ai_blind_median_and_no_prices(db, monkeypatch):
    monkeypatch.setattr(ai, "SAMPLES", 3)
    ev = EventInfo(
        id="e1",
        title="Кто победит?",
        topic="politics",
        exclusive=True,
        outcomes=[
            OutcomeInfo("k1", "А", "Победит ли А?", None, market_id="m1"),
            OutcomeInfo("k2", "Б", "Победит ли Б?", None, market_id="m2"),
        ],
    )
    items = [
        {"kind": "data", "title": "Опрос", "value": 48, "unit": "%", "source": "ВЦИОМ", "note": ""},
        {"kind": "forecast", "title": "Kalshi: А", "value": 0.77, "source": "Kalshi"},  # цены площадок ИИ не видит
        {"kind": "news", "title": "Новость", "source": "Reuters", "ts_pub": int(time.time())},
    ]
    client = FakeClient([_answer(0.6, 0.5), _answer(0.7, 0.2), "refusal"])
    res = ai.estimate(db, ev, items, 123, client=client)
    assert res["samples"] == 2
    # середина: А — 0,65, Б — 0,35; затем сумма = 1
    assert res["outcomes"]["k1"] == pytest.approx(0.65 / 1.0) and res["outcomes"]["k2"] == pytest.approx(0.35)
    assert "0.77" not in client.prompts[0] and "Kalshi" not in client.prompts[0]
    assert ai.cached(db, "e1", 123) and ai.cached(db, "e1", 999) is None  # новое досье — нужен новый ответ


def test_ai_component_only_in_ai_mode(world, db, monkeypatch):
    _, evs = world
    ev = detail(evs[1])
    _with_dossier(db, ev)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    d = dz.load(db, ev["id"])
    db.set_state(
        f"ai_event:{ev['id']}",
        {
            "outcomes": {ev["outcomes"][0]["key"]: 0.5},
            "confidence": "high",
            "samples": 3,
            "ts": time.time(),
            "dossier_ts": d["collected_ts"],
        },
    )
    manual = model.analyze(db, ev, "manual", save=False)
    with_ai = model.analyze(db, ev, "ai", save=False)
    names = lambda r: {c["name"] for c in r["outcomes"][0]["components"]}
    assert "ai_blind" not in names(manual) and "ai_blind" in names(with_ai)


def test_ai_daily_limit(db):
    ev = EventInfo(id="x", title="?", topic="other")
    db.set_state("ai_events_day", {"day": time.strftime("%Y-%m-%d", time.gmtime()), "n": 5})
    assert ai.used_today(db) == 5
    assert ai.start(db, ev, [], 1, limit=5)["state"] == "limit"


# ---------------------------------------------------------------- поправка цены
def test_calibration_curve_monotone_and_shrunk():
    xs = [i / 100 for i in range(1, 100)]
    samples = [(p, 1 if (i * 37) % 100 < p * 80 else 0) for i, p in enumerate(xs * 20)]  # исходы реже цены
    c = calibration.Curve.fit(samples)
    vals = [c.apply(p) for p in (0.05, 0.2, 0.5, 0.8, 0.95)]
    assert vals == sorted(vals)  # чем выше цена — тем выше оценка
    assert c.apply(0.5) < 0.5  # исходы сбываются реже цены — поправка вниз
    few = calibration.Curve.fit(samples[:20])
    assert abs(few.apply(0.5) - 0.5) <= abs(c.apply(0.5) - 0.5) + 1e-9  # мало данных — поправка слабее


def test_baseline_market_calibrated_off_by_default(db):
    reg = plugins.registry()
    assert not reg.is_enabled(reg.plugins["market_calibrated"], plugins.user_settings(db))
    p, label = calibration.baseline().apply(0.1, "politics", 30)
    assert 0 < p < 1 and label


# ---------------------------------------------------------------- сервис и API
def test_service_event_analysis(world, db):
    w, evs = world
    svc = Services(db, fast_params(), gamma=EventsGamma(w, evs), clob=EventsClob(w), clock=lambda: w.now)
    ev = detail(evs[0])
    _with_dossier(db, ev, 0.30)
    r = svc.event_analysis("9001", "ai")  # ключа нет — без ИИ
    assert r["ok"] and r["mode"] == "manual" and "ИИ не подключён" in r["ai"]["note"]
    assert len(r["outcomes"]) == 4
    assert sum(o["final"] for o in r["outcomes"]) == pytest.approx(sum(o["market"] for o in r["outcomes"]))
    assert r["line"] and all(len(v) == 1 for v in r["line"].values())
    assert svc.event_analysis("nope")["error"] == "Событие не найдено"
