"""HTTP API под экраны интерфейса (FastAPI). Документация со схемами — на /docs.

GET  /api/status                      состояние системы (индикатор внизу меню)
GET  /api/dashboard                   экран «Сигналы»
GET  /api/signals/{id}                карточка сигнала
POST /api/signals/{id}/confirm        подтвердить ставку {size_usd?}
POST /api/signals/{id}/skip           пропустить
POST /api/exits/{id}                  кит вышел: {action: hold|sell}
GET  /api/positions?status=open       «Мои ставки» (open|closed)
GET  /api/whales                      «Киты»
GET  /api/whales/{id}                 профиль кита
GET  /api/bets/{token_id}             ставка: цифры, график цены, хронология
POST /api/bets/{token_id}/sell        продать ставку сейчас (виртуально)
GET  /api/diagnostics[?live=1]        проверка источников, задачи, объём данных, текст отчёта
GET  /api/leaderboard?category&period&order&offset   рейтинг трейдеров Polymarket
GET  /api/whales/{id}/simulate?days=90 | ?start&end  что было бы, если бы копировали этого кита
GET  /api/players/resolve?q=          ссылка, адрес или имя → игроки
GET  /api/players/{wallet}?mini=1     карточка игрока по готовым данным Polymarket
POST /api/players/add                 {wallets: [...], mode: watch|copy} — добавить пачкой
GET  /api/markets/{token_id}          панель рынка: правила, объём, держатели, позиция кита, график
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
GET  /api/rotation?window_weeks&min_bets&min_result_usd&max_drawdown_share  ротация китов по форме
PUT  /api/rotation/rule               сохранить правило «в форме»
GET  /api/catalog?topic&sort&q&offset  «События»: список событий Polymarket по темам, поиск, сортировка
GET  /api/catalog/{id}                событие: исходы, цены, правила, наши ставки, лимиты модуля
GET  /api/catalog/{id}/history?period=1w&keys=a,b   история цен исходов (1h 6h 1d 1w 1m all)
GET  /api/catalog/{id}/live?tokens=a,b живые цены исходов (поток стаканов или стакан CLOB)
GET  /api/catalog/{id}/dossier?collect=auto|force|no  досье события: факты из источников и ход сбора
POST /api/catalog/{id}/buy            {token_id, usd, max_price?} — виртуальная покупка исхода
GET  /api/plugins                     экран «Плагины»: источники, уведомления, стратегии, состояние и ошибки
PATCH /api/plugins/{name}             {enabled} — включить/выключить плагин
GET  /api/plugins/{name}/backtest?days=90  бэктест стратегии на китах из списка
GET  /api/push/key                    открытый ключ VAPID для подписки и число подписанных устройств
POST /api/push/subscribe              {subscription, label} — подписать это устройство на push
POST /api/push/unsubscribe            {endpoint} — отписать
POST /api/push/test                   пробное push-уведомление на все устройства

Обработчики синхронные: FastAPI выполняет их в пуле потоков, долгий запрос не держит остальные.
Ответ собирается сразу в JSON (без промежуточного обхода jsonable_encoder) — см. `json_response`.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..services import Services
from . import schemas as S

GZIP_MIN = 1024  # меньшие ответы не сжимаем: выигрыша нет
JSON_CTYPE = "application/json; charset=utf-8"


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":")).encode("utf-8")


def json_response(request: Request | None, payload: Any, status: int = 200, cookie: str | None = None) -> Response:
    """JSON-ответ: сжатие gzip для больших ответов, без кэша, с cookie входа при необходимости."""
    headers = {"Cache-Control": "no-store"}
    if status in (204, 304):
        data = b""
    else:
        data = dumps(payload)
        accept = request.headers.get("accept-encoding", "") if request is not None else ""
        if len(data) >= GZIP_MIN and "gzip" in accept.lower():
            data = gzip.compress(data, 5)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
    if cookie:
        headers["Set-Cookie"] = cookie
    return Response(content=data, status_code=status, media_type=JSON_CTYPE, headers=headers)


def _need(v: Any, msg: str = "Не найдено") -> Any:
    if v is None:
        raise HttpError(404, msg)
    return v


def _int(v: Any) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _doc(model: type) -> dict:
    return {200: {"model": model}, 400: {"model": S.ErrorOut}, 404: {"model": S.ErrorOut}}


def build_router(svc: Services) -> APIRouter:
    r = APIRouter(prefix="/api")

    def J(request: Request, payload: Any) -> Response:
        return json_response(request, payload)

    def need_agent() -> Any:
        if svc.agent is None:
            raise HttpError(503, "Агент не запущен")
        return svc.agent

    # ---------------------------------------------------------------- сигналы и ставки
    @r.get("/status", responses=_doc(S.StatusOut), tags=["Состояние"])
    def status(request: Request) -> Response:
        return J(request, svc.status())

    @r.get("/dashboard", responses=_doc(S.DashboardOut), tags=["Сигналы"])
    def dashboard(request: Request) -> Response:
        return J(request, svc.dashboard())

    @r.get("/signals/{signal_id}", tags=["Сигналы"])
    def signal(request: Request, signal_id: str) -> Response:
        return J(request, _need(svc.signal_detail(signal_id)))

    @r.post("/signals/{signal_id}/confirm", tags=["Сигналы"])
    def confirm(request: Request, signal_id: str, body: S.ConfirmIn | None = None) -> Response:
        return J(request, need_agent().confirm(signal_id, size_usd=body.size_usd if body else None))

    @r.post("/signals/{signal_id}/skip", tags=["Сигналы"])
    def skip(request: Request, signal_id: str) -> Response:
        return J(request, need_agent().reject(signal_id))

    @r.post("/exits/{signal_id}", tags=["Сигналы"])
    def exit_(request: Request, signal_id: str, body: S.ExitIn | None = None) -> Response:
        action = body.action if body else None
        if action not in ("hold", "sell"):
            raise HttpError(400, "action: hold | sell")
        return J(request, need_agent().resolve_exit(signal_id, action))

    @r.get("/positions", responses=_doc(S.ItemsOut), tags=["Ставки"])
    def positions(request: Request, status: str = "open") -> Response:
        return J(request, svc.positions(status))

    @r.get("/bets/{token_id}", tags=["Ставки"])
    def bet(request: Request, token_id: str) -> Response:
        return J(request, _need(svc.bet_detail(token_id)))

    @r.post("/bets/{token_id}/sell", tags=["Ставки"])
    def sell(request: Request, token_id: str) -> Response:
        return J(request, svc.sell_bet(token_id))

    # ---------------------------------------------------------------- киты и игроки
    @r.get("/whales", responses=_doc(S.ItemsOut), tags=["Киты"])
    def whales(request: Request) -> Response:
        return J(request, svc.whales())

    @r.post("/whales/check", responses=_doc(S.CheckJobOut), tags=["Киты"])
    def check(request: Request, body: S.CheckIn | None = None) -> Response:
        if not body or not body.address:
            raise HttpError(400, "Нужен адрес кошелька или ссылка")
        if body.wait:  # синхронно (скрипты, тесты)
            return J(request, svc.check_wallet(body.address))
        return J(request, svc.start_check(body.address))

    @r.get("/whales/check/{job_id}", responses=_doc(S.CheckJobOut), tags=["Киты"])
    def check_status(request: Request, job_id: str) -> Response:
        return J(request, _need(svc.check_status(job_id)))

    @r.post("/whales", tags=["Киты"])
    def add_whale(request: Request, body: S.AddWhaleIn | None = None) -> Response:
        if not body or not body.entity_id:
            raise HttpError(400, "Нужен entity_id")
        return J(request, svc.set_whale(body.entity_id, mode=body.mode))

    @r.get("/whales/{entity_id}/simulate", tags=["Киты"])
    def simulate(
        request: Request, entity_id: str, days: str | None = None, start: str | None = None, end: str | None = None
    ) -> Response:
        return J(request, svc.simulate(entity_id, _int(days) or 90, _int(start), _int(end)))

    @r.get("/whales/{entity_id}", tags=["Киты"])
    def whale(request: Request, entity_id: str) -> Response:
        return J(request, _need(svc.whale_profile(entity_id)))

    @r.patch("/whales/{entity_id}", tags=["Киты"])
    def patch_whale(request: Request, entity_id: str, body: S.PatchWhaleIn | None = None) -> Response:
        b = body or S.PatchWhaleIn()
        return J(request, svc.set_whale(entity_id, mode=b.mode, category=b.category, enabled=b.enabled))

    @r.get("/players/resolve", tags=["Игроки"])
    def resolve_players(request: Request, q: str = "") -> Response:
        return J(request, svc.resolve_players(q))

    @r.post("/players/add", tags=["Игроки"])
    def add_players(request: Request, body: S.AddPlayersIn | None = None) -> Response:
        b = body or S.AddPlayersIn()
        ws = b.wallets or ([b.wallet] if b.wallet else [])
        if not ws:
            raise HttpError(400, "Нужен список wallets")
        return J(request, svc.add_players(ws, b.mode))

    @r.get("/players/{wallet}", tags=["Игроки"])
    def player(request: Request, wallet: str, mini: str | None = None, refresh: str | None = None) -> Response:
        return J(request, svc.player_card(wallet, mini=mini == "1", refresh=refresh == "1"))

    @r.get("/rotation", tags=["Киты"])
    def rotation(
        request: Request,
        window_weeks: str | None = None,
        min_bets: str | None = None,
        min_result_usd: str | None = None,
        max_drawdown_share: str | None = None,
    ) -> Response:
        rule = {
            k: v
            for k, v in {
                "window_weeks": _int(window_weeks),
                "min_bets": _int(min_bets),
                "min_result_usd": _float(min_result_usd),
                "max_drawdown_share": _float(max_drawdown_share),
            }.items()
            if v is not None
        }
        return J(request, svc.rotation(rule))

    @r.put("/rotation/rule", tags=["Киты"])
    def rotation_rule(request: Request, body: S.RotationRuleIn | None = None) -> Response:
        return J(request, svc.rotation(body.rule() if body else {}, save=True))

    @r.get("/markets/{token_id}", tags=["Рынки"])
    def market(request: Request, token_id: str) -> Response:
        return J(request, _need(svc.market_panel(token_id)))

    @r.get("/leaderboard", responses=_doc(S.ItemsOut), tags=["Игроки"])
    def leaderboard(
        request: Request,
        category: str = "overall",
        period: str = "month",
        order: str = "pnl",
        offset: str | None = None,
    ) -> Response:
        return J(request, svc.leaderboard(category, period, order, _int(offset) or 0))

    # ---------------------------------------------------------------- итоги, настройки, служебное
    @r.get("/results", tags=["Итоги"])
    def results(request: Request, period: str = "month") -> Response:
        return J(request, svc.results(period))

    @r.get("/settings", responses=_doc(S.SettingsOut), tags=["Настройки"])
    def get_settings(request: Request) -> Response:
        return J(request, svc.get_settings())

    @r.put("/settings", responses=_doc(S.SettingsOut), tags=["Настройки"])
    def put_settings(request: Request, body: S.SettingsIn | None = None) -> Response:
        return J(request, svc.update_settings(body.model_dump() if body else {}))

    @r.post("/onboarding", responses=_doc(S.OkOut), tags=["Настройки"])
    def onboarding(request: Request, body: S.OnboardingIn | None = None) -> Response:
        b = body or S.OnboardingIn()
        return J(request, svc.onboarding(b.jurisdiction_ok, b.risk_ack, b.paper))

    @r.get("/notifications", responses=_doc(S.ItemsOut), tags=["Уведомления"])
    def notifications(request: Request) -> Response:
        return J(request, svc.notifications())

    @r.post("/notifications/read", tags=["Уведомления"])
    def mark_read(request: Request) -> Response:
        return J(request, svc.mark_read())

    @r.post("/stop", responses=_doc(S.StopOut), tags=["Состояние"])
    def stop(request: Request) -> Response:
        return J(request, svc.stop_all())

    @r.post("/resume", responses=_doc(S.StopOut), tags=["Состояние"])
    def resume(request: Request) -> Response:
        return J(request, svc.resume())

    @r.get("/trial", tags=["Итоги"])
    def trial(request: Request) -> Response:
        return J(request, svc.trial())

    @r.post("/trial/tune", tags=["Итоги"])
    def tune(request: Request, body: S.TuneIn | None = None) -> Response:
        return J(request, svc.set_tuning(body.edge_threshold if body else 0))

    @r.get("/data", tags=["Данные"])
    def data_usage(request: Request) -> Response:
        return J(request, svc.data_usage())

    @r.post("/data/purge", tags=["Данные"])
    def purge(request: Request) -> Response:
        return J(request, svc.purge_untracked())

    @r.get("/diagnostics", responses=_doc(S.DiagnosticsOut), tags=["Диагностика"])
    def diagnostics(request: Request, live: str | None = None) -> Response:
        return J(request, svc.diagnostics(live=live == "1"))

    # ---------------------------------------------------------------- модуль «События»
    def _csv(v: str | None) -> list[str]:
        return [x for x in (v or "").split(",") if x][:24]

    @r.get("/catalog", tags=["События"])
    def events_list(
        request: Request, topic: str = "", sort: str = "volume24", q: str = "", offset: str | None = None
    ) -> Response:
        return J(request, svc.events_list(topic, sort, q, _int(offset) or 0))

    @r.get("/catalog/{event_id}", tags=["События"])
    def event_detail(request: Request, event_id: str) -> Response:
        return J(request, _need(svc.event_detail(event_id), "Событие не найдено"))

    @r.get("/catalog/{event_id}/history", tags=["События"])
    def event_history(request: Request, event_id: str, period: str = "1w", keys: str | None = None) -> Response:
        return J(request, svc.event_history(event_id, period, _csv(keys) or None))

    @r.get("/catalog/{event_id}/live", tags=["События"])
    def event_live(request: Request, event_id: str, tokens: str | None = None) -> Response:
        return J(request, svc.event_live(event_id, _csv(tokens)))

    @r.get("/catalog/{event_id}/dossier", tags=["События"])
    def event_dossier(request: Request, event_id: str, collect: str = "auto") -> Response:
        return J(request, svc.event_dossier(event_id, collect if collect in ("auto", "force", "no") else "auto"))

    @r.post("/catalog/{event_id}/buy", tags=["События"])
    def event_buy(request: Request, event_id: str, body: S.EventBuyIn | None = None) -> Response:
        b = body or S.EventBuyIn()
        return J(request, svc.event_buy(event_id, b.token_id, b.usd, b.max_price))

    # ---------------------------------------------------------------- плагины
    @r.get("/plugins", tags=["Плагины"])
    def plugins_list(request: Request) -> Response:
        return J(request, svc.plugins_list())

    @r.patch("/plugins/{name}", tags=["Плагины"])
    def plugin_set(request: Request, name: str, body: S.PluginPatchIn | None = None) -> Response:
        return J(request, svc.plugin_set(name, body.enabled if body else True))

    @r.get("/plugins/{name}/backtest", tags=["Плагины"])
    def plugin_backtest(request: Request, name: str, days: str | None = None) -> Response:
        return J(request, svc.plugin_backtest(name, _int(days) or 90))

    # ---------------------------------------------------------------- push-уведомления
    @r.get("/push/key", tags=["Уведомления"])
    def push_key(request: Request) -> Response:
        return J(request, svc.push_key())

    @r.post("/push/subscribe", tags=["Уведомления"])
    def push_subscribe(request: Request, body: S.PushSubscribeIn | None = None) -> Response:
        b = body or S.PushSubscribeIn()
        return J(request, svc.push_subscribe(b.subscription or {}, b.label))

    @r.post("/push/unsubscribe", tags=["Уведомления"])
    def push_unsubscribe(request: Request, body: S.PushUnsubscribeIn | None = None) -> Response:
        return J(request, svc.push_unsubscribe(body.endpoint if body else ""))

    @r.post("/push/test", tags=["Уведомления"])
    def push_test(request: Request) -> Response:
        return J(request, svc.push_test())

    @r.get("/journal", responses=_doc(S.ItemsOut), tags=["Итоги"])
    def journal(request: Request, limit: str | None = None) -> Response:
        lim = _int(limit)
        return J(request, svc.journal(100 if lim is None else lim))

    return r
