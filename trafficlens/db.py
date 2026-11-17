"""Database access: the per-request connection and the schema.

The database path lives in app.config["DATABASE"] (set in app.py), so tests and
tools can point the app at another file without touching this module.
Migrations (init) will move here in a later step.
"""
import sqlite3

from flask import current_app, g


def db():
    """Return this request's SQLite connection, opening it on first use."""
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_exc=None):
    """Registered with app.teardown_appcontext: close the connection when the request ends."""
    connection = g.pop("db", None)
    if connection:
        connection.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, pw TEXT, role TEXT DEFAULT 'client');
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, user_id INTEGER, created TEXT, d1 TEXT, d2 TEXT,
  services TEXT, area TEXT, bbox TEXT, coords TEXT, status TEXT DEFAULT 'Pending', results TEXT,
  type TEXT DEFAULT 'area', reply TEXT, reply_at TEXT, data TEXT, hours TEXT, path TEXT, road_km REAL, junctions INTEGER,
  start_time TEXT, end_time TEXT, point_c TEXT, point_c_distance_km REAL,
  show_point_c INTEGER NOT NULL DEFAULT 0, title TEXT,
  junction_quality_ack INTEGER NOT NULL DEFAULT 0, junction_quality_ack_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, user_id INTEGER, user_name TEXT,
  role TEXT, action TEXT NOT NULL, request_id INTEGER, details TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id, id);
CREATE TABLE IF NOT EXISTS service_info(key TEXT PRIMARY KEY, description TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  request_id INTEGER NOT NULL REFERENCES requests(id), kind TEXT NOT NULL,
  title TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
  read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread ON notifications(user_id, read_at, id);
CREATE TABLE IF NOT EXISTS outscraper_jobs(
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  config_json TEXT NOT NULL, config_hash TEXT NOT NULL, approval_token_hash TEXT NOT NULL,
  approval_status TEXT NOT NULL DEFAULT 'pending', execution_status TEXT NOT NULL DEFAULT 'awaiting_approval',
  approved_at TEXT, rejected_at TEXT, started_at TEXT, completed_at TEXT,
  estimated_cost REAL, estimated_currency TEXT, estimate_confidence TEXT NOT NULL DEFAULT 'unknown',
  actual_cost REAL, actual_currency TEXT, provider_ids TEXT NOT NULL DEFAULT '[]',
  error TEXT, result_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS outscraper_job_routes(
  id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES outscraper_jobs(id),
  route_index INTEGER NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
  execution_status TEXT NOT NULL DEFAULT 'awaiting_approval', provider_request_id TEXT,
  provider_status TEXT, submitted_at TEXT, completed_at TEXT, last_polled_at TEXT,
  request_details_json TEXT, raw_response_json TEXT, result_count INTEGER NOT NULL DEFAULT 0, error TEXT,
  UNIQUE(job_id, route_index)
);
CREATE INDEX IF NOT EXISTS idx_outscraper_jobs_created ON outscraper_jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_outscraper_jobs_status ON outscraper_jobs(approval_status, execution_status);
CREATE INDEX IF NOT EXISTS idx_outscraper_routes_status ON outscraper_job_routes(execution_status);
CREATE TABLE IF NOT EXISTS request_directions_workflows(
  job_id TEXT PRIMARY KEY REFERENCES outscraper_jobs(id),
  request_id INTEGER NOT NULL REFERENCES requests(id),
  status TEXT NOT NULL DEFAULT 'awaiting_response',
  interval_seconds INTEGER NOT NULL,
  interval_minutes INTEGER,
  result_json TEXT,
  error TEXT,
  confirmed_by INTEGER REFERENCES users(id),
  confirmed_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_directions_request ON request_directions_workflows(request_id, created_at);
CREATE TABLE IF NOT EXISTS volumes_workflows(
  request_id INTEGER PRIMARY KEY REFERENCES requests(id),
  status TEXT NOT NULL DEFAULT 'waiting_for_inputs',
  lanes REAL,
  actual_result_json TEXT,
  freeflow_speed REAL,
  freeflow_accepted INTEGER NOT NULL DEFAULT 0,
  freeflow_result_json TEXT,
  freeflow_validation_json TEXT,
  freeflow_override INTEGER NOT NULL DEFAULT 0,
  freeflow_accepted_at TEXT,
  freeflow_job_id TEXT,
  result_json TEXT,
  workbook_path TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at TEXT,
  sent_at TEXT
);
CREATE TABLE IF NOT EXISTS volume_freeflow_attempts(
  job_id TEXT PRIMARY KEY REFERENCES outscraper_jobs(id),
  request_id INTEGER NOT NULL REFERENCES requests(id),
  submitted_at TEXT NOT NULL,
  status TEXT NOT NULL,
  result_json TEXT,
  validation_json TEXT,
  error TEXT,
  accepted INTEGER NOT NULL DEFAULT 0,
  override INTEGER NOT NULL DEFAULT 0,
  accepted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_volume_freeflow_attempts_request
  ON volume_freeflow_attempts(request_id,submitted_at);
CREATE TABLE IF NOT EXISTS request_groups(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), title TEXT,
  created TEXT NOT NULL, study_date TEXT NOT NULL, services TEXT NOT NULL,
  mode TEXT NOT NULL, sent_at TEXT
);
CREATE TABLE IF NOT EXISTS periods(
  id INTEGER PRIMARY KEY, label TEXT NOT NULL UNIQUE,
  is_active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0
);
"""
