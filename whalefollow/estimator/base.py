"""Независимая оценка вероятности (ТЗ раздел 9): интерфейс и простые реализации."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Protocol


@dataclass
class Estimate:
    q: float
    confidence: str = "low"  # low | medium | high
    base_rate: float | None = None
    arguments_for: list[str] = field(default_factory=list)
    arguments_against: list[str] = field(default_factory=list)
    key_uncertainties: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    model_version: str = "none"
    blind: bool = True  # рыночная цена и ставка кита модели не показывались

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MarketContext:
    """Что видит оценщик. Намеренно без цены рынка и без ставки кита (9.2)."""

    market_id: str
    question: str
    rules: str
    end_date: str
    category: str
    materials: list[dict] = field(default_factory=list)  # [{source, source_type, ts_pub, title, url}]


class Estimator(Protocol):
    name: str

    def estimate(self, ctx: MarketContext) -> Estimate | None: ...


class NoEstimator:
    """Режим «без LLM» (до этапа 7): решение принимается только по киту."""

    name = "none"

    def estimate(self, ctx: MarketContext) -> Estimate | None:
        return None


class FunctionEstimator:
    """Оценщик из функции — для тестов и бэктеста с заранее известными оценками."""

    name = "function"

    def __init__(self, fn: Callable[[MarketContext], float | None], confidence: str = "medium"):
        self.fn = fn
        self.confidence = confidence

    def estimate(self, ctx: MarketContext) -> Estimate | None:
        q = self.fn(ctx)
        return None if q is None else Estimate(q=float(q), confidence=self.confidence, model_version="function")
