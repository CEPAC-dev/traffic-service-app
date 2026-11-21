"""Background upkeep: scheduled backups and cleanup of old volume runs."""
import backup as backup_tool
import datetime as dt
import os
import sqlite3
import threading
import time
import volumes_runner
from trafficlens.db import db
from trafficlens.runtime import flask_app


def _cleanup_superseded_volume_runs(rid):
    """After a result is approved, keep only that run's workbooks for the request."""
    try:
        row = db().execute(
            "SELECT r.user_id, w.workbook_path FROM requests r JOIN volumes_workflows w ON w.request_id=r.id WHERE r.id=?",
            (rid,),
        ).fetchone()
        if row and row["workbook_path"]:
            volumes_runner.cleanup_request_runs(row["user_id"], rid, [row["workbook_path"]])
    except (OSError, ValueError, sqlite3.Error) as exc:
        flask_app.logger.warning("could not clean old volume runs for request %s: %s", rid, exc)


def _run_maintenance_once():
    """Purge stale run files and take a backup if one is due. Safe to call repeatedly."""
    retention_days = int(os.environ.get("VOLUME_RUN_RETENTION_DAYS", "30") or 0)
    backup_hours = float(os.environ.get("TRAFFICLENS_BACKUP_EVERY_HOURS", "24") or 0)
    with flask_app.app_context():
        if retention_days > 0:
            referenced = [row["workbook_path"] for row in db().execute("SELECT workbook_path FROM volumes_workflows WHERE workbook_path IS NOT NULL")]
            removed = volumes_runner.purge_stale_runs(referenced, retention_days)
            if removed:
                flask_app.logger.info("removed %s stale MATLAB run workbook(s)", removed)
    if backup_hours > 0:
        dest = backup_tool.default_backup_dir()
        last = backup_tool.latest_backup_time(dest)
        if last is None or (dt.datetime.now() - last).total_seconds() >= backup_hours * 3600:
            keep = int(os.environ.get("TRAFFICLENS_BACKUP_KEEP", "7") or 7)
            path = backup_tool.run_backup(
                db_path=flask_app.config["DATABASE"], runs_dir=volumes_runner.volume_runs_root(), dest_dir=dest, keep=keep,
            )
            flask_app.logger.info("backup written: %s", path)


def _maintenance_loop():
    while True:
        try:
            _run_maintenance_once()
        except Exception:
            flask_app.logger.exception("scheduled maintenance failed")
        time.sleep(3600)


def start_maintenance():
    thread = threading.Thread(target=_maintenance_loop, name="trafficlens-maintenance", daemon=True)
    thread.start()
    return thread
