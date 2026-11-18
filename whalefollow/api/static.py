"""Файлы интерфейса (папка web/ — сборка frontend/): чтение из памяти, сжатая копия и метка версии (ETag).

Файлы из web/assets/ названы по хэшу содержимого (сборка Vite) — их можно кэшировать навсегда.
"""

from __future__ import annotations

import gzip
import hashlib
import threading
from importlib import resources
from pathlib import Path
from typing import Any

WEB_DIR = Path(str(resources.files("whalefollow").joinpath("web")))
CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".map": "application/json; charset=utf-8",
}
GZIP_MIN = 1024
COMPRESSIBLE = ("text/", "application/json", "application/manifest+json", "image/svg+xml")
_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
_lock = threading.Lock()


def _target(path: str) -> Path | None:
    rel = path.lstrip("/") or "index.html"
    target = (WEB_DIR / rel).resolve()
    if not target.is_relative_to(WEB_DIR.resolve()):
        return None  # попытка выйти за пределы папки
    if not target.is_file():
        if "." in rel.rsplit("/", 1)[-1]:
            return None
        target = WEB_DIR / "index.html"  # путь без расширения — роутинг на клиенте
    return target


def cache_control(path: str) -> str:
    """Хэшированные файлы сборки не меняются — браузер не перепроверяет их; остальное — по ETag."""
    if path.lstrip("/").startswith("assets/"):
        return "public, max-age=31536000, immutable"
    return "no-cache"


def static_entry(path: str) -> dict[str, Any] | None:
    """Файл интерфейса из памяти: содержимое, сжатая копия и ETag. Перечитывается, только если изменился."""
    target = _target(path)
    if target is None:
        return None
    st = target.stat()
    key = str(target)
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == (st.st_mtime_ns, st.st_size):
            return hit[1]
    data = target.read_bytes()
    ctype = CTYPES.get(target.suffix, "application/octet-stream")
    entry = {
        "data": data,
        "ctype": ctype,
        "etag": '"' + hashlib.sha1(data).hexdigest()[:16] + '"',
        "gz": gzip.compress(data, 6) if ctype.startswith(COMPRESSIBLE) and len(data) >= GZIP_MIN else None,
    }
    with _lock:
        _cache[key] = ((st.st_mtime_ns, st.st_size), entry)
    return entry
