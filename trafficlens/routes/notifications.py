"""Notification pages and the notification API."""
import datetime as dt
from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for
from trafficlens.db import db
from trafficlens.security.decorators import login_required
from trafficlens.services import notifications as notifications_svc

bp = Blueprint("notifications", __name__)


@bp.get("/notifications")
@login_required
def notifications():
    rows = db().execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",
        (session["uid"],),
    ).fetchall()
    return render_template("notifications.html", notifications=rows)


@bp.get("/notifications/<int:notification_id>/open")
@login_required
def open_notification(notification_id):
    notification = db().execute(
        "SELECT * FROM notifications WHERE id=? AND user_id=?",
        (notification_id, session["uid"]),
    ).fetchone()
    if not notification:
        abort(404)
    if notification["read_at"] is None:
        db().execute(
            "UPDATE notifications SET read_at=? WHERE id=? AND user_id=? AND read_at IS NULL",
            (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), notification_id, session["uid"]),
        )
        db().commit()
    return redirect(notifications_svc._notification_destination(notification["request_id"], session.get("role") == "admin"))


@bp.get("/api/notifications")
@login_required
def notification_menu_items():
    rows = db().execute(
        "SELECT id,kind,title,message,created_at,read_at FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 30",
        (session["uid"],)).fetchall()
    total = db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=?", (session["uid"],)).fetchone()[0]
    return jsonify(
        notifications=[{
            "id": row["id"], "kind": row["kind"], "title": row["title"], "message": row["message"],
            "created_at": row["created_at"], "read": row["read_at"] is not None,
            "url": url_for("notifications.open_notification", notification_id=row["id"]),
        } for row in rows],
        unread=notifications_svc._unread_notification_count(), total=total)


@bp.post("/api/notifications/<int:notification_id>/read")
@login_required
def set_notification_read(notification_id):
    read = (request.get_json(silent=True) or {}).get("read", True)
    if not isinstance(read, bool):
        return jsonify(error="Invalid value"), 400
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S") if read else None
    updated = db().execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=?",
                           (stamp, notification_id, session["uid"])).rowcount
    if not updated:
        return jsonify(error="Notification not found"), 404
    db().commit()
    return jsonify(ok=True, unread=notifications_svc._unread_notification_count())


@bp.post("/api/notifications/read-all")
@login_required
def mark_all_notifications_read():
    db().execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
                 (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session["uid"]))
    db().commit()
    return jsonify(ok=True, unread=0)


@bp.post("/api/notifications/<int:notification_id>/delete")
@login_required
def delete_notification(notification_id):
    deleted = db().execute("DELETE FROM notifications WHERE id=? AND user_id=?", (notification_id, session["uid"])).rowcount
    if not deleted:
        return jsonify(error="Notification not found"), 404
    db().commit()
    return jsonify(ok=True, unread=notifications_svc._unread_notification_count())


@bp.post("/api/notifications/clear")
@login_required
def clear_notifications():
    db().execute("DELETE FROM notifications WHERE user_id=?", (session["uid"],))
    db().commit()
    return jsonify(ok=True, unread=0)


@bp.get("/api/notifications/unread-count")
@login_required
def notification_unread_count():
    count = db().execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
        (session["uid"],),
    ).fetchone()[0]
    return jsonify(count=count)


@bp.get("/api/notifications/device-feed")
@login_required
def notification_device_feed():
    try:
        after_id = int(request.args.get("after", "0"))
    except ValueError:
        return jsonify(error="Invalid notification cursor"), 400
    if after_id < 0:
        return jsonify(error="Invalid notification cursor"), 400
    rows = db().execute(
        "SELECT id,request_id,kind,title,message,created_at FROM notifications "
        "WHERE user_id=? AND id>? ORDER BY id ASC LIMIT 30",
        (session["uid"], after_id),
    ).fetchall()
    notifications_out = []
    for row in rows:
        destination = notifications_svc._notification_destination(row["request_id"], session.get("role") == "admin")
        notifications_out.append({
            "id": row["id"],
            "title": row["title"],
            "message": row["message"],
            "created_at": row["created_at"],
            "url": destination,
        })
    return jsonify(notifications=notifications_out)
