"""HTTP API под экраны интерфейса. Маршруты описаны один раз и подключаются
к встроенному серверу (без зависимостей) или к FastAPI (если установлен).

GET  /api/status                      состояние системы (индикатор внизу меню)
GET  /api/dashboard                   экран «Сигналы»
GET  /api/signals/{id}                карточка сигнала
POST /api/signals/{id}/confirm        подтвердить ставку {size_usd?}
POST /api/signals/{id}/skip           пропустить
POST /api/exits/{id}                  кит вышел: {action: hold|sell}
GET  /api/positions?status=open       «Мои ставки» (open|closed)
GET  /api/whales                      «Киты»
GET  /api/whales/{id}                 профиль кита
GET  /api/bets/{token_id}              ставка: цифры, график цены, хронология
POST /api/bets/{token_id}/sell         продать ставку сейчас (виртуально)
GET  /api/diagnostics[?live=1]          проверка источников, задачи, объём данных, текст отчёта
GET  /api/leaderboard?category&period&order&offset   рейтинг трейдеров Polymarket
GET  /api/whales/{id}/simulate?days=90 | ?start&end  что было бы, если бы копировали этого кита
GET  /api/players/resolve?q=          ссылка, адрес или имя → игроки
GET  /api/players/{wallet}?mini=1      карточка игрока по готовым данным Polymarket
POST /api/players/add                 {wallets: [...], mode: watch|copy} — добавить пачкой
GET  /api/markets/{token_id}           панель рынка: правила, объём, держатели, позиция кита, график
GET  /api/data                        объём данных: по китам из списка и по остальным кошелькам
POST /api/data/purge                  удалить данные по кошелькам вне списка
POST /api/trial/tune                  применить порог выгоды по итогам {edge_threshold}
POST /api/whales/check                проверить кошелёк {address, wait?} → фоновая проверка {job_id, state}
GET  /api/whales/check/{job_id}       ход проверки: этап, счётчики, результат
POST /api/whales                      добавить {entity_id, mode: copy|watch}
PATCH /api/whales/{id}                {mode?} или {category, enabled}
GET  /api/results?period=month        «Итоги» (week|month|all)
GET  /api/settings, PUT /api/settings «Настройки»
POST /api/onboarding                  первый запуск {jurisdiction_ok, risk_ack}
GET  /api/notifications, POST /api/notifications/read
POST /api/stop, POST /api/resume      «Остановить всё» / возобновить
GET  /api/journal?limit=100           журнал всех решений
GET  /api/trial                       прогресс пробного режима и работа сбора данных
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from ..services import Services


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Route:
    method: str
    pattern: str
    handler: Callable[..., Any]

    def __post_init__(self):
        self.regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.pattern) + "$")


def _need(v, msg="Не найдено"):
    if v is None:
        raise HttpError(404, msg)
    return v


def _int(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def build_routes(svc: Services) -> list[Route]:
    def confirm(signal_id, body, **_):
        if svc.agent is None:
            raise HttpError(503, "Агент не запущен")
        return svc.agent.confirm(signal_id, size_usd=(body or {}).get("size_usd"))

    def skip(signal_id, **_):
        if svc.agent is None:
            raise HttpError(503, "Агент не запущен")
        return svc.agent.reject(signal_id)

    def exit_(signal_id, body, **_):
        action = (body or {}).get("action")
        if action not in ("hold", "sell"):
            raise HttpError(400, "action: hold | sell")
        return svc.agent.resolve_exit(signal_id, action)

    def add_whale(body, **_):
        if not body or "entity_id" not in body:
            raise HttpError(400, "Нужен entity_id")
        return svc.set_whale(body["entity_id"], mode=body.get("mode", "watch"))

    def patch_whale(entity_id, body, **_):
        body = body or {}
        return svc.set_whale(entity_id, mode=body.get("mode"), category=body.get("category"), enabled=body.get("enabled"))

    def check(body, **_):
        if not body or not body.get("address"):
            raise HttpError(400, "Нужен адрес кошелька или ссылка")
        if body.get("wait"):                       # синхронно (скрипты, тесты)
            return svc.check_wallet(body["address"])
        return svc.start_check(body["address"])

    def add_players(body, **_):
        body = body or {}
        ws = body.get("wallets") or ([body["wallet"]] if body.get("wallet") else [])
        if not ws:
            raise HttpError(400, "Нужен список wallets")
        return svc.add_players(ws, body.get("mode", "watch"))

    def onboarding(body, **_):
        body = body or {}
        return svc.onboarding(bool(body.get("jurisdiction_ok")), bool(body.get("risk_ack")), bool(body.get("paper", True)))

    return [
        Route("GET", "/api/status", lambda **_: svc.status()),
        Route("GET", "/api/dashboard", lambda **_: svc.dashboard()),
        Route("GET", "/api/signals/{signal_id}", lambda signal_id, **_: _need(svc.signal_detail(signal_id))),
        Route("POST", "/api/signals/{signal_id}/confirm", confirm),
        Route("POST", "/api/signals/{signal_id}/skip", skip),
        Route("POST", "/api/exits/{signal_id}", exit_),
        Route("GET", "/api/positions", lambda query, **_: svc.positions(query.get("status", "open"))),
        Route("GET", "/api/bets/{token_id}", lambda token_id, **_: _need(svc.bet_detail(token_id))),
        Route("POST", "/api/bets/{token_id}/sell", lambda token_id, **_: svc.sell_bet(token_id)),
        Route("GET", "/api/whales", lambda **_: svc.whales()),
        Route("POST", "/api/whales/check", check),
        Route("GET", "/api/whales/check/{job_id}", lambda job_id, **_: _need(svc.check_status(job_id))),
        Route("POST", "/api/whales", add_whale),
        Route("GET", "/api/whales/{entity_id}/simulate", lambda entity_id, query, **_: svc.simulate(
            entity_id, int(query.get("days", 90) or 90), _int(query.get("start")), _int(query.get("end")))),
        Route("GET", "/api/players/resolve", lambda query, **_: svc.resolve_players(query.get("q", ""))),
        Route("POST", "/api/players/add", add_players),
        Route("GET", "/api/players/{wallet}", lambda wallet, query, **_: svc.player_card(
            wallet, mini=query.get("mini") == "1", refresh=query.get("refresh") == "1")),
        Route("GET", "/api/markets/{token_id}", lambda token_id, **_: _need(svc.market_panel(token_id))),
        Route("GET", "/api/whales/{entity_id}", lambda entity_id, **_: _need(svc.whale_profile(entity_id))),
        Route("PATCH", "/api/whales/{entity_id}", patch_whale),
        Route("GET", "/api/results", lambda query, **_: svc.results(query.get("period", "month"))),
        Route("GET", "/api/settings", lambda **_: svc.get_settings()),
        Route("PUT", "/api/settings", lambda body, **_: svc.update_settings(body or {})),
        Route("POST", "/api/onboarding", onboarding),
        Route("GET", "/api/notifications", lambda **_: svc.notifications()),
        Route("POST", "/api/notifications/read", lambda **_: svc.mark_read()),
        Route("POST", "/api/stop", lambda **_: svc.stop_all()),
        Route("POST", "/api/resume", lambda **_: svc.resume()),
        Route("GET", "/api/trial", lambda **_: svc.trial()),
        Route("GET", "/api/data", lambda **_: svc.data_usage()),
        Route("GET", "/api/diagnostics", lambda query, **_: svc.diagnostics(live=query.get("live") == "1")),
        Route("GET", "/api/leaderboard", lambda query, **_: svc.leaderboard(
            query.get("category", "overall"), query.get("period", "month"), query.get("order", "pnl"),
            int(query.get("offset", 0) or 0))),
        Route("POST", "/api/data/purge", lambda **_: svc.purge_untracked()),
        Route("POST", "/api/trial/tune", lambda body, **_: svc.set_tuning((body or {}).get("edge_threshold", 0))),
        Route("GET", "/api/journal", lambda query, **_: svc.journal(int(query.get("limit", 100)))),
    ]


def dispatch(routes: list[Route], method: str, path: str, query: dict, body: dict | None):
    for r in routes:
        if r.method != method:
            continue
        m = r.regex.match(path)
        if m:
            return r.handler(**m.groupdict(), query=query, body=body)
    raise HttpError(404, f"Нет маршрута {method} {path}")
