"""Точность модели, F1: теневые анализы — выборка, очередь порциями, пометка «теневой», чистка, настройки."""

from __future__ import annotations

import time

import pytest
from events_fake import EventsClob, EventsGamma, make_events

from whalefollow import plugins
from whalefollow.db import DB
from whalefollow.eventhub import selfcheck, shadow
from whalefollow.eventhub.catalog import EventsCatalog
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
def setup():
    w = SyntheticWorld(seed=3, n_events=40, n_random=6, n_skilled=2)
    evs = make_events(w)
    db = DB("sqlite://:memory:")
    db.init_schema()
    db.set_setting("news_sources", OFF)
    cat = EventsCatalog(EventsGamma(w, evs), EventsClob(w), db)
    return w, evs, db, cat


class FakeCat:
    """Каталог со списками событий по темам и сортировкам."""

    def __init__(self, items):
        self.items = items
        self.calls = []

    def browse(self, topic, sort, q, offset):
        self.calls.append((topic, sort, offset))
        got = [x for x in self.items if x["topic"] == topic]
        return {"items": got[offset : offset + 24]}


def _it(i, topic, end_in_h, vol=50_000, n=3):
    return {
        "id": f"{topic}{i}",
        "topic": topic,
        "title": f"t{i}",
        "end_ts": int(time.time()) + end_in_h * 3600,
        "volume": vol,
        "outcomes_n": n,
    }


def test_pick_filters_and_mixes():
    items = [
        _it(1, "crypto", 1),  # закрывается через час — цена почти 0/1
        _it(2, "crypto", 24),
        _it(3, "crypto", 48, vol=500),  # тонкий рынок
        _it(4, "crypto", 24 * 200),  # слишком далеко
        _it(5, "crypto", 72),
        _it(6, "crypto", 96, n=0),  # открытых исходов нет
        _it(7, "weather", 30),
    ]
    got = shadow.pick(FakeCat(items), per_topic=4)
    assert [x["id"] for x in got] == ["crypto2", "crypto5", "weather7"]
    assert {x["topic"] for x in got} == {"crypto", "weather"}


def test_outcomes_for_shadow_range_and_limit():
    ev = {
        "outcomes": [{"key": str(i), "price": p, "closed": False} for i, p in enumerate([0.5, 0.01, 0.99, 0.3, None])]
        + [{"key": "c", "price": 0.4, "closed": True}]
    }
    assert [r["key"] for r in shadow.outcomes_for_shadow(ev)] == ["0", "3"]
    many = {"outcomes": [{"key": str(i), "price": 0.05 + i * 0.01, "closed": False} for i in range(30)]}
    rows = shadow.outcomes_for_shadow(many)
    assert len(rows) == shadow.MAX_OUTCOMES and rows[0]["key"] == "29"  # самые вероятные


def test_run_queue_batches_and_origin(setup, monkeypatch):
    w, evs, db, cat = setup
    monkeypatch.setattr(
        shadow, "pick", lambda cat, per_topic, now=None: [{"id": "9001"}, {"id": "9002"}, {"id": "nope"}]
    )
    monkeypatch.setattr(shadow, "BATCH", 2)
    r = shadow.run(db, cat, {}, plugins.user_settings(db))
    assert r == {"state": "ok", "done": 2, "saved": 5, "errors": 0, "left": 1}  # 4 кандидата + «Да» у второго
    origins = {x["origin"] for x in db.query("SELECT DISTINCT origin FROM event_analyses")}
    assert origins == {"shadow"}
    assert db.scalar("SELECT COUNT(*) AS n FROM markets", (), 0) >= 5  # рынки сохранены — исход узнаем
    assert db.one("SELECT collected_ts FROM event_dossiers WHERE event_id='9001'")  # досье собрано
    r = shadow.run(db, cat, {}, plugins.user_settings(db))
    assert r["done"] == 1 and r["left"] == 0 and r["saved"] == 0  # «nope» — нет такого события
    r = shadow.run(db, cat, {}, plugins.user_settings(db))
    assert r["done"] == 0  # очередь пуста, а 6 часов не прошло — новую не набираем
    st = shadow.status(db)
    assert st["round"] == 1 and st["events"] == 2 and st["outcomes"] == 5


def test_run_off_and_budget(setup, monkeypatch):
    w, evs, db, cat = setup
    db.set_setting("shadow", {"enabled": False, "per_topic": 10, "every_h": 6})
    assert shadow.run(db, cat, {}, plugins.user_settings(db)) == {"state": "off"}
    db.set_setting("shadow", {"enabled": True, "per_topic": 10, "every_h": 6})
    monkeypatch.setattr(shadow, "pick", lambda cat, per_topic, now=None: [{"id": "9001"}, {"id": "9002"}])
    r = shadow.run(db, cat, {}, plugins.user_settings(db), budget_s=0.0)
    assert r["done"] == 0 and r["left"] == 2  # бюджет времени исчерпан сразу — ничего не делаем


def test_prune_old_evidence(setup):
    w, evs, db, cat = setup
    now = time.time()
    for k, age_d in (("old", 60), ("new", 1)):
        ts = int(now - age_d * 86400)
        db.upsert(
            "evidence", {"id": k, "event_id": k, "collected_ts": ts, "kind": "news", "source": "s", "title": k}, ["id"]
        )
        db.upsert("event_dossiers", {"event_id": k, "collected_ts": ts, "sources": "{}"}, ["event_id"])
    assert shadow.prune(db, now) == 1
    assert [r["id"] for r in db.query("SELECT id FROM evidence")] == ["new"]
    assert [r["event_id"] for r in db.query("SELECT event_id FROM event_dossiers")] == ["new"]


def test_settings_validation_and_report(setup):
    w, evs, db, cat = setup
    svc = Services(db, fast_params(), gamma=cat.gamma, clob=cat.clob, clock=lambda: int(time.time()))
    r = svc.update_settings({"shadow": {"enabled": False, "per_topic": 99, "every_h": 0}})
    assert not r.get("errors")
    assert db.get_setting("shadow") == {"enabled": False, "per_topic": 50, "every_h": 1}
    assert svc.update_settings({"shadow": {"x": 1}})["errors"]["shadow"]
    rep = svc.events_accuracy()
    assert rep["shadow"]["enabled"] is False and rep["shadow"]["events"] == 0
    assert selfcheck.report(db)["by_origin"] == {}
