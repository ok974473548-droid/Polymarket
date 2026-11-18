"""Калибровка оценок (ТЗ 9.5): Brier, изотоническая регрессия, допуск LLM к решению."""

from __future__ import annotations

import numpy as np

from ..db import DB
from ..scoring.stats import brier


def isotonic_fit(q, y) -> tuple[np.ndarray, np.ndarray]:
    """Pool-adjacent-violators: монотонное отображение q → частота исхода."""
    q, y = np.asarray(q, float), np.asarray(y, float)
    order = np.argsort(q)
    xs, ys = q[order], y[order]
    blocks = [[ys[i], 1.0, xs[i], xs[i]] for i in range(len(ys))]  # mean, weight, x_lo, x_hi
    merged: list[list[float]] = []
    for b in blocks:
        merged.append(b)
        while len(merged) > 1 and merged[-2][0] > merged[-1][0]:
            m2, w2, lo2, hi2 = merged.pop()
            m1, w1, lo1, _ = merged.pop()
            merged.append([(m1 * w1 + m2 * w2) / (w1 + w2), w1 + w2, lo1, hi2])
    x = np.array([(b[2] + b[3]) / 2 for b in merged])
    v = np.array([b[0] for b in merged])
    return x, v


class Calibrator:
    def __init__(self):
        self.x: np.ndarray | None = None
        self.v: np.ndarray | None = None
        self.n = 0
        self.brier_llm = float("nan")
        self.brier_market = float("nan")
        self.brier_llm_common = float("nan")
        self.n_common = 0

    def fit(self, q, y, p_market=None) -> Calibrator:
        q, y = np.asarray(q, float), np.asarray(y, float)
        self.n = len(q)
        if self.n >= 20:
            self.x, self.v = isotonic_fit(q, y)
        self.brier_llm = brier(q, y)
        if p_market is not None:
            # сравнение с рынком — только на тех оценках, где известна рыночная цена (на одном наборе)
            pm = np.array([np.nan if v is None else v for v in p_market], float)
            m = np.isfinite(pm)
            if m.sum() >= 1:
                self.brier_llm_common = brier(q[m], y[m])
                self.brier_market = brier(pm[m], y[m])
                self.n_common = int(m.sum())
        return self

    def apply(self, q: float) -> float:
        if self.x is None or len(self.x) < 2:
            return q
        return float(np.clip(np.interp(q, self.x, self.v), 0.01, 0.99))

    def trusted(self, min_n: int) -> bool:
        """LLM может влиять на вероятность, только если она лучше рынка (Brier) на ≥ min_n рынках."""
        return self.n_common >= min_n and np.isfinite(self.brier_market) and self.brier_llm_common < self.brier_market


def load_calibrator(db: DB) -> Calibrator:
    rows = db.query("SELECT q, p_market, y FROM calibration_log WHERE y IS NOT NULL")
    if not rows:
        return Calibrator()
    q = [r["q"] for r in rows]
    y = [r["y"] for r in rows]
    pm = [r["p_market"] for r in rows]
    return Calibrator().fit(q, y, pm)


def record_outcomes(db: DB) -> int:
    """Дописывает исходы в calibration_log для разрешённых рынков."""
    rows = db.query("SELECT c.est_id, c.market_id, c.q, c.p_market FROM calibration_log c WHERE c.y IS NULL")
    n = 0
    for r in rows:
        tok = db.one(
            "SELECT payout FROM tokens WHERE market_id=? AND outcome_label IN ('Yes','YES','yes') "
            "AND payout IS NOT NULL",
            (r["market_id"],),
        )
        if tok is None or tok["payout"] not in (0, 1, 0.0, 1.0):
            continue  # «50 на 50» — не исход «да/нет», в калибровку не идёт
        y = int(tok["payout"] >= 0.5)
        db.execute(
            "UPDATE calibration_log SET y=?, brier=?, brier_market=? WHERE est_id=?",
            (y, (r["q"] - y) ** 2, None if r["p_market"] is None else (r["p_market"] - y) ** 2, r["est_id"]),
        )
        n += 1
    return n
