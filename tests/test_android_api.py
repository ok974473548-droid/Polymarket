"""Android-приложение: уведомления для фоновой проверки (время, ссылка на экран, фильтр «как push»)."""

from __future__ import annotations

from whalefollow.db import DB
from whalefollow.services import Services

from conftest import fast_params


def _svc():
    db = DB("sqlite://:memory:")
    db.init_schema()
    return db, Services(db, fast_params(), clock=lambda: 2_000_000_000)


def test_notifications_have_ts_and_link():
    db, svc = _svc()
    db.notify("signal", "Новый сигнал", "Вопрос", ref="s_1", ts=1_999_999_000)
    db.notify("event_edge", "Выгода", "…", ref="9001", ts=1_999_999_100)
    items = svc.notifications()["items"]
    assert items[0]["link"] == "/#/events/9001" and items[0]["ts"] == 1_999_999_100
    assert items[1]["link"] == "/#/signal/s_1"


def test_push_channel_follows_settings():
    db, svc = _svc()
    db.notify("signal", "Новый сигнал", "", ref="s_1", ts=100)
    db.notify("settings", "Настройки сохранены", "", ts=200)  # не для push
    db.notify("weekly", "Итоги недели", "", ts=300)
    assert [x["kind"] for x in svc.notifications(channel="push")["items"]] == ["weekly", "signal"]
    db.set_setting("notify", {"push": True, "signals": False, "weekly": True})
    assert [x["kind"] for x in svc.notifications(channel="push")["items"]] == ["weekly"]
    db.set_setting("notify", {"push": False})
    assert svc.notifications(channel="push")["items"] == []  # «Push» выключен — телефону ничего
    assert len(svc.notifications()["items"]) == 3  # колокольчик показывает всё


def test_api_route(tmp_path):
    from fastapi.testclient import TestClient

    from whalefollow.api.app import create_app

    db, svc = _svc()
    db.notify("signal", "Новый сигнал", "", ref="s_1")
    db.notify("settings", "Настройки", "")
    c = TestClient(create_app(svc))
    r = c.get("/api/notifications", params={"channel": "push", "limit": 5}).json()
    assert [x["kind"] for x in r["items"]] == ["signal"]
    assert len(c.get("/api/notifications").json()["items"]) == 2
