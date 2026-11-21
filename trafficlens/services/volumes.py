"""Traffic volume calculation (MATLAB runs) and free-flow evaluation."""
import datetime as dt
import json
import math
import threading
import volumes_runner
from trafficlens.config import MAX_VOLUME_PER_LANE
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.services import common as common_svc
from trafficlens.services import notifications as notifications_svc


def _save_volume_lanes_service(request_data, requested_services, lanes):
    if "lanes" in requested_services and lanes is not None:
        request_data["lanes"] = {
            "rows": [{"value": lanes}],
            "note": "Number of lanes used for the Traffic volumes calculation.",
            "source": "admin",
            "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }


def _sync_volume_actual_speeds(request_id):
    workflow = db().execute(
        "SELECT actual_result_json FROM volumes_workflows WHERE request_id=?",
        (request_id,),
    ).fetchone()
    request_row = db().execute(
        "SELECT services,hours,data FROM requests WHERE id=?", (request_id,)
    ).fetchone()
    if not workflow or not request_row:
        return False

    actual_result = json.loads(workflow["actual_result_json"] or "{}")
    actual_hourly = {
        int(item["hour"]): item
        for item in actual_result.get("hourly", [])
        if isinstance(item, dict) and str(item.get("hour", "")).isdigit()
    }
    directions_row = db().execute(
        "SELECT result_json FROM request_directions_workflows "
        "WHERE request_id=? AND status IN ('ready','confirmed') AND result_json IS NOT NULL "
        "ORDER BY updated_at DESC,created_at DESC LIMIT 1",
        (request_id,),
    ).fetchone()
    directions_result = json.loads(directions_row["result_json"] or "{}") if directions_row else {}
    changed = False
    for item in directions_result.get("hourly", []):
        if not isinstance(item, dict) or not str(item.get("hour", "")).isdigit():
            continue
        hour = int(item["hour"])
        target = actual_hourly.setdefault(hour, {"hour": hour})
        for key in ("speed_kmh", "distance_m", "time_seconds", "travel_minutes", "reused_observation"):
            if item.get(key) is not None and target.get(key) != item[key]:
                target[key] = item[key]
                changed = True

    services = set(json.loads(request_row["services"] or "[]"))
    request_data = json.loads(request_row["data"] or "{}")
    if "speed" in services:
        for item in (request_data.get("speed") or {}).get("rows", []):
            if not isinstance(item, dict) or item.get("hour") is None or item.get("value") is None:
                continue
            hour = int(item["hour"])
            target = actual_hourly.setdefault(hour, {"hour": hour})
            if target.get("speed_kmh") != item["value"]:
                target["speed_kmh"] = item["value"]
                changed = True

    requested_hours = sorted(set(json.loads(request_row["hours"] or "[]")))
    actual_result["hourly"] = [
        actual_hourly[hour] for hour in sorted(actual_hourly)
    ]
    complete = bool(requested_hours) and all(
        hour in actual_hourly and actual_hourly[hour].get("speed_kmh") is not None
        for hour in requested_hours
    )
    if (changed or not workflow["actual_result_json"]) and actual_result["hourly"]:
        db().execute(
            "UPDATE volumes_workflows SET actual_result_json=?,updated_at=? WHERE request_id=?",
            (json.dumps(actual_result, ensure_ascii=False), common_svc._timestamp_now(), request_id),
        )
    return complete


def _evaluate_volume_freeflow(request_id):
    _sync_volume_actual_speeds(request_id)
    workflow = db().execute(
        "SELECT * FROM volumes_workflows WHERE request_id=?", (request_id,)
    ).fetchone()
    request_row = db().execute(
        "SELECT services,hours,data,user_id FROM requests WHERE id=?", (request_id,)
    ).fetchone()
    if not workflow or not request_row or not workflow["freeflow_result_json"]:
        return False
    source_result = json.loads(workflow["freeflow_result_json"])
    candidate_speed = source_result.get("selected_speed_kmh")
    actual_result = json.loads(workflow["actual_result_json"] or "{}")
    request_data = json.loads(request_row["data"] or "{}")
    requested_services = set(json.loads(request_row["services"] or "[]"))
    actual_by_hour = {
        item.get("hour"): item for item in actual_result.get("hourly", [])
        if isinstance(item, dict)
    }
    requested_hours = sorted(set(json.loads(request_row["hours"] or "[]")))
    comparison = []
    missing = []
    for hour in requested_hours:
        actual_item = actual_by_hour.get(hour)
        actual_speed = actual_item.get("speed_kmh") if actual_item else None
        try:
            valid = (
                math.isfinite(float(candidate_speed))
                and math.isfinite(float(actual_speed))
                and float(candidate_speed) > float(actual_speed)
            )
        except (TypeError, ValueError):
            valid = False
        if actual_speed is None:
            missing.append(f"{hour:02d}:00")
        comparison.append({
            "hour": hour,
            "actual_speed_kmh": actual_speed,
            "freeflow_speed_kmh": candidate_speed,
            "valid": valid if actual_speed is not None else None,
        })
    validation = {
        "rows": comparison,
        "missing_hours": missing,
        "mismatched_hours": [
            f"{item['hour']:02d}:00" for item in comparison
            if item["valid"] is False
        ],
        "is_valid": bool(comparison) and not missing and all(
            item["valid"] is True for item in comparison
        ),
    }
    if validation["missing_hours"]:
        validation_error = "Waiting for actual speed for selected hour(s): " + ", ".join(validation["missing_hours"]) + "."
    elif validation["mismatched_hours"]:
        validation_error = "Needs re-send: actual speed exceeds free-flow at " + ", ".join(validation["mismatched_hours"]) + "."
    else:
        validation_error = None
    db().execute(
        "UPDATE volumes_workflows SET freeflow_speed=?, freeflow_validation_json=?, "
        "freeflow_accepted=?, freeflow_override=0, freeflow_accepted_at=?, error=?, updated_at=? "
        "WHERE request_id=?",
        (
            candidate_speed,
            json.dumps(validation, ensure_ascii=False),
            int(validation["is_valid"]),
            common_svc._timestamp_now() if validation["is_valid"] else None,
            validation_error,
            common_svc._timestamp_now(),
            request_id,
        ),
    )
    attempt_status = "accepted" if validation["is_valid"] else (
        "waiting_for_actual_speed" if validation["missing_hours"] else "needs_resend"
    )
    db().execute(
        "UPDATE volume_freeflow_attempts SET validation_json=?,status=?,accepted=?,"
        "override=0,accepted_at=?,error=? WHERE job_id=?",
        (
            json.dumps(validation, ensure_ascii=False),
            attempt_status,
            int(validation["is_valid"]),
            common_svc._timestamp_now() if validation["is_valid"] else None,
            validation_error,
            workflow["freeflow_job_id"],
        ),
    )
    volume_data = request_data.get("volumes") or {}
    volume_data["freeflow_context"] = source_result.get("context")
    request_data["volumes"] = volume_data
    if validation["is_valid"] and "freeflow_speed" in requested_services:
        request_data["freeflow_speed"] = {
            "rows": [{"value": candidate_speed}],
            "note": "Selected as the highest speed returned for the reviewed A→C weekly-weekend window.",
            "source": "outscraper",
            "updated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "outscraper_job_id": workflow["freeflow_job_id"],
            "reference_context": source_result.get("context"),
        }
        notifications_svc._create_notification(
            request_row["user_id"], request_id, "directions_result",
            "Free-flow speed results are ready",
            f"The administrator approved free-flow speed for request REQ-{request_id}. Traffic volumes remain under review.",
        )
    status = "ready_for_review" if workflow["status"] == "ready_for_review" else (
        "sent" if workflow["status"] == "sent" else "waiting_for_inputs"
    )
    db().execute(
        "UPDATE requests SET data=?, status=CASE WHEN status NOT IN ('Rejected','Completed') "
        "THEN 'Processing' ELSE status END WHERE id=?",
        (json.dumps(request_data, ensure_ascii=False), request_id),
    )
    db().execute(
        "UPDATE volumes_workflows SET status=?, error=?, updated_at=? WHERE request_id=?",
        (status, validation_error, common_svc._timestamp_now(), request_id),
    )
    db().commit()
    return validation["is_valid"]


def _volume_result_warnings(result):
    """Plain-language checks the admin sees before approving MATLAB output."""
    lanes = ((result.get("inputs") or {}).get("lanes")) or None
    warnings = []
    for item in result.get("hourly", []):
        label = f"{int(item['hour']):02d}:00"
        value, tolerance = item["value"], item.get("tolerance")
        if value < 0:
            warnings.append(f"{label}: the estimated volume is negative.")
        elif value == 0:
            warnings.append(f"{label}: the estimated volume is zero.")
        elif lanes and value / lanes > MAX_VOLUME_PER_LANE:
            warnings.append(
                f"{label}: {value / lanes:.0f} veh/h per lane is above {MAX_VOLUME_PER_LANE}, "
                "which is higher than a lane normally carries."
            )
        if tolerance is not None and value > 0 and tolerance > value:
            warnings.append(f"{label}: the RMSE tolerance is larger than the estimated volume.")
    return warnings


def _start_volume_calculation(request_id):
    _sync_volume_actual_speeds(request_id)
    workflow = db().execute(
        "SELECT * FROM volumes_workflows WHERE request_id=?", (request_id,)
    ).fetchone()
    if not workflow or workflow["status"] in {"running", "ready_for_review", "sent"}:
        return False
    missing_inputs = []
    if not workflow["actual_result_json"]:
        missing_inputs.append("completed actual Directions speeds")
    if workflow["lanes"] is None:
        missing_inputs.append("lane count")
    if workflow["freeflow_speed"] is None:
        missing_inputs.append("free-flow speed")
    elif not workflow["freeflow_accepted"]:
        missing_inputs.append("accepted free-flow speed")
    if missing_inputs:
        error = "Waiting for inputs: " + ", ".join(missing_inputs) + "."
        db().execute(
            "UPDATE volumes_workflows SET status='waiting_for_inputs', error=?, updated_at=? WHERE request_id=?",
            (error, common_svc._timestamp_now(), request_id),
        )
        db().commit()
        return False

    try:
        actual_result = json.loads(workflow["actual_result_json"] or "{}")
        actual_by_hour = {
            item.get("hour"): item for item in actual_result.get("hourly", [])
            if isinstance(item, dict)
        }
        request_row = db().execute(
            "SELECT hours,data,user_id FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if not request_row:
            raise ValueError("The source request no longer exists.")
        requested_hours = sorted(set(json.loads(request_row["hours"] or "[]")))
        rows = []
        missing_hours = []
        for hour in requested_hours:
            actual = actual_by_hour.get(hour)
            speed = actual.get("speed_kmh") if actual else None
            try:
                valid_speed = math.isfinite(float(speed)) and float(speed) > 0
            except (TypeError, ValueError):
                valid_speed = False
            if not valid_speed:
                missing_hours.append(f"{hour:02d}:00")
                continue
            rows.append({
                "hour": hour,
                "lanes": workflow["lanes"],
                "freeflow_speed": workflow["freeflow_speed"],
                "actual_speed": speed,
            })
        if missing_hours:
            error = "Waiting for actual speed for selected hour(s): " + ", ".join(missing_hours) + "."
            db().execute(
                "UPDATE volumes_workflows SET status='waiting_for_inputs', error=?, updated_at=? WHERE request_id=?",
                (error, common_svc._timestamp_now(), request_id),
            )
            db().commit()
            return False
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        error = f"Waiting for valid request inputs: {exc}"
        db().execute(
            "UPDATE volumes_workflows SET status='waiting_for_inputs', error=?, updated_at=? WHERE request_id=?",
            (error[:1000], common_svc._timestamp_now(), request_id),
        )
        db().commit()
        return False

    existing_result = volumes_runner.find_existing_volume_result(request_id, rows, request_row["user_id"])
    if existing_result:
        request_data = json.loads(request_row["data"] or "{}")
        existing_result["inputs"] = {
        "lanes": workflow["lanes"],
        "freeflow_speed": workflow["freeflow_speed"],
        "actual_speed_by_hour": [
            {"hour": hour, "speed_kmh": actual_by_hour[hour]["speed_kmh"]}
            for hour in requested_hours
        ],
        "freeflow_context": (request_data.get("volumes") or {}).get("freeflow_context"),
        }
        existing_result["warnings"] = _volume_result_warnings(existing_result)
        db().execute(
        "UPDATE volumes_workflows SET status='ready_for_review', result_json=?, "
        "workbook_path=?, error=NULL, updated_at=? "
        "WHERE request_id=? AND status IN ('waiting_for_inputs','failed')",
        (
            json.dumps(existing_result, ensure_ascii=False),
            existing_result["result_workbook"],
            common_svc._timestamp_now(),
            request_id,
        ),
        )
        db().commit()
        return True

    worker_key = f"volumes-{request_id}"
    with common_svc._outscraper_workers_lock:
        if worker_key in common_svc._outscraper_workers:
            return False
        common_svc._outscraper_workers.add(worker_key)

    try:
        prepared_run = volumes_runner.prepare_volume_run(request_id, rows, request_row["user_id"])
    except ValueError as exc:
        with common_svc._outscraper_workers_lock:
            common_svc._outscraper_workers.discard(worker_key)
        db().execute(
            "UPDATE volumes_workflows SET status='waiting_for_inputs', error=?, updated_at=? WHERE request_id=?",
            (str(exc)[:1000], common_svc._timestamp_now(), request_id),
        )
        db().commit()
        return False
    except Exception as exc:
        with common_svc._outscraper_workers_lock:
            common_svc._outscraper_workers.discard(worker_key)
        flask_app.logger.exception("Could not save MATLAB inputs for volume request %s", request_id)
        db().execute(
            "UPDATE volumes_workflows SET status='failed', error=?, updated_at=? WHERE request_id=?",
            (f"Could not complete and verify Excel input preparation; MATLAB was not started. {str(exc)}"[:1000], common_svc._timestamp_now(), request_id),
        )
        db().commit()
        return False

    db().execute(
        "UPDATE volumes_workflows SET status='running', error=NULL, result_json=NULL, workbook_path=?, updated_at=? WHERE request_id=?",
        (prepared_run["input_workbook"], common_svc._timestamp_now(), request_id),
    )
    db().commit()
    try:
        threading.Thread(
            target=_run_volume_calculation,
            args=(request_id, prepared_run),
            name=worker_key,
            daemon=True,
        ).start()
    except RuntimeError as exc:
        with common_svc._outscraper_workers_lock:
            common_svc._outscraper_workers.discard(worker_key)
        db().execute(
            "UPDATE volumes_workflows SET status='failed', error=?, updated_at=? WHERE request_id=?",
            ("Excel inputs were saved and verified, but the local MATLAB worker could not be started.", common_svc._timestamp_now(), request_id),
        )
        db().commit()
        flask_app.logger.error("Could not start MATLAB volume worker for request %s: %s", request_id, exc)
        return False
    return True


def _run_volume_calculation(request_id, prepared_run):
    worker_key = f"volumes-{request_id}"
    try:
        with flask_app.app_context():
            workflow = db().execute(
                "SELECT * FROM volumes_workflows WHERE request_id=?", (request_id,)
            ).fetchone()
            request_row = db().execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if not workflow or not request_row:
                raise ValueError("The volume workflow or source request no longer exists.")
            actual_result = json.loads(workflow["actual_result_json"] or "{}")
            actual_by_hour = {item["hour"]: item for item in actual_result.get("hourly", [])}
            result = volumes_runner.run_prepared_volume_estimation(prepared_run)
            request_data = json.loads(request_row["data"] or "{}")
            freeflow_context = (request_data.get("volumes") or {}).get("freeflow_context")
            result["inputs"] = {
                "lanes": workflow["lanes"],
                "freeflow_speed": workflow["freeflow_speed"],
                "actual_speed_by_hour": [
                    {"hour": hour, "speed_kmh": actual_by_hour[hour]["speed_kmh"]}
                    for hour in prepared_run["hours"]
                ],
                "freeflow_context": freeflow_context,
            }
            result["warnings"] = _volume_result_warnings(result)
            db().execute(
                "UPDATE volumes_workflows SET status='ready_for_review', result_json=?, workbook_path=?, "
                "error=NULL, updated_at=? WHERE request_id=? AND status='running'",
                (json.dumps(result, ensure_ascii=False), result["result_workbook"], common_svc._timestamp_now(), request_id),
            )
            db().commit()
    except Exception as exc:
        flask_app.logger.exception("MATLAB volume calculation failed for request %s", request_id)
        with flask_app.app_context():
            db().execute(
                "UPDATE volumes_workflows SET status='failed', error=?, updated_at=? "
                "WHERE request_id=? AND status='running'",
                (f"Excel inputs were saved and verified, but the MATLAB calculation did not complete. {str(exc)}"[:1000], common_svc._timestamp_now(), request_id),
            )
            db().commit()
    finally:
        with common_svc._outscraper_workers_lock:
            common_svc._outscraper_workers.discard(worker_key)


def _volume_input_rows(request_id):
    """What the Traffic volumes model used for one request: actual speed, free-flow speed and lanes per hour, next to the result.
    Admin reports only: the client report keeps these supporting inputs internal."""
    workflow = db().execute("SELECT * FROM volumes_workflows WHERE request_id=?", (request_id,)).fetchone()
    request_row = db().execute("SELECT data FROM requests WHERE id=?", (request_id,)).fetchone()
    if not workflow or not request_row:
        return []
    def load(text):
        try:
            return json.loads(text or "null") or {}
        except (TypeError, ValueError):
            return {}
    result, actual = load(workflow["result_json"]), load(workflow["actual_result_json"])
    volume_data = (load(request_row["data"]).get("volumes") or {})
    inputs = result.get("inputs") or {}
    speeds = {item["hour"]: item.get("speed_kmh") for item in (inputs.get("actual_speed_by_hour") or actual.get("hourly") or [])
              if isinstance(item, dict) and "hour" in item}
    sent = {item.get("hour"): item for item in (volume_data.get("rows") or []) if isinstance(item, dict)}
    drafted = {item.get("hour"): item for item in (result.get("hourly") or []) if isinstance(item, dict)}
    context = inputs.get("freeflow_context") or volume_data.get("freeflow_context") or {}
    reference = " ".join(str(part) for part in (context.get("weekend_day"), context.get("weekend_date")) if part)
    freeflow = inputs.get("freeflow_speed", workflow["freeflow_speed"])
    lanes = inputs.get("lanes", workflow["lanes"])
    state = {"sent": "Approved and sent", "ready_for_review": "Awaiting admin review"}.get(workflow["status"], str(workflow["status"]).replace("_", " ").capitalize())
    rows = []
    for hour in sorted(set(speeds) | set(sent) | set(drafted)):
        output = sent.get(hour) or drafted.get(hour) or {}
        value = output.get("value")
        rows.append({"hour": hour, "actual_speed": speeds.get(hour), "freeflow_speed": freeflow, "lanes": lanes,
                     "volume": round(value) if isinstance(value, (int, float)) and sent.get(hour) is None else value,
                     "tolerance": output.get("tolerance"), "reference": reference, "state": state})
    return rows
