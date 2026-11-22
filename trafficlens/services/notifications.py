"""In-app notifications for clients and admins."""
import datetime as dt
import re
from flask import session, url_for
from trafficlens.db import db


def _create_notification(user_id, request_id, kind, title, message):
    hidden_child = kind != "group_ready" and db().execute(
        "SELECT 1 FROM requests r JOIN request_groups g ON g.id=r.group_id "
        "JOIN users u ON u.id=? WHERE r.id=? AND g.sent_at IS NULL AND u.role!='admin'",
        (user_id, request_id),
    ).fetchone()
    if hidden_child:
        return   # a client hears about a grouped order once, when the admin sends the whole report
    db().execute(
        "INSERT INTO notifications(user_id,request_id,kind,title,message,created_at) VALUES(?,?,?,?,?,?)",
        (user_id, request_id, kind, title, message, dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )


def _notify_admins_new_request(request_id, client_name):
    admins = db().execute("SELECT id FROM users WHERE role='admin'").fetchall()
    for admin_user in admins:
        _create_notification(
            admin_user["id"], request_id, "new_request", "New request",
            f"{client_name} submitted a new request: REQ-{request_id}.",
        )


def _notify_request_owner_reply(request_id, user_id, old_reply, new_reply):
    if new_reply and new_reply != (old_reply or ""):
        _create_notification(
            user_id, request_id, "admin_reply", "New reply to your request",
            f"The administrator replied to request REQ-{request_id}: {new_reply}",
        )


def _translate_legacy_notifications(c):
    """Rewrite notifications stored in Arabic by earlier versions into English. Safe to run on every start."""
    titles = {
        "طلب جديد": "New request",
        "رد جديد على طلبك": "New reply to your request",
        "نتائج أحجام المرور جاهزة": "Traffic volume results are ready",
        "نتائج الطلب جاهزة": "Request results are ready",
    }
    for arabic, english in titles.items():
        c.execute("UPDATE notifications SET title=? WHERE title=?", (english, arabic))
    patterns = (
        (r"^قدم (.+) طلبًا جديدًا: (REQ-\d+)\.$", r"\1 submitted a new request: \2."),
        (r"^أضاف المسؤول ردًا على الطلب (REQ-\d+): (.*)$", r"The administrator replied to request \1: \2"),
        (r"^اعتمد المسؤول نتائج أحجام المرور للطلب (REQ-\d+)\.$", r"The administrator approved the traffic volume results for request \1."),
        (r"^اعتمد المسؤول نتائج الاتجاهات للطلب (REQ-\d+)\.$", r"The administrator approved the directions results for request \1."),
    )
    rows = c.execute(
        "SELECT id,message FROM notifications WHERE message LIKE 'قدم%' OR message LIKE 'أضاف%' OR message LIKE 'اعتمد%'"
    ).fetchall()
    for notification_id, message in rows:
        for pattern, replacement in patterns:
            translated, count = re.subn(pattern, replacement, message or "", flags=re.S)
            if count:
                c.execute("UPDATE notifications SET message=? WHERE id=?", (translated, notification_id))
                break


def _notification_destination(request_id, is_admin, study_id=None):
    if study_id is not None:
        study = db().execute(
            "SELECT user_id FROM turning_movement_studies WHERE id=?", (study_id,)
        ).fetchone()
        if study and (is_admin or study["user_id"] == session.get("uid")):
            return url_for("turning.view", study_id=study_id)

    if request_id is not None:
        row = db().execute(
            "SELECT r.group_id,r.user_id,g.user_id AS group_user_id "
            "FROM requests r LEFT JOIN request_groups g ON g.id=r.group_id WHERE r.id=?",
            (request_id,),
        ).fetchone()
        if row and (is_admin or row["user_id"] == session.get("uid")):
            group_id = row["group_id"]
            if group_id:
                if row["group_user_id"] is None or (
                    not is_admin and row["group_user_id"] != session.get("uid")
                ):
                    return url_for("notifications.notifications")
                return url_for("groups.admin_group", gid=group_id) if is_admin else url_for(
                    "groups.client_group", gid=group_id
                )
            return url_for("admin_requests.admin_request", rid=request_id) if is_admin else url_for(
                "client.result", rid=request_id
            )

    return url_for("notifications.notifications")


def _unread_notification_count():
    return db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL", (session["uid"],)).fetchone()[0]
