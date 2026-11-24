"""Password accounts and cookie sessions.

The first person to open the app creates the owner (administrator) account. Other people join by an
invitation from an administrator (see accounts.py); everything is kept per user.
"""

import hashlib
import hmac
import re
import secrets
import time

from . import db as store

ITERATIONS = 240_000
SESSION_DAYS = 30
COOKIE = "kontur_session"
NAME = re.compile(r"[A-Za-z0-9_.@-]{3,40}\Z")
START_BALANCE = 1000.0


def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS).hex()


def _check_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 200:
        raise ValueError("Пароль должен содержать от 8 до 200 символов")
    return password


def _check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name.strip()):
        raise ValueError("Логин: от 3 до 40 латинских букв, цифр и символов _ . @ -")
    return name.strip()


def has_users(db):
    return db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(db, username, password, *, admin=False):
    username, password = _check_name(username), _check_password(password)
    salt = secrets.token_hex(16)
    with store.tx(db):
        if db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise ValueError("Такой логин уже есть")
        cur = db.execute("INSERT INTO users(username, pw_hash, pw_salt, is_admin, created) VALUES(?,?,?,?,?)",
                         (username, _hash(password, salt), salt, int(admin), time.time()))
        uid = cur.lastrowid
        db.execute("INSERT INTO accounts(user_id, kind, name, balance, created) VALUES(?,?,?,?,?)",
                   (uid, "main", "Основной счёт", START_BALANCE, time.time()))
        acc = db.execute("SELECT id FROM accounts WHERE user_id=? AND kind='main'", (uid,)).fetchone()[0]
        db.execute("INSERT INTO ledger(account_id, kind, amount, balance_after, note, at) VALUES(?,?,?,?,?,?)",
                   (acc, "deposit", START_BALANCE, START_BALANCE, "Стартовый виртуальный баланс", time.time()))
    return uid


def setup(db, body):
    """Create the owner account; allowed only while the database has no users."""
    if has_users(db):
        raise PermissionError("Учётная запись уже создана. Войдите с паролем.")
    uid = create_user(db, body.get("username"), body.get("password"), admin=True)
    return start_session(db, uid)


def _throttled(db, ip):
    since = time.time() - 900
    db.execute("DELETE FROM login_attempts WHERE at < ?", (since,))
    return db.execute("SELECT COUNT(*) FROM login_attempts WHERE ip=?", (ip,)).fetchone()[0] >= 10


def login(db, body, ip):
    if _throttled(db, ip):
        raise PermissionError("Слишком много попыток входа. Подождите 15 минут.")
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    row = db.execute("SELECT id, pw_hash, pw_salt, blocked FROM users WHERE username=?", (username,)).fetchone()
    expected = row["pw_hash"] if row else _hash("x", "00" * 16)
    actual = _hash(password, row["pw_salt"] if row else "00" * 16)
    if not row or not hmac.compare_digest(expected, actual):
        db.execute("INSERT INTO login_attempts VALUES(?,?)", (ip, time.time()))
        raise PermissionError("Неверный логин или пароль")
    if row["blocked"]:
        raise PermissionError("Учётная запись заблокирована администратором")
    return start_session(db, row["id"])


def start_session(db, uid):
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.execute("INSERT INTO sessions VALUES(?,?,?,?)",
               (hashlib.sha256(token.encode()).hexdigest(), uid, now, now + SESSION_DAYS * 86400))
    db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    db.execute("UPDATE users SET last_login=? WHERE id=?", (now, uid))
    return token


def user_for(db, token):
    if not token or len(token) > 100:
        return None
    row = db.execute("""SELECT u.id, u.username, u.is_admin FROM sessions s JOIN users u ON u.id=s.user_id
                        WHERE s.token_hash=? AND s.expires > ? AND u.blocked=0""",
                     (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
    return dict(row) if row else None


def logout(db, token):
    if token:
        db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))


def change_password(db, uid, body):
    row = db.execute("SELECT pw_hash, pw_salt FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not hmac.compare_digest(row["pw_hash"], _hash(str(body.get("current") or ""), row["pw_salt"])):
        raise PermissionError("Текущий пароль указан неверно")
    password = _check_password(body.get("password"))
    salt = secrets.token_hex(16)
    with store.tx(db):
        db.execute("UPDATE users SET pw_hash=?, pw_salt=? WHERE id=?", (_hash(password, salt), salt, uid))
        db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return start_session(db, uid)
