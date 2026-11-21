"""Admin work on one request: review, save values, status and reply."""
import datetime as dt
import json
from flask import Blueprint, abort, jsonify, render_template, request
from trafficlens.config import SHARED_FLOW_SERVICES, STATUSES
from trafficlens.db import db
from trafficlens.security.decorators import admin_required
from trafficlens.services import audit as audit_svc, catalog as catalog_svc, network_links as network_links_svc, notifications as notifications_svc, request_groups as request_groups_svc

bp = Blueprint("admin_requests", __name__)


@bp.route("/admin/requests/<int:rid>")
@admin_required
def admin_request(rid):
    r = db().execute("SELECT r.*, u.name AS client, u.email AS email FROM requests r JOIN users u ON u.id=r.user_id WHERE r.id=?", (rid,)).fetchone()
    if not r: abort(404)
    volumes_row = db().execute(
        "SELECT * FROM volumes_workflows WHERE request_id=?", (rid,)
    ).fetchone()
    volumes_workflow = dict(volumes_row) if volumes_row else None
    if (volumes_workflow and volumes_workflow.get("lanes") is None
            and volumes_workflow.get("suggested_lanes") is None and volumes_workflow["status"] != "sent"):
        volumes_workflow["suggested_lanes"] = network_links_svc._backfill_suggested_lanes(rid, r["path"]) or 0
    if volumes_workflow and volumes_workflow.get("result_json"):
        volumes_workflow["result"] = json.loads(volumes_workflow["result_json"])
    if volumes_workflow and volumes_workflow.get("freeflow_result_json"):
        volumes_workflow["freeflow_result"] = json.loads(volumes_workflow["freeflow_result_json"])
    if volumes_workflow and volumes_workflow.get("freeflow_validation_json"):
        volumes_workflow["freeflow_validation"] = json.loads(volumes_workflow["freeflow_validation_json"])
    if volumes_workflow and volumes_workflow.get("freeflow_job_id"):
        job_row = db().execute(
            "SELECT execution_status FROM outscraper_jobs WHERE id=?",
            (volumes_workflow["freeflow_job_id"],),
        ).fetchone()
        volumes_workflow["freeflow_job_status"] = job_row["execution_status"] if job_row else None
    if volumes_workflow:
        volumes_workflow["freeflow_attempts"] = []
        for attempt_row in db().execute(
            "SELECT job_id AS id,submitted_at,status,result_json,validation_json,error,accepted,override,accepted_at "
            "FROM volume_freeflow_attempts WHERE request_id=? ORDER BY submitted_at DESC LIMIT 100",
            (rid,),
        ).fetchall():
            attempt = dict(attempt_row)
            attempt["result"] = json.loads(attempt.pop("result_json") or "null")
            attempt["validation"] = json.loads(attempt.pop("validation_json") or "null")
            volumes_workflow["freeflow_attempts"].append(attempt)
    if volumes_workflow:
        request_data = json.loads(r["data"] or "{}")
        volumes_workflow["freeflow_context"] = (
            request_data.get("volumes") or {}
        ).get("freeflow_context")
    workflow_row = db().execute(
        "SELECT * FROM request_directions_workflows WHERE request_id=? ORDER BY created_at DESC LIMIT 1",
        (rid,),
    ).fetchone()
    directions_workflow = None
    if workflow_row:
        directions_workflow = dict(workflow_row)
        directions_workflow["result"] = json.loads(directions_workflow.pop("result_json") or "null")
    shared_leaders = {name: owner for name in SHARED_FLOW_SERVICES if (owner := request_groups_svc._shared_flow_leader(r, name))}
    return render_template("admin_request.html", r=catalog_svc.to_dict(r), forms=catalog_svc.forms_catalog(), statuses=STATUSES, shared_leaders=shared_leaders,
                           directions_workflow=directions_workflow, volumes_workflow=volumes_workflow,
                           audit_entries=audit_svc._audit_entries_for_request(rid))


@bp.post("/api/requests/<int:rid>/data/<service>")
@admin_required
def save_service(rid, service):
    r = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not r or service not in catalog_svc.forms_catalog() or service not in json.loads(r["services"]): return jsonify(error="Invalid"), 400
    if service == "volumes":
        return jsonify(error="Traffic volumes are managed by the MATLAB review workflow and cannot be edited manually."), 409
    j = request.get_json(force=True) or {}
    data = json.loads(r["data"] or "{}")
    saved = {"rows": catalog_svc.clean_rows(service, j.get("rows")), "note": str(j.get("note") or "")[:5000], "source": "admin",
             "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    for context_key in ("holiday_context", "reference_context"):
        if data.get(service, {}).get(context_key):
            saved[context_key] = data[service][context_key]
            saved["outscraper_job_id"] = data[service].get("outscraper_job_id")
    data[service] = saved
    db().execute("UPDATE requests SET data=? WHERE id=?", (json.dumps(data), rid))
    audit_svc.audit("service_saved", rid, service=service, rows=saved["rows"], note=saved["note"][:200])
    db().commit()
    request_groups_svc._propagate_shared_services(rid, [service])
    return jsonify(ok=True, rows=len(data[service]["rows"]))


@bp.post("/api/requests/<int:rid>/save-all")
@admin_required
def save_request_changes(rid):
    row = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not row: return jsonify(error="Request not found"), 404
    payload = request.get_json(force=True) or {}
    services = payload.get("services", {})
    if not isinstance(services, dict): return jsonify(error="Invalid service data"), 400
    requested_services = set(json.loads(row["services"] or "[]"))
    forms = catalog_svc.forms_catalog()
    clean_services = {}
    for service, values in services.items():
        if service not in requested_services or service not in forms:
            return jsonify(error=f"Service is not part of this request: {service}"), 400
        if service == "volumes":
            return jsonify(error="Traffic volumes are managed by the MATLAB review workflow and cannot be edited manually."), 409
        if not isinstance(values, dict) or not isinstance(values.get("rows", []), list):
            return jsonify(error=f"Invalid data for service: {service}"), 400
        note = values.get("note", "")
        if not isinstance(note, str): return jsonify(error=f"Invalid note for service: {service}"), 400
        clean_services[service] = {
            "rows": catalog_svc.clean_rows(service, values.get("rows", [])),
            "note": note[:5000],
            "source": "admin",
            "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }

    status = payload.get("status")
    if status is not None and status not in STATUSES:
        return jsonify(error="Invalid request status"), 400
    reply = payload.get("reply")
    if reply is not None and not isinstance(reply, str):
        return jsonify(error="Invalid reply"), 400
    reply = reply.strip() if reply is not None else None
    point_c_visible = payload.get("point_c_visible")
    if point_c_visible is not None and (
        not isinstance(point_c_visible, bool)
        or (point_c_visible and not row["point_c"])
    ):
        return jsonify(error="Point C visibility must be true or false, and this request must have a point C"), 400

    data = json.loads(row["data"] or "{}")
    for service, values in clean_services.items():
        for context_key in ("holiday_context", "reference_context"):
            existing_context = (data.get(service) or {}).get(context_key)
            if existing_context:
                values[context_key] = existing_context
                values["outscraper_job_id"] = data[service].get("outscraper_job_id")
    data.update(clean_services)
    if status == "Completed" and not catalog_svc._all_requested_services_ready(requested_services, data):
        return jsonify(error="A request cannot be marked Completed until every selected service has a result ready for the client."), 409
    updates, params = [], []
    if clean_services:
        updates.append("data=?")
        params.append(json.dumps(data))
    if status is not None:
        updates.append("status=?")
        params.append(status)
    if reply is not None:
        updates.extend(("reply=?", "reply_at=?"))
        params.extend((reply or None, dt.datetime.now().strftime("%Y-%m-%d %H:%M") if reply else None))
    if point_c_visible is not None:
        updates.append("show_point_c=?")
        params.append(int(point_c_visible))
    if updates:
        params.append(rid)
        db().execute(f"UPDATE requests SET {', '.join(updates)} WHERE id=?", params)
        if reply is not None:
            notifications_svc._notify_request_owner_reply(rid, row["user_id"], row["reply"] or "", reply)
        audit_svc.audit(
            "request_saved", rid, services={name: values["rows"] for name, values in clean_services.items()},
            status=status, old_status=row["status"], reply_changed=reply is not None and reply != (row["reply"] or ""),
        )
        db().commit()
    if clean_services:
        request_groups_svc._propagate_shared_services(rid, list(clean_services))
    return jsonify(ok=True, saved_services=list(clean_services), status=status or row["status"])


@bp.post("/api/requests/<int:rid>/status")
@admin_required
def set_status(rid):
    s = (request.get_json(force=True) or {}).get("status")
    row = db().execute(    "SELECT services,data,user_id FROM requests WHERE id=?", (rid,)).fetchone()
    if not row or s not in STATUSES: return jsonify(error="Invalid"), 400
    if s == "Completed" and not catalog_svc._all_requested_services_ready(
        json.loads(row["services"] or "[]"), json.loads(row["data"] or "{}")
    ):
        return jsonify(error="A request cannot be marked Completed until every selected service has a result ready for the client."), 409
    previous = db().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()["status"]
    db().execute("UPDATE requests SET status=? WHERE id=?", (s, rid))
    audit_svc.audit("status_changed", rid, old_status=previous, new_status=s)
    db().commit()
    return jsonify(ok=True, status=s)


@bp.post("/api/requests/<int:rid>/reply")
@admin_required
def set_reply(rid):
    text = ((request.get_json(force=True) or {}).get("reply") or "").strip()
    row = db().execute("SELECT user_id,reply FROM requests WHERE id=?", (rid,)).fetchone()
    if not row: return jsonify(error="Not found"), 404
    db().execute("UPDATE requests SET reply=?, reply_at=? WHERE id=?", (text or None, dt.datetime.now().strftime("%Y-%m-%d %H:%M") if text else None, rid))
    notifications_svc._notify_request_owner_reply(rid, row["user_id"], row["reply"] or "", text)
    db().commit()
    return jsonify(ok=True)


@bp.post("/api/requests/<int:rid>/network")
@admin_required
def rerun_network(rid):
    if not db().execute("SELECT 1 FROM requests WHERE id=?", (rid,)).fetchone(): return jsonify(error="Not found"), 404
    try: info = network_links_svc.apply_network(rid)
    except Exception as e: return jsonify(error=f"Network lookup failed: {e}"), 400
    if info is None: return jsonify(error="No road network is configured yet (Admin > Network)"), 400
    return jsonify(ok=True, **info)
