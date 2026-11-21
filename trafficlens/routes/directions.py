"""Route directions workflow for a request."""
import datetime as dt
import json
import math
from flask import Blueprint, jsonify, request, session, url_for
from trafficlens.config import SERVICES
from trafficlens.db import db
from trafficlens.security.decorators import admin_required
from trafficlens.services import audit as audit_svc, catalog as catalog_svc, common as common_svc, directions as directions_svc, notifications as notifications_svc, volumes as volumes_svc

bp = Blueprint("directions", __name__)


@bp.get("/api/requests/<int:rid>/directions")
@admin_required
def request_directions_status(rid):
    workflow = db().execute(
        "SELECT w.*, j.execution_status AS job_status, j.error AS job_error "
        "FROM request_directions_workflows w JOIN outscraper_jobs j ON j.id=w.job_id "
        "WHERE w.request_id=? ORDER BY w.created_at DESC LIMIT 1",
        (rid,),
    ).fetchone()
    if not workflow:
        return jsonify(workflow=None)
    result = json.loads(workflow["result_json"]) if workflow["result_json"] else None
    return jsonify({
        "workflow": {
            "job_id": workflow["job_id"],
            "status": workflow["status"],
            "job_status": workflow["job_status"],
            "interval_minutes": workflow["interval_minutes"] or max(1, math.ceil(workflow["interval_seconds"] / 60)),
            "result": result,
            "error": workflow["error"] or workflow["job_error"],
            "confirmed_at": workflow["confirmed_at"],
            "view_url": url_for("outscraper_admin.outscraper_job_detail", job_id=workflow["job_id"]),
        }
    })


@bp.get("/api/requests/<int:rid>/directions/location")
@admin_required
def request_directions_location(rid):
    row = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not row:
        return jsonify(error="Source request not found."), 404
    try:
        location = directions_svc._request_directions_location(row)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(location=location)


@bp.post("/api/requests/<int:rid>/directions/confirm")
@admin_required
def confirm_request_directions(rid):
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("job_id")
    workflow = db().execute(
        "SELECT * FROM request_directions_workflows WHERE request_id=? AND job_id=?",
        (rid, job_id),
    ).fetchone() if isinstance(job_id, str) else None
    if not workflow:
        return jsonify(error="No Scraper workflow is linked to this request."), 404
    if workflow["status"] == "confirmed":
        return jsonify(error="These results have already been confirmed and sent."), 409
    if workflow["status"] != "ready" or not workflow["result_json"]:
        return jsonify(error="The Scraper response is not complete and ready for review."), 409
    row = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not row:
        return jsonify(error="Source request not found."), 404
    result = json.loads(workflow["result_json"])
    requested_services = set(json.loads(row["services"] or "[]"))
    hourly = result["hourly"]
    data = json.loads(row["data"] or "{}")
    if "volumes" in requested_services:
        now = common_svc._timestamp_now()
        d = db()
        d.execute("BEGIN IMMEDIATE")
        current = d.execute(
            "SELECT status FROM request_directions_workflows WHERE request_id=? AND job_id=?",
            (rid, job_id),
        ).fetchone()
        volume_workflow = d.execute(
            "SELECT status,lanes,freeflow_result_json,freeflow_accepted FROM volumes_workflows WHERE request_id=?", (rid,)
        ).fetchone()
        if not current or current["status"] != "ready" or not volume_workflow:
            d.rollback()
            return jsonify(error="The volume or Directions workflow changed before confirmation. Reload and review the current status."), 409
        if volume_workflow["status"] not in {"running", "ready_for_review", "sent"}:
            d.execute(
                "UPDATE volumes_workflows SET actual_result_json=?, status='waiting_for_inputs', "
                "error=NULL, updated_at=? WHERE request_id=?",
                (json.dumps(result, ensure_ascii=False), now, rid),
            )
        updated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        if "travel" in requested_services:
            data["travel"] = {
                "rows": [{"hour": item["hour"], "value": item["travel_minutes"]} for item in hourly],
                "note": "Automatically extracted from the A→C Scraper response at the saved A→B road distance.",
                "source": "outscraper",
                "updated": updated,
                "outscraper_job_id": job_id,
                "directions_result": result,
            }
        if "speed" in requested_services:
            data["speed"] = {
                "rows": [{"hour": item["hour"], "value": item["speed_kmh"]} for item in hourly],
                "note": "Automatically calculated as A→B distance / time and converted to km/h.",
                "source": "outscraper",
                "updated": updated,
                "outscraper_job_id": job_id,
                "directions_result": result,
            }
        volumes_svc._save_volume_lanes_service(data, requested_services, volume_workflow["lanes"])
        all_services_ready = catalog_svc._all_requested_services_ready(requested_services, data)
        d.execute(
            "UPDATE requests SET data=?, status=CASE WHEN status='Rejected' THEN status "
            "WHEN ? THEN 'Completed' WHEN status!='Completed' THEN 'Processing' ELSE status END WHERE id=?",
            (json.dumps(data, ensure_ascii=False), int(all_services_ready), rid),
        )
        d.execute(
            "UPDATE request_directions_workflows SET status='confirmed', confirmed_by=?, confirmed_at=?, updated_at=? "
            "WHERE request_id=? AND job_id=? AND status='ready'",
            (session["uid"], now, now, rid, job_id),
        )
        if {"travel", "speed"}.intersection(requested_services):
            visible_services = [
                SERVICES[service] for service in ("travel", "speed")
                if service in requested_services
            ]
            notifications_svc._create_notification(
                row["user_id"], rid, "directions_result",
                "Request results are ready",
                f"The administrator approved {', '.join(visible_services)} for request REQ-{rid}. Traffic volumes are still being prepared.",
            )
        audit_svc.audit("directions_confirmed", rid, services=sorted({"travel", "speed"}.intersection(requested_services)), with_volumes=True)
        d.commit()
        if volume_workflow["status"] != "sent":
            if volume_workflow["freeflow_result_json"]:
                if volumes_svc._evaluate_volume_freeflow(rid):
                    volumes_svc._start_volume_calculation(rid)
            elif volume_workflow["freeflow_accepted"]:
                volumes_svc._start_volume_calculation(rid)
        return jsonify(
            ok=True, status="confirmed", confirmed_at=now,
            message=(
                "The approved directions results are available to the client; actual speeds were also saved internally for Traffic volumes."
                if {"travel", "speed"}.intersection(requested_services)
                else "Actual speed was saved internally for Traffic volumes; no unrequested service data was sent to the client."
            ),
        )
    updated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    reuse_note = (
        " No separate result was returned for "
        + ", ".join(f"{hour:02d}:00" for hour in result.get("reused_hours", []))
        + "; the available completed result was reused for those hours."
        if result.get("reused_hours") else ""
    )
    if "travel" in requested_services:
        data["travel"] = {
            "rows": [{"hour": item["hour"], "value": item["travel_minutes"]} for item in hourly],
            "note": "Automatically extracted from the A→C Scraper response at the saved A→B road distance." + reuse_note,
            "source": "outscraper",
            "updated": updated,
            "outscraper_job_id": job_id,
            "directions_result": result,
        }
    if "speed" in requested_services:
        data["speed"] = {
            "rows": [{"hour": item["hour"], "value": item["speed_kmh"]} for item in hourly],
            "note": "Automatically calculated as A→B distance / time and converted to km/h." + reuse_note,
            "source": "outscraper",
            "updated": updated,
            "outscraper_job_id": job_id,
            "directions_result": result,
        }
    all_services_ready = catalog_svc._all_requested_services_ready(requested_services, data)
    now = common_svc._timestamp_now()
    d = db()
    d.execute("BEGIN IMMEDIATE")
    current = d.execute(
        "SELECT status FROM request_directions_workflows WHERE request_id=? AND job_id=?",
        (rid, job_id),
    ).fetchone()
    if not current or current["status"] != "ready":
        d.rollback()
        return jsonify(error="The workflow changed before confirmation. Reload and review the current status."), 409
    d.execute(
        "UPDATE requests SET data=?, status=CASE WHEN status!='Rejected' AND ? THEN 'Completed' "
        "WHEN status NOT IN ('Rejected','Completed') THEN 'Processing' ELSE status END WHERE id=?",
        (json.dumps(data, ensure_ascii=False), int(all_services_ready), rid),
    )
    d.execute(
        "UPDATE request_directions_workflows SET status='confirmed', confirmed_by=?, confirmed_at=?, updated_at=? "
        "WHERE request_id=? AND job_id=? AND status='ready'",
        (session["uid"], now, now, rid, job_id),
    )
    notifications_svc._create_notification(
        row["user_id"], rid, "directions_result",
        "Request results are ready",
        f"The administrator approved the directions results for request REQ-{rid}.",
    )
    audit_svc.audit("directions_confirmed", rid, with_volumes=False)
    d.commit()
    return jsonify(ok=True, status="confirmed", confirmed_at=now, result_url=url_for("client.result", rid=rid))
