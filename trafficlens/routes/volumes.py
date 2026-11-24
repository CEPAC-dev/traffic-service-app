"""Traffic volumes workflow for a request: lanes, free-flow, run, confirm, workbook."""
import datetime as dt
import json
import math
import outscraper_integration as outscraper
import volumes_runner
from flask import Blueprint, abort, jsonify, request, send_file, session, url_for
from pathlib import Path
from trafficlens.config import MAX_VOLUME_LANES, SERVICES
from trafficlens.db import db
from trafficlens.security.decorators import admin_required, login_required
from trafficlens.services import result_reuse as reuse_svc
from trafficlens.services import audit as audit_svc, catalog as catalog_svc, common as common_svc, directions as directions_svc, maintenance as maintenance_svc, notifications as notifications_svc, volumes as volumes_svc

bp = Blueprint("volumes", __name__)


@bp.get("/api/requests/<int:rid>/volumes/freeflow-preview")
@admin_required
def prepare_volumes_freeflow_preview(rid):
    source_row = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not source_row:
        return jsonify(error="Request not found."), 404
    if "volumes" not in json.loads(source_row["services"] or "[]"):
        return jsonify(error="This request does not include Traffic volumes."), 404
    workflow = db().execute(
        "SELECT freeflow_accepted,freeflow_job_id FROM volumes_workflows WHERE request_id=?",
        (rid,),
    ).fetchone()
    if not workflow:
        return jsonify(error="The Traffic volumes workflow is not available."), 404
    if workflow["freeflow_accepted"]:
        return jsonify(error="A free-flow speed has already been accepted for this request."), 409
    previous_configuration = None
    if workflow["freeflow_job_id"]:
        active_job = db().execute(
            "SELECT execution_status,config_json FROM outscraper_jobs WHERE id=?",
            (workflow["freeflow_job_id"],),
        ).fetchone()
        if active_job and active_job["execution_status"] in {
            "submitting", "running", "unknown",
        }:
            return jsonify(error="A free-flow speed request is already in progress."), 409
        if active_job:
            previous_configuration = json.loads(active_job["config_json"])
    if not outscraper.api_key_configured():
        return jsonify(error="OUTSCRAPER_API_KEY is not configured on this server."), 503
    try:
        prepared = directions_svc._outscraper_prefill_from_request(source_row, "freeflow_speed")
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    configuration = prepared["prefill_config"]
    if (
        previous_configuration
        and previous_configuration.get("trafficlens_context", {}).get("volume_input")
    ):
        configuration = previous_configuration
    return jsonify(
        source_request=prepared["source_request"],
        configuration=configuration,
    )


@bp.post("/api/requests/<int:rid>/volumes/lanes")
@admin_required
def save_volumes_lanes(rid):
    worker_key = f"volumes-{rid}"
    with common_svc._outscraper_workers_lock:
        if worker_key in common_svc._outscraper_workers:
            return jsonify(error="Lane count cannot be changed while MATLAB inputs are being saved or calculated."), 409
    payload = request.get_json(silent=True) or {}
    try:
        lanes = float(payload.get("lanes"))
    except (TypeError, ValueError):
        return jsonify(error="Enter a valid lane count."), 400
    if not math.isfinite(lanes) or not lanes.is_integer() or not 1 <= lanes <= MAX_VOLUME_LANES:
        return jsonify(error=f"Lane count must be a whole number from 1 to {MAX_VOLUME_LANES}."), 400
    lanes = int(lanes)
    workflow = db().execute(
        "SELECT status FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    if not workflow:
        return jsonify(error="This request does not include Traffic volumes."), 404
    if workflow["status"] in {"running", "ready_for_review", "sent"}:
        return jsonify(error="Lane count cannot be changed while MATLAB is running or after a result is ready for review."), 409
    request_row = db().execute(
        "SELECT services,data FROM requests WHERE id=?", (rid,)
    ).fetchone()
    if not request_row:
        return jsonify(error="Source request not found."), 404
    requested_services = set(json.loads(request_row["services"] or "[]"))
    request_data = json.loads(request_row["data"] or "{}")
    volumes_svc._save_volume_lanes_service(request_data, requested_services, lanes)
    db().execute("BEGIN IMMEDIATE")
    db().execute(
        "UPDATE volumes_workflows SET lanes=?, status='waiting_for_inputs', error=NULL, updated_at=? WHERE request_id=?",
        (lanes, common_svc._timestamp_now(), rid),
    )
    if "lanes" in requested_services:
        db().execute("UPDATE requests SET data=? WHERE id=?", (json.dumps(request_data, ensure_ascii=False), rid))
    audit_svc.audit("volumes_lanes_saved", rid, lanes=lanes)
    db().commit()
    volumes_svc._start_volume_calculation(rid)
    return jsonify(ok=True)


@bp.get("/api/requests/<int:rid>/volumes")
@admin_required
def volumes_workflow_status(rid):
    workflow = db().execute(
        "SELECT * FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    if not workflow:
        return jsonify(error="This request does not include Traffic volumes."), 404
    actual_hours_complete = volumes_svc._sync_volume_actual_speeds(rid)
    validation = json.loads(workflow["freeflow_validation_json"] or "{}")
    if (
        workflow["freeflow_result_json"]
        and validation.get("missing_hours")
        and actual_hours_complete
    ):
        if volumes_svc._evaluate_volume_freeflow(rid):
            volumes_svc._start_volume_calculation(rid)
        workflow = db().execute(
            "SELECT * FROM volumes_workflows WHERE request_id=?", (rid,)
        ).fetchone()
    if workflow["freeflow_speed"] is None and workflow["freeflow_job_id"]:
        completed_job = db().execute(
            "SELECT execution_status FROM outscraper_jobs WHERE id=?",
            (workflow["freeflow_job_id"],),
        ).fetchone()
        if completed_job and completed_job["execution_status"] == "completed":
            directions_svc._persist_holiday_result_context(workflow["freeflow_job_id"])
            workflow = db().execute(
                "SELECT * FROM volumes_workflows WHERE request_id=?", (rid,)
            ).fetchone()
    result = json.loads(workflow["result_json"]) if workflow["result_json"] else None
    request_row = db().execute("SELECT data FROM requests WHERE id=?", (rid,)).fetchone()
    request_data = json.loads(request_row["data"] or "{}") if request_row else {}
    freeflow_job = None
    if workflow["freeflow_job_id"]:
        freeflow_job = db().execute(
            "SELECT execution_status FROM outscraper_jobs WHERE id=?",
            (workflow["freeflow_job_id"],),
        ).fetchone()
    attempts = []
    for attempt_row in db().execute(
        "SELECT job_id AS id,submitted_at,status,result_json,validation_json,error,accepted,override,accepted_at "
        "FROM volume_freeflow_attempts WHERE request_id=? ORDER BY submitted_at DESC LIMIT 100",
        (rid,),
    ).fetchall():
        attempt = dict(attempt_row)
        attempt["result"] = json.loads(attempt.pop("result_json") or "null")
        attempt["validation"] = json.loads(attempt.pop("validation_json") or "null")
        attempts.append(attempt)
    return jsonify(
        workflow={
            "status": workflow["status"],
            "lanes": workflow["lanes"],
            "freeflow_speed": workflow["freeflow_speed"],
            "freeflow_context": (request_data.get("volumes") or {}).get("freeflow_context"),
            "freeflow_job_id": workflow["freeflow_job_id"],
            "freeflow_job_status": freeflow_job["execution_status"] if freeflow_job else None,
            "freeflow_accepted": bool(workflow["freeflow_accepted"]),
            "freeflow_override": bool(workflow["freeflow_override"]),
            "freeflow_validation": json.loads(workflow["freeflow_validation_json"] or "null"),
            "freeflow_result": json.loads(workflow["freeflow_result_json"] or "null"),
            "freeflow_attempts": attempts,
            "error": workflow["error"],
            "result": result,
        }
    )


@bp.post("/api/requests/<int:rid>/volumes/freeflow/accept")
@admin_required
def accept_volume_freeflow(rid):
    payload = request.get_json(silent=True) or {}
    use_anyway = payload.get("use_anyway") is True
    workflow = db().execute(
        "SELECT * FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    if not workflow:
        return jsonify(error="This request does not include Traffic volumes."), 404
    if workflow["freeflow_accepted"]:
        return jsonify(error="The free-flow speed has already been accepted."), 409
    actual_hours_complete = volumes_svc._sync_volume_actual_speeds(rid)
    validation = json.loads(workflow["freeflow_validation_json"] or "{}")
    if (
        workflow["freeflow_result_json"]
        and validation.get("missing_hours")
        and actual_hours_complete
        and volumes_svc._evaluate_volume_freeflow(rid)
    ):
        started = volumes_svc._start_volume_calculation(rid)
        current = db().execute(
            "SELECT status FROM volumes_workflows WHERE request_id=?", (rid,)
        ).fetchone()
        return jsonify(
            ok=True,
            accepted=True,
            overridden=False,
            status=current["status"] if current else "waiting_for_inputs",
            matlab_started=started,
        )
    workflow = db().execute(
        "SELECT * FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    if not workflow["freeflow_result_json"] or not workflow["freeflow_validation_json"]:
        return jsonify(error="A complete free-flow response and comparison are required before acceptance."), 409
    validation = json.loads(workflow["freeflow_validation_json"])
    if validation.get("missing_hours"):
        return jsonify(error="Actual Directions speeds are missing for selected hours. Complete and confirm every requested hour before accepting free-flow speed."), 409
    if not validation.get("is_valid") and not use_anyway:
        return jsonify(error="The free-flow speed is not higher than actual speed for every selected hour. Explicitly confirm Use anyway to continue."), 409
    if use_anyway and not validation.get("mismatched_hours"):
        return jsonify(error="Use anyway is available only when the comparison has mismatched hours."), 409
    now = common_svc._timestamp_now()
    source_result = json.loads(workflow["freeflow_result_json"])
    request_row = db().execute(
        "SELECT services,data,user_id FROM requests WHERE id=?", (rid,)
    ).fetchone()
    if not request_row:
        return jsonify(error="Source request not found."), 404
    request_data = json.loads(request_row["data"] or "{}")
    requested_services = set(json.loads(request_row["services"] or "[]"))
    if "freeflow_speed" in requested_services:
        request_data["freeflow_speed"] = {
            "rows": [{"value": source_result["selected_speed_kmh"]}],
            "note": "Selected as the highest speed returned for the reviewed A→C weekly-weekend window; accepted with an explicit administrator override.",
            "source": "outscraper",
            "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "outscraper_job_id": workflow["freeflow_job_id"],
            "reference_context": source_result.get("context"),
        }
        notifications_svc._create_notification(
            request_row["user_id"], rid, "directions_result",
            "Free-flow speed results are ready",
            f"The administrator approved free-flow speed for request REQ-{rid}. Traffic volumes remain under review.",
        )
        db().execute(
            "UPDATE requests SET data=?,status=CASE WHEN status NOT IN ('Rejected','Completed') "
            "THEN 'Processing' ELSE status END WHERE id=?",
            (json.dumps(request_data, ensure_ascii=False), rid),
        )
    db().execute(
        "UPDATE volumes_workflows SET freeflow_accepted=1,freeflow_override=?,"
        "freeflow_accepted_at=?,error=NULL,updated_at=? WHERE request_id=? AND freeflow_accepted=0",
        (int(use_anyway), now, now, rid),
    )
    if db().execute("SELECT changes()").fetchone()[0] != 1:
        db().rollback()
        return jsonify(error="The free-flow workflow changed before acceptance. Reload and review the latest response."), 409
    db().execute(
        "UPDATE volume_freeflow_attempts SET status='accepted',accepted=1,override=?,"
        "accepted_at=?,validation_json=? WHERE job_id=?",
        (
            int(use_anyway),
            now,
            json.dumps(validation, ensure_ascii=False),
            workflow["freeflow_job_id"],
        ),
    )
    audit_svc.audit("volumes_freeflow_accepted", rid, freeflow_speed=workflow["freeflow_speed"], used_anyway=use_anyway)
    db().commit()
    started = volumes_svc._start_volume_calculation(rid)
    return jsonify(
        ok=True,
        accepted=True,
        overridden=use_anyway,
        status=db().execute(
            "SELECT status FROM volumes_workflows WHERE request_id=?", (rid,)
        ).fetchone()["status"],
        matlab_started=started,
    )


@bp.post("/api/requests/<int:rid>/volumes/run")
@admin_required
def retry_volumes_calculation(rid):
    workflow = db().execute(
        "SELECT status FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    if not workflow:
        return jsonify(error="This request does not include Traffic volumes."), 404
    if workflow["status"] != "failed":
        return jsonify(error="Only a failed MATLAB calculation can be retried."), 409
    db().execute(
        "UPDATE volumes_workflows SET status='waiting_for_inputs', error=NULL, updated_at=? WHERE request_id=?",
        (common_svc._timestamp_now(), rid),
    )
    audit_svc.audit("volumes_retry_requested", rid)
    db().commit()
    started = volumes_svc._start_volume_calculation(rid)
    current = db().execute(
        "SELECT status FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    status = current["status"] if current else "waiting_for_inputs"
    return jsonify(ok=started, status=status)


@bp.get("/api/requests/<int:rid>/volumes/workbook")
@login_required
def download_volumes_workbook(rid):
    """Serve this request's own MATLAB result workbook: its client after approval, or an admin."""
    row = db().execute(
        "SELECT r.user_id, w.status, w.workbook_path FROM requests r "
        "JOIN volumes_workflows w ON w.request_id=r.id WHERE r.id=?",
        (rid,),
    ).fetchone()
    is_admin = session.get("role") == "admin"
    if not row or (row["user_id"] != session["uid"] and not is_admin):
        abort(404)
    if not is_admin and row["status"] != "sent":
        abort(404)
    try:
        resolved = Path(row["workbook_path"] or "").resolve(strict=True)
        resolved.relative_to(volumes_runner.volume_runs_root().resolve())
    except (OSError, ValueError):
        abort(404)
    if not resolved.is_file() or not resolved.name.endswith("-results.xlsx"):
        abort(404)
    return send_file(
        resolved,
        as_attachment=True,
        download_name=f"REQ-{rid}-volumes.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@bp.post("/api/requests/<int:rid>/volumes/confirm")
@admin_required
def confirm_volumes_result(rid):
    workflow = db().execute(
        "SELECT * FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    if not workflow:
        return jsonify(error="This request does not include Traffic volumes."), 404
    if workflow["status"] == "sent":
        return jsonify(error="These volume results have already been sent."), 409
    if workflow["status"] != "ready_for_review" or not workflow["result_json"]:
        return jsonify(error="The MATLAB result is not ready for review."), 409
    row = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not row:
        return jsonify(error="Source request not found."), 404
    result = json.loads(workflow["result_json"])
    outputs = result.get("hourly") or []
    requested_hours = sorted(set(json.loads(row["hours"] or "[]")))
    if [item.get("hour") for item in outputs] != requested_hours:
        return jsonify(error="The MATLAB result does not match the requested hours. Recalculate before sending."), 409
    if any(item.get("value") is None or item["value"] < 0 for item in outputs):
        return jsonify(error="The MATLAB result contains a negative volume. Check the inputs and recalculate before sending."), 409
    data = json.loads(row["data"] or "{}")
    volume_data = {
        "rows": [
            {"hour": item["hour"], "value": int(math.floor(item["value"] + 0.5)), "tolerance": round(item["tolerance"], 1)}
            for item in outputs
        ],
        "note": "Estimated by the MATLAB traffic model using the saved lanes, actual hourly speed, and weekly-weekend free-flow speed.",
        "source": "matlab",
        "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    freeflow_context = result.get("inputs", {}).get("freeflow_context")
    if freeflow_context:
        volume_data["freeflow_context"] = freeflow_context
    data["volumes"] = volume_data
    requested_services = set(json.loads(row["services"] or "[]"))
    volumes_svc._save_volume_lanes_service(data, requested_services, workflow["lanes"])
    # One approval sends everything that is ready: the reviewed Directions result (Speed / Travel)
    # goes out together with Traffic volumes and Lanes.
    directions_row = db().execute(
        "SELECT job_id,result_json FROM request_directions_workflows "
        "WHERE request_id=? AND status='ready' AND result_json IS NOT NULL "
        "ORDER BY updated_at DESC,created_at DESC LIMIT 1",
        (rid,),
    ).fetchone()
    directions_job_id = None
    if directions_row:
        directions_job_id = directions_row["job_id"]
        data.update(directions_svc._directions_service_data(
            requested_services, json.loads(directions_row["result_json"]), directions_job_id
        ))
    not_ready = [
        SERVICES[service] for service in ("speed", "travel")
        if service in requested_services and not (data.get(service) or {}).get("rows")
    ]
    if not_ready:
        return jsonify(error="These results are not ready yet: " + ", ".join(not_ready) + ". Wait for the Directions result before sending."), 409
    all_services_ready = catalog_svc._all_requested_services_ready(requested_services, data)
    now = common_svc._timestamp_now()
    d = db()
    d.execute("BEGIN IMMEDIATE")
    current = d.execute(
        "SELECT status FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    if not current or current["status"] != "ready_for_review":
        d.rollback()
        return jsonify(error="The volume workflow changed before confirmation. Reload and review the current status."), 409
    d.execute(
        "UPDATE requests SET data=?, status=CASE WHEN status='Rejected' THEN status "
        "WHEN ? THEN 'Completed' ELSE 'Processing' END WHERE id=?",
        (json.dumps(data, ensure_ascii=False), int(all_services_ready), rid),
    )
    d.execute(
        "UPDATE volumes_workflows SET status='sent', reviewed_by=?, reviewed_at=?, sent_at=?, updated_at=? "
        "WHERE request_id=? AND status='ready_for_review'",
        (session["uid"], now, now, now, rid),
    )
    if directions_job_id:
        d.execute(
            "UPDATE request_directions_workflows SET status='confirmed', confirmed_by=?, confirmed_at=?, updated_at=? "
            "WHERE request_id=? AND job_id=? AND status='ready'",
            (session["uid"], now, now, rid, directions_job_id),
        )
    sent_directions = bool(directions_job_id and {"speed", "travel"}.intersection(requested_services))
    notifications_svc._create_notification(
        row["user_id"], rid, "volume_result",
        "Request results are ready" if sent_directions else "Traffic volume results are ready",
        f"The administrator approved the results for request REQ-{rid}." if sent_directions
        else f"The administrator approved the traffic volume results for request REQ-{rid}.",
    )
    audit_svc.audit(
        "volumes_approved", rid,
        lanes=workflow["lanes"], freeflow_speed=workflow["freeflow_speed"],
        sent_values=[{"hour": item["hour"], "value": item["value"], "tolerance": item["tolerance"]} for item in volume_data["rows"]],
        warnings_shown=result.get("warnings") or [],
    )
    d.commit()
    maintenance_svc._cleanup_superseded_volume_runs(rid)
    return jsonify(
        ok=True,
        status="sent",
        request_status="Completed" if all_services_ready else "Processing",
        result_url=url_for("client.result", rid=rid),
    )


@bp.get("/api/requests/<int:rid>/reuse-candidates")
@admin_required
def reuse_candidates(rid):
    """Earlier completed API results this request could reuse. Only a list: nothing is reused until the admin asks."""
    try:
        return jsonify(directions=reuse_svc.directions_candidates(rid), freeflow=reuse_svc.freeflow_candidates(rid))
    except reuse_svc.ReuseError as exc:
        return jsonify(error=str(exc)), 404


def _reuse(rid, action):
    payload = request.get_json(force=True, silent=True) or {}
    source = payload.get("source_job_id")
    if not isinstance(source, str) or not source:
        return jsonify(error="Choose the earlier result to reuse."), 400
    try:
        return jsonify(ok=True, **action(rid, source, session["uid"]))
    except reuse_svc.ReuseError as exc:
        return jsonify(error=str(exc)), 409


@bp.post("/api/requests/<int:rid>/directions/reuse")
@admin_required
def reuse_directions_result(rid):
    return _reuse(rid, reuse_svc.reuse_directions)


@bp.post("/api/requests/<int:rid>/volumes/freeflow-reuse")
@admin_required
def reuse_freeflow_result(rid):
    return _reuse(rid, reuse_svc.reuse_freeflow)
