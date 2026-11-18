"""Модуль «События», этап E: самопроверка — только закрытые рынки и анализы до закрытия, честный вывод,
обучение λ и весов по прошлому, модель берёт выученное."""

from __future__ import annotations

import json
import math
import random

import pytest

from whalefollow import plugins
from whalefollow.db import DB
from whalefollow.eventhub import selfcheck as S

NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("WF_PLUGINS_DIR", str(tmp_path / "none"))
    plugins.reset_registry()
    yield
    plugins.reset_registry()


@pytest.fixture
def db():
    d = DB("sqlite://:memory:")
    d.init_schema()
    return d


def _sig(x):
    return 1 / (1 + math.exp(-x))


def _logit(p):
    return math.log(p / (1 - p))


def add(
    db,
    i,
    *,
    y,
    market,
    model,
    final=None,
    ts=NOW - 86400,
    close=NOW - 3600,
    payout_known=True,
    topic="politics",
    mode="manual",
    comp_name="platforms",
    n=1,
):
    """Событие с одним исходом: рынок, токен, n анализов."""
    mid, tok, eid = f"m{i}", f"t{i}", f"e{i}"
    db.upsert(
        "markets",
        {
            "market_id": mid,
            "event_id": eid,
            "category": topic,
            "status": "resolved",
            "resolved_ts": close,
            "end_ts": close,
        },
        ["market_id"],
    )
    db.upsert(
        "tokens",
        {"token_id": tok, "market_id": mid, "outcome_label": "Yes", "payout": float(y) if payout_known else None},
        ["token_id"],
    )
    for k in range(n):
        db.upsert(
            "event_analyses",
            {
                "id": f"a{i}_{mode}_{k}",
                "event_id": eid,
                "ts": ts + k,
                "mode": mode,
                "outcome_key": tok,
                "market_id": mid,
                "market_p": market,
                "anchor_p": market,
                "model_p": model,
                "final_p": final if final is not None else market,
                "lo": 0,
                "hi": 1,
                "confidence": "средняя",
                "lam": 0.3,
                "dossier_ts": ts,
                "components": json.dumps(
                    [{"name": comp_name, "title": "Площадки", "p": model, "w": 0.6, "role": "independent"}]
                ),
            },
            ["id"],
        )


def world(db, n, informative, seed=1, topic="politics"):
    rnd = random.Random(seed)
    for i in range(n):
        q = rnd.uniform(0.1, 0.9)
        y = 1 if rnd.random() < q else 0
        market = _sig(_logit(q) + rnd.gauss(0, 0.5))  # рынок шумный
        model = _sig(_logit(q) + rnd.gauss(0, 0.1)) if informative else _sig(_logit(q) + rnd.gauss(0, 1.5))
        final = _sig(_logit(market) + 0.3 * (_logit(model) - _logit(market)))
        add(db, f"{topic}{i}", y=y, market=market, model=model, final=final, topic=topic)


def test_only_closed_markets_and_analyses_before_close(db):
    add(db, 1, y=1, market=0.6, model=0.7)
    add(db, 2, y=0, market=0.6, model=0.7, payout_known=False)  # исход неизвестен
    add(db, 3, y=1, market=0.6, model=0.7, ts=NOW, close=NOW - 10)  # анализ после закрытия — подглядывание
    add(db, 4, y=1, market=0.6, model=0.7, close=NOW + 100)  # рынок закроется позже «сейчас»
    add(db, 5, y=0, market=0.4, model=0.3, n=4)  # четыре анализа одного исхода — вместе весят как один
    rows = S.samples(db, NOW)
    assert sorted({r["event_id"] for r in rows}) == ["e1", "e5"]
    assert sum(r["w"] for r in rows if r["event_id"] == "e5") == pytest.approx(1.0)
    assert S.pending_markets(db) == ["m2"]


def test_compare_and_honest_verdict(db):
    world(db, 10, informative=True)
    assert S.compare(S.samples(db, NOW))["verdict"]["code"] == "few"  # 10 исходов — мало
    world(db, 400, informative=True, seed=2, topic="crypto")
    c = S.compare(S.samples(db, NOW))
    assert c["n"] == 410 and c["brier"] < c["brier_market"] and c["diff"] < 0
    assert c["verdict"]["code"] == "better"


def test_noise_model_is_not_better(db):
    world(db, 300, informative=False, seed=3)
    c = S.compare(S.samples(db, NOW))
    assert c["verdict"]["code"] in ("same", "worse")  # шумная модель не «лучше рынка»


def test_fit_lambda_shrinks_and_follows_skill(db):
    world(db, 20, informative=True)
    lam, raw, n = S.fit_lambda(S.samples(db, NOW))
    assert (lam, raw, n) == (S.LAMBDA_PRIOR, S.LAMBDA_PRIOR, 20)  # мало данных — не трогаем
    world(db, 600, informative=True, seed=4, topic="crypto")
    lam, raw, n = S.fit_lambda(S.samples(db, NOW))
    assert raw > 0.4 and S.LAMBDA_PRIOR < lam < raw  # модель точнее — λ растёт, но со сжатием к 0,3
    db2 = DB("sqlite://:memory:")
    db2.init_schema()
    world(db2, 600, informative=False, seed=5)
    lam2, raw2, _ = S.fit_lambda(S.samples(db2, NOW))
    assert raw2 < 0.2 and lam2 < S.LAMBDA_PRIOR  # шумная модель — λ уменьшается


def test_learn_weights_and_model_uses_them(db):
    world(db, 300, informative=True, seed=6, topic="crypto")
    p = S.learn(db, NOW)
    assert p["n"] == 300 and p["lambda"] > S.LAMBDA_PRIOR
    assert p["by_topic"]["crypto"]["n"] == 300
    assert p["weights"]["platforms"]["mult"] > 1  # поправка площадок улучшала цену — вес больше
    assert len(p["history"]) == 1
    S.learn(db, NOW + 10)
    assert len(S.params(db)["history"]) == 1  # ничего не изменилось — история не растёт
    assert S.lambda_for(S.params(db), "crypto") == p["by_topic"]["crypto"]["lambda"]
    assert S.lambda_for(S.params(db), "sports") == p["lambda"]
    assert S.mult_for(S.params(db), "unknown") == 1.0


def test_weight_mult_bounds():
    assert S.weight_mult({"n": 5, "diff": -0.01, "brier_market": 0.2}) == 1.0
    assert S.weight_mult({"n": 1000, "diff": -0.2, "brier_market": 0.2}) == S.WEIGHT_MAX
    assert S.weight_mult({"n": 1000, "diff": 0.2, "brier_market": 0.2}) == S.WEIGHT_MIN


def test_report_modes_and_calibration(db):
    world(db, 50, informative=True, seed=7)
    add(db, "ai1", y=1, market=0.5, model=0.8, final=0.6, mode="ai")
    add(db, "noinfo", y=0, market=0.3, model=None, final=0.3)  # без независимых оценок: итог = цена
    rep = S.report(db, NOW)
    assert rep["modes"]["manual"]["n"] == 51 and rep["modes"]["manual"]["with_info"]["n"] == 50
    assert set(rep["modes"]) == {"manual", "ai"}
    assert rep["modes"]["ai"]["n"] == 1 and rep["modes"]["ai"]["verdict"]["code"] == "few"
    cal = rep["modes"]["manual"]["calibration"]
    assert sum(b["n"] for b in cal) == 51 and all(0 <= b["actual"] <= 1 for b in cal)
    assert rep["estimators"][0]["name"] == "platforms" and rep["topics"][0]["topic"] == "politics"


def test_model_analyze_uses_learned_lambda(db):
    from events_fake import make_events

    from whalefollow.eventhub import model
    from whalefollow.eventhub.catalog import detail

    from synthetic import SyntheticWorld

    ev = detail(make_events(SyntheticWorld(seed=3, n_events=40, n_random=6, n_skilled=2))[1])

    class Est(plugins.EstimatorPlugin):
        name = "platforms_like"
        title = "Тест"

        def estimate(self, ctx, ev, dossier, market):
            return [plugins.ComponentEstimate(ev.outcomes[0].key, 0.8, 0.5, "")]

    plugins.registry().register(Est(), "тест")
    db.set_state(S.STATE, {"lambda": 0.5, "n": 400, "by_topic": {}, "weights": {"platforms_like": {"mult": 1.5}}})
    res = model.analyze(db, ev, "manual", {ev["outcomes"][0]["key"]: 0.62}, save=False)
    r = res["outcomes"][0]
    assert res["lambda0"] == 0.5 and r["components"][0]["w"] == pytest.approx(0.75)
    assert r["lam"] == pytest.approx(0.5 * 0.75)
    txt = model.explain(res["outcomes"], False, True, res["lambda0"], 400)
    assert any("50% разницы (подобрано по 400 закрытым исходам)" in t for t in txt)


def test_resolve_and_scheduler_job(db, monkeypatch):
    from whalefollow.ingest import pipeline

    add(db, 1, y=1, market=0.6, model=0.7, payout_known=False)
    called = {}

    def fake_refresh(db_, gamma, ids):
        called["ids"] = list(ids)
        db_.execute("UPDATE tokens SET payout=1 WHERE market_id='m1'")
        return 1

    monkeypatch.setattr(pipeline, "ensure_markets", lambda db_, gamma, ids: 0)
    monkeypatch.setattr(pipeline, "refresh_markets", fake_refresh)
    assert S.resolve(db, object()) == 1 and called["ids"] == ["m1"]
    assert S.pending_markets(db) == []
    from whalefollow.scheduler import Scheduler

    from conftest import fast_params

    sch = Scheduler(db, fast_params(), object(), None, None, None, groups=("slow",), clock=lambda: NOW)
    sch.job_event_selfcheck()
    assert S.params(db)["fitted_ts"] == NOW


def test_lambda_grows_only_when_proven(db):
    """λ и веса растут только при доказанной точности (вывод «лучше рынка»), иначе — не выше исходных."""
    world(db, 40, informative=True, seed=8)
    rows = S.samples(db, NOW)
    _, raw, _ = S.fit_lambda(rows)
    p = S.learn(db, NOW)
    assert raw > S.LAMBDA_PRIOR  # по истории «лучше» было бы больше…
    assert S.compare(rows)["verdict"]["code"] != "better" and not p["proven"]
    assert p["lambda"] == S.LAMBDA_PRIOR  # …но польза не доказана — λ не растёт
    assert p["weights"]["platforms"]["mult"] <= 1.0
