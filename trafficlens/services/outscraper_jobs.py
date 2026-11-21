"""Outscraper jobs: background workers that submit routes and collect their results."""
import datetime as dt
import json
import outscraper_integration as outscraper
import sqlite3
import threading
import time
from flask import session
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.services import common as common_svc
from trafficlens.services import directions as directions_svc


def _forget_outscraper_token(job_id):
    tokens = dict(session.get("outscraper_tokens", {}))
    tokens.pop(job_id, None)
    session["outscraper_tokens"] = tokens


def _outscraper_job(job_id):
    row = db().execute("SELECT * FROM outscraper_jobs WHERE id=?", (job_id,)).fetchone()
    if not row: return None
    job = dict(row)
    job["config"] = json.loads(job.pop("config_json"))
    job["routes"] = [dict(route) for route in db().execute(
        "SELECT * FROM outscraper_job_routes WHERE job_id=? ORDER BY route_index", (job_id,)).fetchall()]
    for route in job["routes"]:
        route["result"] = json.loads(route["raw_response_json"]) if route["raw_response_json"] else None
        route["request_details"] = json.loads(route["request_details_json"]) if route.get("request_details_json") else None
    return job


def _refresh_job_status(job_id):
    routes = db().execute(
        "SELECT execution_status, result_count, error FROM outscraper_job_routes WHERE job_id=?",
        (job_id,)).fetchall()
    states = [route["execution_status"] for route in routes]
    if states and all(state == "completed" for state in states):
        status = "completed"
    elif any(state == "running" for state in states):
        status = "running"
    elif any(state == "unknown" for state in states):
        status = "unknown"
    elif any(state == "failed" for state in states):
        status = "failed"
    elif any(state == "abandoned" for state in states):
        status = "abandoned"
    elif states and all(state in ("rejected", "invalidated") for state in states):
        status = states[0]
    else:
        status = "submitting"
    errors = [route["error"] for route in routes if route["error"]]
    ids = [row["provider_request_id"] for row in db().execute(
        "SELECT provider_request_id FROM outscraper_job_routes WHERE job_id=? AND provider_request_id IS NOT NULL",
        (job_id,)).fetchall()]
    count = sum(route["result_count"] for route in routes)
    completed = common_svc._timestamp_now() if status in ("completed", "failed", "unknown") else None
    db().execute(
        "UPDATE outscraper_jobs SET execution_status=?, provider_ids=?, result_count=?, error=?, completed_at=COALESCE(?,completed_at), updated_at=? WHERE id=?",
        (status, json.dumps(ids), count, "; ".join(errors) or None, completed, common_svc._timestamp_now(), job_id))
    db().commit()
    db().execute(
        "UPDATE volume_freeflow_attempts SET status=?,error=? WHERE job_id=?",
        (status, "; ".join(errors) or None, job_id),
    )
    db().commit()
    if status == "completed":
        directions_svc._persist_holiday_result_context(job_id)
        workflow = db().execute(
            "SELECT 1 FROM request_directions_workflows WHERE job_id=?", (job_id,)
        ).fetchone()
        if workflow:
            try:
                directions_svc._extract_request_directions_result(job_id)
            except (KeyError, TypeError, ValueError, OverflowError, sqlite3.Error) as exc:
                directions_svc._update_request_directions_workflow(job_id, "failed", str(exc)[:1000])
    elif status in ("failed", "unknown"):
        directions_svc._update_request_directions_workflow(job_id, status, "Scraper execution ended with status: " + status)
    return status


def _poll_outscraper_route(route):
    db().execute("UPDATE outscraper_job_routes SET last_polled_at=? WHERE id=?",
                 (common_svc._timestamp_now(), route["id"]))
    db().commit()
    try:
        response = outscraper.fetch_request(route["provider_request_id"])
    except outscraper.OutscraperError as exc:
        if exc.retryable:
            db().execute("UPDATE outscraper_job_routes SET error=? WHERE id=?", (str(exc), route["id"]))
            db().commit()
            return "running"
        db().execute(
            "UPDATE outscraper_job_routes SET execution_status='failed', completed_at=?, error=? WHERE id=? AND execution_status!='abandoned'",
            (common_svc._timestamp_now(), str(exc), route["id"]))
        db().commit()
        return "failed"
    except ValueError:
        db().execute("UPDATE outscraper_job_routes SET error=? WHERE id=?",
                     ("Outscraper returned an invalid request ID.", route["id"]))
        db().commit()
        return "unknown"
    state = outscraper.provider_state(response)
    finished = common_svc._timestamp_now() if state in ("completed", "failed") else None
    safe_response = outscraper.redact_provider_data(response)
    provider_error = safe_response.get("errorMessage")
    if state == "failed" and not isinstance(provider_error, str):
        provider_error = "Outscraper marked the request as failed."
    db().execute(
        "UPDATE outscraper_job_routes SET execution_status=?, provider_status=?, raw_response_json=?, result_count=?, completed_at=?, error=? WHERE id=? AND execution_status!='abandoned'",
        (state, str(response.get("status") or "")[:80], json.dumps(safe_response, ensure_ascii=False),
         outscraper.result_count(response), finished,
         provider_error[:500] if state == "failed" else
         "Provider status is unclear; do not resubmit this route." if state == "unknown" else None,
         route["id"]))
    db().commit()
    return state


def _monitor_outscraper_job(job_id):
    try:
        for _ in range(16):
            time.sleep(15)
            with flask_app.app_context():
                routes = db().execute(
                    "SELECT * FROM outscraper_job_routes WHERE job_id=? AND execution_status IN ('running','unknown') AND provider_request_id IS NOT NULL",
                    (job_id,)).fetchall()
                for row in routes:
                    last = row["last_polled_at"]
                    if last:
                        try:
                            elapsed = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds()
                        except ValueError:
                            elapsed = 15
                        if elapsed < 15:
                            continue
                    _poll_outscraper_route(dict(row))
                status = _refresh_job_status(job_id)
                remaining = db().execute(
                    "SELECT 1 FROM outscraper_job_routes WHERE job_id=? AND execution_status IN ('running','unknown') AND provider_request_id IS NOT NULL LIMIT 1",
                    (job_id,)).fetchone()
                if not remaining or status in ("completed", "failed"):
                    return
    finally:
        with common_svc._outscraper_workers_lock:
            common_svc._outscraper_workers.discard(job_id)


def _start_outscraper_monitor(job_id):
    if not flask_app.config["OUTSCRAPER_AUTO_POLL"] or not outscraper.api_key_configured():
        return
    with common_svc._outscraper_workers_lock:
        if job_id in common_svc._outscraper_workers:
            return
        common_svc._outscraper_workers.add(job_id)
    try:
        threading.Thread(
            target=_monitor_outscraper_job, args=(job_id,),
            name=f"outscraper-poll-{job_id}", daemon=True,
        ).start()
    except RuntimeError:
        with common_svc._outscraper_workers_lock:
            common_svc._outscraper_workers.discard(job_id)
        flask_app.logger.error("Could not start the local Outscraper status monitor; manual refresh remains available.")


def _run_outscraper_job(job_id):
    with flask_app.app_context():
        job = _outscraper_job(job_id)
        if not job: return
        configuration = job["config"]
        for route in job["routes"]:
            if route["execution_status"] != "queued":
                continue
            db().execute(
                "UPDATE outscraper_job_routes SET execution_status='submitting', submitted_at=?, request_details_json=? WHERE id=? AND execution_status='queued'",
                (common_svc._timestamp_now(), json.dumps(outscraper.directions_request_details(route, configuration), ensure_ascii=False),
                 route["id"]))
            db().execute("UPDATE outscraper_jobs SET updated_at=? WHERE id=?", (common_svc._timestamp_now(), job_id))
            db().commit()
            try:
                response = outscraper.submit_route(route, configuration)
            except outscraper.SubmissionUncertain as exc:
                db().execute("UPDATE outscraper_job_routes SET execution_status='unknown', error=? WHERE id=?",
                             (str(exc), route["id"]))
                db().commit()
                break
            except outscraper.OutscraperError as exc:
                db().execute("UPDATE outscraper_job_routes SET execution_status='failed', error=? WHERE id=?",
                             (str(exc), route["id"]))
                db().commit()
                break
            safe_response = outscraper.redact_provider_data(response)
            provider_id = response.get("id")
            if provider_id is not None and not outscraper.valid_request_id(provider_id):
                db().execute("UPDATE outscraper_job_routes SET execution_status='unknown', provider_status=?, raw_response_json=?, result_count=?, error=? WHERE id=?",
                             (str(response.get("status") or "")[:80], json.dumps(safe_response, ensure_ascii=False),
                              outscraper.result_count(response),
                              "Outscraper returned an invalid request ID; check provider status before retrying.", route["id"]))
                db().commit()
                break
            state = outscraper.provider_state(response)
            if state == "running" and not provider_id:
                state = "unknown"
            finished = common_svc._timestamp_now() if state in ("completed", "failed") else None
            provider_error = safe_response.get("errorMessage")
            if state == "failed" and not isinstance(provider_error, str):
                provider_error = "Outscraper marked the request as failed."
            db().execute(
                "UPDATE outscraper_job_routes SET execution_status=?, provider_request_id=?, provider_status=?, raw_response_json=?, result_count=?, completed_at=?, error=? WHERE id=?",
                (state, provider_id, str(response.get("status") or "")[:80],
                 json.dumps(safe_response, ensure_ascii=False), outscraper.result_count(response),
                 finished, provider_error[:500] if state == "failed" else None if state != "unknown" else
                 "Provider status is unclear; do not resubmit this route.", route["id"]))
            db().commit()
            if state in ("failed", "unknown"):
                break
        db().execute(
            "UPDATE outscraper_job_routes SET execution_status='not_submitted', error=? "
            "WHERE job_id=? AND execution_status='queued'",
            ("Not submitted after a preceding route failed or had an uncertain submission.", job_id))
        db().commit()
        status = _refresh_job_status(job_id)
        if status in ("running", "unknown") and flask_app.config["OUTSCRAPER_AUTO_POLL"]:
            _monitor_outscraper_job(job_id)


def _process_outscraper_job(job_id):
    try:
        _run_outscraper_job(job_id)
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        flask_app.logger.error("Local Outscraper processing failed; marking uncertain routes without retrying.")
        with flask_app.app_context():
            now = common_svc._timestamp_now()
            db().execute(
                "UPDATE outscraper_job_routes SET execution_status='unknown', error=? "
                "WHERE job_id=? AND execution_status IN ('queued','submitting')",
                ("Local processing failed while submission status may be uncertain. Do not resubmit.", job_id))
            db().execute(
                "UPDATE outscraper_jobs SET execution_status='unknown', error=?, completed_at=?, updated_at=? WHERE id=?",
                ("Local processing failed; check provider status before taking further action.", now, now, job_id))
            db().commit()
    finally:
        with common_svc._outscraper_workers_lock:
            common_svc._outscraper_workers.discard(job_id)
