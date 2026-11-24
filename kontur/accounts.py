"""People in the app: invitations, sign-up by invitation, and the administrator's list (block, unblock, delete).

New people join only by an invitation link that an administrator creates: one use, limited time.
The link carries a random token; only its hash is stored, so the database alone cannot be used to join.
"""

import hashlib
import secrets
import time

from . import auth
from . import autopilot
from . import db as store
from . import trading

INVITE_DAYS = 7
MAX_OPEN_INVITES = 50


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _admin(db, uid):
    row = db.execute("SELECT is_admin FROM users WHERE id=?", (uid,)).fetchone()
    if not row or not row[0]:
        raise PermissionError("Это может только администратор")


# ---------------------------------------------------------------- invitations

def create_invite(db, uid, note="", days=INVITE_DAYS):
    _admin(db, uid)
    now = time.time()
    open_count = db.execute("SELECT COUNT(*) FROM invites WHERE used_by IS NULL AND revoked=0 AND expires>?", (now,)).fetchone()[0]
    if open_count >= MAX_OPEN_INVITES:
        raise ValueError(f"Открытых приглашений уже {MAX_OPEN_INVITES}: отзовите ненужные")
    days = int(days) if str(days).isdigit() and 1 <= int(days) <= 30 else INVITE_DAYS
    token = secrets.token_urlsafe(24)
    cur = db.execute("INSERT INTO invites(token_hash, created_by, note, created, expires) VALUES(?,?,?,?,?)",
                     (_hash(token), uid, str(note or "").strip()[:80], now, now + days * 86400))
    # The token is shown once, here; the database keeps only its hash.
    return {"id": cur.lastrowid, "token": token, "expires": now + days * 86400}


def invites(db, uid):
    _admin(db, uid)
    rows = store.rows(db.execute("""SELECT i.id, i.note, i.created, i.expires, i.used_at, i.revoked, u.username AS used_by_name
                                    FROM invites i LEFT JOIN users u ON u.id=i.used_by ORDER BY i.id DESC LIMIT 100"""))
    now = time.time()
    for r in rows:
        r["status"] = "used" if r["used_at"] else "revoked" if r["revoked"] else "expired" if r["expires"] < now else "open"
        r.pop("revoked")
    return rows


def revoke_invite(db, uid, invite_id):
    _admin(db, uid)
    cur = db.execute("UPDATE invites SET revoked=1 WHERE id=? AND used_by IS NULL", (int(invite_id),))
    if not cur.rowcount:
        raise LookupError("Приглашение не найдено или уже использовано")
    return {"ok": True}


def _open_invite(db, token):
    if not isinstance(token, str) or not 10 <= len(token) <= 100:
        return None
    return store.one(db.execute("SELECT * FROM invites WHERE token_hash=? AND used_by IS NULL AND revoked=0 AND expires>?",
                                (_hash(token), time.time())))


def check_invite(db, token):
    inv = _open_invite(db, token)
    if not inv:
        raise LookupError("Приглашение недействительно: его уже использовали, отозвали или срок истёк")
    return {"ok": True, "expires": inv["expires"]}


def register(db, body):
    """Create an account by an invitation and start a session. The invitation is used up in the same step."""
    inv = _open_invite(db, body.get("invite"))
    if not inv:
        raise PermissionError("Приглашение недействительно: его уже использовали, отозвали или срок истёк")
    uid = auth.create_user(db, body.get("username"), body.get("password"))
    cur = db.execute("UPDATE invites SET used_by=?, used_at=? WHERE id=? AND used_by IS NULL", (uid, time.time(), inv["id"]))
    if not cur.rowcount:  # two people with one link at the same moment: only the first one keeps the account
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        raise PermissionError("Приглашение уже использовано")
    return auth.start_session(db, uid)


# ---------------------------------------------------------------- administrator's list

def users(db, uid):
    _admin(db, uid)
    rows = store.rows(db.execute("SELECT id, username, is_admin, blocked, created, last_login FROM users ORDER BY id"))
    for r in rows:
        r["is_admin"], r["blocked"] = bool(r["is_admin"]), bool(r["blocked"])
        total = 0.0
        for (aid,) in db.execute("SELECT id FROM accounts WHERE user_id=?", (r["id"],)).fetchall():
            total += trading.equity(db, aid)["total"]
        r["equity"] = round(total, 2)
        r["autopilot"] = autopilot.settings(db, r["id"])["enabled"]
        r["copies"] = db.execute("""SELECT COUNT(*) FROM copy_trades c JOIN accounts a ON a.id=c.account_id
                                    WHERE a.user_id=? AND c.paused=0""", (r["id"],)).fetchone()[0]
        r["me"] = r["id"] == uid
    return rows


def _target(db, uid, target):
    _admin(db, uid)
    row = store.one(db.execute("SELECT id, username, is_admin FROM users WHERE id=?", (int(target),)))
    if not row:
        raise LookupError("Пользователь не найден")
    if row["id"] == uid:
        raise ValueError("Это ваша учётная запись")
    if row["is_admin"]:
        raise ValueError("Администратора нельзя заблокировать или удалить")
    return row


def set_blocked(db, uid, target, blocked):
    """A blocked person cannot sign in, and their copying and autopilot stop until they are unblocked."""
    row = _target(db, uid, target)
    with store.tx(db):
        db.execute("UPDATE users SET blocked=? WHERE id=?", (int(bool(blocked)), row["id"]))
        if blocked:
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    return {"ok": True}


def delete_user(db, uid, target):
    """Delete a person with everything they have: accounts, orders, copies, autopilot, watchlist, notifications."""
    row = _target(db, uid, target)
    with store.tx(db):
        db.execute("DELETE FROM kv WHERE key LIKE ?", (f"ap:{row['id']}:%",))
        db.execute("DELETE FROM users WHERE id=?", (row["id"],))  # the rest goes with it (ON DELETE CASCADE)
    return {"ok": True}
