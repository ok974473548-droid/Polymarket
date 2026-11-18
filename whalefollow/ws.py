"""Клиент WebSocket на библиотеке websockets (синхронный интерфейс — работает в обычных потоках).

Что сохранено от прежнего самописного клиента: wss/ws, прокси через CONNECT (HTTPS_PROXY, с учётом NO_PROXY),
текстовый keepalive (PING раз в N секунд — так требует Polymarket), ответ на текстовый PING сервера,
переподключение с нарастающей паузой, повторная подписка в on_open, статистика для «Диагностики».
Маскирование кадров, сборка фрагментов и ответы на служебные ping делает библиотека.
Колбэки вызываются в потоке чтения; исключения в них логируются и не рвут соединение.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from websockets.exceptions import WebSocketException
from websockets.sync.client import ClientConnection, connect

from . import __version__

log = logging.getLogger(__name__)
MAX_MESSAGE = 32 * 1024 * 1024            # снимок стакана бывает большим; без предела было раньше
KEEPALIVE_TEXT = {"PING", "PONG", "ping", "pong"}


class WSError(RuntimeError):
    pass


def _proxy_for(host: str | None, scheme: str) -> str | None:
    """Прокси только для wss (как раньше) и только если хост не в NO_PROXY."""
    if scheme != "wss" or not host:
        return None
    no = [h.strip() for h in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",") if h.strip()]
    if any(host == h or host.endswith("." + h.lstrip(".*")) for h in no):
        return None
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


class WSConnection:
    """Одно соединение: открыть, отправить текст, прочитать сообщение, закрыть."""

    def __init__(self, url: str, timeout: float = 10.0, headers: dict | None = None):
        self.url = url
        u = urlparse(url)
        self.scheme, self.host = u.scheme, u.hostname
        self.timeout = timeout
        self.headers = headers or {}
        self.conn: ClientConnection | None = None

    def connect(self) -> None:
        try:
            self.conn = connect(self.url, open_timeout=self.timeout, close_timeout=2,
                                proxy=_proxy_for(self.host, self.scheme),
                                additional_headers=self.headers or None,
                                user_agent_header=f"whalefollow/{__version__}",
                                ping_interval=None,          # keepalive — текстовый PING (требование Polymarket)
                                max_size=MAX_MESSAGE, legacy=True)   # закрываем сами (close)
        except (WebSocketException, OSError, TimeoutError) as exc:
            raise WSError(f"{type(exc).__name__}: {exc}") from exc

    def send_text(self, text: str) -> None:
        if self.conn is None:
            raise WSError("нет соединения")
        self.conn.send(text)

    def recv(self, timeout: float | None = None) -> str:
        """Следующее сообщение (текст). TimeoutError — ничего не пришло за timeout секунд."""
        if self.conn is None:
            raise WSError("нет соединения")
        data = self.conn.recv(timeout=timeout)
        return data if isinstance(data, str) else data.decode("utf-8", errors="replace")

    def close(self) -> None:
        c, self.conn = self.conn, None
        if c is not None:
            try:
                c.close()
            except (WebSocketException, OSError):
                pass


class WSClient:
    """Соединение с переподключением и keepalive. on_message(text) — каждое сообщение (кроме PING/PONG)."""

    def __init__(self, url: str, on_message: Callable[[str], None],
                 on_open: Callable[[WSClient], None] | None = None,
                 name: str = "ws", ping_text: str | None = "PING", ping_interval: float = 10.0,
                 read_timeout: float | None = None, max_backoff: float = 30.0):
        self.url, self.name = url, name
        self.on_message, self.on_open = on_message, on_open
        self.ping_text, self.ping_interval = ping_text, ping_interval
        self.read_timeout = read_timeout or max(30.0, ping_interval * 3)
        self.max_backoff = max_backoff
        self.conn: WSConnection | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats: dict[str, Any] = {"connected": False, "connects": 0, "messages": 0, "last_msg_ts": None,
                                      "last_rx_ts": None, "last_error": None, "connected_since": None,
                                      "connect_ms": None}
        self.generation = 0                                        # растёт с каждым новым соединением

    @property
    def connected(self) -> bool:
        return bool(self.stats["connected"])

    def alive(self, max_silence_s: float) -> bool:
        """Соединение открыто и за последние max_silence_s секунд что-то приходило (хотя бы PONG)."""
        rx = self.stats.get("last_rx_ts") or self.stats.get("connected_since") or 0
        return self.connected and time.time() - rx <= max_silence_s

    def start(self) -> WSClient:
        self._thread = threading.Thread(target=self._run, name=f"ws-{self.name}", daemon=True)
        self._thread.start()
        threading.Thread(target=self._pinger, name=f"ws-{self.name}-ping", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        c = self.conn
        if c is not None:
            c.close()

    def send(self, obj: Any) -> bool:
        text = obj if isinstance(obj, str) else json.dumps(obj)
        try:
            if self.conn and self.connected:
                self.conn.send_text(text)
                return True
        except (OSError, WSError, WebSocketException) as exc:
            self.stats["last_error"] = repr(exc)[:200]
        return False

    def _pinger(self) -> None:
        while not self._stop.wait(self.ping_interval):
            if self.ping_text and self.connected:
                self.send(self.ping_text)

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.conn = WSConnection(self.url)
                self.conn.connect()
                self.generation += 1
                self.stats.update(connected=True, connects=self.stats["connects"] + 1, connected_since=time.time(),
                                  connect_ms=int((time.time() - t0) * 1000), last_error=None)
                backoff = 1.0
                if self.on_open:
                    self.on_open(self)
                self._read_loop()
            except Exception as exc:  # noqa: BLE001 — сеть: переподключаемся
                if not self._stop.is_set():
                    self.stats["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                    log.info("WebSocket %s: %s", self.name, self.stats["last_error"])
            finally:
                self.stats["connected"] = False
                c = self.conn
                if c is not None:
                    c.close()
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.max_backoff)

    def _read_loop(self) -> None:
        while not self._stop.is_set() and self.conn is not None:
            text = self.conn.recv(timeout=self.read_timeout)      # тишина дольше read_timeout — обрыв
            self.stats["last_rx_ts"] = time.time()                # любое сообщение, включая PONG: связь жива
            if text in KEEPALIVE_TEXT:
                if text in ("PING", "ping"):
                    self.send("PONG" if text == "PING" else "pong")
                continue
            self.stats["messages"] += 1
            self.stats["last_msg_ts"] = time.time()
            try:
                self.on_message(text)
            except Exception:  # noqa: BLE001
                log.exception("обработка сообщения %s", self.name)
