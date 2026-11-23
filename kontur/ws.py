"""Minimal WebSocket client on the standard library (RFC 6455, client side).

Enough for Polymarket's public streams: TLS, the system HTTPS proxy (CONNECT), masking, ping/pong,
fragmented messages, close, read timeouts that keep partial frames. No third-party packages.
"""

import base64
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlsplit

from . import net

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_MESSAGE = 8 * 1024 * 1024


class WsError(ConnectionError):
    """The stream broke or the server refused the upgrade."""


def _proxy_for(host):
    """HTTPS proxy from the system settings (environment or OS), honouring the bypass list."""
    p = net.proxy_for(host)
    return p


def _mask(data, key):
    n = len(data)
    if not n:
        return b""
    m = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")


def _read_headers(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(1)
        if not chunk:
            raise WsError("Соединение закрыто во время рукопожатия")
        data += chunk
        if len(data) > 65536:
            raise WsError("Слишком длинный ответ на рукопожатие")
    return data.decode("latin-1")


class WebSocket:
    def __init__(self, url, *, timeout=15, headers=None):
        u = urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError("Нужен адрес ws:// или wss://")
        self.host, self.secure = u.hostname, u.scheme == "wss"
        self.port = u.port or (443 if self.secure else 80)
        self.path = (u.path or "/") + ("?" + u.query if u.query else "")
        self._send_lock = threading.Lock()
        self._buf = bytearray()   # bytes received but not yet parsed into frames (survives timeouts)
        self._parts, self._kind = [], None
        self.sock = self._open(timeout, headers or {})
        self.closed = False

    def _open(self, timeout, headers):
        proxy = _proxy_for(self.host) if self.secure else None
        if proxy:
            raw = socket.create_connection(proxy[:2], timeout=timeout)
            auth = f"Proxy-Authorization: {proxy[2]}\r\n" if proxy[2] else ""
            raw.sendall(f"CONNECT {self.host}:{self.port} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n{auth}\r\n".encode())
            status = _read_headers(raw).split("\r\n", 1)[0]
            if " 200" not in status:
                raw.close()
                raise WsError("Прокси не открыл соединение: " + status)
        else:
            raw = socket.create_connection((self.host, self.port), timeout=timeout)
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host) if self.secure else raw
        key = base64.b64encode(os.urandom(16)).decode()
        extra = "".join(f"{k}: {v}\r\n" for k, v in headers.items())
        sock.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{extra}\r\n").encode())
        reply = _read_headers(sock)
        if " 101" not in reply.split("\r\n", 1)[0]:
            sock.close()
            raise WsError("Сервер отказал в WebSocket: " + reply.split("\r\n", 1)[0])
        return sock

    def settimeout(self, seconds):
        self.sock.settimeout(seconds)

    def _send(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        body = _mask(payload, mask)
        with self._send_lock:
            self.sock.sendall(bytes(header) + mask + body)

    def send(self, text):
        self._send(OP_TEXT, text.encode())

    def ping(self, data=b""):
        self._send(OP_PING, data)

    def _frame(self):
        """Parse one complete frame from the buffer: (fin, opcode, payload), or None if more bytes are needed."""
        buf = self._buf
        if len(buf) < 2:
            return None
        b1, b2 = buf[0], buf[1]
        n, i = b2 & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            n, i = struct.unpack("!H", buf[2:4])[0], 4
        elif n == 127:
            if len(buf) < 10:
                return None
            n, i = struct.unpack("!Q", buf[2:10])[0], 10
        if n > MAX_MESSAGE:
            raise WsError("Слишком большое сообщение")
        mask = None
        if b2 & 0x80:
            if len(buf) < i + 4:
                return None
            mask, i = bytes(buf[i:i + 4]), i + 4
        if len(buf) < i + n:
            return None
        payload = bytes(buf[i:i + n])
        del buf[:i + n]
        if mask:
            payload = _mask(payload, mask)
        return b1 & 0x80, b1 & 0x0F, payload

    def recv(self):
        """Next text message (str). Answers pings itself; raises WsError when the stream ends.

        A socket timeout (TimeoutError) can be raised and recv called again: partial data stays buffered.
        """
        while True:
            frame = self._frame()
            if frame is None:
                chunk = self.sock.recv(65536)
                if not chunk:
                    raise WsError("Соединение закрыто сервером")
                self._buf += chunk
                continue
            fin, opcode, payload = frame
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.closed = True
                raise WsError("Сервер закрыл поток")
            if opcode in (OP_TEXT, OP_BIN):
                self._parts, self._kind = [payload], opcode
            elif opcode == OP_CONT:
                self._parts.append(payload)
                if sum(len(x) for x in self._parts) > MAX_MESSAGE:
                    raise WsError("Слишком большое сообщение")
            if fin and self._kind is not None:
                data = b"".join(self._parts)
                self._parts, self._kind = [], None
                return data.decode("utf-8", "replace")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self._send(OP_CLOSE, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
