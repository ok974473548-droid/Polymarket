"""Пример плагинов WhaleFollow — по одному каждого вида. Всё в одном файле, ядро править не нужно.

Как подключить: скопируйте этот файл в папку плагинов и перезапустите приложение:
  * Docker: /data/plugins/ (это том с базой: docker compose cp example_plugins.py api:/data/plugins/);
  * без Docker: папка plugins/ рядом с местом запуска, или любая папка из WF_PLUGINS_DIR.
Плагины появятся на экране «Настройки» → «Плагины»; там же их включают и видят ошибки.

Плагин — обычный класс-наследник SourcePlugin / NotifierPlugin / StrategyPlugin с уникальным name.
Файл должен объявить список PLUGINS. Ошибки внутри плагина не роняют приложение: они видны на экране.
"""

from __future__ import annotations

import time
from pathlib import Path

from whalefollow.estimator.sources import RssSource
from whalefollow.plugins import Candidate, Notice, NotifierPlugin, PluginContext, SourcePlugin, StrategyPlugin, Verdict


class MyRssNews(SourcePlugin):
    """Источник: новости из своей RSS-ленты (адрес — в переменной окружения EXAMPLE_RSS_URL)."""

    name = "example_rss"
    title = "Моя RSS-лента (пример)"
    description = "Статьи из ленты EXAMPLE_RSS_URL, где встречаются слова вопроса рынка"
    default_enabled = False
    requires_env = ("EXAMPLE_RSS_URL",)

    def news(self, ctx: PluginContext, query: str, since_ts: int) -> list:
        items = RssSource("my_rss", ctx.env("EXAMPLE_RSS_URL"), source_type="media").search(query, since_ts)
        words = {w for w in query.lower().split() if len(w) > 3}
        return [it for it in items if words & set(it.title.lower().split())]


class JournalFile(NotifierPlugin):
    """Уведомления: дописывать каждое уведомление строкой в текстовый файл (EXAMPLE_JOURNAL, по умолчанию notices.txt)."""

    name = "example_journal"
    title = "Журнал в файл (пример)"
    description = "Каждое уведомление — строка в текстовом файле"
    default_enabled = False

    def send(self, ctx: PluginContext, notice: Notice) -> None:
        path = Path(ctx.env("EXAMPLE_JOURNAL", "notices.txt"))
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(notice.ts or time.time()))
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{stamp}\t{notice.kind}\t{notice.title}\t{notice.body}\t{notice.url}\n")


class BigBetsOnly(StrategyPlugin):
    """Стратегия: брать только крупные покупки кита — от $5 000 (кит уверен и ставит много)."""

    name = "example_big_bets"
    title = "Только крупные ставки (пример)"
    description = "Сигнал, только если кит купил минимум на $5 000"
    min_usd = 5000.0

    def accept(self, c: Candidate) -> Verdict:
        if c.size_usd >= self.min_usd:
            return Verdict(True, f"крупная покупка: ${c.size_usd:,.0f}".replace(",", " "))
        return Verdict(False, f"покупка ${c.size_usd:,.0f} меньше порога ${self.min_usd:,.0f}".replace(",", " "))


PLUGINS = [MyRssNews(), JournalFile(), BigBetsOnly()]
