"""Push-уведомления: шифрование (RFC 8291 / 8188), подпись VAPID (RFC 8292), подписки и отправка."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from whalefollow import push
from whalefollow.db import DB
from whalefollow.push import PushNotifier, Vapid, b64u, unb64u


def _priv(b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64u(b64), "big"), ec.SECP256R1())


def _pub(key: ec.EllipticCurvePrivateKey) -> str:
    return b64u(
        key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )


def _decrypt(body: bytes, ua_key: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """Независимая расшифровка «на стороне устройства» — по тексту RFC 8291 §3.4 и RFC 8188 §2."""

    def hkdf(salt: bytes, ikm: bytes, info: bytes, n: int) -> bytes:
        prk = hmac.new(salt, ikm, hashlib.sha256).digest()
        return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]

    salt, rs, idlen = body[:16], struct.unpack("!I", body[16:20])[0], body[20]
    as_public = body[21 : 21 + idlen]
    assert rs == 4096 and idlen == 65
    ua_public = unb64u(_pub(ua_key))
    shared = ua_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = hkdf(auth, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen :], None)
    assert plain.endswith(b"\x02")  # последняя запись
    return plain[:-1]


def test_encrypt_matches_rfc8291_example():
    """Пример из RFC 8291, приложение A: с теми же ключами и солью — тот же зашифрованный текст."""
    as_key = _priv("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
    ua_key = _priv("q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94")
    assert _pub(as_key) == "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
    assert _pub(ua_key) == "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
    body = push.encrypt(
        b"When I grow up, I want to be a watermelon",
        _pub(ua_key),
        "BTBZMqHH6r4Tts7J_aSIgg",
        salt=unb64u("DGv6ra1nlYgDCS1FRnbzlw"),
        server_key=as_key,
    )
    assert b64u(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_"
        "yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_encrypt_roundtrip_with_random_keys():
    ua_key = ec.generate_private_key(ec.SECP256R1())
    auth = b"0123456789abcdef"
    msg = json.dumps({"title": "Новый сигнал", "body": "Кит купил «Да» по 41¢"}, ensure_ascii=False).encode()
    body = push.encrypt(msg, _pub(ua_key), b64u(auth))
    assert _decrypt(body, ua_key, auth) == msg
    assert push.encrypt(msg, _pub(ua_key), b64u(auth)) != body  # каждый раз свой одноразовый ключ и соль


def test_too_long_message_rejected():
    ua_key = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ValueError):
        push.encrypt(b"x" * 5000, _pub(ua_key), b64u(b"0123456789abcdef"))


def test_vapid_header_is_valid_es256_jwt(db):
    v = Vapid.load(db)
    assert Vapid.load(db).public == v.public  # ключ создаётся один раз и хранится в базе
    h = v.header("https://fcm.googleapis.com/fcm/send/abc")
    t, k = h.removeprefix("vapid t=").split(", k=")
    assert k == v.public and len(unb64u(k)) == 65
    head, claims, sig = t.split(".")
    c = json.loads(base64.urlsafe_b64decode(claims + "=="))
    assert c["aud"] == "https://fcm.googleapis.com" and c["sub"].startswith(("mailto:", "https://"))
    raw = unb64u(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(k))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # бросит, если подпись неверна


class _Resp:
    def __init__(self, code: int):
        self.status_code, self.text = code, ""


class _Session:
    def __init__(self, codes: dict[str, int]):
        self.codes, self.calls = codes, []

    def post(self, url, content=None, headers=None, timeout=None):
        self.calls.append((url, content, headers))
        return _Resp(self.codes.get(url, 201))


def _sub(db: DB, endpoint: str) -> tuple[ec.EllipticCurvePrivateKey, bytes]:
    key, auth = ec.generate_private_key(ec.SECP256R1()), b"0123456789abcdef"
    r = push.subscribe(db, {"endpoint": endpoint, "keys": {"p256dh": _pub(key), "auth": b64u(auth)}}, "Chrome")
    assert r["ok"]
    return key, auth


def test_subscribe_validates_and_sends(db):
    assert not push.subscribe(db, {"endpoint": "http://x", "keys": {}})["ok"]
    assert not push.subscribe(db, {"endpoint": "https://x/1", "keys": {"p256dh": "AAAA", "auth": "AAAA"}})["ok"]
    key, auth = _sub(db, "https://push.example/ok")
    _sub(db, "https://push.example/gone")
    assert push.devices(db) == 2
    s = _Session({"https://push.example/gone": 410})
    n = PushNotifier(db, session=s)
    r = n.send_all(push.message({"kind": "signal", "ref": "s_1", "title": "Новый сигнал", "body": "кит купил"}))
    assert r == {"sent": 1, "failed": 0, "removed": 1, "errors": []}
    assert push.devices(db) == 1  # отписавшееся устройство удалено
    url, body, headers = next(c for c in s.calls if c[0].endswith("/ok"))
    assert headers["Content-Encoding"] == "aes128gcm" and headers["Authorization"].startswith("vapid t=")
    msg = json.loads(_decrypt(body, key, auth))
    assert msg["url"] == "/#/signal/s_1" and msg["signal"] == "s_1" and msg["title"] == "Новый сигнал"
    assert push.unsubscribe(db, "https://push.example/ok")["devices"] == 0


def test_notify_respects_settings(db):
    _sub(db, "https://push.example/ok")
    n = PushNotifier(db, session=_Session({}))
    sent: list[dict] = []
    n.q.put = sent.append  # type: ignore[method-assign]
    db.listeners.append(n.on_notify)
    db.notify("signal", "Новый сигнал", ref="s_9")
    db.notify("whale_exit", "Кит вышел", ref="s_9")
    db.notify("settled", "Ставка рассчитана")  # не для push
    assert [m["kind"] for m in sent] == ["signal", "whale_exit"]
    db.set_setting("notify", {"push": False})
    db.notify("signal", "Ещё сигнал", ref="s_10")
    assert len(sent) == 2
    db.set_setting("notify", {"push": True, "whale_exit": False})
    db.notify("whale_exit", "Кит вышел", ref="s_9")
    assert len(sent) == 2
