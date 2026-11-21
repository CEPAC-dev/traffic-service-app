"""Admin pages and API for Outscraper route jobs."""
import csv
import datetime as dt
import io
import json
import outscraper_integration as outscraper
import secrets
import threading
from flask import Blueprint, abort, jsonify, render_template, request, session
from trafficlens.config import ABANDON_MESSAGE, SERVICES
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.security.decorators import admin_required
from trafficlens.services import common as common_svc, directions as directions_svc, exports as exports_svc, holiday_calendar as holiday_calendar_svc, outscraper_jobs as outscraper_jobs_svc

bp = Blueprint("outscraper_admin", __name__)


@bp.route("/admin/outscraper")
@admin_required
def admin_outscraper():
    start, end = request.args.get("from", "").strip(), request.args.get("to", "").strip()
    status, query = request.args.get("status", "").strip(), request.args.get("q", "").strip()[:200]
    where, params = [], []
    if start:
        try: dt.date.fromisoformat(start)
        except ValueError: abort(400, "Invalid start date")
        where.append("created_at >= ?"); params.append(start + "T00:00:00")
    if end:
        try: dt.date.fromisoformat(end)
        except ValueError: abort(400, "Invalid end date")
        where.append("created_at <= ?"); params.append(end + "T23:59:59")
    if status:
        if status not in {"pending", "approved", "rejected", "invalidated", "awaiting_approval",
                          "submitting", "running", "completed", "failed", "unknown"}:
            abort(400, "Invalid status filter")
        where.append("(approval_status=? OR execution_status=?)"); params.extend((status, status))
    if query:
        where.append("EXISTS (SELECT 1 FROM outscraper_job_routes r WHERE r.job_id=outscraper_jobs.id AND (r.origin LIKE ? OR r.destination LIKE ?))")
        params.extend((f"%{query}%", f"%{query}%"))
    clause = " WHERE " + " AND ".join(where) if where else ""
    jobs = db().execute(
        "SELECT * FROM outscraper_jobs" + clause + " ORDER BY created_at DESC LIMIT 250", params).fetchall()
    summaries = []
    for row in jobs:
        job = dict(row)
        job["config"] = json.loads(job.pop("config_json"))
        job["routes"] = [dict(route) for route in db().execute(
            "SELECT route_index, origin, destination, execution_status FROM outscraper_job_routes WHERE job_id=? ORDER BY route_index",
            (job["id"],)).fetchall()]
        job["provider_ids"] = json.loads(job["provider_ids"] or "[]")
        summaries.append(job)
    source_request = None
    prefill_config = None
    copied_job_id = None
    source_request_id = request.args.get("source_request_id", "").strip()
    if source_request_id:
        try: source_id = int(source_request_id)
        except ValueError: abort(400, "Invalid source request ID")
        source_row = db().execute("SELECT * FROM requests WHERE id=?", (source_id,)).fetchone()
        if not source_row: abort(404)
        source_service = request.args.get("service", "travel").strip()
        prefill = directions_svc._outscraper_prefill_from_request(source_row, source_service)
        source_request, prefill_config = prefill["source_request"], prefill["prefill_config"]
    duplicate_job_id = request.args.get("duplicate_job_id", "").strip()
    if duplicate_job_id:
        previous = db().execute("SELECT config_json FROM outscraper_jobs WHERE id=?", (duplicate_job_id,)).fetchone()
        if not previous: abort(404)
        try: prefill_config = outscraper.validate_configuration(json.loads(previous["config_json"]))
        except (ValueError, json.JSONDecodeError): abort(400, "This saved request configuration cannot be edited.")
        copied_job_id = duplicate_job_id
    return render_template("outscraper.html", jobs=summaries, api_configured=outscraper.api_key_configured(),
                           languages=sorted(outscraper.LANGUAGES), regions=sorted(outscraper.REGIONS),
                           source_request=source_request, prefill_config=prefill_config,
                           copied_job_id=copied_job_id,
                           filters={"from": start, "to": end, "status": status, "q": query})


@bp.post("/api/outscraper/csv-preview")
@admin_required
def outscraper_csv_preview():
    if request.content_length and request.content_length > 7_000_000:
        return jsonify(error="CSV request body is too large"), 413
    payload = request.get_json(silent=True) or {}
    try:
        routes = outscraper.parse_routes_csv(payload.get("csv", ""))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(routes=routes, count=len(routes))


@bp.post("/api/outscraper/preview")
@admin_required
def outscraper_preview():
    payload = request.get_json(silent=True)
    try:
        config = outscraper.validate_configuration(payload)
        holiday_calendar_svc._validate_holiday_source_request(config)
        directions_svc._validate_trafficlens_request(config)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    holiday_context = config.get("trafficlens_context") or {}
    if holiday_context.get("service") == "freeflow_speed":
        volume_workflow = db().execute(
            "SELECT freeflow_accepted,freeflow_job_id FROM volumes_workflows WHERE request_id=?",
            (holiday_context["source_request_id"],),
        ).fetchone()
        if not volume_workflow:
            return jsonify(error="The Traffic volumes workflow is not available."), 404
        if holiday_context.get("volume_input") and volume_workflow["freeflow_accepted"]:
            return jsonify(error="The free-flow speed has already been accepted for this request."), 409
        if volume_workflow["freeflow_job_id"]:
            active_job = db().execute(
                "SELECT execution_status FROM outscraper_jobs WHERE id=?",
                (volume_workflow["freeflow_job_id"],),
            ).fetchone()
            if active_job and active_job["execution_status"] in {
                "submitting", "running", "unknown",
            }:
                return jsonify(error="A free-flow speed request is already in progress."), 409
    if config.get("trafficlens_request") and not outscraper.api_key_configured():
        return jsonify(error="OUTSCRAPER_API_KEY is not configured on this server."), 503
    job_id, token, now = secrets.token_hex(16), secrets.token_urlsafe(32), common_svc._timestamp_now()
    encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = outscraper.configuration_fingerprint(config)
    d = db()
    d.execute("BEGIN IMMEDIATE")
    request_context = config.get("trafficlens_request")
    if request_context:
        active_workflow = d.execute(
            "SELECT job_id,status FROM request_directions_workflows WHERE request_id=? "
            "AND status NOT IN ('failed','rejected','invalidated') ORDER BY created_at DESC LIMIT 1",
            (request_context["source_request_id"],),
        ).fetchone()
        if active_workflow:
            d.rollback()
            return jsonify(
                error=f"This client request already has a Directions workflow ({active_workflow['status']}). "
                      "Open its current review or Scraper details before starting another request."
            ), 409
    for old_id, old_token in session.get("outscraper_tokens", {}).items():
        d.execute(
            "UPDATE outscraper_jobs SET approval_status='invalidated', execution_status='invalidated', updated_at=? "
            "WHERE id=? AND approval_status='pending' AND execution_status='awaiting_approval' AND approval_token_hash=?",
            (now, old_id, common_svc._token_hash(old_token)))
        if d.execute("SELECT changes()").fetchone()[0]:
            d.execute("UPDATE outscraper_job_routes SET execution_status='invalidated' WHERE job_id=?", (old_id,))
            d.execute(
                "UPDATE request_directions_workflows SET status='invalidated', updated_at=? WHERE job_id=?",
                (now, old_id),
            )
    d.execute(
        "INSERT INTO outscraper_jobs(id,created_at,updated_at,config_json,config_hash,approval_token_hash,estimated_cost,estimated_currency,estimate_confidence,actual_cost,actual_currency) "
        "VALUES(?,?,?,?,?,?,NULL,NULL,'unknown',NULL,NULL)",
        (job_id, now, now, encoded, fingerprint, common_svc._token_hash(token)))
    d.executemany(
        "INSERT INTO outscraper_job_routes(job_id,route_index,origin,destination) VALUES(?,?,?,?)",
        [(job_id, index, route["origin"], route["destination"])
         for index, route in enumerate(config["routes"], 1)])
    if request_context:
        d.execute(
            "INSERT INTO request_directions_workflows(job_id,request_id,status,interval_seconds,interval_minutes,created_at,updated_at) "
            "VALUES(?,?,'awaiting_approval',?,?,?,?)",
            (job_id, request_context["source_request_id"], request_context["interval_minutes"] * 60,
             request_context["interval_minutes"], now, now),
        )
    holiday_context = config.get("trafficlens_context") or {}
    if holiday_context.get("service") == "freeflow_speed":
        if holiday_context.get("volume_input"):
            d.execute(
                "INSERT INTO volume_freeflow_attempts(job_id,request_id,submitted_at,status) "
                "VALUES(?,?,?,'awaiting_approval')",
                (job_id, holiday_context["source_request_id"], now),
            )
        d.execute(
            "UPDATE volumes_workflows SET freeflow_job_id=?, freeflow_speed=NULL, "
            "freeflow_result_json=NULL, freeflow_validation_json=NULL, freeflow_accepted=0, "
            "freeflow_override=0, freeflow_accepted_at=NULL, status='waiting_for_inputs', "
            "error=NULL, updated_at=? WHERE request_id=? AND freeflow_accepted=0",
            (job_id, now, holiday_context["source_request_id"]),
        )
    d.commit()
    session["outscraper_tokens"] = {job_id: token}
    return jsonify(id=job_id, approval_token=token, configuration=config,
                   route_count=len(config["routes"]), planned_http_submissions=len(config["routes"]),
                   cost_estimate=None, currency=None, estimate_confidence="unknown"), 201


@bp.post("/api/outscraper/<job_id>/invalidate")
@admin_required
def invalidate_outscraper_preview(job_id):
    payload = request.get_json(silent=True) or {}
    token = payload.get("approval_token", "")
    if not isinstance(token, str): return jsonify(error="Invalid approval token"), 400
    cursor = db().execute(
        "UPDATE outscraper_jobs SET approval_status='invalidated', execution_status='invalidated', updated_at=? "
        "WHERE id=? AND approval_status='pending' AND execution_status='awaiting_approval' AND approval_token_hash=?",
        (common_svc._timestamp_now(), job_id, common_svc._token_hash(token)))
    if cursor.rowcount != 1: return jsonify(error="Preview is no longer awaiting approval"), 409
    db().execute("UPDATE outscraper_job_routes SET execution_status='invalidated' WHERE job_id=?", (job_id,))
    db().execute(
        "UPDATE request_directions_workflows SET status='invalidated', updated_at=? WHERE job_id=?",
        (common_svc._timestamp_now(), job_id),
    )
    db().commit()
    outscraper_jobs_svc._forget_outscraper_token(job_id)
    return jsonify(ok=True)


@bp.post("/api/outscraper/<job_id>/reject")
@admin_required
def reject_outscraper_job(job_id):
    payload = request.get_json(silent=True) or {}
    token = payload.get("approval_token", "")
    if not isinstance(token, str): return jsonify(error="Invalid approval token"), 400
    now = common_svc._timestamp_now()
    cursor = db().execute(
        "UPDATE outscraper_jobs SET approval_status='rejected', execution_status='rejected', rejected_at=?, updated_at=? "
        "WHERE id=? AND approval_status='pending' AND execution_status='awaiting_approval' AND approval_token_hash=?",
        (now, now, job_id, common_svc._token_hash(token)))
    if cursor.rowcount != 1: return jsonify(error="Request is no longer awaiting approval"), 409
    db().execute("UPDATE outscraper_job_routes SET execution_status='rejected' WHERE job_id=?", (job_id,))
    db().execute(
        "UPDATE request_directions_workflows SET status='rejected', updated_at=? WHERE job_id=?",
        (now, job_id),
    )
    db().commit()
    outscraper_jobs_svc._forget_outscraper_token(job_id)
    return jsonify(ok=True, rejected_at=now)


@bp.post("/api/outscraper/<job_id>/approve")
@admin_required
def approve_outscraper_job(job_id):
    payload = request.get_json(silent=True) or {}
    token, proposed = payload.get("approval_token", ""), payload.get("configuration")
    if not isinstance(token, str): return jsonify(error="Invalid approval token"), 400
    if payload.get("acknowledge_unknown_cost") is not True:
        return jsonify(error="Cost is unknown. Explicitly approve execution despite the unknown cost."), 409
    try:
        config = outscraper.validate_configuration(proposed)
        holiday_calendar_svc._validate_holiday_source_request(config)
        directions_svc._validate_trafficlens_request(config)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if not outscraper.api_key_configured():
        return jsonify(error="OUTSCRAPER_API_KEY is not configured on this server"), 503
    now = common_svc._timestamp_now()
    d = db()
    d.execute("BEGIN IMMEDIATE")
    job = d.execute("SELECT config_hash, approval_token_hash FROM outscraper_jobs WHERE id=? AND approval_status='pending' AND execution_status='awaiting_approval'", (job_id,)).fetchone()
    if not job:
        d.rollback()
        return jsonify(error="Request is no longer awaiting approval; duplicate submission was prevented."), 409
    proposed_hash = outscraper.configuration_fingerprint(config)
    if not secrets.compare_digest(job["config_hash"], proposed_hash) or not secrets.compare_digest(job["approval_token_hash"], common_svc._token_hash(token)):
        d.rollback()
        return jsonify(error="Configuration changed or approval expired. Preview and approve it again."), 409
    d.execute(
        "UPDATE outscraper_jobs SET approval_status='approved', execution_status='submitting', approved_at=?, started_at=?, updated_at=? WHERE id=?",
        (now, now, now, job_id))
    d.execute(
        "UPDATE volume_freeflow_attempts SET status='submitting',error=NULL WHERE job_id=?",
        (job_id,),
    )
    d.execute("UPDATE outscraper_job_routes SET execution_status='queued' WHERE job_id=?", (job_id,))
    d.execute(
        "UPDATE request_directions_workflows SET status='running',error=NULL,updated_at=? WHERE job_id=?",
        (now, job_id),
    )
    d.commit()
    outscraper_jobs_svc._forget_outscraper_token(job_id)
    with common_svc._outscraper_workers_lock:
        if job_id in common_svc._outscraper_workers:
            return jsonify(error="Request is already being processed"), 409
        common_svc._outscraper_workers.add(job_id)
    worker = threading.Thread(target=outscraper_jobs_svc._process_outscraper_job, args=(job_id,), name=f"outscraper-{job_id}", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        with common_svc._outscraper_workers_lock:
            common_svc._outscraper_workers.discard(job_id)
        d.execute("UPDATE outscraper_jobs SET execution_status='unknown', error=?, updated_at=? WHERE id=?",
                  ("Could not start the local worker. Do not resubmit automatically.", common_svc._timestamp_now(), job_id))
        d.execute("UPDATE outscraper_job_routes SET execution_status='unknown', error=? WHERE job_id=?",
                  ("Local submission did not start; verify request status before taking further action.", job_id))
        d.commit()
        return jsonify(error="Execution could not start safely; request marked unknown and will not be resubmitted."), 503
    return jsonify(ok=True, status="submitting", id=job_id), 202


@bp.post("/api/outscraper/<job_id>/refresh")
@admin_required
def refresh_outscraper_job(job_id):
    job = outscraper_jobs_svc._outscraper_job(job_id)
    if not job: return jsonify(error="Request not found"), 404
    polled = []
    for route in job["routes"]:
        if not route["provider_request_id"] or route["execution_status"] not in ("running", "unknown"):
            continue
        last = route.get("last_polled_at")
        if last:
            try:
                elapsed = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds()
            except ValueError:
                elapsed = 15
            if elapsed < 15:
                polled.append({"route": route["route_index"], "status": route["execution_status"], "throttled": True})
                continue
        state = outscraper_jobs_svc._poll_outscraper_route(route)
        polled.append({"route": route["route_index"], "status": state})
    status = outscraper_jobs_svc._refresh_job_status(job_id)
    return jsonify(ok=True, status=status, routes=polled)


@bp.post("/api/outscraper/<job_id>/abandon")
@admin_required
def abandon_outscraper_job(job_id):
    """Stop waiting for a Directions job that never returned data so a NEW request can be prepared.

    This never resubmits anything: the old job stays in history, and a re-send still needs its own
    preview, cost acknowledgement and approval. The provider may still bill for the original call.
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("acknowledge_possible_charge") is not True:
        return jsonify(error="Confirm that the provider may still bill the original request before stopping the wait."), 409
    d = db()
    d.execute("BEGIN IMMEDIATE")
    job = d.execute(
        "SELECT execution_status, updated_at FROM outscraper_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if not job:
        d.rollback()
        return jsonify(error="Request not found"), 404
    state = job["execution_status"]
    stale_submit = False
    if state == "submitting":
        try:
            age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(job["updated_at"])).total_seconds()
        except (TypeError, ValueError):
            age = 0
        stale_submit = age > 300
    if state not in ("running", "unknown") and not stale_submit:
        d.rollback()
        return jsonify(error="Only a request that is still waiting for the provider, or whose status is uncertain, can be stopped."), 409
    now = common_svc._timestamp_now()
    d.execute(
        "UPDATE outscraper_job_routes SET execution_status='abandoned', error=?, completed_at=COALESCE(completed_at,?) "
        "WHERE job_id=? AND execution_status IN ('queued','submitting','running','unknown')",
        (ABANDON_MESSAGE, now, job_id),
    )
    d.execute(
        "UPDATE outscraper_jobs SET execution_status='abandoned', error=?, completed_at=COALESCE(completed_at,?), updated_at=? WHERE id=?",
        (ABANDON_MESSAGE, now, now, job_id),
    )
    d.execute(
        "UPDATE request_directions_workflows SET status='failed', error=?, updated_at=? "
        "WHERE job_id=? AND status NOT IN ('ready','confirmed')",
        (ABANDON_MESSAGE, now, job_id),
    )
    d.execute(
        "UPDATE volume_freeflow_attempts SET status='abandoned', error=? WHERE job_id=? AND accepted=0",
        (ABANDON_MESSAGE, job_id),
    )
    d.execute(
        "UPDATE volumes_workflows SET status=CASE WHEN status IN ('running','ready_for_review','sent') THEN status ELSE 'waiting_for_inputs' END, "
        "error=?, updated_at=? WHERE freeflow_job_id=? AND freeflow_accepted=0",
        (ABANDON_MESSAGE, now, job_id),
    )
    d.commit()
    return jsonify(ok=True, status="abandoned", message=ABANDON_MESSAGE)


@bp.route("/admin/outscraper/<job_id>")
@admin_required
def outscraper_job_detail(job_id):
    job = outscraper_jobs_svc._outscraper_job(job_id)
    if not job: abort(404)
    token = session.get("outscraper_tokens", {}).get(job_id)
    if (job["approval_status"] != "pending" or job["execution_status"] != "awaiting_approval"
            or not token or not secrets.compare_digest(job["approval_token_hash"], common_svc._token_hash(token))):
        token = None
    job["approval_token"] = token
    for route in job["routes"]:
        route["result_pretty"] = json.dumps(route["result"], ensure_ascii=False, indent=2) if route["result"] is not None else ""
    request_workflow = db().execute(
        "SELECT request_id,status,error,confirmed_at FROM request_directions_workflows WHERE job_id=?",
        (job_id,),
    ).fetchone()
    return render_template("outscraper_detail.html", job=job, api_configured=outscraper.api_key_configured(),
                           request_workflow=request_workflow)


@bp.get("/admin/outscraper/<job_id>/results.csv")
@admin_required
def export_outscraper_results(job_id):
    job = outscraper_jobs_svc._outscraper_job(job_id)
    if not job: abort(404)
    records = []
    for route in job["routes"]:
        response = route["result"] or {}
        context = job["config"].get("trafficlens_context") or {}
        for record in exports_svc._result_records(response.get("data")):
            records.append({
                **record,
                "configured_route_index": route["route_index"],
                "configured_origin": route["origin"], "configured_destination": route["destination"],
                "provider_request_id": route["provider_request_id"] or "", "provider_status": route["provider_status"] or "",
                "requested_service": SERVICES.get(context.get("service"), ""),
                "study_date": context.get("study_date", ""),
                "reference_date_used": context.get("weekend_date") or context.get("holiday_date", ""),
                "reference_day_or_holiday": context.get("weekend_day") or context.get("holiday_name", ""),
                "holiday_date_used": context.get("holiday_date", ""),
                "holiday_name": context.get("holiday_name", ""),
                "route_country": context.get("country_name", ""),
                "route_timezone": context.get("timezone", ""),
                "departure_time_local": "07:00" if context else "",
            })
    fields = list(dict.fromkeys(key for record in records for key in record)) or [
        "configured_route_index", "configured_origin", "configured_destination", "provider_request_id", "provider_status"]
    output = io.StringIO(newline="")
    safe_fields = [exports_svc._csv_safe_cell(field) for field in fields]
    writer = csv.DictWriter(output, fieldnames=safe_fields, extrasaction="ignore")
    writer.writeheader()
    for record in records:
        writer.writerow({exports_svc._csv_safe_cell(key): exports_svc._csv_safe_cell(value) for key, value in record.items()})
    return flask_app.response_class(
        output.getvalue(), mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="outscraper-{job_id}.csv"'}
    )
