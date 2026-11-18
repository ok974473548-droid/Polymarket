"""Точность модели, F3: согласованность связанных исходов — лестницы, проекция, без риска, советник, покупка."""

from __future__ import annotations

import time
from datetime import date

import pytest

from whalefollow import fees, plugins
from whalefollow.db import DB
from whalefollow.eventhub import advisor, model, watch
from whalefollow.eventhub import consistency as C
from whalefollow.services import Services

from conftest import fast_params


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("WF_PLUGINS_DIR", str(tmp_path / "none"))
    plugins.reset_registry()
    yield
    plugins.reset_registry()


def o(key, title, q=""):
    return {"key": key, "title": title, "question": q}


def kinds(outs, today=date(2026, 11, 18)):
    return {(lad.kind, tuple(r.key for r in lad.rungs)) for lad in C.ladders(outs, today)}


def test_ladders_thresholds():
    outs = [
        o("a", "↑ 120,000", "Will Bitcoin reach $120,000?"),
        o("b", "↑ 130,000"),
        o("c", "↓ 60,000"),
        o("d", "↓ 50,000"),
        o("e", "↑ 110,000"),
    ]
    got = kinds(outs)
    assert ("up", ("e", "a", "b")) in got  # выше порог — уже событие: вероятность не растёт вдоль списка
    assert ("down", ("c", "d")) in got  # ниже порог «вниз» — уже
    assert kinds([o("x", "$100M", "FDV above $100M?"), o("y", "$1B", "FDV above $1B?")]) == {("up", ("x", "y"))}
    assert kinds([o("p", "O/U 198.5"), o("q", "O/U 202.5"), o("r", "1Q O/U 47.5")]) == {("up", ("p", "q"))}


def test_ladders_dates_top_and_plus():
    by = "Will it happen by {}?"
    got = kinds(
        [
            o("n", "November 30", by.format("November 30")),
            o("m", "March 31", by.format("March 31")),
            o("d", "December 31", by.format("December 31")),
        ]
    )
    assert got == {("date", ("m", "d", "n"))}  # «March 31» — следующего года: самая поздняя, самая широкая
    assert kinds([o("y1", "December 31, 2027", by.format("Dec")), o("y0", "November 30, 2026", by.format("Nov"))]) == {
        ("date", ("y1", "y0"))
    }
    assert kinds([o("on", "November 18", "Will BTC hit $1 on November 18?")]) == set()  # не «к дате»
    assert kinds([o("t1", "Top 1", "Rank debut?"), o("t20", "Top 20", "Rank debut?")]) == {("down", ("t20", "t1"))}
    assert kinds([o("a", "41.0+", "approval"), o("b", "42.0+", "approval")]) == {("up", ("a", "b"))}
    # «+» в вопросе («GPT 6.1+») — не слово «вверх»; диапазоны — корзины, не лестница
    assert kinds(
        [o("a", "Top 5", "Will GPT 6.1+ debut in top 5?"), o("b", "Top 1", "Will GPT 6.1+ debut in top 1?")]
    ) == {("down", ("a", "b"))}
    assert kinds([o("r1", "340-359"), o("r2", "360-379")]) == set()


def test_isotonic_and_projection_liquid_only():
    assert C.isotonic_down([0.5, 0.3, 0.4, 0.1]) == pytest.approx([0.5, 0.35, 0.35, 0.1])
    outs = [o("a", "↑ 100"), o("b", "↑ 110"), o("c", "↑ 120")]
    pr = C.projected(outs, {"a": 0.40, "b": 0.45, "c": 0.10})
    assert pr["a"]["p"] == pytest.approx(0.425) and pr["b"]["p"] == pytest.approx(0.425) and "c" not in pr
    assert C.projected(outs, {"a": 0.40, "b": 0.45, "c": 0.10}, liquid={"a", "c"}) == {}  # пустой рынок не в счёт


def ev_ladder():
    rows = []
    for i, k in enumerate((100, 110, 120)):
        rows.append(
            {
                "key": f"y{k}",
                "title": f"↑ {k}",
                "question": f"Will X reach {k}?",
                "yes_token": f"y{k}",
                "no_token": f"n{k}",
                "yes_label": "Да",
                "no_label": "Нет",
                "closed": False,
                "price": 0.5 - i * 0.1,
                "market_id": f"m{k}",
                "single": False,
            }
        )
    return {
        "id": "L",
        "title": "What will X hit?",
        "topic": "crypto",
        "neg_risk": False,
        "outcomes": rows,
        "end_ts": int(time.time()) + 86400 * 10,
        "tag_slugs": [],
    }


def test_arbitrage_ladder_with_fees():
    ev = ev_ladder()
    rate = fees.rate_for("crypto")
    # «Да» на 110 по 30¢ и «Нет» на 120 по 60¢ — 90¢ + комиссии < $1: выплата ≥ $1 при любом исходе
    qs = {"y110": {"asks": [(0.30, 50.0)]}, "n120": {"asks": [(0.60, 20.0)]}}
    arbs = C.arbitrage(ev, qs, rate)
    assert len(arbs) == 1 and arbs[0]["kind"] == "ladder" and arbs[0]["shares"] == 20.0
    cost = 0.30 + fees.fee_per_share(rate, 0.30) + 0.60 + fees.fee_per_share(rate, 0.60)
    assert arbs[0]["cost"] == pytest.approx(cost, abs=1e-4) and arbs[0]["profit"] == pytest.approx(1 - cost, abs=1e-4)
    assert [leg["token_id"] for leg in arbs[0]["legs"]] == ["y110", "n120"]
    assert C.arbitrage(ev, {"y110": {"asks": [(0.45, 50.0)]}, "n120": {"asks": [(0.55, 20.0)]}}, rate) == []
    # обратная пара (узкое «Да» + широкое «Нет») — не без риска
    assert C.arbitrage(ev, {"y120": {"asks": [(0.01, 50.0)]}, "n110": {"asks": [(0.5, 50.0)]}}, rate) == []


def test_arbitrage_exclusive():
    rows = [
        {"key": k, "title": k, "yes_token": k, "no_token": "n" + k, "closed": False, "price": p}
        for k, p in (("A", 0.5), ("B", 0.3), ("C", 0.2))
    ]
    ev = {"id": "E", "neg_risk": True, "outcomes": rows}
    qs = {"A": {"asks": [(0.45, 10.0)]}, "B": {"asks": [(0.28, 30.0)]}, "C": {"asks": [(0.19, 30.0)]}}
    arbs = C.arbitrage(ev, qs, 0.0)
    assert arbs[0]["kind"] == "exclusive_yes" and arbs[0]["profit"] == pytest.approx(0.08) and arbs[0]["shares"] == 10
    rows[2]["price"] = 0.05  # сумма цен 85% — перечислены не все исходы: не без риска
    assert C.arbitrage(ev, qs, 0.0) == []


def test_log_and_stats():
    db = DB("sqlite://:memory:")
    db.init_schema()
    a = {"kind": "ladder", "profit": 0.05, "shares": 10, "legs": [{"token_id": "x"}, {"token_id": "y"}]}
    C.log_arbs(db, {"id": "E", "title": "t"}, [a], now=1000)
    C.log_arbs(db, {"id": "E", "title": "t"}, [a], now=2000)  # та же комбинация в течение часа — одна запись
    C.log_arbs(db, {"id": "E", "title": "t"}, [a], now=5000)
    st = C.arb_stats(db, 30, now=6000)
    assert st["n"] == 2 and st["events"] == 1 and st["best"] == 0.05


def test_model_finals_follow_ladder():
    db = DB("sqlite://:memory:")
    db.init_schema()
    ev = ev_ladder()

    class Bumpy(plugins.EstimatorPlugin):
        name = "bumpy"
        title = "Неровная оценка"

        def estimate(self, ctx, ev, dossier, market):
            # оценка порогу 120 выше, чем порогу 110, — противоречие
            return [plugins.ComponentEstimate("y110", 0.20, 1.0, ""), plugins.ComponentEstimate("y120", 0.60, 1.0, "")]

    plugins.registry().register(Bumpy(), "тест")
    res = model.analyze(db, ev, "manual", {"y100": 0.5, "y110": 0.30, "y120": 0.28}, save=False)
    f = {r["key"]: r["final"] for r in res["outcomes"]}
    assert f["y100"] >= f["y110"] >= f["y120"]  # порядок восстановлен
    assert any(r.get("consistency") for r in res["outcomes"])


class FakeCat:
    def __init__(self, ev, quotes):
        self.ev, self.qs = ev, quotes

    def get(self, eid):
        return self.ev if eid == self.ev["id"] else None

    def quotes(self, tokens, live=None):
        return {t: self.qs[t] for t in tokens if t in self.qs}


class FakeClob:
    def __init__(self, qs):
        self.qs = qs

    def book(self, tok):
        return [], list(self.qs.get(tok, {}).get("asks") or [])


def test_advice_and_buy_both_legs(monkeypatch):
    db = DB("sqlite://:memory:")
    db.init_schema()
    ev = ev_ladder()
    qs = {
        "y110": {"bid": 0.29, "ask": 0.30, "asks": [(0.30, 50.0)]},
        "n120": {"bid": 0.59, "ask": 0.60, "asks": [(0.60, 20.0)]},
    }
    svc = Services(db, fast_params(), clob=FakeClob(qs), clock=lambda: int(time.time()))
    cat = FakeCat(ev, qs)
    monkeypatch.setattr(svc, "_events", lambda: cat)
    adv = advisor.advise({"outcomes": []}, ev, qs, fees.rate_for("crypto"), 1000.0, 25.0)
    a = adv["arbs"][0]
    assert a["pairs"] == pytest.approx(min(20.0, 25.0 / a["cost"])) and a["profit_usd"] == pytest.approx(
        a["pairs"] * a["profit"], abs=0.01
    )
    r = svc.event_buy_arb("L", ["y110", "n120"], a["pairs"])
    assert r["ok"], r
    sids = {x["signal_id"] for x in db.query("SELECT signal_id FROM orders")}
    assert sids == {"ev:L:arb"}
    toks = {x["token_id"] for x in db.query("SELECT token_id FROM fills")}
    assert toks == {"y110", "n120"}
    res = svc.results("month")
    assert any(g["kind"] == "event_arb" and g["bets"] == 2 for g in res["events"])
    assert svc.event_buy_arb("L", ["y100", "n120"], 5)["error"].startswith("Комбинации без риска уже нет")


def test_watch_notifies_arbitrage(monkeypatch):
    db = DB("sqlite://:memory:")
    db.init_schema()
    ev = ev_ladder()
    watch.set_watch(db, "L", True, "t")
    arb = {
        "kind": "ladder",
        "profit": 0.06,
        "shares": 10,
        "cost": 0.94,
        "pairs": 10,
        "usd": 9.4,
        "profit_usd": 0.6,
        "legs": [
            {"token_id": "y110", "title": "↑ 110", "label": "Да", "price": 0.3},
            {"token_id": "n120", "title": "↑ 120", "label": "Нет", "price": 0.6},
        ],
    }
    monkeypatch.setattr(advisor, "evaluate", lambda *a, **k: {"analysis": {"outcomes": []}, "quotes": {}})
    monkeypatch.setattr(advisor, "advise", lambda *a, **k: {"buys": [], "items": [], "arbs": [arb]})
    r = watch.check(db, FakeCat(ev, {}), {}, lambda now: {"bank": 1000.0, "cap": 25.0}, plugins.user_settings(db))
    assert r["notified"] == 1
    n = db.one("SELECT * FROM notifications WHERE kind='event_edge'")
    assert n["title"].startswith("Без риска") and "6,0¢ на пару" in n["body"]
    assert C.arb_stats(db)["n"] == 1
