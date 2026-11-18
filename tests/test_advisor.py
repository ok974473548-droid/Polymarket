"""Модуль «События», этап D: советник — выгода с комиссией, порог, Келли, лимиты, стакан, «Следить», учёт ставок."""

from __future__ import annotations

import json
import time

import pytest
from events_fake import EventsClob, EventsGamma, make_events

from whalefollow import fees, plugins
from whalefollow.db import DB
from whalefollow.estimator.sources import Forecast
from whalefollow.eventhub import advisor as A
from whalefollow.eventhub import dossier as dz
from whalefollow.eventhub import watch
from whalefollow.eventhub.catalog import EventsCatalog, detail
from whalefollow.plugins import SourcePlugin
from whalefollow.services import Services

from conftest import fast_params
from synthetic import SyntheticWorld

OFF = {"google_news": False, "manifold": False, "kalshi": False, "bookmakers": False, "reddit": False}
YEAR = 365 * 86400


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("WF_PLUGINS_DIR", str(tmp_path / "none"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
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


# ---------------------------------------------------------------- математика совета
def test_cost_includes_taker_fee():
    assert A.cost(0.4, 0.05) == pytest.approx(0.4 + 0.05 * 0.4 * 0.6)
    assert A.kelly(0.6, 0.5) == pytest.approx(0.2)
    assert A.kelly(0.5, 0.5) == 0.0 and A.kelly(0.4, 0.5) == 0.0


def test_limit_price_satisfies_both_rules():
    p, rate, need, years = 0.6, 0.05, 0.03, 0.1
    lim = A.limit_price(p, rate, need, years)
    assert lim is not None
    assert p - A.cost(lim, rate) >= need - 1e-6  # выгода не меньше порога
    assert p - A.cost(lim + 0.001, rate) < need  # и это самая высокая такая цена
    assert p / A.cost(lim, rate) >= (1 + A.MIN_APY) ** years - 1e-9
    # долгий срок: порог годовой доходности строже порога выгоды
    far = A.limit_price(p, rate, need, 3.0)
    assert far < lim and p / A.cost(far, rate) == pytest.approx((1 + A.MIN_APY) ** 3.0, rel=5e-3)
    assert A.limit_price(0.01, rate, need, years) is None


def test_advise_side_buy_with_kelly_caps_and_book():
    asks = [(0.40, 50.0), (0.42, 100.0), (0.55, 1000.0)]
    r = A.advise_side(p=0.55, asks=asks, rate=0.05, half_width=0.02, years=0.05, bank=1000, cap_usd=25)
    c = A.cost(0.40, 0.05)
    assert r["verdict"] == "buy" and r["edge"] == pytest.approx(0.55 - c, abs=1e-4)
    assert r["kelly_usd"] == pytest.approx(A.KELLY_FRAC * (0.55 - c) / (1 - c) * 1000, abs=0.01)
    assert r["size_usd"] == pytest.approx(25.0) and r["capped"]  # Келли больше лимита — урезано
    # 50 акций по 40¢ ($20) + остаток по 42¢; дороже предельной цены не берём
    assert r["shares"] == pytest.approx(50 + 5 / 0.42, abs=0.01) and r["avg_price"] < r["limit_price"]
    fee = fees.fee_usd(0.05, r["shares"], r["avg_price"])
    assert r["fee_usd"] == pytest.approx(fee, abs=0.01)
    assert r["win_usd"] == pytest.approx(r["shares"] - 25 - fee, abs=0.02)
    assert r["lose_usd"] == pytest.approx(-25 - fee, abs=0.01)
    assert r["ev_usd"] == pytest.approx(r["shares"] * 0.55 - 25 - fee, abs=0.02)


def test_advise_side_only_cheap_levels():
    # предельная цена ниже второго уровня: Келли просит больше, но берём только выгодные акции
    asks = [(0.40, 10.0), (0.50, 1000.0)]
    r = A.advise_side(p=0.52, asks=asks, rate=0.05, half_width=0.0, years=0.05, bank=10000, cap_usd=100)
    assert r["verdict"] == "buy" and r["limit_price"] < 0.5 and r["shares"] == pytest.approx(10) and r["partial"]


def test_advise_side_rejections():
    base: dict = {"asks": [(0.50, 100.0)], "rate": 0.05, "years": 0.05, "bank": 1000, "cap_usd": 25}
    assert A.advise_side(p=0.49, half_width=0.0, **base)["verdict"] == "no"  # цена выше оценки
    small_edge = A.advise_side(p=0.53, half_width=0.0, **base)  # выгода 1,75 п.п. < 2 п.п.
    assert small_edge["verdict"] == "no" and "меньше порога" in small_edge["reason"]
    wide = A.advise_side(p=0.56, half_width=0.10, **base)  # широкий интервал поднимает порог: 2 + 5 п.п.
    assert wide["verdict"] == "no" and wide["need"] == pytest.approx(0.07)
    assert A.advise_side(p=0.56, half_width=0.0, **base)["verdict"] == "buy"
    slow = A.advise_side(p=0.56, half_width=0.0, **{**base, "years": 2.0})  # 8% годовых — мало
    assert slow["verdict"] == "no" and "годовых" in slow["reason"]
    assert A.advise_side(p=0.56, half_width=0.0, **{**base, "cap_usd": 0})["verdict"] == "limit"
    assert A.advise_side(p=0.56, half_width=0.0, **{**base, "bank": 20})["verdict"] == "small"
    assert A.advise_side(p=0.56, half_width=0.0, **{**base, "asks": []})["verdict"] == "no_book"


def test_advise_no_side_and_no_info():
    ev = {
        "end_ts": int(time.time()) + 10 * 86400,
        "outcomes": [
            {
                "key": "y",
                "title": "Да",
                "yes_token": "y",
                "no_token": "n",
                "yes_label": "Да",
                "no_label": "Нет",
                "closed": False,
            }
        ],
    }
    an = {"outcomes": [{"key": "y", "final": 0.30, "lo": 0.28, "hi": 0.32, "model": 0.25}]}
    q = {"y": {"asks": [(0.36, 500.0)]}, "n": {"asks": [(0.62, 500.0)]}}
    adv = A.advise(an, ev, q, 0.05, 1000, 25)
    yes, no = adv["items"]
    assert yes["verdict"] == "no" and no["verdict"] == "buy" and no["p"] == pytest.approx(0.70)
    assert adv["best"]["side"] == "no" and adv["best"]["token_id"] == "n"
    an["outcomes"][0]["model"] = None  # нет независимых оценок — совета нет
    adv2 = A.advise(an, ev, q, 0.05, 1000, 25)
    assert all(x["verdict"] == "no_info" for x in adv2["items"]) and adv2["best"] is None


# ---------------------------------------------------------------- на синтетическом событии
class Platform(SourcePlugin):
    name = "fake_platform"
    title = "Площадка"
    p = 0.62

    def forecasts(self, ctx, question, market=None):
        return [Forecast("Kalshi", question, "https://k/1", self.p)]


def _svc(w, evs, db):
    return Services(db, fast_params(), gamma=EventsGamma(w, evs), clob=EventsClob(w), clock=lambda: int(time.time()))


def _collect(db, svc, eid, p):
    src = Platform()
    src.p = p
    plugins.registry().register(src, "тест")
    dz.collect(db, svc._events().get(eid))


def test_service_advice_and_advised_buy(world, db):
    w, evs = world
    svc = _svc(w, evs, db)
    _collect(db, svc, "9002", 0.90)  # рынок 62%, площадка 90% → модель заметно выше цены
    r = svc.event_analysis("9002")
    adv = r["advice"]
    assert r["ok"] and adv["fee_rate"] == fees.rate_for("economics")
    yes = next(x for x in adv["items"] if x["side"] == "yes")
    assert yes["p"] == pytest.approx(r["outcomes"][0]["final"], abs=1e-4)
    assert adv["caps"]["cap"] == pytest.approx(min(25.0, 40.0, fast_params().max_bet_frac * 1000))
    assert yes["verdict"] == "buy", yes  # сторона «Да» выгодна — покупаем по совету
    assert next(x for x in adv["items"] if x["side"] == "no")["verdict"] == "no"
    assert adv["best"]["token_id"] == yes["token_id"]
    b = svc.event_buy("9002", yes["token_id"], yes["size_usd"], yes["limit_price"], advice=True)
    assert b["ok"]
    assert b["avg_price"] <= yes["limit_price"] + 1e-9
    sig = db.scalar("SELECT signal_id AS s FROM orders WHERE order_id=?", (b["order_id"],))
    assert sig == "ev:9002:adv"
    fee = db.scalar("SELECT fee_usd AS f FROM fills WHERE order_id=?", (b["order_id"],))
    assert fee == pytest.approx(fees.fee_usd(fees.rate_for("economics"), b["shares"], b["avg_price"]), abs=1e-4)
    pos = svc.positions("open")
    row = next(x for x in pos["items"] if x["token_id"] == yes["token_id"])
    assert row["origin"] == "event_model"
    res = svc.results("month")
    assert [g["kind"] for g in res["events"]] == ["event_model"] and res["events"][0]["bets"] == 1
    # после покупки остаток дневного лимита модуля меньше — и совет это учитывает
    assert svc.event_analysis("9002")["advice"]["caps"]["left_today"] == pytest.approx(100 - b["usd"], abs=0.01)


def test_event_fee_by_topic(world, db):
    """Было: комиссия ставок по событиям — общая 5%, если рынка нет в нашей таблице; стало — по теме события."""
    w, evs = world
    svc = _svc(w, evs, db)
    ev = svc._events().get("9001")
    db.execute("DELETE FROM markets WHERE market_id=?", (ev["outcomes"][0]["market_id"],))
    assert A.event_fee_rate(db, ev) == fees.rate_for("politics") == 0.04
    assert A.event_fee_rate(db, {**ev, "tag_slugs": ["geopolitics"]}) == 0.0


# ---------------------------------------------------------------- «Следить»
def test_watch_notifies_once_and_on_growth(world, db, monkeypatch):
    w, evs = world
    svc = _svc(w, evs, db)
    _collect(db, svc, "9002", 0.80)
    watch.set_watch(db, "9002", True, "ставка")
    cat = EventsCatalog(svc.gamma, svc.clob, db)
    edge = {"v": 0.05}

    def fake_advise(an, ev, q, rate, bank, cap, now=None):
        k = ev["outcomes"][0]["key"]
        item = {
            "key": k,
            "side": "yes",
            "edge": edge["v"],
            "title": "Да",
            "label": "Да",
            "limit_price": 0.66,
            "p": 0.7,
            "ask": 0.6,
            "verdict": "buy",
            "ev_usd": 1.0,
        }
        return {"buys": [item], "items": [item]}

    monkeypatch.setattr(A, "advise", fake_advise)
    caps = lambda now: {"bank": 1000.0, "cap": 25.0}
    settings = plugins.user_settings(db)
    assert watch.check(db, cat, {}, caps, settings)["notified"] == 1
    n = db.query("SELECT * FROM notifications WHERE kind='event_edge'")
    assert len(n) == 1 and n[0]["ref"] == "9002" and "до 66,0¢" in n[0]["body"]
    db.execute("UPDATE event_watch SET checked_ts=0")
    assert watch.check(db, cat, {}, caps, settings)["notified"] == 0  # та же выгода — не повторяем
    edge["v"] = 0.08
    db.execute("UPDATE event_watch SET checked_ts=0")
    assert watch.check(db, cat, {}, caps, settings)["notified"] == 1  # выгода выросла на 3 п.п.
    assert json.loads(db.one("SELECT state FROM event_watch")["state"])
    assert watch.check(db, cat, {}, caps, settings)["checked"] == 0  # 15 минут ещё не прошло


def test_watch_api_and_closed_event(world, db):
    w, evs = world
    svc = _svc(w, evs, db)
    r = svc.event_watch("9001", True)
    assert r["ok"] and r["watch"]["title"] == "Кто победит на выборах мэра?"
    assert [x["event_id"] for x in svc.events_watched()["items"]] == ["9001"]
    assert svc.event_analysis("9001")["watch"]["event_id"] == "9001"
    assert svc.event_watch("nope", True)["error"] == "Событие не найдено"
    svc.event_watch("9001", False)
    assert svc.events_watched()["items"] == []
    # событие закрылось — следить перестаём сами
    watch.set_watch(db, "9002", True, "x")
    evs[1]["closed"] = True
    cat = EventsCatalog(svc.gamma, svc.clob, db)
    watch.check(db, cat, {}, lambda now: {"bank": 1000.0, "cap": 25.0}, plugins.user_settings(db))
    assert watch.get(db, "9002") is None


def test_scheduler_job_runs(world, db):
    from whalefollow.scheduler import Scheduler

    w, evs = world
    svc = _svc(w, evs, db)
    sch = Scheduler(db, fast_params(), svc.gamma, None, svc.clob, None, groups=("slow",))
    sch.job_event_watch()  # список пуст — ничего не делает
    watch.set_watch(db, "9002", True, "x")
    sch.job_event_watch()
    assert db.one("SELECT checked_ts, last_error FROM event_watch")["checked_ts"]
    assert db.one("SELECT last_error FROM event_watch")["last_error"] is None
    assert detail(evs[1])["id"] == "9002"
