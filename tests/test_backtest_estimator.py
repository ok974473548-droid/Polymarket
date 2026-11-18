import numpy as np
import pytest

from conftest import fast_params
from whalefollow.backtest.engine import Frames, run_backtest, walk_forward
from whalefollow.estimator.base import MarketContext
from whalefollow.estimator.calibration import Calibrator, isotonic_fit
from whalefollow.estimator.llm import build_prompt, parse_reply
from whalefollow.estimator.sources import manipulation_flags

DAY = 86400


def test_positions_at_hides_future(scored_world):
    world, db, _, _ = scored_world
    fr = Frames(db)
    at = world.now - 60 * DAY
    pos = fr.positions_at(at)
    assert pos["opened_ts"].max() <= at
    resolved_later = pos[(pos["resolved_ts"].isna()) & (pos["status"] == "resolved")]
    assert resolved_later.empty
    assert (pos.loc[pos["status"] == "resolved", "resolved_ts"] <= at).all()


def test_backtest_report_shape(scored_world):
    world, db, params, _ = scored_world
    res = run_backtest(db, params, world.now - 120 * DAY, world.now, delays=(5, 30))
    assert set(res) >= {"strategy", "baseline_top_pnl", "baseline_random", "verdict", "params_version"}
    s30 = res["strategy"].get(30, {})
    assert s30.get("n", 0) > 0 and "roi_ci90" in s30 and "by_bucket" in s30
    assert s30["optimistic_share"] == 1.0          # снимков стакана нет → результат помечен как оптимистичный


def test_walk_forward_runs(scored_world):
    world, db, _, _ = scored_world
    res = walk_forward(db, fast_params(bootstrap_b=300, null_sims=500), world.now - 150 * DAY,
                       world.now - 90 * DAY, step_days=60)
    assert res["folds"] and "selection_works" in res


def test_isotonic_monotone_and_calibrator():
    rng = np.random.default_rng(0)
    q = rng.uniform(0, 1, 500)
    y = (rng.random(500) < q).astype(float)
    x, v = isotonic_fit(q, y)
    assert np.all(np.diff(v) >= -1e-12)
    cal = Calibrator().fit(q, y, p_market=np.clip(q + rng.normal(0, 0.2, 500), 0, 1))
    assert cal.trusted(300)                       # оценка лучше «зашумлённого рынка»
    assert not Calibrator().fit(q[:50], y[:50], q[:50]).trusted(300)


def test_llm_prompt_is_blind_and_reply_parsed():
    ctx = MarketContext("m", "Will X happen?", "Resolves YES if X.", "2026-12-31", "politics",
                        [{"source": "Reuters", "source_type": "media", "ts_pub": 1, "title": "X likely", "url": "u"}])
    prompt = build_prompt(ctx)
    assert "Reuters" in prompt and "0.55" not in prompt and "¢" not in prompt
    est = parse_reply('Here: {"base_rate": 0.3, "q": 0.42, "confidence": "medium", "arguments_for": ["a"]}', "m1")
    assert est.q == pytest.approx(0.42) and est.confidence == "medium" and est.blind
    assert parse_reply("no json", "m1") is None
    assert parse_reply('{"q": 7}', "m1") is None


def test_manipulation_flags():
    items = [{"source": s, "source_type": t, "ts_pub": 100, "text_hash": "h", "title": "x", "url": "", "source_weight": 0.2}
             for s, t in (("tg1", "telegram"), ("tg2", "telegram"), ("tg3", "telegram"))]
    f = manipulation_flags(items, [(50, 0.40), (90, 0.50), (200, 0.52)], now=300)
    assert f["coordinated_text"] and f["news_after_price_move"] and not f["enough_confirmation"]
