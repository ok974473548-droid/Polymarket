"""Shared HTTPS client on the standard library: persistent connections, gzip, proxies.

Every call to Polymarket used to open a new TCP + TLS connection (urllib). Here idle connections are
kept per host and reused (HTTP/1.1 keep-alive), answers are requested gzip-compressed, and the
system proxy settings (HTTPS_PROXY / NO_PROXY or the OS settings, as urllib reads them) still apply.
"""

import base64
import http.client
import socket
import ssl
import threading
import time
import zlib
from urllib.parse import unquote, urlsplit
from urllib.request import getproxies, proxy_bypass

IDLE_PER_HOST = 16          # idle connections kept per host
IDLE_SECONDS = 50           # servers drop idle keep-alive connections after about a minute
_ctx = ssl.create_default_context()
_lock = threading.Lock()
_idle = {}                  # host -> [(connection, last_used)]
STATS = {"requests": 0, "reused": 0, "opened": 0, "bytes": 0, "bytes_plain": 0}


class NetError(OSError):
    """Network failure (no answer, broken connection, timeout)."""


def proxy_for(host):
    """(proxy_host, proxy_port, auth_header or None) for an HTTPS host, or None for a direct connection."""
    try:
        if proxy_bypass(host):
            return None
    except OSError:
        pass
    proxy = getproxies().get("https")
    if not proxy:
        return None
    p = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    auth = None
    if p.username:
        token = base64.b64encode(f"{unquote(p.username)}:{unquote(p.password or '')}".encode()).decode()
        auth = "Basic " + token
    return p.hostname, p.port or 80, auth


def _open(host, port, timeout):
    proxy = proxy_for(host)
    if proxy:
        conn = http.client.HTTPSConnection(proxy[0], proxy[1], context=_ctx, timeout=timeout)
        conn.set_tunnel(host, port, headers={"Proxy-Authorization": proxy[2]} if proxy[2] else None)
    else:
        conn = http.client.HTTPSConnection(host, port, context=_ctx, timeout=timeout)
    conn.connect()
    try:
        conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    with _lock:
        STATS["opened"] += 1
    return conn


def _take(key):
    now = time.monotonic()
    with _lock:
        pool = _idle.get(key) or []
        while pool:
            conn, used = pool.pop()
            if now - used < IDLE_SECONDS:
                return conn
            conn.close()
    return None


def _give(key, conn):
    with _lock:
        pool = _idle.setdefault(key, [])
        if len(pool) < IDLE_PER_HOST:
            pool.append((conn, time.monotonic()))
            return
    conn.close()


def close_all():
    with _lock:
        for pool in _idle.values():
            for conn, _ in pool:
                conn.close()
        _idle.clear()


def _inflate(body, encoding, limit):
    if encoding == "gzip":
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        d = zlib.decompressobj()
    else:
        return body
    try:
        out = d.decompress(body, limit + 1)
    except zlib.error as exc:
        raise NetError("Повреждённый сжатый ответ") from exc
    return out


def get(url, *, headers=None, timeout=15, limit=16 * 1024 * 1024):
    """GET over a pooled HTTPS connection. Returns (status, headers, body) with the body decompressed.

    A reused connection that the server has already closed is retried once on a fresh one.
    """
    u = urlsplit(url)
    if u.scheme != "https":
        raise ValueError("Поддерживается только https")
    host, port = u.hostname, u.port or 443
    key = (host, port)
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    hdrs = {"Accept-Encoding": "gzip", "Connection": "keep-alive", **(headers or {})}
    for attempt in range(2):
        conn = _take(key)
        reused = conn is not None
        try:
            if conn is None:
                conn = _open(host, port, timeout)
            elif conn.sock is not None:
                conn.sock.settimeout(timeout)
            conn.request("GET", path, headers=hdrs)
            resp = conn.getresponse()
            declared = resp.getheader("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                conn.close()
                raise NetError("Слишком большой ответ источника")
            body = resp.read()  # read to the end, so the connection can carry the next request
        except (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError, http.client.BadStatusLine) as exc:
            if conn is not None:
                conn.close()
            if reused and attempt == 0:
                continue
            raise NetError(str(exc) or "Соединение оборвалось") from exc
        except (OSError, http.client.HTTPException) as exc:
            if conn is not None:
                conn.close()
            raise NetError(str(exc) or "Источник недоступен") from exc
        if resp.will_close:
            conn.close()
        else:
            _give(key, conn)
        raw_len = len(body)
        body = _inflate(body, resp.getheader("Content-Encoding"), limit)
        with _lock:
            STATS["requests"] += 1
            STATS["reused"] += int(reused)
            STATS["bytes"] += raw_len
            STATS["bytes_plain"] += len(body)
        return resp.status, resp.headers, body
    raise NetError("Соединение оборвалось")
