"""Reuse a Directions result that was already paid for, when the admin chooses to.

A new request may use the raw answer of an earlier completed Scraper job instead of sending a new one when
that answer already contains what the new request needs:

* Actual speed: the same route from A to point C, and enough timing data inside the new request's own time
  window for every selected hour. The new window may be a part of the old one, and point B may differ because
  the A-to-B time is read from the route's distance profile.
* Free-flow speed: the same route from A to point C and the same weekend reference day and hours.

Nothing is reused automatically: the functions only list candidates, and ``reuse_*`` runs when an admin asks.
The earlier job is copied (marked as completed and as reused) and the normal extraction runs on the copy with
the new request's own settings, so every later check works exactly as if the job had just finished.
"""
import copy
import datetime as dt
import json
import math
import secrets
from zoneinfo import ZoneInfo

from flask import abort
from werkzeug.exceptions import HTTPException

import outscraper_integration as outscraper
from trafficlens.db import db
from trafficlens.services import audit as audit_svc
from trafficlens.services import catalog as catalog_svc
from trafficlens.services import common as common_svc
from trafficlens.services import directions as directions_svc
from trafficlens.services import volumes as volumes_svc

COORD_TOLERANCE = 3e-5          # degrees, about 3 metres: the same point picked twice on the map
DEAD_STATUSES = ("failed", "rejected", "invalidated")


class ReuseError(ValueError):
    """The result cannot be reused; the message is safe to show to the admin."""


def _point(text):
    lat, lng = (float(part) for part in str(text).split(","))
    return lat, lng


def _same_point(first, second):
    return abs(first[0] - second[0]) <= COORD_TOLERANCE and abs(first[1] - second[1]) <= COORD_TOLERANCE


def _same_route(route, origin, destination):
    try:
        return _same_point(_point(route["origin"]), origin) and _same_point(_point(route["destination"]), destination)
    except (KeyError, TypeError, ValueError):
        return False


def _request_source(request_id):
    row = db().execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if not row:
        raise ReuseError("This request does not exist.")
    return row, catalog_svc.to_dict(row)


def _format(point):
    return ", ".join(f"{float(value):.6f}" for value in point)


def new_window(source, timezone_name):
    """Departure and finish (UTC seconds) of the request's own period, as the Volumes page proposes them."""
    zone = ZoneInfo(timezone_name)
    day = dt.date.fromisoformat(source["d1"])
    start = dt.datetime.combine(day, dt.time.fromisoformat(source["start_time"]), tzinfo=zone)
    end = dt.datetime.combine(day, dt.time.fromisoformat(source["end_time"]), tzinfo=zone)
    if end <= start:
        end += dt.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def usable_hours(raw_response, departure, finish, road_meters, timezone_name):
    """The hours the extraction could fill from this raw answer inside the window (the same rules it uses)."""
    zone, hours = ZoneInfo(timezone_name), set()
    for record in directions_svc._flatten_provider_records((raw_response or {}).get("data")):
        try:
            timestamp = int(record["timestamp"])
            if timestamp < departure or timestamp > finish:
                continue
            seconds = directions_svc._interpolate_route_seconds(record.get("road_distance_timing(meters:seconds)"), road_meters)
            if seconds > 0:
                hours.add(dt.datetime.fromtimestamp(timestamp, zone).hour)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return hours


def _requested_hours(source):
    return sorted({int(hour) for hour in source["hours"] if str(hour).isdigit() and 0 <= int(hour) <= 23})


def _first_route(job_id):
    return db().execute("SELECT * FROM outscraper_job_routes WHERE job_id=? ORDER BY route_index LIMIT 1", (job_id,)).fetchone()


def _label(timestamp, zone):
    return dt.datetime.fromtimestamp(timestamp, zone).strftime("%d %b %H:%M")


def _titled(request_id):
    row = db().execute("SELECT title FROM requests WHERE id=?", (request_id,)).fetchone()
    return (row["title"] if row and row["title"] else "") or ""


# ------------------------------------------------------------------ actual speed
def directions_candidates(request_id):
    """Earlier completed Directions jobs this request could reuse, best first."""
    row, source = _request_source(request_id)
    try:
        location = directions_svc._request_directions_location(row)
    except (ValueError, HTTPException):
        return []
    if len(source["coords"]) != 2 or not source.get("point_c") or not source.get("road_km"):
        return []
    origin, destination = tuple(source["coords"][0]), tuple(source["point_c"])
    try:
        departure, finish = new_window(source, location["timezone"])
    except (TypeError, ValueError, KeyError):
        return []
    hours, road_meters = _requested_hours(source), float(source["road_km"]) * 1000
    if not hours:
        return []
    zone, found = ZoneInfo(location["timezone"]), []
    rows = db().execute(
        "SELECT w.job_id, w.request_id, j.config_json, j.completed_at FROM request_directions_workflows w "
        "JOIN outscraper_jobs j ON j.id=w.job_id WHERE w.request_id!=? AND w.status IN ('ready','confirmed') "
        "AND j.execution_status='completed' ORDER BY j.completed_at DESC LIMIT 400", (request_id,)).fetchall()
    for candidate in rows:
        config = json.loads(candidate["config_json"])
        if not config.get("trafficlens_request") or not config.get("routes") or not _same_route(config["routes"][0], origin, destination):
            continue
        old_departure, old_finish = config.get("departure_time"), config.get("finish_time")
        if not isinstance(old_departure, int) or not isinstance(old_finish, int) or departure < old_departure or finish > old_finish:
            continue
        route = _first_route(candidate["job_id"])
        if not route or route["execution_status"] != "completed" or not route["raw_response_json"]:
            continue
        if not set(hours) <= usable_hours(json.loads(route["raw_response_json"]), departure, finish, road_meters, location["timezone"]):
            continue
        found.append({
            "source_job_id": candidate["job_id"], "source_request_id": candidate["request_id"],
            "source_title": _titled(candidate["request_id"]), "completed_at": candidate["completed_at"],
            "exact_window": (old_departure, old_finish) == (departure, finish),
            "source_window": f"{_label(old_departure, zone)} – {_label(old_finish, zone)}",
            "new_window": f"{_label(departure, zone)} – {_label(finish, zone)}",
            "hours": hours,
        })
    found.sort(key=lambda item: (not item["exact_window"], item["completed_at"] or ""), reverse=False)
    return found


def _active_directions_workflow(request_id):
    return db().execute(
        "SELECT status FROM request_directions_workflows WHERE request_id=? AND status NOT IN ('failed','rejected','invalidated') "
        "ORDER BY created_at DESC LIMIT 1", (request_id,)).fetchone()


def _clone_job(source_job_id, config, request_id, kind, user_id):
    """A completed copy of a finished job (its routes and raw answers included), recorded as reused."""
    now, job_id = common_svc._timestamp_now(), secrets.token_hex(16)
    d = db()
    old = d.execute("SELECT * FROM outscraper_jobs WHERE id=?", (source_job_id,)).fetchone()
    d.execute(
        "INSERT INTO outscraper_jobs(id,created_at,updated_at,config_json,config_hash,approval_token_hash,approval_status,"
        "execution_status,approved_at,started_at,completed_at,estimate_confidence,provider_ids,result_count) "
        "VALUES(?,?,?,?,?,?,'approved','completed',?,?,?,'unknown',?,?)",
        (job_id, now, now, json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
         outscraper.configuration_fingerprint(config), common_svc._token_hash(secrets.token_urlsafe(32)), now, now, now,
         old["provider_ids"], old["result_count"]))
    for route in d.execute("SELECT * FROM outscraper_job_routes WHERE job_id=? ORDER BY route_index", (source_job_id,)):
        d.execute(
            "INSERT INTO outscraper_job_routes(job_id,route_index,origin,destination,execution_status,provider_request_id,"
            "provider_status,submitted_at,completed_at,last_polled_at,request_details_json,raw_response_json,result_count) "
            "VALUES(?,?,?,?,'completed',?,?,?,?,?,?,?,?)",
            (job_id, route["route_index"], route["origin"], route["destination"], route["provider_request_id"],
             route["provider_status"], route["submitted_at"], route["completed_at"], route["last_polled_at"],
             route["request_details_json"], route["raw_response_json"], route["result_count"]))
    source_request = d.execute("SELECT request_id FROM request_directions_workflows WHERE job_id=?", (source_job_id,)).fetchone()
    d.execute("INSERT INTO outscraper_job_reuse(job_id,source_job_id,source_request_id,request_id,kind,reused_by,reused_at) VALUES(?,?,?,?,?,?,?)",
              (job_id, source_job_id, source_request["request_id"] if source_request else None, request_id, kind, user_id, now))
    return job_id


def reuse_directions(request_id, source_job_id, user_id):
    row, source = _request_source(request_id)
    if _active_directions_workflow(request_id):
        raise ReuseError("This request already has a Directions request. Open it instead of reusing another.")
    chosen = next((item for item in directions_candidates(request_id) if item["source_job_id"] == source_job_id), None)
    if not chosen:
        raise ReuseError("That earlier result can no longer be reused for this request.")
    location = directions_svc._request_directions_location(row)
    departure, finish = new_window(source, location["timezone"])
    old_config = json.loads(db().execute("SELECT config_json FROM outscraper_jobs WHERE id=?", (source_job_id,)).fetchone()["config_json"])
    config = copy.deepcopy(old_config)
    a, b, c = _format(source["coords"][0]), _format(source["coords"][1]), _format(source["point_c"])
    interval = min(math.ceil((finish - departure) / 60), 60)
    config.update(departure_time=departure, finish_time=finish, interval=interval)
    config["routes"] = [{"origin": a, "destination": c}]
    config["trafficlens_request"] = dict(
        old_config["trafficlens_request"], source_request_id=request_id, point_a=a, point_b=b, point_c=c,
        road_ab_meters=float(source["road_km"]) * 1000, interval_minutes=interval,
        country_code=location["country_code"], country_name=location["country_name"], timezone=location["timezone"])
    d, now = db(), common_svc._timestamp_now()
    try:
        d.execute("BEGIN IMMEDIATE")
        job_id = _clone_job(source_job_id, config, request_id, "directions", user_id)
        d.execute("INSERT INTO request_directions_workflows(job_id,request_id,status,interval_seconds,interval_minutes,created_at,updated_at) "
                  "VALUES(?,?,'awaiting_response',?,?,?,?)", (job_id, request_id, interval * 60, interval, now, now))
        directions_svc._extract_request_directions_result(job_id)
        audit_svc.audit("api_result_reused", request_id, service="actual speed", source_request=chosen["source_request_id"],
                        source_job=source_job_id, job=job_id)
        d.commit()
    except Exception as exc:
        d.rollback()
        if isinstance(exc, ReuseError):
            raise
        raise ReuseError(f"The earlier result could not be used: {exc}") from None
    return {"job_id": job_id, "source_request_id": chosen["source_request_id"]}


# ------------------------------------------------------------------ free-flow speed
def _freeflow_prefill(row):
    try:
        return directions_svc._outscraper_prefill_from_request(row, "freeflow_speed")["prefill_config"]
    except (HTTPException, ValueError, KeyError):
        return None


def freeflow_candidates(request_id):
    row, source = _request_source(request_id)
    workflow = db().execute("SELECT freeflow_accepted FROM volumes_workflows WHERE request_id=?", (request_id,)).fetchone()
    if not workflow or workflow["freeflow_accepted"]:
        return []
    prefill = _freeflow_prefill(row)
    if not prefill or not source.get("point_c"):
        return []
    origin, destination = tuple(source["coords"][0]), tuple(source["point_c"])
    context = prefill.get("trafficlens_context") or {}
    found = []
    rows = db().execute(
        "SELECT a.job_id, a.request_id, j.config_json, j.completed_at FROM volume_freeflow_attempts a "
        "JOIN outscraper_jobs j ON j.id=a.job_id WHERE a.request_id!=? AND a.accepted=1 AND j.execution_status='completed' "
        "ORDER BY j.completed_at DESC LIMIT 400", (request_id,)).fetchall()
    for candidate in rows:
        config = json.loads(candidate["config_json"])
        old = config.get("trafficlens_context") or {}
        if not config.get("routes") or not _same_route(config["routes"][0], origin, destination):
            continue
        if old.get("weekend_date") != context.get("weekend_date") or old.get("departure_time") != context.get("departure_time") \
                or old.get("finish_time") != context.get("finish_time"):
            continue
        route = _first_route(candidate["job_id"])
        if not route or route["execution_status"] != "completed" or not route["raw_response_json"]:
            continue
        found.append({"source_job_id": candidate["job_id"], "source_request_id": candidate["request_id"],
                      "source_title": _titled(candidate["request_id"]), "completed_at": candidate["completed_at"],
                      "weekend_date": old.get("weekend_date")})
    return found


def reuse_freeflow(request_id, source_job_id, user_id):
    row, source = _request_source(request_id)
    chosen = next((item for item in freeflow_candidates(request_id) if item["source_job_id"] == source_job_id), None)
    if not chosen:
        raise ReuseError("That earlier free-flow result can no longer be reused for this request.")
    workflow = db().execute("SELECT freeflow_job_id FROM volumes_workflows WHERE request_id=?", (request_id,)).fetchone()
    if workflow["freeflow_job_id"]:
        active = db().execute("SELECT execution_status FROM outscraper_jobs WHERE id=?", (workflow["freeflow_job_id"],)).fetchone()
        if active and active["execution_status"] in ("submitting", "running", "unknown"):
            raise ReuseError("A free-flow request is already in progress for this link.")
    config = _freeflow_prefill(row)
    d, now = db(), common_svc._timestamp_now()
    try:
        d.execute("BEGIN IMMEDIATE")
        job_id = _clone_job(source_job_id, config, request_id, "freeflow", user_id)
        d.execute("INSERT INTO volume_freeflow_attempts(job_id,request_id,submitted_at,status) VALUES(?,?,?,'running')", (job_id, request_id, now))
        d.execute("UPDATE volumes_workflows SET freeflow_job_id=?, freeflow_speed=NULL, freeflow_result_json=NULL, "
                  "freeflow_validation_json=NULL, freeflow_accepted=0, freeflow_override=0, freeflow_accepted_at=NULL, "
                  "status='waiting_for_inputs', error=NULL, updated_at=? WHERE request_id=? AND freeflow_accepted=0", (job_id, now, request_id))
        d.commit()
        directions_svc._persist_holiday_result_context(job_id)
        volumes_svc._evaluate_volume_freeflow(request_id)
        audit_svc.audit("api_result_reused", request_id, service="free-flow speed", source_request=chosen["source_request_id"],
                        source_job=source_job_id, job=job_id)
        d.commit()
    except Exception as exc:
        d.rollback()
        raise ReuseError(f"The earlier result could not be used: {exc}") from None
    return {"job_id": job_id, "source_request_id": chosen["source_request_id"]}


def reuse_marks(request_ids):
    """Which of these requests use a reused result, for the badge in the tables: {request_id: {kind: source}}."""
    if not request_ids:
        return {}
    marks = {}
    for row in db().execute(
            "SELECT request_id, kind, source_request_id FROM outscraper_job_reuse WHERE request_id IN (%s)" % ",".join("?" * len(request_ids)),
            list(request_ids)):
        marks.setdefault(row["request_id"], {})[row["kind"]] = row["source_request_id"]
    return marks
