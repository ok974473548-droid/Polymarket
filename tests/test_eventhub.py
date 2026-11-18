"""Модуль «События», этап A: каталог, разбор событий, история цен, живые цены, виртуальная покупка, API."""

from __future__ import annotations

import json
import threading
import time
import urllib.request

import pytest
from events_fake import EventsClob, EventsGamma, make_events

from whalefollow.db import DB
from whalefollow.eventhub.catalog import EventsCatalog, detail, summary
from whalefollow.services import Services, watch_tokens, watched_tokens

from conftest import fast_params
from synthetic import SyntheticWorld


@pytest.fixture
def world():
    w = SyntheticWorld(seed=3, n_events=40, n_random=6, n_skilled=2)
    return w, make_events(w)


@pytest.fixture
def svc(world):
    w, evs = world
    d = DB("sqlite://:memory:")
    d.init_schema()
    s = Services(d, fast_params(), gamma=EventsGamma(w, evs), clob=EventsClob(w), clock=lambda: w.now)
    return s


def test_parse_multi_and_single(world):
    _, evs = world
    multi = detail(evs[0])
    assert multi["neg_risk"] and multi["topic"] == "politics" and multi["outcomes_n"] == 4
    titles = [r["title"] for r in multi["outcomes"]]
    assert titles == ["Анна Смирнова", "Борис Ким", "Вера Ли", "Глеб Орлов"]  # по убыванию цены
    r0 = multi["outcomes"][0]
    assert (
        (r0["yes_label"], r0["no_label"]) == ("Да", "Нет")
        and r0["no_token"]
        and r0["price"] == pytest.approx(0.41, abs=0.01)
    )
    assert multi["chart_keys"] == [r["key"] for r in multi["outcomes"]]
    single = detail(evs[1])
    assert len(single["outcomes"]) == 1 and single["outcomes"][0]["title"] == "Да" and single["outcomes"][0]["single"]
    assert single["topic"] == "economics"
    s = summary(evs[0])
    assert [t["title"] for t in s["top"]] == titles[:3] and s["volume"] == 1_250_000.0


def test_placeholders_and_team_outcomes():
    raw = {
        "id": "1",
        "title": "Матч",
        "markets": [
            {
                "conditionId": "0xa",
                "clobTokenIds": '["a1","a2"]',
                "outcomes": '["Команда А","Команда Б"]',
                "outcomePrices": '["0.35","0.65"]',
                "active": True,
            },
        ],
    }
    rows = detail(raw)["outcomes"]
    assert [(r["title"], r["yes_label"], r["no_label"]) for r in rows] == [
        ("Команда А", "Команда А", "Команда Б"),
        ("Команда Б", "Команда Б", "Команда А"),
    ]
    raw2 = {
        "id": "2",
        "title": "Кто станет CEO?",
        "markets": [
            {
                "conditionId": "0xb",
                "groupItemTitle": "Реальный",
                "clobTokenIds": '["b1","b2"]',
                "outcomePrices": '["0.2","0.8"]',
                "active": True,
            },
            {
                "conditionId": "0xc",
                "groupItemTitle": "Кандидат T",
                "clobTokenIds": '["c1","c2"]',
                "outcomePrices": '["0.5","0.5"]',
                "active": False,
            },
            {
                "conditionId": "0xd",
                "groupItemTitle": "Ещё один",
                "clobTokenIds": '["d1","d2"]',
                "outcomePrices": '["0.1","0.9"]',
                "active": True,
            },
        ],
    }
    assert [r["title"] for r in detail(raw2)["outcomes"]] == ["Реальный", "Ещё один"]  # заготовки скрыты


def test_catalog_list_search_cache_and_store(world):
    w, evs = world
    g = EventsGamma(w, evs)
    d = DB("sqlite://:memory:")
    d.init_schema()
    cat = EventsCatalog(g, EventsClob(w), d)
    assert [e["id"] for e in cat.browse()["items"]] == ["9001", "9002"]
    assert [e["id"] for e in cat.browse("politics")["items"]] == ["9001"]
    assert [e["id"] for e in cat.browse("economics")["items"]] == ["9002"]  # раздел economy
    n = g.calls
    cat.browse("politics")
    assert g.calls == n  # повтор — из кэша
    assert [e["id"] for e in cat.browse(q="центробанк")["items"]] == ["9002"]
    ev = cat.get("9001")
    assert ev["title"] == "Кто победит на выборах мэра?"
    assert d.scalar("SELECT COUNT(*) AS n FROM markets") == 4 and d.scalar("SELECT COUNT(*) AS n FROM tokens") == 8
    assert {r["category"] for r in d.query("SELECT category FROM markets")} == {"politics"}
    assert cat.get("nope") is None


def test_history_and_quotes(world):
    w, evs = world
    cat = EventsCatalog(EventsGamma(w, evs), EventsClob(w))
    h = cat.history("9001", "1d")
    assert h["ok"] and len(h["lines"]) == 4 and all(len(ln["points"]) > 10 for ln in h["lines"])
    assert cat.history("9001", "bogus")["period"] == "1w"
    k = detail(evs[0])["outcomes"][1]["key"]
    assert [ln["key"] for ln in cat.history("9001", "1h", [k])["lines"]] == [k]
    toks = [r["yes_token"] for r in detail(evs[0])["outcomes"]]
    q = cat.quotes(toks)
    assert set(q) == set(toks) and all(v["bid"] < v["ask"] for v in q.values()) and not q[toks[0]]["live"]
    live = cat.quotes(toks[:1], {toks[0]: {"bid": 0.4, "ask": 0.42}})
    assert live[toks[0]]["live"] and live[toks[0]]["bid"] == 0.4


def test_live_watch_feeds_streams(svc):
    db = svc.db
    watch_tokens(db, ["t1", "t2"], ttl_s=60)
    assert set(watched_tokens(db)) == {"t1", "t2"}
    db.set_state("live_watch", {"old": time.time() - 1})
    assert watched_tokens(db) == []
    r = svc.event_live("9001", [])
    assert r["ok"] and r["quotes"] == {}
    ev = svc.event_detail("9001")
    toks = [o["yes_token"] for o in ev["outcomes"]]
    r = svc.event_live("9001", toks)
    assert set(r["quotes"]) == set(toks) and set(watched_tokens(svc.db)) == set(toks)
    assert set(toks) <= set(svc._live_tokens())  # SSE шлёт цены исходов открытой страницы


def test_event_buy_limits_and_positions(svc):
    ev = svc.event_detail("9001")
    assert ev["ok"] and ev["limits"]["max_bet_usd"] == 20.0  # меньшее из лимита модуля ($25) и общего (2% банка)
    yes = ev["outcomes"][0]["yes_token"]
    assert not svc.event_buy("9001", "чужой", 10)["ok"]
    assert not svc.event_buy("9001", yes, 0)["ok"]
    r = svc.event_buy("9001", yes, 50)
    assert r["ok"] and r["capped"] and r["usd"] == pytest.approx(20.0, abs=0.01) and r["fee"] > 0
    pos = svc.positions("open")["items"]
    assert [(p["origin"], p["event_id"]) for p in pos] == [("event", "9001")]
    ev = svc.event_detail("9001")
    assert ev["mine"][0]["title"] == "Анна Смирнова" and ev["limits"]["left_today_usd"] == pytest.approx(80.0, abs=0.01)
    svc.db.set_setting("events_daily_limit_usd", 30)  # лимит модуля в день
    r = svc.event_buy("9002", svc.event_detail("9002")["outcomes"][0]["yes_token"], 20)
    assert r["ok"] and r["usd"] == pytest.approx(10.0, abs=0.01)  # осталось $10 из $30
    assert svc.event_detail("9001")["limits"]["left_today_usd"] == 0
    r = svc.event_buy("9001", yes, 5)
    assert not r["ok"] and "исчерпан" in r["error"]


def test_event_buy_respects_stop_and_paper_only(svc):
    svc.db.set_setting("mode", "manual")
    yes = svc.event_detail("9002")["outcomes"][0]["yes_token"]
    assert "отключены" in svc.event_buy("9002", yes, 5)["error"]
    svc.db.set_setting("mode", "paper")
    svc.stop_all()
    assert not svc.event_buy("9002", yes, 5)["ok"]


def test_catalog_api(svc):
    from whalefollow.api.server import serve

    httpd = serve(svc, port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    try:
        assert [e["id"] for e in call("GET", "/catalog?topic=politics")["items"]] == ["9001"]
        ev = call("GET", "/catalog/9001")
        assert ev["ok"] and len(ev["outcomes"]) == 4
        h = call("GET", "/catalog/9001/history?period=1w&keys=" + ev["outcomes"][0]["key"])
        assert len(h["lines"]) == 1
        tok = ev["outcomes"][0]["yes_token"]
        assert tok in call("GET", f"/catalog/9001/live?tokens={tok}")["quotes"]
        assert call("POST", "/catalog/9001/buy", {"token_id": tok, "usd": 5})["ok"]
        try:
            call("GET", "/catalog/nope")
            raise AssertionError("ожидалась 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        httpd.shutdown()
