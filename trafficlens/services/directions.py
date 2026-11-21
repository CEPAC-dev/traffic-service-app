"""Route directions: preparing, validating and reading results for a request."""
import datetime as dt
import json
import math
import outscraper_integration as outscraper
import sqlite3
from flask import abort
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from trafficlens.config import SERVICES
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.services import catalog as catalog_svc
from trafficlens.services import common as common_svc
from trafficlens.services import holiday_calendar as holiday_calendar_svc
from trafficlens.services import request_groups as request_groups_svc
from trafficlens.services import volumes as volumes_svc


def _persist_holiday_result_context(job_id):
    job_row = db().execute("SELECT config_json FROM outscraper_jobs WHERE id=?", (job_id,)).fetchone()
    if not job_row:
        return
    context = json.loads(job_row["config_json"]).get("trafficlens_context")
    if not context:
        return
    request_id, service = context["source_request_id"], context["service"]
    request_row = db().execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if not request_row:
        flask_app.logger.warning("Completed reference-date Directions job %s has no matching source request.", job_id)
        return
    requested_services = set(json.loads(request_row["services"] or "[]"))
    if (
        service == "freeflow_speed"
        and "volumes" in requested_services
        and context.get("volume_input")
    ):
        try:
            route_row = db().execute(
                "SELECT raw_response_json FROM outscraper_job_routes WHERE job_id=? ORDER BY route_index LIMIT 1",
                (job_id,),
            ).fetchone()
            response = json.loads(route_row["raw_response_json"] or "{}") if route_row else {}
            timezone = ZoneInfo(context["timezone"])
            departure = int(context["departure_time"])
            finish = int(context["finish_time"])
            distance_meters = float(context["road_ab_meters"])
            values_by_hour = {}
            for record in _flatten_provider_records(response.get("data")):
                try:
                    timestamp = int(record["timestamp"])
                    if not departure <= timestamp <= finish:
                        continue
                    seconds = _interpolate_route_seconds(
                        record.get("road_distance_timing(meters:seconds)"), distance_meters
                    )
                    if seconds <= 0:
                        continue
                    hour = dt.datetime.fromtimestamp(timestamp, timezone).hour
                    values_by_hour.setdefault(hour, []).append(distance_meters / seconds * 3.6)
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
            if not values_by_hour:
                raise ValueError("The completed A→C response contains no usable time-stamped free-flow observations.")
            hourly = [
                {
                    "hour": hour,
                    "speed_kmh": max(speeds),
                    "sample_count": len(speeds),
                }
                for hour, speeds in sorted(values_by_hour.items())
            ]
            selected_speed = max(item["speed_kmh"] for item in hourly)
            result = {
                "hourly": hourly,
                "selected_speed_kmh": selected_speed,
                "selection_rule": "highest_speed_in_reviewed_window",
                "context": context,
                "outscraper_job_id": job_id,
            }
        except (KeyError, TypeError, ValueError, OverflowError, sqlite3.Error, ZoneInfoNotFoundError) as exc:
            db().execute(
                "UPDATE volumes_workflows SET freeflow_result_json=NULL, freeflow_accepted=0, "
                "status='waiting_for_inputs', error=?, updated_at=? WHERE request_id=?",
                (str(exc)[:1000], common_svc._timestamp_now(), request_id),
            )
            db().execute(
                "UPDATE volume_freeflow_attempts SET status='failed',error=? WHERE job_id=?",
                (str(exc)[:1000], job_id),
            )
            db().commit()
            flask_app.logger.error("Could not extract A-to-C weekly-weekend free-flow speed for volume request %s: %s", request_id, exc)
            return
        request_data = json.loads(request_row["data"] or "{}")
        volume_data = request_data.get("volumes") or {}
        volume_data["freeflow_context"] = {
            key: context[key]
            for key in (
                "study_date", "country_code", "country_name", "timezone",
                "reference_type", "weekend_date", "weekend_day", "departure_time",
                "finish_time", "interval_minutes",
            )
            if key in context
        }
        request_data["volumes"] = volume_data
        db().execute(
            "UPDATE volumes_workflows SET freeflow_speed=?, freeflow_job_id=?, "
            "freeflow_result_json=?, freeflow_accepted=0, freeflow_validation_json=NULL, "
            "freeflow_override=0, freeflow_accepted_at=NULL, status='waiting_for_inputs', "
            "error=NULL, updated_at=? WHERE request_id=?",
            (
                selected_speed,
                job_id,
                json.dumps(result, ensure_ascii=False),
                common_svc._timestamp_now(),
                request_id,
            ),
        )
        db().execute(
            "UPDATE volume_freeflow_attempts SET status='waiting_for_actual_speed',result_json=?,error=NULL "
            "WHERE job_id=?",
            (json.dumps(result, ensure_ascii=False), job_id),
        )
        db().execute(
            "UPDATE requests SET data=? WHERE id=?",
            (json.dumps(request_data, ensure_ascii=False), request_id),
        )
        db().commit()
        if volumes_svc._evaluate_volume_freeflow(request_id):
            volumes_svc._start_volume_calculation(request_id)
        return
    if service == "freeflow_speed" and "volumes" in requested_services and service not in requested_services:
        try:
            route_row = db().execute(
                "SELECT raw_response_json FROM outscraper_job_routes WHERE job_id=? ORDER BY route_index LIMIT 1",
                (job_id,),
            ).fetchone()
            response = json.loads(route_row["raw_response_json"] or "{}") if route_row else {}
            target_meters = float(request_row["road_km"]) * 1000
            seconds = []
            for record in _flatten_provider_records(response.get("data")):
                seconds.append(_interpolate_route_seconds(
                    record.get("road_distance_timing(meters:seconds)"), target_meters
                ))
            if not seconds or any(value <= 0 for value in seconds):
                raise ValueError("The completed Directions response has no usable A-to-B timing profile.")
            freeflow_speed = target_meters / (sum(seconds) / len(seconds)) * 3.6
        except (KeyError, TypeError, ValueError, OverflowError, sqlite3.Error) as exc:
            db().execute(
                "UPDATE volumes_workflows SET status='failed', error=?, updated_at=? WHERE request_id=?",
                (str(exc)[:1000], common_svc._timestamp_now(), request_id),
            )
            db().commit()
            flask_app.logger.error("Could not extract weekly-weekend free-flow speed for volume request %s: %s", request_id, exc)
            return
        db().execute(
            "UPDATE volumes_workflows SET freeflow_speed=?, freeflow_job_id=?, freeflow_accepted=1, "
            "freeflow_accepted_at=?, status='waiting_for_inputs', "
            "error=NULL, updated_at=? WHERE request_id=?",
            (freeflow_speed, job_id, common_svc._timestamp_now(), common_svc._timestamp_now(), request_id),
        )
        context = json.loads(job_row["config_json"]).get("trafficlens_context") or {}
        request_data = json.loads(request_row["data"] or "{}")
        volumes_data = request_data.get("volumes") or {}
        volumes_data["freeflow_context"] = {
            key: context[key]
            for key in (
                "study_date", "country_code", "country_name", "timezone",
                "reference_type", "weekend_date", "weekend_day", "departure_time",
            )
            if key in context
        }
        request_data["volumes"] = volumes_data
        db().execute(
            "UPDATE requests SET data=? WHERE id=?",
            (json.dumps(request_data, ensure_ascii=False), request_id),
        )
        db().commit()
        volumes_svc._start_volume_calculation(request_id)
        return
    if service not in requested_services:
        flask_app.logger.warning("Completed reference-date Directions job %s has no matching source service.", job_id)
        return
    data = json.loads(request_row["data"] or "{}")
    saved = data.get(service) or {"rows": [], "note": "", "source": "outscraper"}
    if saved.get("source") == "network":
        saved["rows"] = []
        saved["note"] = ""
    if not saved.get("rows"):
        saved["note"] = "Directions API response is complete and awaiting administrator review of the service value."
        saved["source"] = "outscraper"
        if service == "freeflow_time":
            suggested = _suggest_freeflow_minutes(job_id, request_row["road_km"])
            if suggested is not None:
                saved["suggested_minutes"] = suggested
                saved["note"] += f" Suggested A-to-B free flow time: {suggested:.2f} min (average of the holiday Directions response). Confirm or correct it."
        elif service == "freeflow_speed":
            suggested = _suggest_freeflow_speed(job_id, request_row["road_km"])
            if suggested is not None:
                saved["suggested_speed"] = suggested
                saved["note"] += f" Suggested A-to-B free flow speed: {suggested:.2f} km/h (from the weekend Directions response). Confirm or correct it."
    reference_context = {
        key: context[key] for key in (
            "study_date", "country_code", "country_name", "timezone",
            "reference_type", "holiday_date", "holiday_name",
            "weekend_date", "weekend_day", "departure_time",
        ) if key in context
    }
    saved["holiday_context" if service == "freeflow_time" else "reference_context"] = reference_context
    saved["outscraper_job_id"] = job_id
    data[service] = saved
    db().execute(
        "UPDATE requests SET data=?, status=CASE WHEN status='Pending' THEN 'Processing' ELSE status END WHERE id=?",
        (json.dumps(data, ensure_ascii=False), request_id),
    )
    db().commit()


def _validate_trafficlens_request(configuration):
    context = configuration.get("trafficlens_request")
    if not context:
        return
    row = db().execute("SELECT * FROM requests WHERE id=?", (context["source_request_id"],)).fetchone()
    if not row:
        raise ValueError("The TrafficLens request associated with this Directions preview no longer exists.")
    source = catalog_svc.to_dict(row)
    if not {"travel", "speed", "volumes"}.intersection(source["services"]) or not source.get("point_c") or not source.get("road_km"):
        raise ValueError("This request needs Travel time, Speed, or Volumes, a calculated Point C, and a saved A-to-B road distance.")
    expected_a = ", ".join(f"{float(value):.6f}" for value in source["coords"][0])
    expected_b = ", ".join(f"{float(value):.6f}" for value in source["coords"][-1])
    expected_c = ", ".join(f"{float(value):.6f}" for value in source["point_c"])
    coordinates = source["path"] or source["coords"]
    midpoint = coordinates[len(coordinates) // 2]
    country_code, country_name, timezone_name = holiday_calendar_svc._route_holiday_location(midpoint)
    route = configuration["routes"][0]
    if (
        context["point_a"] != expected_a
        or context["point_b"] != expected_b
        or context["point_c"] != expected_c
        or route["origin"] != expected_a
        or route["destination"] != expected_c
        or abs(context["road_ab_meters"] - float(source["road_km"]) * 1000) > 1
        or configuration["interval"] != context["interval_minutes"]
        or context["country_code"] != country_code
        or context["country_name"] != country_name
        or context["timezone"] != timezone_name
        or configuration["region"] != country_code
        or configuration["departure_time"] is None
        or configuration["finish_time"] is None
    ):
        raise ValueError("The A-to-C route, A-to-B distance, route country, time zone, or interval no longer matches the request.")


def _flatten_provider_records(value):
    if isinstance(value, list):
        for item in value:
            yield from _flatten_provider_records(item)
    elif isinstance(value, dict):
        yield value


def _interpolate_route_seconds(profile, target_meters):
    if not isinstance(profile, dict):
        raise ValueError("The Scraper response is missing road_distance_timing(meters:seconds).")
    points = []
    for distance, seconds in profile.items():
        try:
            distance_meters, elapsed_seconds = float(distance), float(seconds)
        except (TypeError, ValueError):
            continue
        if math.isfinite(distance_meters) and math.isfinite(elapsed_seconds) and distance_meters >= 0 and elapsed_seconds >= 0:
            points.append((distance_meters, elapsed_seconds))
    points.sort()
    if not points or points[0][0] > 0 or points[-1][0] < target_meters:
        raise ValueError("The road timing profile does not reach Point B on the A-to-C response.")
    for index, (distance, seconds) in enumerate(points):
        if math.isclose(distance, target_meters, abs_tol=0.01):
            return seconds
        if distance > target_meters:
            before_distance, before_seconds = points[index - 1]
            ratio = (target_meters - before_distance) / (distance - before_distance)
            return before_seconds + ratio * (seconds - before_seconds)
    raise ValueError("The road timing profile does not contain the A-to-B distance.")


def _mean_route_minutes(response, target_meters):
    """Average A-to-B travel time, in minutes, across every record of a Directions response."""
    seconds = []
    for record in _flatten_provider_records((response or {}).get("data")):
        try:
            value = _interpolate_route_seconds(
                record.get("road_distance_timing(meters:seconds)"), target_meters
            )
        except ValueError:
            continue
        if value > 0:
            seconds.append(value)
    return sum(seconds) / len(seconds) / 60 if seconds else None


def _suggest_freeflow_minutes(job_id, road_km):
    try:
        target_meters = float(road_km) * 1000
        route_row = db().execute(
            "SELECT raw_response_json FROM outscraper_job_routes WHERE job_id=? ORDER BY route_index LIMIT 1",
            (job_id,),
        ).fetchone()
        response = json.loads(route_row["raw_response_json"] or "{}") if route_row else {}
        return _mean_route_minutes(response, target_meters) if target_meters > 0 else None
    except (TypeError, ValueError, KeyError, sqlite3.Error):
        return None


def _suggest_freeflow_speed(job_id, road_km):
    """A-to-B free flow speed in km/h from the completed weekend Directions response."""
    minutes = _suggest_freeflow_minutes(job_id, road_km)
    try:
        distance_km = float(road_km)
        return distance_km / (minutes / 60) if minutes and minutes > 0 and distance_km > 0 else None
    except (TypeError, ValueError):
        return None


def _extract_request_directions_result(job_id):
    workflow = db().execute(
        "SELECT request_id FROM request_directions_workflows WHERE job_id=?", (job_id,)
    ).fetchone()
    if not workflow:
        return
    route = db().execute(
        "SELECT raw_response_json, execution_status FROM outscraper_job_routes WHERE job_id=? ORDER BY route_index LIMIT 1",
        (job_id,),
    ).fetchone()
    if not route or route["execution_status"] != "completed" or not route["raw_response_json"]:
        raise ValueError("The Scraper response is not complete yet.")
    source_row = db().execute("SELECT * FROM requests WHERE id=?", (workflow["request_id"],)).fetchone()
    if not source_row:
        raise ValueError("The source request no longer exists.")
    source = catalog_svc.to_dict(source_row)
    config_row = db().execute("SELECT config_json FROM outscraper_jobs WHERE id=?", (job_id,)).fetchone()
    config = json.loads(config_row["config_json"])
    context = config["trafficlens_request"]
    start_timestamp, end_timestamp = config["departure_time"], config["finish_time"]
    response = json.loads(route["raw_response_json"])
    records = list(_flatten_provider_records(response.get("data")))
    values_by_hour = {}
    timezone = ZoneInfo(context["timezone"])
    for record in records:
        try:
            timestamp = int(record["timestamp"])
            if timestamp < start_timestamp or timestamp > end_timestamp:
                continue
            elapsed_seconds = _interpolate_route_seconds(
                record.get("road_distance_timing(meters:seconds)"), context["road_ab_meters"]
            )
            hour = dt.datetime.fromtimestamp(timestamp, timezone).hour
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        values_by_hour.setdefault(hour, []).append(elapsed_seconds)
    requested_hours = sorted({
        int(hour) for hour in source["hours"] if str(hour).isdigit() and 0 <= int(hour) <= 23
    })
    if not requested_hours:
        raise ValueError("The source request has no selected study hours.")
    if not values_by_hour:
        raise ValueError("The completed Scraper response contains no usable timing data for the selected time range.")
    distance_meters = float(context["road_ab_meters"])
    all_seconds = [seconds for hour_values in values_by_hour.values() for seconds in hour_values]
    fallback_seconds = sum(all_seconds) / len(all_seconds)
    reused_hours = [hour for hour in requested_hours if not values_by_hour.get(hour)]
    hourly = []
    for hour in requested_hours:
        hour_values = values_by_hour.get(hour)
        seconds = sum(hour_values) / len(hour_values) if hour_values else fallback_seconds
        if seconds <= 0:
            raise ValueError(f"The Scraper returned an invalid travel time for {hour:02d}:00.")
        hourly.append({
            "hour": hour,
            "distance_m": distance_meters,
            "time_seconds": seconds,
            "travel_minutes": seconds / 60,
            "speed_kmh": distance_meters / seconds * 3.6,
            "reused_observation": not bool(hour_values),
        })
    mean_seconds = fallback_seconds
    result = {
        "route": "A → B",
        "point_a": context["point_a"],
        "point_b": context["point_b"],
        "point_c": context["point_c"],
        "distance_meters": distance_meters,
        "time_seconds": mean_seconds,
        "speed_kmh": distance_meters / mean_seconds * 3.6,
        "hourly": hourly,
        "reused_hours": reused_hours,
        "source_request_id": workflow["request_id"],
        "outscraper_job_id": job_id,
    }
    db().execute(
        "UPDATE request_directions_workflows SET status='ready', result_json=?, error=NULL, updated_at=? "
        "WHERE job_id=? AND status!='confirmed'",
        (json.dumps(result, ensure_ascii=False), common_svc._timestamp_now(), job_id),
    )
    db().commit()
    requested_services = set(source["services"])
    if "volumes" in requested_services:
        db().execute(
            "UPDATE volumes_workflows SET actual_result_json=?, status='waiting_for_inputs', "
            "error=NULL, updated_at=? WHERE request_id=? AND status NOT IN ('ready_for_review','sent')",
            (json.dumps(result, ensure_ascii=False), common_svc._timestamp_now(), workflow["request_id"]),
        )
        db().commit()
        if db().execute(
            "SELECT freeflow_result_json FROM volumes_workflows WHERE request_id=?",
            (workflow["request_id"],),
        ).fetchone()["freeflow_result_json"]:
            volumes_svc._evaluate_volume_freeflow(workflow["request_id"])
        volumes_svc._start_volume_calculation(workflow["request_id"])


def _update_request_directions_workflow(job_id, status, error=None):
    db().execute(
        "UPDATE request_directions_workflows SET status=?, error=?, updated_at=? "
        "WHERE job_id=? AND status NOT IN ('ready','confirmed')",
        (status, error, common_svc._timestamp_now(), job_id),
    )
    db().commit()


def _request_directions_location(request_row):
    source = catalog_svc.to_dict(request_row)
    if not {"travel", "speed", "volumes"}.intersection(source["services"]):
        raise ValueError("This request does not include Travel time, Speed, or Traffic volumes.")
    if not source.get("point_c") or not source.get("road_km"):
        raise ValueError("This request has no calculated Point C or saved A-to-B road distance.")
    coordinates = source["path"] or source["coords"]
    midpoint = coordinates[len(coordinates) // 2]
    country_code, country_name, timezone_name = holiday_calendar_svc._route_holiday_location(midpoint)
    if country_code not in outscraper.REGIONS:
        raise ValueError(f"The detected country {country_name} ({country_code}) is not a supported Outscraper region.")
    return {
        "country_code": country_code,
        "country_name": country_name,
        "timezone": timezone_name,
    }


def _outscraper_prefill_from_request(request_row, service="travel"):
    owner = request_groups_svc._shared_flow_leader(request_row, service)
    if owner:
        abort(409, f"{SERVICES.get(service, service)} is shared by every period of this link. Run the Directions API on REQ-{owner}; its confirmed value is copied here automatically.")
    source = catalog_svc.to_dict(request_row)
    service_requested = service in source["services"]
    service_for_volumes = service in {"travel", "freeflow_speed"} and "volumes" in source["services"]
    volume_freeflow = service == "freeflow_speed" and "volumes" in source["services"]
    if service not in {"travel", "freeflow_time", "freeflow_speed"} or not (service_requested or service_for_volumes):
        abort(400, "This request did not include the selected Directions service.")
    if len(source["coords"]) != 2:
        abort(400, "This request does not contain valid start and end coordinates.")
    try:
        origin = ", ".join(f"{float(value):.6f}" for value in source["coords"][0])
        destination_point = source["point_c"] if volume_freeflow else source["coords"][1]
        if volume_freeflow and not destination_point:
            abort(400, "This request has no calculated Point C for a free-flow A→C request.")
        destination = ", ".join(f"{float(value):.6f}" for value in destination_point)
        study_day = dt.date.fromisoformat(source["d1"])
        route = {"origin": origin, "destination": destination}
        region = "KW"
        trafficlens_context = None
        if service == "travel":
            hours = sorted({
                int(hour) for hour in source["hours"]
                if str(hour).isdigit() and 0 <= int(hour) <= 23
            })
            if not hours:
                abort(400, "This request does not contain selected study hours.")
            timezone = ZoneInfo("Asia/Kuwait")
            departure_time = dt.time.fromisoformat(source.get("start_time") or f"{hours[0]:02d}:00:00")
            departure = dt.datetime.combine(study_day, departure_time, tzinfo=timezone)
            if source.get("end_time"):
                finish = dt.datetime.combine(study_day, dt.time.fromisoformat(source["end_time"]), tzinfo=timezone)
            else:
                finish = dt.datetime.combine(study_day, dt.time(), tzinfo=timezone) + dt.timedelta(hours=hours[-1] + 1)
        else:
            coordinates = source["path"] or source["coords"]
            midpoint = coordinates[len(coordinates) // 2]
            country_code, country_name, timezone_name = holiday_calendar_svc._route_holiday_location(midpoint)
            timezone = ZoneInfo(timezone_name)
            if service == "freeflow_speed":
                try:
                    if volume_freeflow:
                        reference_date, reference_label = holiday_calendar_svc._nearest_previous_weekly_weekend(
                            country_code, timezone_name, study_day
                        )
                    else:
                        reference_date, reference_label = holiday_calendar_svc._latest_previous_weekly_weekend(
                            country_code, timezone_name
                        )
                except ValueError as exc:
                    abort(400, str(exc))
                departure = dt.datetime.combine(reference_date, dt.time(7), tzinfo=timezone)
                finish = dt.datetime.combine(reference_date, dt.time(8), tzinfo=timezone)
                reference_context = {
                    "reference_type": "weekly_weekend",
                    "weekend_date": reference_date.isoformat(),
                    "weekend_day": reference_label,
                }
            else:
                try:
                    reference_date, reference_label = holiday_calendar_svc._nearest_public_holiday(country_code, study_day)
                except (NotImplementedError, ValueError) as exc:
                    abort(400, str(exc) or f"No supported public holiday calendar is available for route country {country_code}.")
                departure = dt.datetime.combine(reference_date, dt.time(7), tzinfo=timezone)
                reference_context = {
                    "reference_type": "public_holiday",
                    "holiday_date": reference_date.isoformat(),
                    "holiday_name": reference_label,
                }
            departure_timestamp = int(departure.timestamp())
            route["departure_time"] = departure_timestamp
            region = country_code if country_code in outscraper.REGIONS else ""
            trafficlens_context = {
                "source_request_id": source["id"], "service": service,
                "study_date": study_day.isoformat(), "country_code": country_code,
                "country_name": country_name, "timezone": timezone_name,
                "departure_time": departure_timestamp,
                "origin": origin, "destination": destination,
                **reference_context,
            }
            if volume_freeflow:
                trafficlens_context.update({
                    "volume_input": True,
                    "road_ab_meters": float(source["road_km"]) * 1000,
                    "finish_time": int(finish.timestamp()),
                    "interval_minutes": 60,
                })
    except (TypeError, ValueError, IndexError, ZoneInfoNotFoundError):
        abort(400, "This request's route, date, or selected hours cannot be used to prepare directions.")
    prefill_config = {
        "routes": [route],
        "departure_time": int(departure.timestamp()) if service == "travel" or volume_freeflow else None,
        "finish_time": int(finish.timestamp()) if service == "travel" or volume_freeflow else None,
        "interval": (source.get("data_interval") or 1) if service == "travel" else 60 if volume_freeflow else None,
        "travel_mode": "car",
        "language": "en",
        "region": region,
    }
    if trafficlens_context:
        prefill_config["trafficlens_context"] = trafficlens_context
    return {
        "source_request": {
            "id": source["id"],
            "date": source["d1"],
            "service": service,
            "start_time": source.get("start_time"),
            "end_time": source.get("end_time"),
            "point_c": source.get("point_c"),
            "hours": source["hours"],
            "hours_label": source["hours_label"],
            "area": source["area"],
        },
        "prefill_config": prefill_config,
    }


def _directions_service_data(requested_services, result, job_id):
    """Speed / Travel service payloads built from a reviewed A->C Directions result."""
    updated = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    hourly = result["hourly"]
    reuse_note = (
        " No separate result was returned for "
        + ", ".join(f"{hour:02d}:00" for hour in result.get("reused_hours", []))
        + "; the available completed result was reused for those hours."
        if result.get("reused_hours") else ""
    )
    payloads = {}
    if "travel" in requested_services:
        payloads["travel"] = {
            "rows": [{"hour": item["hour"], "value": item["travel_minutes"]} for item in hourly],
            "note": "Automatically extracted from the A→C Scraper response at the saved A→B road distance." + reuse_note,
            "source": "outscraper", "updated": updated,
            "outscraper_job_id": job_id, "directions_result": result,
        }
    if "speed" in requested_services:
        payloads["speed"] = {
            "rows": [{"hour": item["hour"], "value": item["speed_kmh"]} for item in hourly],
            "note": "Automatically calculated as A→B distance / time and converted to km/h." + reuse_note,
            "source": "outscraper", "updated": updated,
            "outscraper_job_id": job_id, "directions_result": result,
        }
    return payloads
