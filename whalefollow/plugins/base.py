"""Интерфейс плагинов WhaleFollow: три вида — источники, уведомления, стратегии.

Плагин — объект класса-наследника `SourcePlugin`, `NotifierPlugin` или `StrategyPlugin` с уникальным `name`.
Ядро само находит плагины (см. `registry.py`): встроенные, пакеты с entry point `whalefollow.plugins`
и файлы `*.py` в папке плагинов. Ничего в ядре править не нужно.

Общее для всех видов:
  * `name` — ключ (латиница, без пробелов); `title`, `description` — для экрана «Плагины»;
  * `default_enabled` — включён ли плагин, пока пользователь его не переключал;
  * `requires_env` — переменные окружения, без которых плагин недоступен (например, ключ API);
  * `available(ctx)` — None, если работать можно, иначе понятная причина «почему нет».
Ошибки плагина не роняют систему: ядро ловит исключения, показывает их на экране «Плагины» и продолжает.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

KINDS = ("source", "notifier", "strategy")
KIND_TITLES = {"source": "Источники данных", "notifier": "Уведомления", "strategy": "Стратегии"}


@dataclass
class PluginContext:
    """Что плагин получает от ядра: база, настройки пользователя и общие клиенты (могут отсутствовать)."""

    db: Any
    settings: dict = field(default_factory=dict)
    clients: dict = field(default_factory=dict)  # например {"kalshi": KalshiClient, "odds": OddsClient}

    def env(self, key: str, default: str = "") -> str:
        return os.environ.get(key, default)


class Plugin:
    name: str = ""
    kind: str = ""
    title: str = ""
    description: str = ""
    default_enabled: bool = True
    requires_env: tuple[str, ...] = ()
    # где хранится «включён ли» у встроенных плагинов, которые были до системы плагинов:
    # ("news_sources", "kalshi") или ("notify", "telegram"); у остальных — настройка "plugins"
    setting: tuple[str, str] | None = None
    origin: str = "встроенный"  # откуда загружен (заполняет реестр)

    def available(self, ctx: PluginContext) -> str | None:
        missing = [k for k in self.requires_env if not os.environ.get(k)]
        if missing:
            return "Нужно задать в .env: " + ", ".join(missing)
        return None

    def info(self) -> dict:
        return {"name": self.name, "kind": self.kind, "title": self.title or self.name, "description": self.description}


# ---------------------------------------------------------------- досье события (модуль «События»)
@dataclass
class OutcomeInfo:
    key: str  # токен «Да» исхода — ключ на экране
    title: str  # «Rene Haas», «Да», «Команда А»
    question: str  # полный вопрос рынка исхода
    price: float | None  # цена Polymarket сейчас (0–1)
    market_id: str = ""
    yes_token: str = ""
    no_token: str = ""


@dataclass
class EventInfo:
    """Событие Polymarket, по которому собирается досье."""

    id: str
    title: str
    topic: str  # тема приложения: politics, sports, crypto, weather, economics, ...
    description: str = ""
    end_ts: int | None = None
    tags: list[str] = field(default_factory=list)
    outcomes: list[OutcomeInfo] = field(default_factory=list)  # открытые исходы, по убыванию цены


@dataclass
class Evidence:
    """Факт для досье. kind: news — статья/сообщение; forecast — вероятность того же события в другом месте
    (value 0–1); data — число по теме (цена, волатильность, прогноз погоды, статистика; value + unit);
    flow — сделки и держатели на самом Polymarket."""

    kind: str
    source: str
    title: str
    url: str = ""
    ts: int = 0  # время публикации/измерения (unix)
    value: float | None = None
    unit: str = ""
    outcome_key: str = ""  # к какому исходу относится ("" — к событию целиком)
    note: str = ""  # пояснение простыми словами
    meta: dict = field(default_factory=dict)  # сырые данные для модели (этап C), например члены ансамбля прогноза


# ---------------------------------------------------------------- источники
class SourcePlugin(Plugin):
    """Источник данных для проверки сигнала: новости и/или вероятности с других площадок.

    news(ctx, query, since_ts) -> list[NewsItem]       — статьи и сообщения по вопросу рынка
    forecasts(ctx, question, market) -> list[Forecast] — вероятность того же события в другом месте
    sync(ctx) -> int | None                             — фоновое обновление своего кэша (раз в sync_interval_s)
    Реализуйте только нужное; остальное по умолчанию ничего не возвращает.
    """

    kind = "source"
    sync_interval_s: float | None = None  # None — фоновой задачи нет
    job_name: str = ""  # имя фоновой задачи (по умолчанию plugin:<name>)
    topics: tuple[str, ...] | None = None  # темы событий, где источник полезен (None — все)

    def applies(self, ev: EventInfo) -> bool:
        """Нужен ли источник для досье этого события (по умолчанию — по теме)."""
        return self.topics is None or ev.topic in self.topics

    def dossier(self, ctx: PluginContext, ev: EventInfo) -> list[Evidence] | None:
        """Факты для досье события. None — ядро само спросит news() по названию события и forecasts()
        по вопросам главных исходов."""
        return None

    def news(self, ctx: PluginContext, query: str, since_ts: int) -> list:
        return []

    def forecasts(self, ctx: PluginContext, question: str, market: dict | None = None) -> list:
        return []

    def sync(self, ctx: PluginContext) -> int | None:
        return None

    @property
    def gives_news(self) -> bool:
        return type(self).news is not SourcePlugin.news

    @property
    def gives_forecasts(self) -> bool:
        return type(self).forecasts is not SourcePlugin.forecasts


# ---------------------------------------------------------------- уведомления
@dataclass
class Notice:
    """Уведомление для отправки: то же, что видно в колокольчике, плюс ссылка на экран."""

    kind: str  # signal | whale_exit | whale_check | pause | stopped | system | weekly | settled | ...
    title: str
    body: str = ""
    ref: str = ""  # id сигнала, кита и т.п.
    url: str = ""  # полный адрес нужного экрана (WF_PUBLIC_URL + #/...)
    ts: int = 0


class NotifierPlugin(Plugin):
    """Канал уведомлений. send(ctx, notice) вызывается в фоновом потоке; бросить исключение — значит
    «не доставлено» (ошибка видна на экране «Плагины»). Что присылать, решают флажки «О чём» в настройках.
    start(ctx, services) — необязательно: запуск приёма команд (как у Telegram)."""

    kind = "notifier"
    kinds: tuple[str, ...] | None = None  # какие виды уведомлений принимает (None — все, что разрешены «О чём»)

    def send(self, ctx: PluginContext, notice: Notice) -> None:
        raise NotImplementedError

    def start(self, ctx: PluginContext, services: Any = None) -> None:
        return None


# ---------------------------------------------------------------- стратегии
@dataclass
class Candidate:
    """Новая покупка кита из вашего списка — кандидат в сигнал. Всё «на момент ts»:
    стратегия не должна смотреть на данные позже ts (так же она работает и в бэктесте)."""

    db: Any
    ts: int
    entity_id: str
    wallet: str
    token_id: str
    market_id: str
    price: float  # цена кита
    size_usd: float
    category: str = "other"
    tracked: dict[str, str] | None = None  # кошелёк → кит (весь ваш список); None — загрузить при первом обращении

    def tracked_map(self) -> dict[str, str]:
        if self.tracked is None:
            self.tracked = tracked_map(self.db)
        return self.tracked

    def buyers(self, window_s: int) -> dict[str, float]:
        """Киты из списка, купившие этот же исход за window_s до ts (включительно): кит → сумма покупок, $."""
        tracked = self.tracked_map()
        if not tracked:
            return {}
        ws = list(tracked)
        ph = ",".join("?" for _ in ws)
        out: dict[str, float] = {}
        for r in self.db.query(
            f"SELECT wallet, size_usd FROM trades WHERE token_id=? AND side='BUY' AND ts>=? AND ts<=? "
            f"AND (source IS NULL OR source='') AND wallet IN ({ph})",
            [self.token_id, self.ts - window_s, self.ts, *ws],
        ):
            e = tracked.get(r["wallet"], r["wallet"])
            out[e] = out.get(e, 0.0) + float(r["size_usd"] or 0)
        return out


def tracked_map(db: Any, modes: tuple[str, ...] = ("copy", "watch")) -> dict[str, str]:
    """Кошелёк → кит для китов из списка пользователя (у объединённых игроков — все кошельки)."""
    out: dict[str, str] = {}
    ph = ",".join("?" for _ in modes)
    for r in db.query(f"SELECT entity_id FROM watchlist WHERE mode IN ({ph})", list(modes)):
        e = r["entity_id"]
        ws = [w["wallet"] for w in db.query("SELECT wallet FROM entity_wallets WHERE entity_id=?", (e,))]
        for w in ws or ([e] if not e.startswith("E-") else []):
            out[w.lower()] = e
    return out


@dataclass
class Verdict:
    ok: bool
    reason: str = ""  # коротко, по-русски: почему да или нет


class StrategyPlugin(Plugin):
    """Стратегия решает, превращать ли покупку кита в сигнал. Включённые стратегии работают как «или»:
    сигнал создаётся, если его берёт хотя бы одна. Дальше — общие проверки (цена, стакан, лимиты, ИИ).
    У каждой стратегии свой бэктест: та же функция accept на истории ваших китов, без заглядывания вперёд."""

    kind = "strategy"
    default_enabled = False

    def accept(self, c: Candidate) -> Verdict:
        raise NotImplementedError
