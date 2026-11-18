"""Статистика скоринга (ТЗ 6.3–6.4): сжатие, bootstrap, тест «навык или везение», BH."""
from __future__ import annotations

import numpy as np


def shrink(edge: float, n: int, k: float) -> float:
    """Байесовское сжатие к нулю: чем меньше данных, тем меньше доверия."""
    if n <= 0 or not np.isfinite(edge):
        return 0.0
    return n / (n + k) * edge


def wmean(x: np.ndarray, w: np.ndarray) -> float:
    m = np.isfinite(x) & np.isfinite(w) & (w > 0)
    if not m.any():
        return float("nan")
    return float(np.sum(x[m] * w[m]) / np.sum(w[m]))


def bootstrap_wmean(x: np.ndarray, w: np.ndarray, b: int, rng: np.random.Generator,
                    k: float | None = None, q_lo: float = 5, q_hi: float = 95,
                    chunk: int = 1000) -> tuple[float, float]:
    """Перцентильный ДИ средневзвешенного по пересборкам кластеров (с повторами).

    Если задан k, к каждой пересборке применяется сжатие n/(n+k) — получаем ДИ E_shrunk.
    """
    m = np.isfinite(x) & np.isfinite(w) & (w > 0)
    x, w = np.asarray(x, float)[m], np.asarray(w, float)[m]
    n = len(x)
    if n == 0:
        return float("nan"), float("nan")
    if n == 1:
        v = x[0] * (n / (n + k) if k else 1.0)
        return float(v), float(v)
    out = np.empty(b)
    chunk = max(1, min(chunk, max(1, 20_000_000 // max(n, 1))))
    for s in range(0, b, chunk):
        size = min(chunk, b - s)
        idx = rng.integers(0, n, (size, n))
        ws = w[idx]
        out[s:s + size] = (x[idx] * ws).sum(1) / ws.sum(1)
    if k:
        out *= n / (n + k)
    lo, hi = np.percentile(out, [q_lo, q_hi])
    return float(lo), float(hi)


def null_pvalue(p: np.ndarray, r: np.ndarray, w: np.ndarray, sims: int, rng: np.random.Generator,
                chunk: int = 1000) -> float:
    """Тест «навык или везение» (6.4).

    H0: рынок откалиброван, исход позиции i ~ Bernoulli(p_i). Статистика
    T = Σ w_i (R_i − p_i). p-value = (1 + #{T* ≥ T_obs}) / (1 + sims).
    """
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    r = np.asarray(r, float)
    w = np.asarray(w, float)
    m = np.isfinite(p) & np.isfinite(r) & np.isfinite(w) & (w > 0)
    p, r, w = p[m], r[m], w[m]
    n = len(p)
    if n == 0:
        return 1.0
    t_obs = float(np.sum(w * (r - p)))
    count = 0
    chunk = max(1, min(chunk, max(1, 20_000_000 // n)))
    for s in range(0, sims, chunk):
        size = min(chunk, sims - s)
        y = rng.random((size, n)) < p
        t = ((y - p) * w).sum(1)
        count += int(np.sum(t >= t_obs - 1e-12))
    return (1 + count) / (1 + sims)


def bh_qvalues(pvals) -> np.ndarray:
    """q-value Бенджамини–Хохберга (контроль FDR при множественных проверках)."""
    p = np.asarray(pvals, float)
    n = len(p)
    if n == 0:
        return p
    order = np.argsort(p)
    ranked = p[order] * n / np.arange(1, n + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(q, 1.0)
    return out


def zscore(x) -> np.ndarray:
    x = np.asarray(x, float)
    sd = np.nanstd(x)
    if not np.isfinite(sd) or sd == 0:
        return np.zeros_like(x)
    return (x - np.nanmean(x)) / sd


def brier(q, y) -> float:
    q, y = np.asarray(q, float), np.asarray(y, float)
    return float(np.mean((q - y) ** 2)) if len(q) else float("nan")
