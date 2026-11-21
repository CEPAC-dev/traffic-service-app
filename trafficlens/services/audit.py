"""The audit log: who changed what on a request, and how entries are shown."""
import json
import sqlite3
from flask import has_request_context, session
from trafficlens.config import AUDIT_LABELS
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.services import common as common_svc


def audit(action, request_id=None, **details):
    """Record who did what to a request. The caller commits together with its own change."""
    actor = (session.get("uid"), session.get("name"), session.get("role")) if has_request_context() else (None, "system", "system")
    try:
        db().execute(
            "INSERT INTO audit_log(created_at,user_id,user_name,role,action,request_id,details) VALUES(?,?,?,?,?,?,?)",
            (common_svc._timestamp_now(), *actor, action, request_id, json.dumps(details, default=str, ensure_ascii=False)),
        )
    except sqlite3.Error as exc:
        flask_app.logger.warning("audit log write failed: %s", exc)


def _audit_details_text(raw):
    try:
        details = json.loads(raw or "{}")
    except ValueError:
        return str(raw or "")[:400]
    if not isinstance(details, dict):
        return str(details)[:400]
    text = "; ".join(f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value}" for key, value in details.items())
    return text if len(text) <= 600 else text[:597] + "..."


def _audit_entries_for_request(rid, limit=200):
    rows = db().execute(
        "SELECT created_at,user_name,role,action,details FROM audit_log WHERE request_id=? ORDER BY id DESC LIMIT ?",
        (rid, limit),
    ).fetchall()
    return [
        {
            "when": row["created_at"],
            "who": f"{row['user_name'] or 'unknown'} ({row['role'] or '-'})",
            "action": AUDIT_LABELS.get(row["action"], row["action"]),
            "details": _audit_details_text(row["details"]),
        }
        for row in rows
    ]
