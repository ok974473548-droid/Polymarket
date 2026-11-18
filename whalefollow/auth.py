"""Вход в интерфейс по паролю.

Пароль задаётся переменной окружения WF_UI_PASSWORD. Если она пустая — вход не нужен
(удобно, пока интерфейс открыт только на этом компьютере: порт в docker-compose привязан к 127.0.0.1).

После входа браузер получает cookie wf_session (HttpOnly, SameSite=Strict, 30 дней):
подписанный срок действия. Смена пароля делает все старые входы недействительными.
Перебор пароля: после 5 неудачных попыток с одного адреса за 10 минут вход закрывается на 10 минут.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time

from .db import DB

COOKIE = "wf_session"
TTL_S = 30 * 86400
MAX_FAILS, WINDOW_S = 5, 600


class Auth:
    def __init__(self, db: DB, password: str | None = None):
        self.password = password if password is not None else os.environ.get("WF_UI_PASSWORD", "")
        server_secret = db.get_setting("auth_secret")
        if not server_secret:
            server_secret = secrets.token_hex(32)
            db.set_setting("auth_secret", server_secret)
        # ключ подписи зависит от пароля: сменили пароль — старые cookie перестают действовать
        self._key = hashlib.sha256((server_secret + "|" + self.password).encode()).digest()
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def required(self) -> bool:
        return bool(self.password)

    def _sign(self, exp: int) -> str:
        return hmac.new(self._key, str(exp).encode(), hashlib.sha256).hexdigest()

    def token(self, now: float | None = None) -> str:
        exp = int((now or time.time()) + TTL_S)
        return f"{exp}.{self._sign(exp)}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not self.required:
            return True
        if not token or "." not in token:
            return False
        exp_s, sig = token.split(".", 1)
        if not exp_s.isdigit() or int(exp_s) < (now or time.time()):
            return False
        return hmac.compare_digest(sig, self._sign(int(exp_s)))

    def blocked(self, ip: str, now: float | None = None) -> bool:
        now = now or time.time()
        with self._lock:
            recent = [t for t in self._fails.get(ip, []) if now - t < WINDOW_S]
            self._fails[ip] = recent
            return len(recent) >= MAX_FAILS

    def login(self, password: str, ip: str, now: float | None = None) -> str | None:
        """Токен при верном пароле, иначе None (и попытка засчитывается)."""
        if self.blocked(ip, now):
            return None
        if self.required and hmac.compare_digest(str(password or "").encode(), self.password.encode()):
            with self._lock:
                self._fails.pop(ip, None)
            return self.token(now)
        with self._lock:
            self._fails.setdefault(ip, []).append(now or time.time())
        time.sleep(0.3)  # замедляет перебор
        return None

    @staticmethod
    def cookie_header(token: str, max_age: int = TTL_S) -> str:
        return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Strict"

    @staticmethod
    def from_cookie_header(header: str | None) -> str | None:
        for part in (header or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE:
                return v
        return None
