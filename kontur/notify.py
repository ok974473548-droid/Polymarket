"""In-app notifications shown under the bell in the header."""

import time

from . import db as store


def add(db, uid, kind, title, body="", link=""):
    db.execute("INSERT INTO notifications(user_id, kind, title, body, link, at) VALUES(?,?,?,?,?,?)",
               (uid, kind, title[:200], body[:500], link[:300], time.time()))
    db.execute("""DELETE FROM notifications WHERE user_id=? AND id NOT IN
                  (SELECT id FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 300)""", (uid, uid))


def listing(db, uid, limit=50):
    rows = store.rows(db.execute("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)))
    unread = db.execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0", (uid,)).fetchone()[0]
    return {"rows": rows, "unread": unread}


def mark_read(db, uid):
    db.execute("UPDATE notifications SET read=1 WHERE user_id=? AND read=0", (uid,))
    return {"unread": 0}
