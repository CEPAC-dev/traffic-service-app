"""Notifications for turning-movement studies.

notifications.request_id is NOT NULL and references requests, and SQLite cannot drop a NOT NULL
constraint in place, so ensure_study_notifications() rebuilds the table once to add a study_id column
and make request_id optional. It does nothing when study_id already exists, so it is safe on every start.
Rows, ids and read state are preserved.
"""
import logging
import sqlite3

EXPECTED = ["id", "user_id", "request_id", "kind", "title", "message", "created_at", "read_at"]
log = logging.getLogger(__name__)


def ensure_study_notifications(conn):
    columns = [row[1] for row in conn.execute("PRAGMA table_info(notifications)")]
    if "study_id" in columns:
        return False
    if columns != EXPECTED:
        raise RuntimeError(
            "notifications has unexpected columns %s; adjust EXPECTED and the CREATE TABLE below to match." % columns)
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE notifications_new(
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            request_id INTEGER REFERENCES requests(id),
            study_id INTEGER REFERENCES turning_movement_studies(id),
            kind TEXT NOT NULL, title TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL, read_at TEXT,
            CHECK (request_id IS NOT NULL OR study_id IS NOT NULL))""")
        conn.execute("INSERT INTO notifications_new(id,user_id,request_id,study_id,kind,title,message,created_at,read_at) "
                     "SELECT id,user_id,request_id,NULL,kind,title,message,created_at,read_at FROM notifications")
        conn.execute("DROP TABLE notifications")
        conn.execute("ALTER TABLE notifications_new RENAME TO notifications")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, read_at, id)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def notify_study(user_id, study_id, kind, title, message):
    """Add one notification about a study. A notification problem never breaks the action that caused it."""
    from trafficlens.db import db
    from trafficlens.services import common as common_svc
    try:
        db().execute(
            "INSERT INTO notifications(user_id,study_id,kind,title,message,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, study_id, kind, title, message, common_svc._timestamp_now()))
        db().commit()
    except sqlite3.Error:
        db().rollback()
        log.exception("Could not save the notification for turning-movement study %s.", study_id)


def notify_admins_new_study(study_id, client_name, resubmitted=False):
    from trafficlens.db import db
    verb = "fixed and resubmitted" if resubmitted else "submitted"
    for admin in db().execute("SELECT id FROM users WHERE role='admin'").fetchall():
        notify_study(admin["id"], study_id, "tm_submitted", "Turning-movement study",
                     f"{client_name or 'A client'} {verb} turning-movement study TM-{study_id}.")


def notify_owner(user_id, study_id, kind, title, message):
    notify_study(user_id, study_id, kind, title, message)


def forget_study(study_id):
    """Remove the notifications of a study that was deleted, so they do not point at a missing page."""
    from trafficlens.db import db
    try:
        db().execute("DELETE FROM notifications WHERE study_id=?", (study_id,))
        db().commit()
    except sqlite3.Error:
        db().rollback()
        log.exception("Could not remove the notifications of deleted study %s.", study_id)
