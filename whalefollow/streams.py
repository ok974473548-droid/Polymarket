"""Потоки Polymarket в реальном времени (WebSocket) — данные приходят сами, без ожидания опроса.

LiveBooks    — стаканы рынков, где есть наши ставки, ждущие сигналы и позиции китов (канал market биржи CLOB).
               Снимок приходит при подписке, дальше — только изменения. Агент берёт стакан из памяти
               за доли миллисекунды вместо запроса /book.
TradeStream  — лента сделок Polymarket (RTDS, тема activity/trades). Из неё берутся только сделки китов
               из списка; остальные отбрасываются сразу и нигде не сохраняются. Сделка кита попадает
               к агенту через доли секунды после исполнения, а не на следующем шаге опроса.
Streams      — запускает оба потока, держит списки подписок в актуальном состоянии и передаёт сделки агенту.

Если поток недоступен (сеть, прокси без WebSocket), всё работает как раньше — опросом; на экране
«Диагностика» видно состояние каждого потока.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable

from .ws import WSClient

log = logging.getLogger(__name__)

MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
LIVE_URL = "wss://ws-live-data.polymarket.com"


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class LiveBooks:
    """Стаканы в памяти, обновляемые каналом market. Стакан выдаётся, только если он получен в текущем
    соединении и связь жива — иначе None, и агент берёт стакан обычным запросом."""

    MAX_ASSETS = 500
    STALE_S = 25.0  # без входящих кадров дольше (PING каждые 10 с) — поток считаем неживым

    def __init__(self, url: str = MARKET_URL, client_cls=WSClient, ping_interval: float = 10.0):
        self.url, self._cls, self.ping_interval = url, client_cls, ping_interval
        self._client: WSClient | None = None
        self.lock = threading.Lock()
        self.books: dict[str, dict] = {}
        self.wanted: dict[str, float] = {}  # токен → когда понадобился (вытеснение старых)
        self.stats = {"updates": 0, "hits": 0, "misses": 0}

    # ---------- подписки
    def _ensure_client(self) -> None:
        if self._client is None:
            self._client = self._cls(
                self.url,
                self._on_message,
                on_open=self._on_open,
                name="books",
                ping_text="PING",
                ping_interval=self.ping_interval,
            ).start()

    def want(self, tokens) -> None:
        """Добавить токены к подписке (старые вытесняются, если их больше MAX_ASSETS)."""
        tokens = [str(t) for t in tokens if t]
        if not tokens:
            return
        now = time.time()
        with self.lock:
            new = [t for t in dict.fromkeys(tokens) if t not in self.wanted]
            for t in tokens:
                self.wanted[t] = now
            drop = []
            if len(self.wanted) > self.MAX_ASSETS:
                drop = sorted(self.wanted, key=self.wanted.get)[: len(self.wanted) - self.MAX_ASSETS]
                for t in drop:
                    self.wanted.pop(t, None)
                    self.books.pop(t, None)
                new = [t for t in new if t in self.wanted]
        self._ensure_client()
        if drop:
            self._client.send({"assets_ids": drop, "operation": "unsubscribe"})
        if new:
            self._client.send({"assets_ids": new, "operation": "subscribe", "custom_feature_enabled": True})

    def set_wanted(self, tokens) -> None:
        """Подписка ровно на этот набор (кроме токенов, запрошенных агентом за последние 10 минут)."""
        keep = {str(t) for t in tokens if t}
        now = time.time()
        with self.lock:
            gone = [t for t, ts in self.wanted.items() if t not in keep and now - ts > 600]
            for t in gone:
                self.wanted.pop(t, None)
                self.books.pop(t, None)
        if gone and self._client is not None:
            self._client.send({"assets_ids": gone, "operation": "unsubscribe"})
        self.want(list(keep))

    def _on_open(self, client: WSClient) -> None:
        with self.lock:
            ids = list(self.wanted)
            self.books.clear()  # после переподключения ждём свежие снимки
        if ids:
            client.send({"assets_ids": ids, "type": "market", "custom_feature_enabled": True})

    # ---------- сообщения
    def _on_message(self, text: str) -> None:
        try:
            data = json.loads(text)
        except ValueError:
            return
        for ev in data if isinstance(data, list) else [data]:
            if isinstance(ev, dict):
                self.apply(ev)

    def apply(self, ev: dict) -> None:
        et = ev.get("event_type") or ev.get("type")
        gen = self._client.generation if self._client else 0
        now = time.time()
        with self.lock:
            if et == "book":
                aid = str(ev.get("asset_id") or "")
                if not aid:
                    return
                bids = {
                    p: s
                    for p, s in (
                        (_f(x.get("price")), _f(x.get("size"))) for x in (ev.get("bids") or ev.get("buys") or [])
                    )
                    if p is not None and s
                }
                asks = {
                    p: s
                    for p, s in (
                        (_f(x.get("price")), _f(x.get("size"))) for x in (ev.get("asks") or ev.get("sells") or [])
                    )
                    if p is not None and s
                }
                old = self.books.get(aid) or {}
                self.books[aid] = {"bids": bids, "asks": asks, "gen": gen, "ts": now, "last": old.get("last")}
            elif et == "price_change":
                for ch in ev.get("price_changes") or ev.get("changes") or []:
                    aid = str(ch.get("asset_id") or ev.get("asset_id") or "")
                    b = self.books.get(aid)
                    p, s = _f(ch.get("price")), _f(ch.get("size"))
                    if not b or p is None or s is None:
                        continue
                    side = b["bids"] if str(ch.get("side", "")).upper() == "BUY" else b["asks"]
                    if s <= 0:
                        side.pop(p, None)
                    else:
                        side[p] = s
                    b["ts"] = now
            elif et == "last_trade_price":
                b = self.books.get(str(ev.get("asset_id") or ""))
                if b is not None:
                    b["last"] = _f(ev.get("price"))
            else:
                return
            self.stats["updates"] += 1

    # ---------- чтение
    def live(self) -> bool:
        return self._client is not None and self._client.alive(self.STALE_S)

    def get(self, token_id: str):
        """(bids по убыванию, asks по возрастанию) из памяти или None, если свежего стакана нет."""
        token_id = str(token_id)
        c = self._client
        with self.lock:
            b = self.books.get(token_id)
            ok = b is not None and c is not None and b["gen"] == c.generation and c.alive(self.STALE_S)
            if ok:
                bids = sorted(b["bids"].items(), key=lambda x: -x[0])
                asks = sorted(b["asks"].items(), key=lambda x: x[0])
                self.wanted[token_id] = time.time()
            self.stats["hits" if ok else "misses"] += 1
        return (bids, asks) if ok else None

    def status(self) -> dict:
        c = self._client
        st = dict(c.stats) if c else {}
        with self.lock:
            n_books = len(self.books)
        return {
            "started": c is not None,
            "connected": bool(c and c.connected),
            "live": self.live(),
            "subscribed": len(self.wanted),
            "books": n_books,
            "updates": self.stats["updates"],
            "hits": self.stats["hits"],
            "misses": self.stats["misses"],
            "connects": st.get("connects", 0),
            "connect_ms": st.get("connect_ms"),
            "last_msg_age_s": _age(st.get("last_msg_ts")),
            "error": st.get("last_error"),
        }

    def stop(self) -> None:
        if self._client:
            self._client.stop()


def _age(ts) -> float | None:
    return round(time.time() - ts, 1) if ts else None


def to_trade_row(p: dict) -> dict | None:
    """Сделка из ленты RTDS → строка в формате Data API /v2/trades (её понимает parse_trade)."""
    wallet, asset, side = p.get("proxyWallet"), p.get("asset"), str(p.get("side") or "").upper()
    price, size = _f(p.get("price")), _f(p.get("size"))
    if not wallet or not asset or side not in ("BUY", "SELL") or price is None or not size:
        return None
    ts = int(_f(p.get("timestamp")) or time.time())
    if ts > 10**11:  # миллисекунды → секунды
        ts //= 1000
    # числа передаются как пришли: parse_trade сам приведёт их к float, а повтор той же сделки из опроса
    # (с другой записью чисел) отсекается по существу сделки — pipeline.natural_key
    return {
        "transaction_hash": p.get("transactionHash"),
        "proxy_wallet": str(wallet).lower(),
        "token_id": str(asset),
        "side": side,
        "size": p.get("size"),
        "price": p.get("price"),
        "timestamp": ts,
        "condition_id": p.get("conditionId"),
        "title": p.get("title"),
        "outcome": p.get("outcome"),
    }


class TradeStream:
    """Лента сделок Polymarket; наружу отдаются только сделки кошельков из списка."""

    STALE_S = 30.0

    def __init__(
        self,
        on_trades: Callable[[list[dict]], None],
        url: str = LIVE_URL,
        client_cls=WSClient,
        ping_interval: float = 5.0,
    ):
        self.on_trades, self.url, self._cls, self.ping_interval = on_trades, url, client_cls, ping_interval
        self._client: WSClient | None = None
        self.wallets: frozenset = frozenset()
        self.stats = {"seen": 0, "matched": 0, "last_match_ts": None, "lag_s": None}

    def set_wallets(self, wallets) -> None:
        self.wallets = frozenset(str(w).lower() for w in wallets if w)
        if self.wallets and self._client is None:  # пока китов нет — поток не нужен
            self._client = self._cls(
                self.url,
                self._on_message,
                on_open=self._on_open,
                name="trades",
                ping_text="PING",
                ping_interval=self.ping_interval,
            ).start()

    @staticmethod
    def _on_open(client: WSClient) -> None:
        client.send({"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]})

    def _on_message(self, text: str) -> None:
        if not self.wallets or "proxyWallet" not in text:  # быстрый отсев без разбора JSON
            return
        try:
            msg = json.loads(text)
        except ValueError:
            return
        rows = []
        for m in msg if isinstance(msg, list) else [msg]:
            p = m.get("payload") if isinstance(m, dict) else None
            if not isinstance(p, dict):
                continue
            self.stats["seen"] += 1
            if str(p.get("proxyWallet") or "").lower() not in self.wallets:
                continue
            row = to_trade_row(p)
            if row:
                rows.append(row)
        if rows:
            now = time.time()
            self.stats["matched"] += len(rows)
            self.stats["last_match_ts"] = now
            self.stats["lag_s"] = round(now - max(r["timestamp"] for r in rows), 1)
            self.on_trades(rows)

    def live(self) -> bool:
        return self._client is not None and self._client.alive(self.STALE_S)

    def status(self) -> dict:
        c = self._client
        st = dict(c.stats) if c else {}
        return {
            "started": c is not None,
            "connected": bool(c and c.connected),
            "live": self.live(),
            "wallets": len(self.wallets),
            "messages": st.get("messages", 0),
            "matched": self.stats["matched"],
            "last_match_age_s": _age(self.stats["last_match_ts"]),
            "lag_s": self.stats["lag_s"],
            "connects": st.get("connects", 0),
            "connect_ms": st.get("connect_ms"),
            "last_msg_age_s": _age(st.get("last_msg_ts")),
            "error": st.get("last_error"),
        }

    def stop(self) -> None:
        if self._client:
            self._client.stop()


class Streams:
    """Оба потока + передача сделок агенту в отдельном потоке (чтение сети не ждёт обработки)."""

    REFRESH_S = 30.0

    def __init__(
        self,
        db,
        agent=None,
        wallets_fn: Callable[[], list] | None = None,
        tokens_fn: Callable[[], list] | None = None,
        books: LiveBooks | None = None,
        trades: TradeStream | None = None,
    ):
        self.db, self.agent = db, agent
        self.wallets_fn, self.tokens_fn = wallets_fn, tokens_fn
        self.books = books or LiveBooks()
        self.trades = trades or TradeStream(self._enqueue)
        if trades is not None:
            trades.on_trades = self._enqueue
        self.q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self.processed = 0
        self.errors = 0

    def _enqueue(self, rows: list[dict]) -> None:
        self.q.put(rows)

    def refresh(self) -> None:
        """Обновить списки: кошельки китов и токены, по которым нужен живой стакан."""
        if self.wallets_fn:
            self.trades.set_wallets(self.wallets_fn())
        if self.tokens_fn:
            self.books.set_wanted(self.tokens_fn())

    def start(self) -> Streams:
        threading.Thread(target=self._worker, name="streams-worker", daemon=True).start()
        threading.Thread(target=self._refresher, name="streams-refresh", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.books.stop()
        self.trades.stop()

    def _refresher(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("обновление подписок потоков")
            self._stop.wait(self.REFRESH_S)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                rows = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            while True:  # пачкой всё, что успело прийти
                try:
                    rows = rows + self.q.get_nowait()
                except queue.Empty:
                    break
            try:
                if self.agent is not None:
                    self.agent.process_stream_trades(rows)
                self.processed += len(rows)
            except Exception:
                self.errors += 1
                log.exception("обработка сделок из потока")

    def status(self) -> dict:
        return {
            "books": self.books.status(),
            "trades": self.trades.status(),
            "queue": self.q.qsize(),
            "processed": self.processed,
            "errors": self.errors,
        }
