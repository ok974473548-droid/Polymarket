"""Точность модели, F4: крипто по опционам Deribit — разбор инструментов, улыбка, вероятности, окно «коснётся»,
источник и оценщик (без сети)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from whalefollow import plugins
from whalefollow.db import DB
from whalefollow.eventhub import crypto as K
from whalefollow.plugins import EventInfo, OutcomeInfo
from whalefollow.plugins.builtin import estimators as es
from whalefollow.plugins.builtin import event_sources as src

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC).timestamp()
DAY = 86400


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("WF_PLUGINS_DIR", str(tmp_path / "none"))
    plugins.reset_registry()
    src._OPT_CACHE.clear()
    yield
    plugins.reset_registry()


def test_parse_instrument():
    i = K.parse_instrument("BTC-20NOV26-64000-P")
    assert i == {
        "prefix": "BTC",
        "exp": int(datetime(2026, 11, 20, 8, tzinfo=UTC).timestamp()),
        "K": 64000.0,
        "cp": "P",
    }
    assert K.parse_instrument("XRP_USDC-28NOV26-2d5-C")["K"] == 2.5
    assert K.parse_instrument("BTC-PERPETUAL") is None


def flat(sigma, f=100.0, exps=(30, 90)):
    return [{"exp": int(NOW + d * DAY), "F": f, "k": [-1.0, 0.0, 1.0], "iv": [sigma] * 3} for d in exps]


def test_build_smiles_filters():
    exp = int(NOW + 10 * DAY)
    pts = [(exp, k, 0.5, 100.0) for k in (60, 80, 90, 100, 110, 120, 150, 500)]  # 500 — слишком далеко
    pts += [(exp, 100, 0.7, 100.0), (int(NOW + 600), 100, 0.5, 100.0), (exp, 90, 9.0, 100.0)]  # дубль, истекает, мусор
    sm = K.build_smiles(pts, NOW)
    assert len(sm) == 1 and len(sm[0]["k"]) == 7 and sm[0]["iv"][3] == pytest.approx(0.6)  # медиана по страйку 100
    assert K.build_smiles(pts[:3], NOW) == []  # мало страйков


def test_flat_smile_matches_lognormal():
    sm = flat(0.5)
    T = NOW + 30 * DAY
    p = K.prob_above(sm, 100.0, T, 120.0, NOW)
    want = es.threshold_prob(100.0, 120.0, 0.5, 30 / 365, "up", "terminal")
    assert p == pytest.approx(want, abs=2e-3)
    assert K.sigma_at(sm[0], 5.0) == 0.5  # за краем — как у крайнего страйка


def test_skew_fattens_down_tail_and_interpolation():
    T = NOW + 30 * DAY
    skew = [{"exp": int(NOW + 30 * DAY), "F": 100.0, "k": [-0.5, 0.0, 0.5], "iv": [0.9, 0.5, 0.5]}]
    assert 1 - K.prob_above(skew, 100.0, T, 70.0, NOW) > 1 - K.prob_above(flat(0.5, exps=(30,)), 100.0, T, 70.0, NOW)
    two = [
        {"exp": int(NOW + 10 * DAY), "F": 100.0, "k": [-1, 0, 1], "iv": [0.4] * 3},
        {"exp": int(NOW + 40 * DAY), "F": 110.0, "k": [-1, 0, 1], "iv": [0.6] * 3},
    ]
    f, w = K.forward_and_var(two, 100.0, NOW + 25 * DAY, 100.0, NOW)
    assert f == pytest.approx(105.0)  # форвард — посередине по времени
    w1, w2 = 0.4**2 * 10 / 365, 0.6**2 * 40 / 365
    assert w == pytest.approx(w1 + 0.5 * (w2 - w1))
    f0, w0 = K.forward_and_var(two, 100.0, NOW + 5 * DAY, 100.0, NOW)  # раньше первой экспирации
    assert f0 == pytest.approx(100.0) and w0 == pytest.approx(0.4**2 * 5 / 365)


def test_probability_kinds():
    sm, T = flat(0.5), NOW + 30 * DAY
    up = K.prob_above(sm, 100.0, T, 120.0, NOW)
    assert K.probability(sm, 100.0, T, {"k": 120.0, "dir": "up", "kind": "touch"}, NOW) == pytest.approx(min(1, 2 * up))
    assert K.probability(sm, 100.0, T, {"k": 120.0, "dir": "up", "kind": "touch"}, NOW, window_hi=121.0) == 1.0
    assert K.probability(sm, 100.0, T, {"k": 80.0, "dir": "down", "kind": "touch"}, NOW, window_lo=79.0) == 1.0
    dn = K.probability(sm, 100.0, T, {"k": 80.0, "dir": "down", "kind": "terminal"}, NOW)
    assert dn == pytest.approx(1 - K.prob_above(sm, 100.0, T, 80.0, NOW))
    rng = K.probability(sm, 100.0, T, {"k": 95.0, "k2": 105.0, "dir": "range", "kind": "terminal"}, NOW)
    assert rng == pytest.approx(K.prob_above(sm, 100.0, T, 95.0, NOW) - K.prob_above(sm, 100.0, T, 105.0, NOW))
    assert K.probability(sm, 100.0, NOW - 1, {"k": 1.0, "dir": "up", "kind": "touch"}, NOW) is None


def test_window_start_and_extremes():
    end = int(datetime(2026, 11, 30, 23, tzinfo=UTC).timestamp())
    assert (
        K.window_start("What price will Bitcoin hit in November?", end) == datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    )
    assert (
        K.window_start("What price will Bitcoin hit November 16-22?", end)
        == datetime(2026, 11, 16, tzinfo=UTC).timestamp()
    )
    assert K.window_start("What price will Bitcoin hit in 2026?", end) == datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    assert K.window_start("Bitcoin above ___ on November 18?", end) == datetime(2026, 11, 18, tzinfo=UTC).timestamp()
    c = [[10, 90.0, 110.0, 0, 0, 0], [20, 80.0, 105.0, 0, 0, 0], [30, 95.0, 130.0, 0, 0, 0]]
    assert K.window_extremes(c, 10, 30) == (110.0, 80.0)


def test_source_and_estimator(monkeypatch):
    db = DB("sqlite://:memory:")
    db.init_schema()
    exp = int(time.time() + 20 * DAY)
    chain = []
    for k in (40000, 50000, 60000, 62000, 64000, 70000, 80000, 100000):
        cp = "C" if k >= 62000 else "P"
        chain.append(
            {
                "instrument_name": f"BTC-{time.strftime('%d%b%y', time.gmtime(exp - 8 * 3600)).upper()}-{k}-{cp}",
                "mark_iv": 50.0 + (62000 - k) / 1000 * (1 if k < 62000 else 0),
                "underlying_price": 62000.0,
            }
        )

    def fake_get(url, params=None, **kw):
        if "deribit" in url:
            return {"result": chain}
        if "/spot" in url:
            return {"data": {"amount": "62000"}}
        if "candles" in url:
            return [[time.time() - 86400 * 3, 61000.0, 66000.0, 0, 0, 0]]
        raise AssertionError(url)

    monkeypatch.setattr(src, "_get", fake_get)
    end = int(time.time() + 10 * DAY)
    outs = [
        OutcomeInfo("u70", "↑ 70,000", "Will Bitcoin reach $70,000 in November?", None, market_id="a"),
        OutcomeInfo("u65", "↑ 65,000", "Will Bitcoin reach $65,000 in November?", None, market_id="b"),
        OutcomeInfo("d50", "↓ 50,000", "Will Bitcoin dip to $50,000 in November?", None, market_id="c"),
    ]
    ev = EventInfo(id="c", title="What price will Bitcoin hit in November?", topic="crypto", end_ts=end, outcomes=outs)
    assert src.CryptoOptions().applies(ev)
    items = src.CryptoOptions().dossier(plugins.context(db), ev)
    opt = items[0].meta["opt"]
    assert opt["asset"] == "BTC" and opt["spot"] == 62000.0 and opt["hi"] == 66000.0 and len(opt["smiles"]) == 1
    dossier = [{"kind": "data", "meta": {"opt": opt}}, {"kind": "data", "meta": {"metric": "spot"}, "value": 62000.0}]
    est = es.CryptoOptionsModel()
    assert est.applies(ev, dossier) and not es.CryptoThreshold().applies(ev, dossier)  # крипто дважды не учитываем
    got = {c.outcome_key: c.p for c in est.estimate(plugins.context(db), ev, dossier, {})}
    assert got["u65"] == 0.999  # уже касалась 66 000 в этом окне
    assert 0.001 < got["u70"] < 0.5 and 0.001 < got["d50"] < 0.5
