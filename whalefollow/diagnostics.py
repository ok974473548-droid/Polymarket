"""Диагностика из интерфейса: доступны ли источники, как быстро отвечают, что сломалось.

Каждая проверка — лёгкий запрос без повторов, с ограничением по времени; все идут параллельно.
Время меряется дважды:
  • «первое подключение» — с установкой защищённого соединения (так было бы без пула);
  • «рабочий запрос» — повтор по уже открытому соединению: именно так ходит приложение.
Результат хранится в state (последняя проверка) и показывается на экране «Диагностика».
"""
from __future__ import annotations

import json
import os
import threading
import time

import requests
import requests.adapters

from .config import Endpoints
from .db import DB, now_ts
from .util import pmap

TIMEOUT = 8.0
WARM_RUNS = 2                      # повторов по открытому соединению; в отчёт идёт лучший
UA = {"User-Agent": "whalefollow-diagnostics"}
# Публичный адрес контракта биржи Polymarket — для проверок, когда в списке ещё нет китов
FALLBACK_WALLET = "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e"
WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_LIVE = "wss://ws-live-data.polymarket.com"

HINTS = {
    "polymarket": "Polymarket не отвечает из вашей сети. Проверьте интернет; если сайт polymarket.com у вас не "
                  "открывается, данные не загрузятся (может понадобиться другая сеть).",
    "stream": "Потоковое соединение не открылось — приложение продолжит работать опросом раз в несколько секунд. "
              "Частая причина — прокси или фильтр, который не пропускает WebSocket.",
    "kalshi": "Kalshi не отвечает — похожие вопросы с Kalshi не появятся, остальное работает.",
    "telegram": "Проверьте WF_TELEGRAM_BOT_TOKEN в .env (токен от @BotFather).",
    "chain": "Проверьте ключ обозревателя блокчейна или переключитесь: WF_CHAIN_PROVIDER=blockscout.",
    "odds": "Проверьте WF_ODDS_API_KEY (the-odds-api.com).",
    "news": "Новостной источник не отвечает — новости по сигналам будут неполными.",
}
# Ответ сервиса по коду: что случилось и что делать. Код важнее группы: 410 — это не «сеть недоступна».
STATUS = {
    410: ("метод API отключён сервисом", "Сервис убрал этот метод API. Обновите WhaleFollow до последней версии — "
                                        "в ней используются действующие методы."),
    404: ("метод API не найден", "Похоже, сервис изменил API. Обновите WhaleFollow; если не поможет — скопируйте "
                                 "отчёт ниже и пришлите разработчику."),
    429: ("превышен лимит запросов", "Слишком много запросов за короткое время. Подождите минуту и повторите проверку."),
    400: ("сервис отклонил запрос", "Сервис изменил правила запроса. Обновите WhaleFollow."),
}
ACCESS = (401, 403, 451)

_session: requests.Session | None = None
_session_lock = threading.Lock()


def session() -> requests.Session:
    """Общее соединение для проверок: повторный запрос идёт без новой установки связи."""
    global _session
    with _session_lock:
        if _session is None:
            s = requests.Session()
            ad = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
            s.mount("https://", ad)
            s.mount("http://", ad)
            s.headers.update(UA)
            _session = s
        return _session


def _get(url: str, params: dict | None = None, headers: dict | None = None):
    r = session().get(url, params=params, headers=headers, timeout=TIMEOUT)
    r.content                                                    # дочитываем: соединение вернётся в пул
    return r


def explain_status(code, group: str) -> tuple[str, str | None]:
    """Код ответа → (подробности, подсказка)."""
    if code in STATUS:
        what, hint = STATUS[code]
        return f"ошибка HTTP {code} — {what}", hint
    if code in ACCESS:
        return f"ошибка HTTP {code} — доступ ограничен", HINTS.get(group)
    if isinstance(code, int) and code >= 500:
        return (f"ошибка HTTP {code} — сбой на стороне сервиса",
                "У сервиса временный сбой — обычно проходит за несколько минут. Ваши настройки в порядке.")
    return f"ошибка HTTP {code}", HINTS.get(group)


def speed_label(ms: int | None) -> str | None:
    if ms is None:
        return None
    return "мгновенно" if ms <= 50 else "быстро" if ms <= 150 else "нормально" if ms <= 400 else "медленно"


def _ws_probe(url: str, subscribe: dict | None, want_message: bool, wait_s: float = 6.0) -> tuple[bool, str]:
    """Открывает поток, подписывается и ждёт первое сообщение с данными."""
    from .ws import WSConnection
    t0 = time.perf_counter()
    c = WSConnection(url, timeout=TIMEOUT)
    c.connect()
    hs = int((time.perf_counter() - t0) * 1000)
    try:
        if subscribe is None:
            return True, f"соединение открыто за {hs} мс"
        c.sock.settimeout(wait_s)
        c.send_text(json.dumps(subscribe))
        t1 = time.perf_counter()
        deadline = t1 + wait_s
        while time.perf_counter() < deadline:
            try:
                op, data = c.recv_message()
            except (TimeoutError, OSError):
                break
            if op == 0x9:
                c.send_frame(0xA, data)
                continue
            if op == 0x8:
                return False, "сервер закрыл поток сразу после подписки"
            text = data.decode("utf-8", "replace").strip()
            if op == 0x1 and text and text.upper() not in ("PONG", "PING"):
                return True, f"соединение {hs} мс, первые данные через {int((time.perf_counter() - t1) * 1000)} мс"
        if want_message:
            return False, f"соединение {hs} мс, но данных за {int(wait_s)} с не пришло"
        return True, f"соединение открыто за {hs} мс"
    finally:
        c.close()


def probes(db: DB) -> list[dict]:
    """Список проверок: ключ, название, группа, функция → (ok, подробности); warm=False — без повторов."""
    ep = Endpoints()
    out: list[tuple] = []
    try:
        from .tracked import tracked_wallets
        tracked = tracked_wallets(db)
    except Exception:  # noqa: BLE001
        tracked = []
    picked: dict = {}
    pick_lock = threading.Lock()

    def wallet() -> str:
        """Кошелёк для проверок: кит из списка, иначе первый игрок рейтинга (настоящий кошелёк трейдера —
        адрес контракта API может отклонить)."""
        if tracked:
            return tracked[0]
        with pick_lock:
            if "w" not in picked:
                try:
                    r = _get(f"{ep.data}/v2/leaderboard", {"time_period": "week", "category": "overall",
                                                           "sort_by": "PNL", "limit": 1})
                    rows = (r.json() or {}).get("data") or [] if r.ok else []
                    picked["w"] = str(rows[0].get("user_id") or rows[0].get("proxy_wallet")).lower() if rows \
                        else FALLBACK_WALLET
                except Exception:  # noqa: BLE001
                    picked["w"] = FALLBACK_WALLET
            return picked["w"]
    token = None
    try:
        # исход, где недавно были сделки китов: у такого рынка точно есть стакан
        row = db.one("SELECT tr.token_id FROM trades tr JOIN markets m ON m.market_id=tr.market_id "
                     "WHERE m.status='open' ORDER BY tr.ts DESC LIMIT 1")
        token = row["token_id"] if row else None
    except Exception:  # noqa: BLE001
        token = None

    def gamma():
        r = _get(f"{ep.gamma}/markets", {"limit": 1, "closed": "false"})
        r.raise_for_status()
        return True, "каталог отвечает"

    def gamma_closed():
        r = _get(f"{ep.gamma}/markets", {"limit": 2, "closed": "true"})
        r.raise_for_status()
        ids = [m.get("conditionId") for m in (r.json() or []) if m.get("conditionId")]
        if len(ids) < 2:
            return True, "закрытых рынков в ответе нет"
        r2 = _get(f"{ep.gamma}/markets", {"condition_ids": ids, "closed": "any", "limit": 2})
        r2.raise_for_status()
        found = len([m for m in (r2.json() or []) if m.get("conditionId") in ids])
        return found == 2, f"закрытые рынки по идентификаторам: найдено {found} из 2"

    def trades():
        r = _get(f"{ep.data}/v2/trades", {"limit": 1})
        r.raise_for_status()
        return True, "лента сделок отвечает"

    def wallet_trades():
        r = _get(f"{ep.data}/v2/trades", {"user": wallet(), "limit": 1})
        r.raise_for_status()
        return True, "сделки кошелька отвечают"

    def activity():
        r = _get(f"{ep.data}/v2/activity", {"user": wallet(), "limit": 1, "type": "SPLIT,MERGE,CONVERSION"})
        r.raise_for_status()
        return True, "журнал операций отвечает"

    def positions():
        r = _get(f"{ep.data}/v2/positions", {"user": wallet(), "limit": 1})
        r.raise_for_status()
        return True, "открытые позиции отвечают"

    def closed_positions():
        r = _get(f"{ep.data}/v2/positions", {"user": wallet(), "limit": 1, "status": "CLOSED"})
        r.raise_for_status()
        return True, "закрытые позиции отвечают"

    def leaderboard():
        r = _get(f"{ep.data}/v2/leaderboard", {"time_period": "week", "category": "overall", "sort_by": "PNL",
                                               "limit": 1})
        r.raise_for_status()
        body = r.json() or {}
        rows = body.get("data") if isinstance(body, dict) else body
        return True, "рейтинг трейдеров отвечает" + ("" if rows else " (пустой ответ)")

    def clob():
        r = _get(f"{ep.clob}/time")
        r.raise_for_status()
        return True, "биржа отвечает"

    def clob_book():
        r = _get(f"{ep.clob}/book", {"token_id": token})
        if r.status_code == 404:
            return True, "биржа отвечает (у выбранного рынка сейчас нет стакана)"
        r.raise_for_status()
        b = r.json() or {}
        return True, f"стакан: {len(b.get('bids') or [])} заявок на покупку, {len(b.get('asks') or [])} на продажу"

    def ws_market():
        sub = {"assets_ids": [token], "type": "market"} if token else None
        return _ws_probe(WS_MARKET, sub, want_message=bool(token))

    def ws_live():
        return _ws_probe(WS_LIVE, {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]},
                         want_message=True, wait_s=8.0)

    out += [("gamma", "Polymarket: каталог рынков", "polymarket", gamma, True),
            ("gamma_closed", "Polymarket: поиск закрытых рынков", "polymarket", gamma_closed, True),
            ("trades", "Polymarket: лента сделок", "polymarket", trades, True),
            ("wallet_trades", "Polymarket: сделки кошелька", "polymarket", wallet_trades, True),
            ("activity", "Polymarket: журнал операций кошелька", "polymarket", activity, True),
            ("positions", "Polymarket: открытые позиции", "polymarket", positions, True),
            ("closed_positions", "Polymarket: закрытые позиции", "polymarket", closed_positions, True),
            ("leaderboard", "Polymarket: рейтинг трейдеров", "polymarket", leaderboard, True),
            ("clob", "Polymarket: биржа (CLOB)", "polymarket", clob, True)]
    if token:
        out.append(("clob_book", "Polymarket: стакан заявок", "polymarket", clob_book, True))
    out += [("ws_market", "Поток стаканов (WebSocket)", "stream", ws_market, False),
            ("ws_live", "Поток сделок китов (WebSocket)", "stream", ws_live, False)]

    def kalshi():
        r = _get("https://api.elections.kalshi.com/trade-api/v2/events", {"status": "open", "limit": 1})
        r.raise_for_status()
        return True, "события Kalshi отвечают"
    out.append(("kalshi", "Kalshi", "kalshi", kalshi, True))

    def news():
        r = _get("https://news.google.com/rss/search", {"q": "polymarket", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        r.raise_for_status()
        return True, "Google News отвечает"

    def manifold():
        r = _get("https://api.manifold.markets/v0/search-markets", {"term": "election", "limit": 1})
        r.raise_for_status()
        return True, "Manifold отвечает"
    out += [("news", "Новости: Google News", "news", news, False),
            ("manifold", "Прогнозы: Manifold", "news", manifold, True)]

    tg_token = os.environ.get("WF_TELEGRAM_BOT_TOKEN")
    if tg_token:
        def tg():
            r = _get(f"https://api.telegram.org/bot{tg_token}/getMe")
            if r.status_code == 401:
                return False, "токен бота неверный"
            r.raise_for_status()
            return True, "бот @" + str((r.json().get("result") or {}).get("username", "?"))
        out.append(("telegram", "Telegram-бот", "telegram", tg, True))

    from .ingest.chain import USDC_E, ExplorerClient
    chain = ExplorerClient.from_env()
    if chain is not None:
        def ch():
            chain.timeout = TIMEOUT
            rows = chain.token_transfers(FALLBACK_WALLET, USDC_E, page_size=1, max_pages=1)
            return True, f"{chain.provider}: отвечает ({len(rows)} перевод в пробном ответе)"
        out.append(("chain", "Блокчейн (переводы USDC)", "chain", ch, False))

    key = os.environ.get("WF_ODDS_API_KEY")
    if key:
        def odds():
            r = _get("https://api.the-odds-api.com/v4/sports", {"apiKey": key})
            if r.status_code == 401:
                return False, "ключ неверный"
            r.raise_for_status()
            left = r.headers.get("x-requests-remaining")
            return True, "отвечает" + (f", осталось кредитов: {left}" if left else "")
        out.append(("odds", "Букмекеры (The Odds API)", "odds", odds, False))
    return [{"key": k, "label": lbl, "group": g, "fn": fn, "warm": w} for k, lbl, g, fn, w in out]


def _call(p: dict) -> tuple[bool, str, str | None]:
    """Один вызов проверки → (ok, подробности, подсказка при ошибке)."""
    try:
        ok, detail = p["fn"]()
        return bool(ok), detail, None if ok else HINTS.get(p["group"])
    except requests.Timeout:
        return False, f"нет ответа за {int(TIMEOUT)} с", HINTS.get(p["group"])
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        detail, hint = explain_status(code, p["group"])
        if code == 400 and exc.response is not None:                 # причину отказа пишет сам сервер
            why = (exc.response.text or "").strip().replace("\n", " ")[:160]
            if why:
                detail += f": {why}"
        return False, detail, hint
    except requests.RequestException as exc:
        return False, f"нет соединения: {type(exc).__name__}", HINTS.get(p["group"])
    except TimeoutError:
        return False, f"нет ответа за {int(TIMEOUT)} с", HINTS.get(p["group"])
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) or type(exc).__name__
        return False, f"ошибка: {msg}"[:200], HINTS.get(p["group"])


def run(db: DB, items: list[dict] | None = None, warm_runs: int = WARM_RUNS) -> dict:
    items = items if items is not None else probes(db)

    def one(p):
        t0 = time.perf_counter()
        ok, detail, hint = _call(p)
        cold = int((time.perf_counter() - t0) * 1000)
        warm = None
        if ok and p.get("warm", True) and warm_runs > 0:
            times = []
            for _ in range(warm_runs):
                t1 = time.perf_counter()
                ok2, _, _ = _call(p)
                if not ok2:
                    break
                times.append(int((time.perf_counter() - t1) * 1000))
            warm = min(times) if times else None
        ms = warm if warm is not None else cold
        return {"key": p["key"], "label": p["label"], "group": p["group"], "ok": ok, "detail": detail,
                "ms": ms, "cold_ms": cold, "warm_ms": warm, "speed": speed_label(ms) if ok else None, "hint": hint}
    res = [r for _, r, _ in pmap(one, items, workers=12)]
    ok_ms = sorted(r["ms"] for r in res if r["ok"] and r["group"] == "polymarket")
    out = {"ts": now_ts(), "checks": res, "ok": all(r["ok"] for r in res),
           "polymarket_median_ms": ok_ms[len(ok_ms) // 2] if ok_ms else None}
    db.set_state("diagnostics", out)
    return out
