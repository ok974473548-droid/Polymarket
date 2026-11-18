"""Минимальный клиент WebSocket (RFC 6455) на стандартной библиотеке — без внешних зависимостей.

Возможности: wss/ws, прокси через CONNECT (HTTPS_PROXY), маскирование кадров клиента,
сборка фрагментов, ответы на ping, текстовый keepalive (PING раз в N секунд — так требует Polymarket),
переподключение с нарастающей паузой, статистика для диагностики.
Колбэки вызываются в потоке чтения; исключения в них логируются и не рвут соединение.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
from typing import Callable
from urllib.parse import urlparse

log = logging.getLogger(__name__)
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(RuntimeError):
    pass


def _mask(data: bytes, key: bytes) -> bytes:
    """XOR с 4-байтным ключом (маскирование кадров) — одним целочисленным действием, без цикла по байтам."""
    n = len(data)
    if not n:
        return data
    k = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(n, "big")


def _proxy_for(host: str, scheme: str) -> str | None:
    if scheme != "wss":
        return None
    no = [h.strip() for h in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",") if h.strip()]
    if any(host == h or host.endswith("." + h.lstrip(".")) for h in no):
        return None
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


class WSConnection:
    """Одно соединение: рукопожатие, отправка и чтение кадров."""

    def __init__(self, url: str, timeout: float = 10.0, headers: dict | None = None):
        self.url = url
        u = urlparse(url)
        self.scheme, self.host = u.scheme, u.hostname
        self.port = u.port or (443 if u.scheme == "wss" else 80)
        self.path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        self.timeout = timeout
        self.headers = headers or {}
        self.sock: socket.socket | None = None
        self._buf = bytearray()
        self._wlock = threading.Lock()

    # ---------- соединение
    def connect(self) -> None:
        proxy = _proxy_for(self.host, self.scheme)
        if proxy:
            p = urlparse(proxy)
            raw = socket.create_connection((p.hostname, p.port or 80), timeout=self.timeout)
            req = f"CONNECT {self.host}:{self.port} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            if p.username:
                cred = base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode()
                req += f"Proxy-Authorization: Basic {cred}\r\n"
            raw.sendall((req + "\r\n").encode())
            head = self._read_head(raw)
            if b" 200" not in head.split(b"\r\n", 1)[0]:
                raw.close()
                raise WSError("прокси не открыл соединение: " + head.split(b"\r\n", 1)[0].decode(errors="replace"))
        else:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)    # без задержки Нейгла: кадры уходят сразу
        if self.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=self.host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {self.path} HTTP/1.1", f"Host: {self.host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13", "User-Agent: whalefollow"]
        lines += [f"{k}: {v}" for k, v in self.headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = self._read_head(self.sock)
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise WSError("сервер не перешёл на WebSocket: " + status.decode(errors="replace"))
        expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode().lower()
        if expect.encode() not in head.lower():
            raise WSError("неверный ответ рукопожатия")

    def _read_head(self, s: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                raise WSError("соединение закрыто во время рукопожатия")
            data += chunk
            if len(data) > 65536:
                raise WSError("слишком длинный ответ рукопожатия")
        head, _, rest = bytes(data).partition(b"\r\n\r\n")
        self._buf += rest                                          # кадры, пришедшие сразу за ответом
        return head

    def close(self) -> None:
        try:
            if self.sock:
                self.send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    # ---------- кадры
    def send_frame(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytearray([0x80 | opcode])
        if n < 126:
            head.append(0x80 | n)
        elif n < 65536:
            head.append(0x80 | 126)
            head += struct.pack("!H", n)
        else:
            head.append(0x80 | 127)
            head += struct.pack("!Q", n)
        mask = os.urandom(4)
        head += mask
        masked = _mask(payload, mask)
        with self._wlock:
            if not self.sock:
                raise WSError("нет соединения")
            self.sock.sendall(bytes(head) + masked)

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode("utf-8"))

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WSError("соединение закрыто сервером")
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv_message(self) -> tuple[int, bytes]:
        """Следующее сообщение целиком: (opcode, данные). Управляющие кадры возвращаются отдельно."""
        parts, first_op = [], None
        while True:
            b1, b2 = self._read_exact(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else None
            data = self._read_exact(n) if n else b""
            if mask:
                data = _mask(data, mask)
            if op >= 0x8:                                          # управляющий кадр — не фрагментируется
                return op, data
            if op != 0:
                first_op = op
            parts.append(data)
            if fin:
                return first_op or 0x1, b"".join(parts)


class WSClient:
    """Соединение с переподключением и keepalive. on_message(text) — каждый текстовый кадр (кроме PING/PONG)."""

    def __init__(self, url: str, on_message: Callable[[str], None], on_open: Callable[["WSClient"], None] | None = None,
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
        self.stats = {"connected": False, "connects": 0, "messages": 0, "last_msg_ts": None, "last_rx_ts": None,
                      "last_error": None, "connected_since": None, "connect_ms": None}
        self.generation = 0                                        # растёт с каждым новым соединением

    @property
    def connected(self) -> bool:
        return bool(self.stats["connected"])

    def alive(self, max_silence_s: float) -> bool:
        """Соединение открыто и за последние max_silence_s секунд что-то приходило (хотя бы PONG)."""
        rx = self.stats.get("last_rx_ts") or self.stats.get("connected_since") or 0
        return self.connected and time.time() - rx <= max_silence_s

    def start(self) -> "WSClient":
        self._thread = threading.Thread(target=self._run, name=f"ws-{self.name}", daemon=True)
        self._thread.start()
        threading.Thread(target=self._pinger, name=f"ws-{self.name}-ping", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self.conn:
            self.conn.close()

    def send(self, obj) -> bool:
        text = obj if isinstance(obj, str) else json.dumps(obj)
        try:
            if self.conn and self.connected:
                self.conn.send_text(text)
                return True
        except (OSError, WSError) as exc:
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
                self.conn.sock.settimeout(self.read_timeout)
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
                if self.conn:
                    try:
                        self.conn.sock and self.conn.sock.close()
                    except OSError:
                        pass
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, self.max_backoff)

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            op, data = self.conn.recv_message()
            self.stats["last_rx_ts"] = time.time()                 # любой кадр, включая PONG: связь жива
            if op == 0x9:                                          # ping сервера → pong
                self.conn.send_frame(0xA, data)
                continue
            if op == 0xA:
                continue
            if op == 0x8:
                raise WSError("сервер закрыл соединение")
            text = data.decode("utf-8", errors="replace")
            if text in ("PONG", "PING", "pong", "ping"):
                if text in ("PING", "ping"):
                    self.send("PONG" if text == "PING" else "pong")
                continue
            self.stats["messages"] += 1
            self.stats["last_msg_ts"] = time.time()
            try:
                self.on_message(text)
            except Exception:  # noqa: BLE001
                log.exception("обработка сообщения %s", self.name)
