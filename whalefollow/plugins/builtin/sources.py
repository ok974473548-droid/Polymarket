"""Встроенные источники данных — те же, что были, но как плагины.

Включение хранится там же, где раньше (настройка `news_sources`), поэтому настройки пользователя не меняются.
"""

from __future__ import annotations

from ...estimator import external as ext
from ...estimator import sources as src
from ...tracked import tracked_entities
from ..base import PluginContext, SourcePlugin


class GoogleNews(SourcePlugin):
    name = "google_news"
    title = "Новости (Google News)"
    description = "Свежие статьи по вопросу рынка"
    setting = ("news_sources", "google_news")

    def news(self, ctx: PluginContext, query: str, since_ts: int) -> list:
        return src.GoogleNewsSource().search(query, since_ts)


class Reddit(SourcePlugin):
    name = "reddit"
    title = "Соцсети (Reddit)"
    description = "Обсуждения за неделю — как повод проверить, не как доказательство"
    default_enabled = False
    setting = ("news_sources", "reddit")

    def news(self, ctx: PluginContext, query: str, since_ts: int) -> list:
        return src.RedditSource().search(query, since_ts)


class TelegramChannels(SourcePlugin):
    name = "telegram_channels"
    title = "Telegram-каналы"
    description = "Публичные каналы из настроек; сообщения помечаются как непроверенные"

    def available(self, ctx: PluginContext) -> str | None:
        return (
            None
            if ctx.settings.get("telegram_channels")
            else "Каналы не заданы — добавьте их в «Настройки» → «Источники данных»"
        )

    def news(self, ctx: PluginContext, query: str, since_ts: int) -> list:
        return src.TelegramChannelSource(list(ctx.settings.get("telegram_channels") or [])).search(query, since_ts)


class Manifold(SourcePlugin):
    name = "manifold"
    title = "Другие рынки прогнозов (Manifold)"
    description = "Вероятности по похожим вопросам"
    setting = ("news_sources", "manifold")

    def forecasts(self, ctx: PluginContext, question: str, market: dict | None = None) -> list:
        return src.ManifoldSource().forecasts(question)


class Kalshi(SourcePlugin):
    name = "kalshi"
    title = "Kalshi"
    description = (
        "Цены похожих вопросов на второй крупной бирже прогнозов (кэш открытых рынков обновляется раз в 30 мин)"
    )
    setting = ("news_sources", "kalshi")
    sync_interval_s = 1800
    job_name = "kalshi"

    def forecasts(self, ctx: PluginContext, question: str, market: dict | None = None) -> list:
        return ext.KalshiSource(ctx.db).forecasts(question)

    def sync(self, ctx: PluginContext) -> int | None:
        client = ctx.clients.get("kalshi")  # клиент даёт процесс, где работают фоновые задачи (worker)
        if client is None or not tracked_entities(ctx.db):
            return None  # китов нет — сигналов не будет, кэш не нужен
        return ext.sync_kalshi(ctx.db, client)


class Bookmakers(SourcePlugin):
    name = "bookmakers"
    title = "Букмекеры (спорт)"
    description = "Вероятности по коэффициентам без маржи букмекера (The Odds API)"
    setting = ("news_sources", "bookmakers")
    requires_env = ("WF_ODDS_API_KEY",)
    job_name = "odds"

    @property
    def sync_interval_s(self) -> float:  # type: ignore[override]
        import os

        return 3600 * float(os.environ.get("WF_ODDS_INTERVAL_H") or 12)

    def forecasts(self, ctx: PluginContext, question: str, market: dict | None = None) -> list:
        return ext.BookmakerSource(ctx.db).forecasts(question, market=market)

    def sync(self, ctx: PluginContext) -> int | None:
        client = ctx.clients.get("odds")  # создаётся в worker из WF_ODDS_API_KEY
        if client is None or not tracked_entities(ctx.db):
            return None
        return ext.sync_odds(ctx.db, client)


PLUGINS = [GoogleNews(), Reddit(), TelegramChannels(), Manifold(), Kalshi(), Bookmakers()]
