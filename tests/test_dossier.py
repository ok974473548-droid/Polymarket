"""Модуль «События», этап B: досье события — сбор из плагинов, хранение, новые источники, API."""

from __future__ import annotations

import time

import pytest
from events_fake import EventsClob, EventsGamma, make_events

from whalefollow import plugins
from whalefollow.db import DB
from whalefollow.estimator.sources import Forecast, NewsItem
from whalefollow.eventhub import dossier
from whalefollow.eventhub.catalog import detail
from whalefollow.plugins import EventInfo, Evidence, SourcePlugin
from whalefollow.plugins.builtin import event_sources as es
from whalefollow.services import Services

from conftest import fast_params
from synthetic import SyntheticWorld

OFF = {"google_news": False, "manifold": False, "kalshi": False, "bookmakers": False, "reddit": False}


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("WF_PLUGINS_DIR", str(tmp_path / "none"))
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
    d.set_setting("news_sources", OFF)  # без сети: встроенные источники выключены
    return d


class FakeNews(SourcePlugin):
    name = "fake_news"
    title = "Тестовые новости"

    def news(self, ctx, query, since_ts):
        now = int(time.time())
        return [
            NewsItem("Reuters", "media", now - 3600, "Мэр: опрос показал лидерство", "https://n/1"),
            NewsItem("Reuters", "media", now - 3600, "Мэр: опрос показал лидерство", "https://n/1"),  # повтор
            NewsItem("Blog", "social", now - 60, "Мэр — опрос показал лидерство!", "https://n/2"),  # тот же заголовок
        ]

    def forecasts(self, ctx, question, market=None):
        return [
            Forecast("Kalshi", question.replace("?", "") + " (Kalshi)", "https://k/1", 0.37),
            Forecast("Kalshi", "Совсем другой вопрос про погоду", "https://k/2", 0.9),  # не похоже — отбросить
        ]


class OwnDossier(SourcePlugin):
    name = "own_dossier"
    title = "Свои данные"
    topics = ("politics",)

    def dossier(self, ctx, ev):
        return [Evidence("data", "Свой", "Явка на прошлых выборах", value=41.5, unit="%", meta={"x": 1})]


class Broken(SourcePlugin):
    name = "broken"
    title = "Сломанный"

    def dossier(self, ctx, ev):
        raise ConnectionError("нет связи")


class OnlyCrypto(SourcePlugin):
    name = "only_crypto"
    topics = ("crypto",)

    def dossier(self, ctx, ev):
        raise AssertionError("не должен вызываться для политики")


def _register(*ps):
    for p in ps:
        plugins.registry().register(p, "тест")


def test_event_info_from_detail(world):
    _, evs = world
    info = dossier.event_info(detail(evs[0]))
    assert info.topic == "politics" and len(info.outcomes) == 4
    assert info.outcomes[0].title == "Анна Смирнова" and info.outcomes[0].question == "Победит ли Анна Смирнова?"
    single = dossier.event_info(detail(evs[1]))
    assert single.outcomes[0].question == evs[1]["title"]  # у рынка «Да/Нет» вопрос — само событие


def test_collect_dedupes_filters_and_records_errors(world, db):
    _, evs = world
    _register(FakeNews(), OwnDossier(), Broken(), OnlyCrypto())
    res = dossier.collect(db, detail(evs[0]))
    assert set(res["sources"]) == {"fake_news", "own_dossier", "broken"}  # «только крипто» к политике не подходит
    assert res["sources"]["broken"]["error"].startswith("ConnectionError")
    assert "нет связи" in plugins.status(db, "broken")["error"]
    got = dossier.load(db, evs[0]["id"])
    kinds = [x["kind"] for x in got["items"]]
    assert kinds.count("news") == 1  # три копии одной новости → одна
    fc = [x for x in got["items"] if x["kind"] == "forecast"]
    assert len(fc) == 4 and all(
        x["value"] == 0.37 for x in fc
    )  # по вопросу каждого из 4 исходов; чужой вопрос отброшен
    assert all(x["meta"]["similarity"] >= dossier.MIN_SIMILARITY for x in fc)
    assert {x["outcome_key"] for x in fc} == {r["key"] for r in detail(evs[0])["outcomes"]}
    data = [x for x in got["items"] if x["kind"] == "data"]
    assert data[0]["value"] == 41.5 and data[0]["meta"] == {"x": 1}
    news = [x for x in got["items"] if x["kind"] == "news"][0]
    assert 0 < news["weight"] <= 1  # вес по надёжности источника
    assert db.get_state("events_used_ts")


def test_keeps_only_recent_collections(world, db):
    _, evs = world
    _register(OwnDossier())
    for k in range(dossier.KEEP_COLLECTIONS + 2):
        db.execute("UPDATE evidence SET collected_ts=collected_ts-?", (100 + k,))  # сборы «разного времени»
        dossier.collect(db, detail(evs[0]))
    n = db.scalar("SELECT COUNT(DISTINCT collected_ts) AS n FROM evidence WHERE event_id=?", (evs[0]["id"],))
    assert n == dossier.KEEP_COLLECTIONS
    assert len(dossier.load(db, evs[0]["id"])["items"]) == 1  # на экране — только последний сбор


def test_background_job_and_service(world, db):
    w, evs = world
    _register(OwnDossier())
    svc = Services(db, fast_params(), gamma=EventsGamma(w, evs), clob=EventsClob(w), clock=lambda: w.now)
    r = svc.event_dossier("9001")
    assert r["ok"] and r["job"]["state"] == "running" and r["collected_ts"] is None
    for _ in range(100):
        if (dossier.job_state("9001") or {}).get("state") == "done":
            break
        time.sleep(0.05)
    r = svc.event_dossier("9001", "no")
    assert r["collected_ts"] and len(r["items"]) == 1 and not r["stale"]
    r2 = svc.event_dossier("9001")  # свежее — заново не собирается
    assert r2["job"]["state"] == "done"
    assert svc.event_dossier("nope", "force")["error"] == "Событие не найдено"


# ---------------------------------------------------------------- новые источники (без сети)
def test_parsers():
    assert es.detect_asset("What price will Bitcoin hit in November?") == "BTC"
    assert es.detect_asset("Solana Up or Down") == "SOL"
    assert es.detect_asset("Fed decision") is None
    w = es.parse_weather("Highest temperature in Hong Kong on November 19?", 1795000000)
    assert w["city"] == "Hong Kong" and w["date"].endswith("-11-19") and w["var"] == "temperature_2m_max"
    assert (
        es.parse_weather("Lowest temperature in New York City on December 3?", 1795000000)["var"]
        == "temperature_2m_min"
    )
    assert es.parse_weather("Where will it rain on November 20?", None) is None
    assert [m[0] for m in es.fred_matches("Fed Decision in December? 25 bps cut")] == ["DFEDTARU"]
    assert [m[0] for m in es.fred_matches("Core CPI above 3%? Inflation")] == ["CPILFESL"]  # базовая важнее общей


def test_weather_ensemble_and_fallback(monkeypatch, db):
    calls = []

    def fake_get(url, params=None, headers=None, timeout=15):
        calls.append(url)
        if "geocoding" in url:
            return {"results": [{"name": "Hong Kong", "country": "China", "latitude": 22.3, "longitude": 114.2}]}
        if "ensemble" in url:
            raise RuntimeError("HTTP 429")
        return {
            "daily": {"time": ["2026-11-19"], **{f"temperature_2m_max_m{i}": [20.0 + i] for i in range(6)}},
            "daily_units": {"temperature_2m_max": "°C"},
        }

    monkeypatch.setattr(es, "_get", fake_get)
    es._WEATHER_CACHE.clear()
    ev = EventInfo(id="1", title="Highest temperature in Hong Kong on November 19?", topic="weather", end_ts=1795000000)
    got = es.Weather().dossier(plugins.context(db), ev)
    assert (
        got[0].value == pytest.approx(22.5)
        and got[0].unit == "°C"
        and got[0].meta["members"] == [20.0, 21.0, 22.0, 23.0, 24.0, 25.0]
    )
    assert got[0].source == "Open-Meteo (модели)"  # ансамбль отказал — прогнозы моделей
    n = len(calls)
    es.Weather().dossier(plugins.context(db), ev)
    assert len(calls) == n + 1  # второй раз прогноз из кэша (только поиск города)


def test_crypto_market(monkeypatch, db):
    def fake_get(url, params=None, headers=None, timeout=15):
        if "get_index_price" in url:
            return {"result": {"index_price": 64000.0}}
        if "get_volatility_index_data" in url:
            return {"result": {"data": [[1_000_000, 0, 0, 0, 40.0], [2_000_000, 0, 0, 0, 44.5]]}}
        if "get_historical_volatility" in url:
            return {"result": [[2_000_000, 41.2]]}
        if "candles" in url:
            return [[i * 86400, 0, 0, 0, 100 * (1.01 if i % 2 else 0.99), 0] for i in range(40)]
        raise AssertionError(url)

    monkeypatch.setattr(es, "_get", fake_get)
    ev = EventInfo(id="1", title="What price will Bitcoin hit in November?", topic="crypto")
    got = es.CryptoMarket().dossier(plugins.context(db), ev)
    by = {e.meta["metric"]: e for e in got}
    assert (
        by["spot"].value == 64000.0 and by["implied_vol"].value == 44.5 and by["implied_vol"].meta["week_ago"] == 40.0
    )
    assert by["realized_vol"].value == 41.2 and by["realized_vol_30d"].value > 0


def test_fred(monkeypatch, db):
    rows = [(f"2025-{m:02d}-01", 300.0 + m) for m in range(1, 13)] + [
        (f"2026-{m:02d}-01", 310.0 + m) for m in range(1, 11)
    ]
    monkeypatch.setattr(es, "fred_csv", lambda sid: rows)
    ev = EventInfo(id="1", title="CPI inflation above 3% in October?", topic="economics")
    got = es.Fred().dossier(plugins.context(db), ev)
    assert got[0].meta["series"] == "CPIAUCSL" and got[0].unit == "%"
    assert got[0].value == pytest.approx((320.0 / 310.0 - 1) * 100, abs=1e-3)  # рост за год
    assert es.Fred().applies(ev) and not es.Fred().applies(
        EventInfo(id="2", title="Who wins the match?", topic="sports")
    )


def test_metaculus_needs_token_and_parses(monkeypatch, db):
    m = es.Metaculus()
    ctx = plugins.context(db)
    assert "WF_METACULUS_TOKEN" in m.available(ctx)
    monkeypatch.setenv("WF_METACULUS_TOKEN", "t")
    seen = {}

    def fake_get(url, params=None, headers=None, timeout=15):
        seen["auth"] = headers["Authorization"]
        return {
            "results": [
                {
                    "id": 7,
                    "title": "Will the Fed cut rates in December 2026?",
                    "question": {"aggregations": {"recency_weighted": {"latest": {"centers": [0.63]}}}},
                },
                {
                    "id": 8,
                    "title": "Unrelated question about Mars",
                    "question": {"aggregations": {"recency_weighted": {"latest": {"centers": [0.2]}}}},
                },
            ]
        }

    monkeypatch.setattr(es, "_get", fake_get)
    got = m.forecasts(ctx, "Will the Fed cut rates in December?")
    assert seen["auth"] == "Token t" and [(f.probability, f.url) for f in got] == [
        (0.63, "https://www.metaculus.com/questions/7/")
    ]


def test_polymarket_flow(db):
    now = int(time.time())

    class Data:
        def trades(self, condition=None, limit=500, max_pages=1):
            return [
                {
                    "timestamp": now - 100,
                    "size": 10000,
                    "price": 0.4,
                    "token_id": "y",
                    "side": "BUY",
                    "proxy_wallet": "0xw",
                },
                {"timestamp": now - 200, "size": 1000, "price": 0.4, "token_id": "y", "side": "SELL"},
                {
                    "timestamp": now - 3 * 86400,
                    "size": 99999,
                    "price": 0.4,
                    "token_id": "y",
                    "side": "BUY",
                },  # старше суток
            ]

        def holders(self, condition, limit=3, include_pnl=True):
            return {"y": [{"name": "Кит", "current_value": 5000, "total_pnl": 1200}]}

    ev = EventInfo(
        id="1",
        title="Т",
        topic="politics",
        outcomes=[plugins.OutcomeInfo("y", "Анна", "Победит ли Анна?", 0.4, "m1", "y", "n")],
    )
    p = es.PolymarketFlow()
    ctx = plugins.context(db, clients={"data": Data()})
    assert p.available(plugins.context(db)) is not None
    got = p.dossier(ctx, ev)
    net_flow = [e for e in got if e.note.startswith("перевес")][0]
    assert net_flow.value == pytest.approx(4000 - 400) and net_flow.meta["yes_buy"] == 4000
    assert any("Крупнейший держатель «Да»" in e.title and e.value == 5000 for e in got)
    assert [e.value for e in got if e.title.startswith("Покупка")] == [4000.0]
