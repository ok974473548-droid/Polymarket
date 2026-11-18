"""Ротация китов по форме: без заглядывания вперёд, честные кривые, главная цифра «держится ли форма»."""

import numpy as np
import pytest

from whalefollow.rotation import WEEK, FormRule, Whale, merge_series, simulate_rotation
from whalefollow.services import Services

from conftest import fast_params

DAY = 86400
NOW = 1_790_000_000 - (1_790_000_000 % DAY)


def make_whales(mus, weeks=60, noise=300.0, seed=1, vol_per_day=1000.0, persistent=True):
    """Киты с недельным результатом mu + шум. persistent=False — у каждого кита «форма» меняется каждую
    неделю случайно (прошлое ничего не говорит о будущем)."""
    rng = np.random.default_rng(seed)
    out = []
    start = NOW - weeks * WEEK
    for i, mu in enumerate(mus):
        series, buys, cum = [], [], 0.0
        level = mu
        for d in range(weeks * 7 + 1):
            t = start + d * DAY
            if not persistent and d % 7 == 1:  # новая «форма» с первого дня каждой недели, независимо от прошлой
                level = rng.normal(0, 1500)
            cum += (level / 7) + rng.normal(0, noise / np.sqrt(7))
            series.append((t, round(cum, 2)))
            buys.append((t - 3600, vol_per_day, f"m{i}-{d}"))
        out.append(Whale(entity_id=f"0x{i:040x}", name=f"кит{i}", series=series, buys=buys))
    return out


def test_persistent_form_is_detected_and_rotation_helps():
    whales = make_whales([1500, 1200, 900, -900, -1200, -1500, 300, -300])
    r = simulate_rotation(whales, FormRule(window_weeks=6, min_bets=5), now=NOW)
    assert r["ok"] and r["verdict"]["holds"] is True and r["persistence"]["rho"] > 0.5
    assert r["persistence"]["p_value"] < 0.01
    t = r["total_pct"]
    assert t["rotation"] > t["all"] and t["rotation"] > t["random_median"]
    assert t["ideal"] >= t["rotation"]  # идеал задним числом — потолок
    assert r["hit_rate"]["in_form"] > r["hit_rate"]["not_in_form"]
    now_in = {c["name"] for c in r["current"] if c["in_form"]}
    assert {"кит0", "кит1", "кит2"} <= now_in and not ({"кит3", "кит4", "кит5"} & now_in)


def test_no_persistence_is_said_plainly():
    whales = make_whales([0.0] * 10, persistent=False, noise=100.0, seed=5)
    r = simulate_rotation(whales, FormRule(window_weeks=6, min_bets=5), now=NOW)
    assert r["ok"] and r["persistence"]["pairs"] >= 30
    assert r["verdict"]["holds"] is False
    assert "не держится" in r["verdict"]["title"] or "Наоборот" in r["verdict"]["title"]


def test_ideal_is_never_below_rotation_week_by_week():
    whales = make_whales([800, -200, 100, -600, 400], seed=9, noise=900)
    r = simulate_rotation(whales, FormRule(window_weeks=4, min_bets=1), now=NOW)
    rot, ideal = np.array(r["curves"]["rotation"]), np.array(r["curves"]["ideal"])
    assert np.all(np.diff(ideal) >= np.diff(rot) - 0.02)  # кривые округлены до сотых процента


def test_decision_uses_only_past_data():
    """Решение «кто в форме» в момент T не меняется, если всё, что было после T, заменить чем угодно."""
    whales = make_whales([1500, -1500, 300, -300, 900], seed=3)
    rule = FormRule(window_weeks=6, min_bets=5)
    T = NOW - 10 * WEEK
    full = simulate_rotation(whales, rule, now=NOW)
    at_T = next(w for w in full["history"] if w["ts"] == T)
    rng = np.random.default_rng(0)
    future_changed = []
    for w in whales:
        series = [(t, v) if t <= T else (t, v + rng.normal(0, 1e5)) for t, v in w.series]
        buys = [b for b in w.buys if b[0] <= T] + [(t, 5e4, "x") for t in range(T + 3600, NOW, 2 * DAY)]
        future_changed.append(Whale(w.entity_id, w.name, series, buys))
    other = simulate_rotation(future_changed, rule, now=NOW)
    assert next(w for w in other["history"] if w["ts"] == T)["in_form"] == at_T["in_form"]
    cut = [
        Whale(w.entity_id, w.name, [p for p in w.series if p[0] <= T], [b for b in w.buys if b[0] <= T]) for w in whales
    ]
    live = simulate_rotation(cut, rule, now=T)  # «сейчас» = T: только прошлое
    assert sorted(c["name"] for c in live["current"] if c["in_form"]) == sorted(at_T["in_form"])


def test_strict_rule_means_cash():
    whales = make_whales([1500, 1200, -900], seed=2)
    r = simulate_rotation(whales, FormRule(window_weeks=6, min_bets=10_000), now=NOW)
    assert r["weeks_in_cash"] == r["weeks"] and r["total_pct"]["rotation"] == 0.0


def test_short_history_and_merge():
    short = make_whales([100], weeks=3)
    assert simulate_rotation(short, FormRule(window_weeks=8), now=NOW)["ok"] is False
    a = [(10, 1.0), (30, 3.0)]
    b = [(20, 10.0), (30, 20.0)]
    assert merge_series([a, b]) == [(10, 1.0), (20, 11.0), (30, 23.0)]
    assert FormRule.from_dict({"window_weeks": 999, "max_drawdown_share": 5}).window_weeks == 52


class _Players:
    def __init__(self, whales):
        self.by_wallet = {w.entity_id: w.series for w in whales}
        self.cards = {}

    def pnl_series(self, wallet):
        return self.by_wallet.get(wallet, [])


def test_rotation_service_and_saved_rule(db):
    whales = make_whales([1500, 1200, -900, -1200], seed=4)
    for w in whales:
        db.upsert(
            "watchlist",
            {"entity_id": w.entity_id, "mode": "watch", "added_by": "user", "added_ts": 1, "categories_off": "[]"},
            ["entity_id"],
        )
        db.insert_ignore(
            "trades",
            [
                {
                    "trade_id": f"{w.entity_id}{i}",
                    "ts": t,
                    "ts_fetched": t,
                    "wallet": w.entity_id,
                    "token_id": m + "y",
                    "market_id": m,
                    "side": "BUY",
                    "price": 0.5,
                    "size_shares": usd * 2,
                    "size_usd": usd,
                    "is_maker": None,
                    "tx_hash": None,
                }
                for i, (t, usd, m) in enumerate(w.buys)
            ],
        )
    svc = Services(db, fast_params(), clock=lambda: NOW)
    svc.players = _Players(whales)
    r = svc.rotation({"window_weeks": 6})
    assert r["ok"] and r["pool"] == 4 and r["rule"]["window_weeks"] == 6 and r["whales"] == 4
    assert r["verdict"]["title"] and len(r["curves"]["rotation"]) == len(r["curves"]["ts"])
    assert svc.rotation()["rule"]["window_weeks"] == FormRule().window_weeks  # примерка не сохраняется
    saved = svc.rotation({"window_weeks": 5, "min_bets": 3}, save=True)
    assert saved["rule"]["window_weeks"] == 5 and svc.rotation()["rule"]["min_bets"] == 3


@pytest.mark.parametrize("bad", [{"window_weeks": "x"}])
def test_rule_from_bad_input(bad):
    with pytest.raises(ValueError):
        FormRule.from_dict(bad)


def test_verdict_is_calibrated_on_random_worlds():
    """В мире без устойчивой формы вывод «форма держится» — редкая случайность (ожидается ~5%), а не правило.
    Раньше пересекающиеся окна давали ложную связь в 15–20% миров."""
    hits = 0
    for seed in range(40):
        w = make_whales([0.0] * 10, persistent=False, noise=100.0, seed=seed)
        hits += (
            simulate_rotation(w, FormRule(window_weeks=6, min_bets=5), now=NOW, n_random=10)["verdict"]["holds"] is True
        )
    assert hits <= 4
