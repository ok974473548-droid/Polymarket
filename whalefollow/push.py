"""Push-уведомления на телефон и компьютер (Web Push, без сторонних служб).

Как работает: браузер устройства подписывается (экран «Настройки» → «Уведомления» → «Push на этом устройстве»)
и отдаёт адрес своей службы push и ключи шифрования. Когда появляется уведомление о новом сигнале или выходе
кита, этот модуль шифрует короткое сообщение ключом устройства (RFC 8291, aes128gcm) и отправляет его на этот
адрес, подписав нашим ключом VAPID (RFC 8292). Ключ VAPID создаётся один раз и хранится в базе.

Что отправлять, решают настройки пользователя: «Push» в «Куда» и темы в «О чём» (как для Telegram).
Отправка — в фоновом потоке, чтобы не задерживать агента. Подписки, которые служба push отвергла
(устройство отписалось), удаляются.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import struct
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import net
from .config import DEFAULT_SETTINGS
from .db import DB, now_ts
from .notifier import KIND_TO_SETTING

log = logging.getLogger(__name__)

# какие уведомления уходят в push: сигналы и выход кита — главное; остальное — по настройкам «О чём»
PUSH_KINDS = {"signal", "whale_exit", "whale_check", "pause", "stopped", "system", "weekly", "event_edge"}
RECORD_SIZE = 4096
TTL_S = 6 * 3600  # сигнал старше нескольких часов уже не нужен
# контакт в подписи VAPID нужен службам push для связи с отправителем; адрес пользователя не используем
DEFAULT_CONTACT = "mailto:whalefollow@example.com"


def contact() -> str:
    """WF_PUSH_CONTACT, иначе адрес интерфейса (если он https), иначе нейтральный адрес."""
    pub = os.environ.get("WF_PUBLIC_URL", "")
    return os.environ.get("WF_PUSH_CONTACT") or (pub if pub.startswith("https://") else DEFAULT_CONTACT)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]  # длина ≤ 32 — хватает одного блока


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, server_key: Any = None) -> bytes:
    """Шифрование сообщения для одного устройства: RFC 8291 (ключи) + RFC 8188 (aes128gcm, одна запись)."""
    ua_public = unb64u(p256dh)
    auth_secret = unb64u(auth)
    as_key = server_key or ec.generate_private_key(ec.SECP256R1())  # одноразовый ключ на каждое сообщение
    as_public = _public_bytes(as_key.public_key())
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) + 17 > RECORD_SIZE:
        raise ValueError("Сообщение слишком длинное для push")
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # \x02 — признак последней записи
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + body


class Vapid:
    """Ключ отправителя (VAPID): подпись запросов к службам push."""

    def __init__(self, private_key: ec.EllipticCurvePrivateKey, contact: str = DEFAULT_CONTACT):
        self.key = private_key
        self.contact = contact
        self.public = b64u(_public_bytes(private_key.public_key()))

    @classmethod
    def load(cls, db: DB) -> Vapid:
        """Ключ из базы; при первом обращении создаётся и сохраняется."""
        st = db.get_state("push_vapid")
        if st and st.get("private"):
            key = serialization.load_pem_private_key(st["private"].encode(), password=None)
            assert isinstance(key, ec.EllipticCurvePrivateKey)
            return cls(key, contact())
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        db.set_state("push_vapid", {"private": pem.decode(), "created_ts": now_ts()})
        return cls(key, contact())

    def header(self, endpoint: str, ttl_s: int = 12 * 3600) -> str:
        u = urlsplit(endpoint)
        claims = {"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + ttl_s, "sub": self.contact}
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        body = b64u(json.dumps(claims, separators=(",", ":")).encode())
        signing = f"{head}.{body}".encode()
        r, s = decode_dss_signature(self.key.sign(signing, ec.ECDSA(hashes.SHA256())))
        sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"vapid t={head}.{body}.{b64u(sig)}, k={self.public}"


def link(kind: str, ref: str) -> str:
    """Куда ведёт нажатие на уведомление (адрес внутри интерфейса)."""
    if kind == "signal" and ref:
        return f"/#/signal/{ref}"
    if kind == "whale_exit":
        return "/#/positions/open"
    if kind in ("whale_check", "whale_status") and ref:
        return f"/#/whales/{ref}"
    if kind == "event_edge" and ref:
        return f"/#/events/{ref}"
    return "/#/"


def message(n: dict) -> dict:
    """Короткое сообщение для устройства: заголовок, текст, адрес, метка (повтор по той же метке заменяет)."""
    kind, ref = n.get("kind") or "", n.get("ref") or ""
    out = {
        "title": str(n.get("title") or "WhaleFollow")[:120],
        "body": str(n.get("body") or "")[:300],
        "url": link(kind, ref),
        "tag": f"{kind}:{ref}" if ref else kind,
        "kind": kind,
    }
    if kind == "signal" and ref.startswith("s_"):
        out["signal"] = ref  # кнопки «Купить» / «Пропустить» прямо в уведомлении (где браузер их показывает)
    return out


class PushNotifier:
    def __init__(self, db: DB, session: Any = None, vapid: Vapid | None = None):
        self.db = db
        self.s = session if session is not None else net.client()
        self._vapid = vapid
        self.sent = 0
        self.q: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, daemon=True, name="push-send").start()

    @classmethod
    def attach_to(cls, db: DB) -> PushNotifier:
        n = cls(db)
        db.listeners.append(n.on_notify)
        return n

    @property
    def vapid(self) -> Vapid:
        if self._vapid is None:
            self._vapid = Vapid.load(self.db)
        return self._vapid

    def wanted(self, kind: str) -> bool:
        if kind not in PUSH_KINDS:
            return False
        base: dict = dict(DEFAULT_SETTINGS["notify"])  # type: ignore[call-overload]
        prefs = {**base, **(self.db.get_setting("notify") or {})}
        return bool(prefs.get("push")) and bool(prefs.get(KIND_TO_SETTING.get(kind, ""), False))

    def on_notify(self, n: dict) -> None:
        if self.wanted(n["kind"]) and self.db.one("SELECT 1 AS x FROM push_subscriptions LIMIT 1"):
            self.q.put(message(n))

    def _worker(self) -> None:
        while True:
            msg = self.q.get()
            try:
                self.send_all(msg)
            except Exception as e:  # сбой отправки не должен ронять процесс
                log.warning("push: %s", e)

    def send_all(self, msg: dict) -> dict:
        """Отправка на все подписанные устройства. Возвращает {"sent", "failed", "removed", "errors"}."""
        out: dict = {"sent": 0, "failed": 0, "removed": 0, "errors": []}
        payload = json.dumps(msg, ensure_ascii=False).encode()
        for sub in self.db.query("SELECT endpoint, p256dh, auth FROM push_subscriptions"):
            status, err = self.send_one(sub, payload)
            if status in (200, 201, 202):
                out["sent"] += 1
                self.sent += 1
                self.db.execute(
                    "UPDATE push_subscriptions SET last_ok_ts=?, fails=0, last_error=NULL WHERE endpoint=?",
                    (now_ts(), sub["endpoint"]),
                )
            elif status in (404, 410):  # устройство отписалось или подписка устарела
                out["removed"] += 1
                self.db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (sub["endpoint"],))
            else:
                out["failed"] += 1
                out["errors"].append(err)
                self.db.execute(
                    "UPDATE push_subscriptions SET fails=fails+1, last_error=? WHERE endpoint=?",
                    (err[:200], sub["endpoint"]),
                )
        return out

    def send_one(self, sub: dict, payload: bytes) -> tuple[int, str]:
        try:
            body = encrypt(payload, sub["p256dh"], sub["auth"])
            r = self.s.post(
                sub["endpoint"],
                content=body,
                headers={
                    "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream",
                    "TTL": str(TTL_S),
                    "Urgency": "high",
                    "Authorization": self.vapid.header(sub["endpoint"]),
                },
                timeout=15,
            )
            return r.status_code, "" if r.status_code < 300 else f"HTTP {r.status_code}: {r.text[:120]}"
        except Exception as e:
            return 0, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- подписки (экран «Настройки»)
def subscribe(db: DB, sub: dict, label: str = "") -> dict:
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
    if not endpoint.startswith("https://") or not p256dh or not auth:
        return {"ok": False, "error": "Неполная подписка: нужны адрес службы push и ключи устройства"}
    try:
        if len(unb64u(p256dh)) != 65 or len(unb64u(auth)) < 16:
            raise ValueError
    except Exception:
        return {"ok": False, "error": "Ключи подписки повреждены"}
    db.upsert(
        "push_subscriptions",
        {
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "label": label[:120],
            "created_ts": now_ts(),
            "fails": 0,
        },
        ["endpoint"],
    )
    return {"ok": True, "devices": devices(db)}


def unsubscribe(db: DB, endpoint: str) -> dict:
    db.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
    return {"ok": True, "devices": devices(db)}


def devices(db: DB) -> int:
    row = db.one("SELECT COUNT(*) AS n FROM push_subscriptions")
    return int(row["n"]) if row else 0
