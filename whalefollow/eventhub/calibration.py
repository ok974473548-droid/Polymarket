"""Поправка цены Polymarket по истории: «при цене 10¢ исход на деле сбывался в 7% случаев».

Кривая строится по закрытым рынкам: цена «Да» за N дней до закрытия → сбылось ли. В каждой корзине цен частота
сжимается к самой цене (мало примеров — почти без поправки), затем кривая делается монотонной (PAV). Кривые —
по сроку до закрытия (≤2 дней, ≤14 дней, дольше) и по теме, если по теме хватает примеров; иначе общая.

Базовые кривые (`calibration_baseline.json`) сняты по закрытым рынкам Polymarket скриптом
`scripts/fit_event_calibration.py`; там же — проверка «вне выборки» (подгонка на ранних рынках, проверка на поздних).
"""

from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EDGES = [0.0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 1.0]
SHRINK_K = 40.0  # сила сжатия к диагонали: столько «виртуальных» примеров без поправки на корзину
MIN_TOPIC_N = 400  # кривая по теме — если по теме не меньше стольких примеров на этот срок
HORIZONS = [(2, "≤2 дн"), (14, "≤14 дн"), (10**6, "дольше")]
BASELINE = Path(__file__).with_name("calibration_baseline.json")


def horizon_of(days_left: float) -> int:
    for i, (lim, _) in enumerate(HORIZONS):
        if days_left <= lim:
            return i
    return len(HORIZONS) - 1


def _pav(xs: list[float], ws: list[float]) -> list[float]:
    """Монотонная (неубывающая) регрессия — пул соседних нарушителей."""
    blocks: list[list[float]] = [[x, w, 1.0] for x, w in zip(xs, ws, strict=True)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0] + 1e-12:
            a, b = blocks[i], blocks[i + 1]
            w = a[1] + b[1]
            blocks[i] = [(a[0] * a[1] + b[0] * b[1]) / w, w, a[2] + b[2]]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    out: list[float] = []
    for v, _, n in blocks:
        out += [v] * int(n)
    return out


@dataclass
class Curve:
    xs: list[float] = field(default_factory=list)  # средняя цена в корзине
    ys: list[float] = field(default_factory=list)  # частота исхода (сжатая, монотонная)
    n: int = 0

    @classmethod
    def fit(cls, samples: list[tuple[float, int]], k: float = SHRINK_K) -> Curve:
        bins: list[list[tuple[float, int]]] = [[] for _ in range(len(EDGES) - 1)]
        for p, y in samples:
            if 0.0 <= p <= 1.0:
                bins[min(len(bins) - 1, max(0, bisect_right(EDGES, p) - 1))].append((p, int(y)))
        xs, ys, ws = [], [], []
        for b in bins:
            if not b:
                continue
            n = len(b)
            x = sum(p for p, _ in b) / n
            freq = sum(y for _, y in b) / n
            xs.append(x)
            ys.append((n * freq + k * x) / (n + k))
            ws.append(n + k)
        return cls(xs, _pav(ys, ws) if ys else [], len(samples))

    def apply(self, p: float) -> float:
        if not self.xs or p is None:
            return p
        p = min(max(float(p), 0.0), 1.0)
        # концы: от 0 и 1 к крайним корзинам — линейно (цена 0 и 1 остаются 0 и 1)
        xs = [0.0, *self.xs, 1.0]
        ys = [0.0, *self.ys, 1.0]
        i = min(max(bisect_right(xs, p) - 1, 0), len(xs) - 2)
        x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
        return y0 if x1 <= x0 else y0 + (y1 - y0) * (p - x0) / (x1 - x0)

    def to_json(self) -> dict:
        return {"xs": [round(x, 5) for x in self.xs], "ys": [round(y, 5) for y in self.ys], "n": self.n}

    @classmethod
    def from_json(cls, d: dict) -> Curve:
        return cls(list(d.get("xs") or []), list(d.get("ys") or []), int(d.get("n") or 0))


class Calibration:
    """Набор кривых: {(тема | "all", срок): Curve}."""

    def __init__(self, curves: dict[str, Curve] | None = None, meta: dict | None = None):
        self.curves = curves or {}
        self.meta = meta or {}

    @staticmethod
    def key(topic: str, h: int) -> str:
        return f"{topic}|{h}"

    def curve(self, topic: str, days_left: float) -> tuple[Curve | None, str]:
        h = horizon_of(days_left)
        c = self.curves.get(self.key(topic, h))
        if c is not None and c.n >= MIN_TOPIC_N:
            return c, f"{topic}, {HORIZONS[h][1]}"
        c = self.curves.get(self.key("all", h))
        return c, f"все темы, {HORIZONS[h][1]}"

    def apply(self, p: float, topic: str, days_left: float) -> tuple[float, str]:
        c, label = self.curve(topic, days_left)
        return (c.apply(p), label) if c is not None else (p, "без поправки")

    @classmethod
    def fit(cls, samples: list[dict]) -> Calibration:
        """samples: {"p", "y", "topic", "days_left"} — цена за days_left дней до закрытия и исход (0/1)."""
        groups: dict[str, list[tuple[float, int]]] = {}
        for s in samples:
            h = horizon_of(float(s["days_left"]))
            for t in ("all", s.get("topic") or "other"):
                groups.setdefault(cls.key(t, h), []).append((float(s["p"]), int(s["y"])))
        return cls({k: Curve.fit(v) for k, v in groups.items()})

    def to_json(self) -> dict:
        return {"meta": self.meta, "curves": {k: c.to_json() for k, c in self.curves.items()}}

    @classmethod
    def from_json(cls, d: dict) -> Calibration:
        return cls({k: Curve.from_json(v) for k, v in (d.get("curves") or {}).items()}, d.get("meta") or {})


_BASE: Calibration | None = None


def baseline() -> Calibration:
    global _BASE
    if _BASE is None:
        try:
            _BASE = Calibration.from_json(json.loads(BASELINE.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            _BASE = Calibration()
    return _BASE


def brier(pairs: list[tuple[float, int]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")


def logloss(pairs: list[tuple[float, int]], eps: float = 1e-4) -> float:
    s = 0.0
    for p, y in pairs:
        p = min(max(p, eps), 1 - eps)
        s -= y * math.log(p) + (1 - y) * math.log(1 - p)
    return s / len(pairs) if pairs else float("nan")


def evaluate(train: list[dict], test: list[dict]) -> dict[str, Any]:
    """Подгонка на train, проверка на test: Brier и log-loss «цена как есть» против «цена с поправкой»."""
    cal = Calibration.fit(train)
    out: dict[str, Any] = {}
    groups: dict[str, list[dict]] = {"all": test}
    for s in test:
        groups.setdefault(f"h:{HORIZONS[horizon_of(s['days_left'])][1]}", []).append(s)
        groups.setdefault(f"t:{s.get('topic')}", []).append(s)
    for g, rows in groups.items():
        raw = [(float(s["p"]), int(s["y"])) for s in rows]
        adj = [(cal.apply(float(s["p"]), s.get("topic") or "other", s["days_left"])[0], int(s["y"])) for s in rows]
        out[g] = {
            "n": len(rows),
            "brier_raw": round(brier(raw), 5),
            "brier_cal": round(brier(adj), 5),
            "logloss_raw": round(logloss(raw), 5),
            "logloss_cal": round(logloss(adj), 5),
        }
    return out
