import json, os, secrets, sqlite3, datetime as dt
from flask import Flask, request, session
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv
import network

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
app = Flask(__name__)
configured_secret = os.environ.get("SECRET_KEY", "").strip()
if configured_secret == "generate-a-random-secret-for-local-use":
    configured_secret = ""
if configured_secret and len(configured_secret) < 32:
    raise RuntimeError("SECRET_KEY must contain at least 32 characters; generate a random key locally.")
app.secret_key = configured_secret or secrets.token_hex(32)
app.config["EPHEMERAL_SECRET_KEY"] = not bool(configured_secret)
if not configured_secret:
    app.logger.warning("SECRET_KEY is not configured; using an ephemeral key for this local run.")
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024   # network files can be large
app.config["OUTSCRAPER_AUTO_POLL"] = os.environ.get("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "1") != "0"
app.config["MAINTENANCE_AUTO_START"] = os.environ.get("TRAFFICLENS_MAINTENANCE", "1") != "0"
app.config["DATABASE"] = os.environ.get("TRAFFICLENS_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "mobility.db")
from trafficlens.config import APP_NAME
from trafficlens.db import SCHEMA, db, close_db
from trafficlens.security.hardening import init_security
from trafficlens.security.settings import debug_enabled, is_production
from trafficlens.runtime import bind_app
from trafficlens.services import (
    catalog as catalog_svc,
    common as common_svc,
    maintenance as maintenance_svc,
    notifications as notifications_svc,
    outscraper_jobs as outscraper_jobs_svc,
)
init_security(app)
bind_app(app)
from trafficlens.routes import public as public_routes, client as client_routes, profile as profile_routes, groups as groups_routes, notifications as notifications_routes, admin as admin_routes, admin_requests as admin_requests_routes, outscraper_admin as outscraper_admin_routes, directions as directions_routes, volumes as volumes_routes
app.register_blueprint(public_routes.bp)
app.register_blueprint(client_routes.bp)
app.register_blueprint(profile_routes.bp)
app.register_blueprint(groups_routes.bp)
app.register_blueprint(notifications_routes.bp)
app.register_blueprint(admin_routes.bp)
app.register_blueprint(admin_requests_routes.bp)
app.register_blueprint(outscraper_admin_routes.bp)
app.register_blueprint(directions_routes.bp)
app.register_blueprint(volumes_routes.bp)


app.teardown_appcontext(close_db)


def _migrate_profile_columns(c):
    user_cols = {column[1] for column in c.execute("PRAGMA table_info(users)")}
    for column in (
        "first_name", "last_name", "phone_number", "job_title",
        "engineering_specialization", "company_name", "bio", "profile_image",
    ):
        if column not in user_cols:
            c.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
    for user_id, name, first_name, last_name in c.execute(
        "SELECT id,name,first_name,last_name FROM users"
    ).fetchall():
        if first_name and last_name:
            continue
        name_parts = (name or "").strip().split(None, 1)
        migrated_first = name_parts[0] if name_parts else ""
        migrated_last = name_parts[1] if len(name_parts) > 1 else ""
        c.execute(
            "UPDATE users SET first_name=COALESCE(NULLIF(first_name,''),?), "
            "last_name=COALESCE(NULLIF(last_name,''),?) WHERE id=?",
            (migrated_first, migrated_last, user_id),
        )


def init():
    c = sqlite3.connect(app.config["DATABASE"]); c.executescript(SCHEMA)
    _migrate_profile_columns(c)
    workflow_cols = {column[1] for column in c.execute("PRAGMA table_info(request_directions_workflows)")}
    if "interval_minutes" not in workflow_cols:
        c.execute("ALTER TABLE request_directions_workflows ADD COLUMN interval_minutes INTEGER")
        c.execute("UPDATE request_directions_workflows SET interval_minutes=MAX(1, CAST((interval_seconds + 59) / 60 AS INTEGER))")
    route_cols = [column[1] for column in c.execute("PRAGMA table_info(outscraper_job_routes)")]
    for column in ("last_polled_at", "request_details_json"):
        if column not in route_cols:
            c.execute(f"ALTER TABLE outscraper_job_routes ADD COLUMN {column} TEXT")
    volume_cols = {column[1] for column in c.execute("PRAGMA table_info(volumes_workflows)")}
    for column, definition in (
        ("freeflow_accepted", "INTEGER NOT NULL DEFAULT 0"),
        ("freeflow_result_json", "TEXT"),
        ("freeflow_validation_json", "TEXT"),
        ("freeflow_override", "INTEGER NOT NULL DEFAULT 0"),
        ("freeflow_accepted_at", "TEXT"),
        ("suggested_lanes", "INTEGER"),
    ):
        if column not in volume_cols:
            c.execute(f"ALTER TABLE volumes_workflows ADD COLUMN {column} {definition}")
    interrupted = [row[0] for row in c.execute("SELECT request_id FROM volumes_workflows WHERE status='running'")]
    if interrupted:
        stamp = common_svc._timestamp_now()
        for interrupted_id in interrupted:
            c.execute(
                "UPDATE volumes_workflows SET status='failed', error=?, updated_at=? WHERE request_id=? AND status='running'",
                ("The app restarted while MATLAB was running. Use Retry to run the calculation again.", stamp, interrupted_id),
            )
            c.execute(
                "INSERT INTO audit_log(created_at,user_id,user_name,role,action,request_id,details) VALUES(?,NULL,'system','system','volumes_run_interrupted',?,?)",
                (stamp, interrupted_id, json.dumps({"reason": "app restart"})),
            )
    c.execute(
        "UPDATE volumes_workflows SET freeflow_accepted=1 "
        "WHERE freeflow_speed IS NOT NULL AND freeflow_accepted=0 AND freeflow_result_json IS NULL"
    )
    for job_id, created_at, execution_status, config_json in c.execute(
        "SELECT id,created_at,execution_status,config_json FROM outscraper_jobs"
    ).fetchall():
        try:
            context = json.loads(config_json or "{}").get("trafficlens_context") or {}
            if not context.get("volume_input"):
                continue
            if not c.execute(
                "SELECT 1 FROM requests WHERE id=?", (context["source_request_id"],)
            ).fetchone():
                continue
            c.execute(
                "INSERT OR IGNORE INTO volume_freeflow_attempts"
                "(job_id,request_id,submitted_at,status) VALUES(?,?,?,?)",
                (job_id, context["source_request_id"], created_at, execution_status),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            app.logger.warning("Could not backfill free-flow attempt history for job %s.", job_id)
    if app.config["EPHEMERAL_SECRET_KEY"]:
        c.execute(
            "UPDATE outscraper_jobs SET approval_status='invalidated', execution_status='invalidated', updated_at=? "
            "WHERE approval_status='pending' AND execution_status='awaiting_approval'",
            (common_svc._timestamp_now(),))
        c.execute("UPDATE outscraper_job_routes SET execution_status='invalidated' WHERE job_id IN "
                  "(SELECT id FROM outscraper_jobs WHERE approval_status='invalidated' AND execution_status='invalidated')")
    c.execute("UPDATE outscraper_jobs SET execution_status='unknown', error='The app restarted while submission was in progress; check provider status before taking further action.' WHERE execution_status='submitting'")
    c.execute("UPDATE outscraper_job_routes SET execution_status='unknown', error='The app restarted while submission was in progress; do not resubmit automatically.' WHERE execution_status='submitting'")
    c.execute(
        "UPDATE outscraper_job_routes SET execution_status='unknown', error='The app restarted before submission status could be recorded; do not resubmit automatically.' "
        "WHERE execution_status='queued' AND job_id IN (SELECT id FROM outscraper_jobs WHERE execution_status='unknown' AND error LIKE 'The app restarted while submission is in progress%')"
    )
    cols = [x[1] for x in c.execute("PRAGMA table_info(requests)")]
    for col, ddl in (("type", "TEXT DEFAULT 'area'"), ("reply", "TEXT"), ("reply_at", "TEXT"), ("data", "TEXT"), ("hours", "TEXT"), ("path", "TEXT"), ("road_km", "REAL"), ("junctions", "INTEGER"), ("start_time", "TEXT"), ("end_time", "TEXT"), ("point_c", "TEXT"), ("point_c_distance_km", "REAL"), ("show_point_c", "INTEGER NOT NULL DEFAULT 0"), ("title", "TEXT"), ("junction_quality_ack", "INTEGER NOT NULL DEFAULT 0"), ("junction_quality_ack_at", "TEXT"), ("group_id", "INTEGER"), ("period_label", "TEXT"), ("data_interval", "INTEGER")):
        if col not in cols: c.execute(f"ALTER TABLE requests ADD COLUMN {col} {ddl}")
    c.execute("CREATE INDEX IF NOT EXISTS idx_requests_group ON requests(group_id)")
    if not c.execute("SELECT 1 FROM periods").fetchone():
        c.executemany("INSERT INTO periods(label,sort_order) VALUES(?,?)", [("AM", 1), ("MD", 2), ("PM", 3)])
    workflow_now = common_svc._timestamp_now()
    for request_id, services_json in c.execute("SELECT id,services FROM requests"):
        if "volumes" in json.loads(services_json or "[]"):
            c.execute(
                "INSERT OR IGNORE INTO volumes_workflows(request_id,status,created_at,updated_at) "
                "VALUES(?,'waiting_for_inputs',?,?)",
                (request_id, workflow_now, workflow_now),
            )
    notifications_svc._translate_legacy_notifications(c)
    if not c.execute("SELECT 1 FROM users").fetchone():
        admin_password = os.environ.get("TRAFFICLENS_ADMIN_PASSWORD", "").strip()
        generated_password = not admin_password
        if is_production() and (generated_password or len(admin_password) < 12):
            raise RuntimeError("Set TRAFFICLENS_ADMIN_PASSWORD (at least 12 characters) before the first start in production.")
        if generated_password:
            admin_password = secrets.token_urlsafe(12)
        c.execute("INSERT INTO users(name,email,pw,role) VALUES(?,?,?,?)",
                  ("Admin", "admin@demo.com", generate_password_hash(admin_password), "admin"))
        if generated_password:
            print(f"[TrafficLens] First-run admin created: admin@demo.com / {admin_password}  "
                  "(shown once; set TRAFFICLENS_ADMIN_PASSWORD to choose your own)", flush=True)
    bundled_network = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Kwait Links.zip")
    setup_done = c.execute("SELECT 1 FROM settings WHERE key='bundled_network_setup_v2'").fetchone()
    network_row = c.execute("SELECT value FROM settings WHERE key='network'").fetchone()
    cfg = json.loads(network_row[0]) if network_row else {}
    configured_file = cfg.get("file") or ""
    is_bundled = bool(configured_file and os.path.isfile(configured_file) and os.path.samefile(configured_file, bundled_network))
    if not setup_done and os.path.isfile(bundled_network) and network.HAVE_GEO and (not configured_file or is_bundled):
        metadata = network.inspect(bundled_network)
        guesses = network.guess(metadata["columns"])
        numeric = network.numeric_columns(bundled_network)
        cfg.update({"file": bundled_network, "filename": "Kwait Links.zip",
                    "uploaded": cfg.get("uploaded") or dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "tol": cfg.get("tol", 25), "min_cov": cfg.get("min_cov", 80),
                    "speed_unit": cfg.get("speed_unit", "km/h"), **metadata})
        for key in ("lanes", "speed", "capacity"):
            if cfg.get(key) not in numeric: cfg[key] = guesses.get(key, "")
        if cfg.get("road_name") not in metadata["columns"]:
            cfg["road_name"] = guesses.get("road_name", "")
        c.execute("INSERT INTO settings(key,value) VALUES('network',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (json.dumps(cfg),))
        c.execute("INSERT INTO settings(key,value) VALUES('bundled_network_setup_v2','true')")
    elif not setup_done and configured_file and not is_bundled:
        c.execute("INSERT INTO settings(key,value) VALUES('bundled_network_setup_v2','true')")
    c.commit(); c.close()


@app.context_processor
def inject():
    catalog = catalog_svc.service_catalog()
    unread_notifications = 0
    latest_notification_id = 0
    if session.get("uid"):
        unread_notifications = db().execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
            (session["uid"],),
        ).fetchone()[0]
        latest_notification_id = db().execute(
            "SELECT COALESCE(MAX(id),0) FROM notifications WHERE user_id=?",
            (session["uid"],),
        ).fetchone()[0]
    return {"SERVICES": {k: v["name"] for k, v in catalog.items()},
            "ACTIVE_SERVICES": catalog_svc.active_services(), "SERVICE_CATALOG": catalog,
            "me": session, "APP_NAME": APP_NAME, "SVINFO": catalog_svc.svinfo(), "NET_SERVICES": catalog_svc.net_services(),
            "unread_notifications": unread_notifications,
            "latest_notification_id": latest_notification_id}

@app.before_request
def refresh_session():
    # keep name/role in sync with the database, so a promoted or demoted user is affected immediately
    if "uid" in session and not request.path.startswith("/static"):
        u = db().execute(
            "SELECT name, role, profile_image FROM users WHERE id=?",
            (session["uid"],),
        ).fetchone()
        if not u: session.clear()
        else:
            session["name"], session["role"] = u["name"], u["role"]
            session["profile_image"] = bool(u["profile_image"])


# ---------------------------------------------------------------------------
# Grouped orders: several links and/or several periods submitted as one order.
# Every (link x period) is still an ordinary row in `requests`, so the whole
# existing admin workflow (volumes, directions, free flow...) keeps working;
# `request_groups` ties the rows together for intake, review and one report.
# ---------------------------------------------------------------------------


init()   # also runs when started with "flask run" or gunicorn
if app.config["OUTSCRAPER_AUTO_POLL"]:
    with app.app_context():
        recoverable_jobs = db().execute(
            "SELECT DISTINCT job_id FROM outscraper_job_routes WHERE execution_status IN ('running','unknown') AND provider_request_id IS NOT NULL"
        ).fetchall()
    for recoverable in recoverable_jobs:
        outscraper_jobs_svc._start_outscraper_monitor(recoverable["job_id"])

# "python app.py" with TRAFFICLENS_DEBUG=1 runs the code twice under the reloader; only the child process does the upkeep.
if app.config["MAINTENANCE_AUTO_START"] and (__name__ != "__main__" or not debug_enabled() or os.environ.get("WERKZEUG_RUN_MAIN") == "true"):
    maintenance_svc.start_maintenance()

if __name__ == "__main__":
    app.run(debug=debug_enabled())