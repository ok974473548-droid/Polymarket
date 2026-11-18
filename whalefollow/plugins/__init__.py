"""Плагины: источники данных, уведомления, стратегии. Как добавить свой — см. README («Плагины»).

Это модуль-«клей» между ядром и плагинами: ядро вызывает функции отсюда и не знает, какие плагины есть.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from .base import (
    KIND_TITLES,
    KINDS,
    Candidate,
    ComponentEstimate,
    EstimatorPlugin,
    EventInfo,
    Evidence,
    Notice,
    NotifierPlugin,
    OutcomeInfo,
    Plugin,
    PluginContext,
    SourcePlugin,
    StrategyPlugin,
    Verdict,
)
from .registry import record, registry, reset_registry, status

log = logging.getLogger(__name__)

__all__ = [
    "KINDS",
    "KIND_TITLES",
    "Candidate",
    "ComponentEstimate",
    "EstimatorPlugin",
    "EventInfo",
    "Evidence",
    "OutcomeInfo",
    "Notice",
    "NotifierPlugin",
    "Plugin",
    "PluginContext",
    "SourcePlugin",
    "StrategyPlugin",
    "Verdict",
    "context",
    "record",
    "registry",
    "reset_registry",
    "research_sources",
    "status",
    "sync_jobs",
    "user_settings",
]


def user_settings(db: Any) -> dict:
    """Настройки пользователя поверх значений по умолчанию (как RiskManager.settings)."""
    from ..config import DEFAULT_SETTINGS

    s = dict(DEFAULT_SETTINGS)
    for k, base in DEFAULT_SETTINGS.items():
        v = db.get_setting(k)
        if v is not None:
            s[k] = {**base, **v} if isinstance(v, dict) and isinstance(base, dict) else v
    return s


def context(db: Any, settings: dict | None = None, clients: dict | None = None) -> PluginContext:
    return PluginContext(db=db, settings=settings if settings is not None else user_settings(db), clients=clients or {})


# ---------------------------------------------------------------- источники → сбор материалов по сигналу
class _News:
    """Обёртка плагина под старый интерфейс сбора (search) + запись состояния."""

    def __init__(self, p: SourcePlugin, ctx: PluginContext):
        self.p, self.ctx = p, ctx

    def search(self, query: str, since_ts: int) -> list:
        try:
            out = list(self.p.news(self.ctx, query, since_ts) or [])
        except Exception as e:
            record(self.ctx.db, self.p.name, ok=False, error=f"{type(e).__name__}: {e}")
            raise
        record(self.ctx.db, self.p.name, ok=True, note=f"найдено: {len(out)}")
        return out


class _Forecasts:
    needs_market = True

    def __init__(self, p: SourcePlugin, ctx: PluginContext):
        self.p, self.ctx = p, ctx

    def forecasts(self, question: str, market: dict | None = None) -> list:
        try:
            out = list(self.p.forecasts(self.ctx, question, market) or [])
        except Exception as e:
            record(self.ctx.db, self.p.name, ok=False, error=f"{type(e).__name__}: {e}")
            raise
        record(self.ctx.db, self.p.name, ok=True, note=f"найдено: {len(out)}")
        return out


def research_sources(db: Any, settings: dict | None = None) -> tuple[list, list]:
    """(источники новостей, источники прогнозов) — включённые и доступные плагины-источники."""
    ctx = context(db, settings)
    news: list = []
    fc: list = []
    for p in registry().active("source", ctx):
        assert isinstance(p, SourcePlugin)
        if p.gives_news:
            news.append(_News(p, ctx))
        if p.gives_forecasts:
            fc.append(_Forecasts(p, ctx))
    return news, fc


def sync_jobs(db: Any, clients: dict | None = None) -> list[tuple[str, float, Callable[[], Any]]]:
    """Фоновые задачи источников: (имя задачи, интервал, функция). Выключенный плагин задачу пропускает."""
    out: list[tuple[str, float, Callable[[], Any]]] = []
    for p in registry().of_kind("source"):
        assert isinstance(p, SourcePlugin)
        if not p.sync_interval_s:
            continue

        def run(p: SourcePlugin = p) -> Any:
            ctx = context(db, clients=clients)
            if not registry().is_enabled(p, ctx.settings) or p.available(ctx) is not None:
                return None
            try:
                n = p.sync(ctx)
            except Exception as e:
                record(db, p.name, ok=False, error=f"обновление кэша: {type(e).__name__}: {e}")
                raise
            if n is not None:
                record(db, p.name, ok=True, note=f"обновлено записей: {n}")
            return n

        out.append((p.job_name or f"plugin:{p.name}", float(p.sync_interval_s), run))
    return out
