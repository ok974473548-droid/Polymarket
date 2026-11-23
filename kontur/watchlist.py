"""Starred traders, events and tags of a user (stored on the server)."""

import time

from . import db as store
from . import polymarket as pm


def listing(db, uid):
    return store.rows(db.execute("SELECT kind, key, label, image, created FROM watchlist WHERE user_id=? ORDER BY created DESC", (uid,)))


def toggle(db, uid, body):
    kind = body.get("kind")
    if kind not in {"trader", "event", "tag"}:
        raise ValueError("Неизвестный тип избранного")
    key = str(body.get("key") or "")
    if kind == "trader":
        key = pm.address(key)
    elif not pm.SLUG.fullmatch(key):
        raise ValueError("Некорректный идентификатор")
    on = body.get("on")
    exists = db.execute("SELECT 1 FROM watchlist WHERE user_id=? AND kind=? AND key=?", (uid, kind, key)).fetchone()
    if on is None:
        on = not exists
    if on and not exists:
        db.execute("INSERT INTO watchlist VALUES(?,?,?,?,?,?)",
                   (uid, kind, key, str(body.get("label") or "")[:200], str(body.get("image") or "")[:600], time.time()))
    elif not on:
        db.execute("DELETE FROM watchlist WHERE user_id=? AND kind=? AND key=?", (uid, kind, key))
    return {"kind": kind, "key": key, "on": bool(on)}
