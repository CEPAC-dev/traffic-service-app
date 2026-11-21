"""The client's own pages: dashboard, new study, requests and results."""
import datetime as dt
import io
import json
import math
import network
import os
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from openpyxl import Workbook
from trafficlens.config import DATA_INTERVALS, DEFAULT_DATA_INTERVAL, MAX_GROUP_LINKS, SERVICES, STATUSES
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.security.decorators import login_required
from trafficlens.services import catalog as catalog_svc, common as common_svc, exports as exports_svc, network_links as network_links_svc, notifications as notifications_svc, request_groups as request_groups_svc, volumes as volumes_svc

bp = Blueprint("client", __name__)


@bp.get("/dashboard")
@login_required
def dashboard():
    if session.get("role") == "admin":
        return redirect(url_for("admin.admin"))
    rows = db().execute(
        "SELECT * FROM requests WHERE user_id=? ORDER BY id DESC LIMIT 5",
        (session["uid"],),
    ).fetchall()
    request_counts = {
        row["status"]: row["count"]
        for row in db().execute(
            "SELECT status,COUNT(*) AS count FROM requests WHERE user_id=? GROUP BY status",
            (session["uid"],),
        ).fetchall()
    }
    all_rows = db().execute(
        "SELECT * FROM requests WHERE user_id=?",
        (session["uid"],),
    ).fetchall()
    results_available = sum(
        1 for row in all_rows
        if row["status"] == "Completed"
        or (row["status"] == "Processing" and catalog_svc.to_dict(row)["progress"][0] > 0)
    )
    return render_template(
        "dashboard.html",
        reqs=[catalog_svc.to_dict(row) for row in rows],
        total_requests=db().execute(
            "SELECT COUNT(*) FROM requests WHERE user_id=?", (session["uid"],)
        ).fetchone()[0],
        request_counts=request_counts,
        results_available=results_available,
        active_services=catalog_svc.active_services(),
        selected_service=request.args.get("service", ""),
    )


@bp.route("/study")
@login_required
def study():
    repeat_request = None
    repeat_group = None
    group_arg = request.args.get("repeat_group_id", "").strip()
    if group_arg:
        try:
            repeat_gid = int(group_arg)
        except ValueError:
            abort(400, "Invalid order ID")
        repeat_group = request_groups_svc._repeat_group_payload(repeat_gid)
        if repeat_group is None:
            flash("An order can be repeated once its report has been sent.", "error")
            return redirect(url_for("groups.client_group", gid=repeat_gid))
    request_id = request.args.get("repeat_request_id", "").strip()
    if request_id and not repeat_group:
        try:
            rid = int(request_id)
        except ValueError:
            abort(400, "Invalid request ID")
        row = db().execute(
            "SELECT * FROM requests WHERE id=? AND user_id=?",
            (rid, session["uid"]),
        ).fetchone()
        if not row:
            abort(404)
        original = catalog_svc.to_dict(row)
        legacy_end_time = (
            f"{original['hours'][-1] + 1:02d}:00:00"
            if original["hours"] and original["hours"][-1] < 23
            else "23:59:59" if original["hours"] else ""
        )
        repeat_request = {
            "id": original["id"],
            "date": original["d1"],
            "coords": original["coords"],
            "services": original["services"],
            "start_time": original.get("start_time") or (f"{original['hours'][0]:02d}:00:00" if original["hours"] else ""),
            "end_time": original.get("end_time") or legacy_end_time,
            "period_label": original.get("period_label"),
            "data_interval": original.get("data_interval") or DEFAULT_DATA_INTERVAL,
        }
    selected_service = request.args.get("service", "")
    if selected_service not in catalog_svc.active_services():
        selected_service = ""
    period_labels = [row["label"] for row in db().execute(
        "SELECT label FROM periods WHERE is_active=1 ORDER BY sort_order,id").fetchall()]
    return render_template(
        "study.html",
        repeat_request=repeat_request,
        repeat_group=repeat_group,
        selected_service=selected_service if not (repeat_request or repeat_group) else "",
        period_labels=period_labels,
        max_links=MAX_GROUP_LINKS,
        data_intervals=DATA_INTERVALS,
        default_data_interval=DEFAULT_DATA_INTERVAL,
    )


@bp.get("/api/network/links")
@login_required
def search_network_links():
    cfg = catalog_svc.get_setting("network", {})
    if not (network.HAVE_GEO and cfg.get("file") and os.path.isfile(cfg["file"])):
        return jsonify(error="The road network is not available"), 503
    query = request.args.get("q", "").strip()
    if not query: return jsonify(error="Enter a search term"), 400
    try:
        offset = int(request.args.get("offset", "0"))
        limit = int(request.args.get("limit", "20"))
    except ValueError: return jsonify(error="Invalid search page"), 400
    if offset < 0: return jsonify(error="Invalid search page"), 400
    limit = max(1, min(limit, 40))
    result = network.search_links(cfg["file"], query, offset, limit)
    return jsonify(**result, offset=offset, limit=limit)


@bp.get("/api/network/links/<int:link_id>")
@login_required
def get_network_link(link_id):
    cfg = catalog_svc.get_setting("network", {})
    if not (network.HAVE_GEO and cfg.get("file") and os.path.isfile(cfg["file"])):
        return jsonify(error="The road network is not available"), 503
    link = network.get_link(cfg["file"], link_id)
    if link is None: return jsonify(error="Link not found"), 404
    return jsonify(link)


@bp.post("/api/requests")
@login_required
def create_request():
    j = request.get_json(force=True)
    enabled = catalog_svc.active_services()
    sv = list(dict.fromkeys(x for x in j.get("services", []) if x in enabled))
    coords = j.get("coords") or []
    try: day = dt.date.fromisoformat(str(j.get("date") or "")).isoformat()
    except ValueError: return jsonify(error="Choose a valid date"), 400
    try:
        start_time = dt.time.fromisoformat(str(j.get("start_time") or ""))
        end_time = dt.time.fromisoformat(str(j.get("end_time") or ""))
    except ValueError:
        return jsonify(error="Choose a valid start and end time, including minutes and seconds"), 400
    if (start_time.tzinfo or end_time.tzinfo or start_time.microsecond or end_time.microsecond):
        return jsonify(error="Enter local times using hours, minutes and whole seconds only"), 400
    start_seconds = start_time.hour * 3600 + start_time.minute * 60 + start_time.second
    end_seconds = end_time.hour * 3600 + end_time.minute * 60 + end_time.second
    if end_seconds <= start_seconds:
        return jsonify(error="End time must be later than start time on the same study day"), 400
    hours = list(range(start_seconds // 3600, (end_seconds - 1) // 3600 + 1))
    start_time, end_time = start_time.strftime("%H:%M:%S"), end_time.strftime("%H:%M:%S")
    data_interval, interval_error = common_svc._clean_data_interval(j.get("data_interval"))
    if interval_error: return jsonify(error=interval_error), 400
    if len(coords) != 2: return jsonify(error="Select the start and the end of the link"), 400
    if not sv: return jsonify(error="Choose at least one service"), 400
    if not hours: return jsonify(error="Choose at least one hour"), 400
    lats, lngs = [c[0] for c in coords], [c[1] for c in coords]
    try:
        path = [[round(float(p[0]), 6), round(float(p[1]), 6)] for p in (j.get("path") or [])[:2000]]
        road_km = round(float(j["road_km"]), 2) if j.get("road_km") is not None else None
        junctions = int(j["junctions"]) if j.get("junctions") is not None else None
    except (TypeError, ValueError, IndexError): path, road_km, junctions = [], None, None
    if junctions is None or junctions < 0 or len(path) < 2:
        return jsonify(error="The road route could not be verified for intersections. Adjust points A and B and try again."), 400
    junction_quality_ack = junctions > 0 and j.get("accept_intersection_quality_impact") is True
    if junctions == 0 and j.get("no_junctions") is not True:
        return jsonify(error="Confirm that the link has no intersections, entrances or exits."), 400
    if junctions > 0 and not junction_quality_ack:
        return jsonify(
            error=(
                f"This link contains {junctions} possible intersection(s). "
                "Confirm that you agree to submit it knowing intersections may affect result quality."
            )
        ), 400
    point_c = None
    point_c_distance_km = None
    if {"speed", "travel", "volumes"}.intersection(sv):
        raw_point_c = j.get("point_c")
        try:
            if not isinstance(raw_point_c, list) or len(raw_point_c) != 2:
                raise ValueError
            point_c = [round(float(raw_point_c[0]), 6), round(float(raw_point_c[1]), 6)]
            if not (-90 <= point_c[0] <= 90 and -180 <= point_c[1] <= 180):
                raise ValueError
            point_c_distance_km = float(j.get("point_c_distance_km"))
            if not math.isfinite(point_c_distance_km) or point_c_distance_km <= 1.5:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify(error="A valid road point C more than 1.5 km from A along the road is required for Time, Speed, or Volumes requests"), 400
    junction_quality_ack_at = common_svc._timestamp_now() if junction_quality_ack else None
    cur = db().execute("INSERT INTO requests(user_id,created,d1,d2,services,area,bbox,coords,type,hours,path,road_km,junctions,start_time,end_time,point_c,point_c_distance_km,title,junction_quality_ack,junction_quality_ack_at,data_interval) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session["uid"], dt.date.today().isoformat(), day, day, json.dumps(sv), str(j.get("area") or "Link")[:120],
         json.dumps([min(lats), min(lngs), max(lats), max(lngs)]), json.dumps(coords), "link", json.dumps(hours), json.dumps(path), road_km, junctions, start_time, end_time, json.dumps(point_c) if point_c else None, point_c_distance_km, common_svc._clean_request_title(j.get("title")), int(junction_quality_ack), junction_quality_ack_at, data_interval))
    if "volumes" in sv:
        now = common_svc._timestamp_now()
        db().execute(
            "INSERT INTO volumes_workflows(request_id,status,created_at,updated_at) VALUES(?,'waiting_for_inputs',?,?)",
            (cur.lastrowid, now, now),
        )
    if session.get("role") != "admin":
        notifications_svc._notify_admins_new_request(cur.lastrowid, session.get("name", "A client"))
    db().commit()
    rid, info = cur.lastrowid, None
    try: info = network_links_svc.apply_network(rid)
    except Exception as e: flask_app.logger.warning("network lookup failed: %s", e)      # never block a client's request
    st = db().execute("SELECT status FROM requests WHERE id=?", (rid,)).fetchone()["status"]
    return jsonify(id=rid, status=st, filled=(info or {}).get("filled", []))


@bp.post("/api/requests/<int:rid>/title")
@login_required
def rename_request(rid):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("title"), str):
        return jsonify(error="Enter a name for the request."), 400
    title = common_svc._clean_request_title(payload["title"])
    cursor = db().execute(
        "UPDATE requests SET title=? WHERE id=? AND user_id=?", (title, rid, session["uid"])
    )
    if cursor.rowcount != 1:
        return jsonify(error="Request not found"), 404
    db().commit()
    return jsonify(ok=True, title=title)


@bp.route("/requests")
@login_required
def my_requests():
    rows = db().execute("SELECT * FROM requests WHERE user_id=? AND group_id IS NULL ORDER BY id DESC", (session["uid"],)).fetchall()
    reqs = [catalog_svc.to_dict(row) for row in rows]
    for item in reqs:
        item["results_available"] = (
            item["status"] == "Completed"
            or (item["status"] == "Processing" and item["progress"][0] > 0)
        )
    groups = [request_groups_svc._group_card(row) for row in db().execute(
        "SELECT * FROM request_groups WHERE user_id=? ORDER BY id DESC", (session["uid"],)).fetchall()]
    initial_status = request.args.get("status", "")
    if initial_status not in STATUSES:
        initial_status = ""
    initial_results = request.args.get("results", "")
    if initial_results != "available":
        initial_results = ""
    return render_template(
        "requests.html",
        reqs=reqs,
        groups=groups,
        statuses=STATUSES,
        initial_status=initial_status,
        initial_results=initial_results,
    )


@bp.get("/requests/<int:rid>")
@login_required
def user_request_detail(rid):
    row = db().execute(
        "SELECT r.*, u.name AS client, u.email FROM requests r "
        "JOIN users u ON u.id=r.user_id WHERE r.id=? AND r.user_id=?",
        (rid, session["uid"]),
    ).fetchone()
    if not row:
        abort(404)
    item = catalog_svc.to_dict(row)
    item["results_available"] = (
        item["status"] == "Completed"
        or (item["status"] == "Processing" and item["progress"][0] > 0)
    )
    return render_template("request_detail.html", r=item)


@bp.route("/results/<int:rid>")
@login_required
def result(rid):
    r = db().execute("SELECT r.*, u.name AS client FROM requests r JOIN users u ON u.id=r.user_id WHERE r.id=?", (rid,)).fetchone()
    if not r or (r["user_id"] != session["uid"] and session["role"] != "admin"): abort(404)
    if r["group_id"] and session["role"] != "admin":
        grp = db().execute("SELECT sent_at FROM request_groups WHERE id=?", (r["group_id"],)).fetchone()
        if grp and not grp["sent_at"]:
            flash("This order is still being prepared. All its results will be sent together in one report.")
            return redirect(url_for("groups.client_group", gid=r["group_id"]))
    if r["status"] == "Pending" and session["role"] != "admin": flash("Your request is still pending. Results will appear here soon."); return redirect(url_for("client.my_requests"))
    result_request = catalog_svc.to_dict(r)
    if session.get("role") != "admin" and not result_request.get("show_point_c"):
        result_request["point_c"] = None
        result_request["point_c_distance_km"] = None
    if session.get("role") != "admin":
        exports_svc._redact_client_directions_context(result_request)
    return render_template("result.html", r=result_request, forms=catalog_svc.forms_catalog())


@bp.get("/results/<int:rid>/export.xlsx")
@login_required
def export_result_excel(rid):
    row = db().execute(
        "SELECT r.*, u.name AS client FROM requests r JOIN users u ON u.id=r.user_id WHERE r.id=?",
        (rid,),
    ).fetchone()
    if not row or (row["user_id"] != session["uid"] and session["role"] != "admin"):
        abort(404)
    result_request = catalog_svc.to_dict(row)
    if session.get("role") != "admin" and not result_request.get("show_point_c"):
        result_request["point_c"] = None
        result_request["point_c_distance_km"] = None
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Request Summary"
    summary.append(["Field", "Value"])
    for label, value in (
        ("Request ID", f"REQ-{result_request['id']}"),
        ("Client", result_request["client"]),
        ("Status", result_request["status"]),
        ("Study date", result_request["d1"]),
        ("Time range", result_request["time_range_label"]),
        ("Hourly periods", result_request["hours_label"]),
        ("Link", result_request["area"]),
        ("Road length (km)", result_request.get("road_km")),
        ("Requested services", ", ".join(SERVICES.get(service, service) for service in result_request["services"])),
        ("Point A", ", ".join(map(str, result_request["coords"][0])) if result_request["coords"] else ""),
        ("Point B", ", ".join(map(str, result_request["coords"][-1])) if result_request["coords"] else ""),
        ("Point C", ", ".join(map(str, result_request["point_c"])) if result_request.get("point_c") else ""),
        ("Admin reply", result_request.get("reply") or ""),
    ):
        summary.append([exports_svc._excel_safe_text(label), value if isinstance(value, (int, float)) else exports_svc._excel_safe_text(value)])
    directions_result = (
        (result_request["data"].get("travel") or result_request["data"].get("speed") or {}).get("directions_result")
    )
    if directions_result:
        for label, value in (
            ("Directions route", directions_result["route"]),
            ("A-to-B distance (m)", directions_result["distance_meters"]),
            ("A-to-B time (seconds)", directions_result["time_seconds"]),
            ("A-to-B speed (km/h)", directions_result["speed_kmh"]),
        ):
            summary.append([exports_svc._excel_safe_text(label), value if isinstance(value, (int, float)) else exports_svc._excel_safe_text(value)])
    for service in result_request["services"]:
        service_data = result_request["data"].get(service) or {}
        reference_context = (
            service_data.get("reference_context")
            or service_data.get("holiday_context")
            or service_data.get("freeflow_context")
        )
        if reference_context:
            service_name = SERVICES.get(service, service)
            weekend_reference = (
                reference_context.get("reference_type") == "weekly_weekend"
                or bool(reference_context.get("weekend_date"))
            )
            reference_date = reference_context.get("weekend_date") or reference_context.get("holiday_date", "")
            reference_label = reference_context.get("weekend_day") or reference_context.get("holiday_name", "")
            reference_kind = "weekly weekend used" if weekend_reference else "public holiday used"
            summary.append([exports_svc._excel_safe_text(f"{service_name} study date"), exports_svc._excel_safe_text(reference_context["study_date"])])
            summary.append([exports_svc._excel_safe_text(f"{service_name} {reference_kind}"), exports_svc._excel_safe_text(
                f"{reference_label} · {reference_date}"
            )])
            summary.append([exports_svc._excel_safe_text(f"{service_name} route country / time zone"), exports_svc._excel_safe_text(
                f'{reference_context["country_name"]} ({reference_context["country_code"]}) · {reference_context["timezone"]}'
            )])
            summary.append([exports_svc._excel_safe_text(f"{service_name} departure"), exports_svc._excel_safe_text("07:00 local time")])
    exports_svc._style_excel_sheet(summary)

    details = workbook.create_sheet("Service Details")
    details.append(["Service", "Hour", "Value", "Tolerance", "Unit", "Source", "Notes", "Updated"])
    forms = catalog_svc.forms_catalog()
    for service in result_request["services"]:
        definition = forms.get(service, {})
        saved = result_request["data"].get(service, {})
        rows = saved.get("rows") or []
        if rows:
            for service_row in rows:
                hour = service_row.get("hour")
                hour_label = f"{int(hour):02d}:00-{int(hour) + 1:02d}:00" if hour is not None else ""
                value = service_row.get("value")
                if isinstance(value, bool):
                    value = exports_svc._excel_safe_text(value)
                elif not isinstance(value, (int, float)):
                    value = exports_svc._excel_safe_text(value)
                tolerance = service_row.get("tolerance")
                if tolerance is not None and not isinstance(tolerance, (int, float)):
                    tolerance = exports_svc._excel_safe_text(tolerance)
                details.append([
                    exports_svc._excel_safe_text(SERVICES.get(service, service)), hour_label, value, tolerance,
                    exports_svc._excel_safe_text(definition.get("unit", "")),
                    exports_svc._excel_safe_text(
                        "Road network" if saved.get("source") == "network"
                        else "MATLAB" if saved.get("source") == "matlab"
                        else f"Shared from REQ-{saved.get('shared_from')}" if saved.get("source") == "shared"
                        else "Admin"
                    ),
                    exports_svc._excel_safe_text(saved.get("note") or ""), exports_svc._excel_safe_text(saved.get("updated") or ""),
                ])
        else:
            details.append([
                exports_svc._excel_safe_text(SERVICES.get(service, service)), "", "", "",
                exports_svc._excel_safe_text(definition.get("unit", "")),
                exports_svc._excel_safe_text(
                    "Road network" if saved.get("source") == "network"
                    else "MATLAB" if saved.get("source") == "matlab"
                    else ""
                ),
                exports_svc._excel_safe_text(saved.get("note") or ""), exports_svc._excel_safe_text(saved.get("updated") or ""),
            ])
    exports_svc._style_excel_sheet(details)
    if session.get("role") == "admin" and "volumes" in result_request["services"]:
        volume_rows = volumes_svc._volume_input_rows(rid)
        if volume_rows:
            inputs_sheet = workbook.create_sheet("Volume Inputs")
            inputs_sheet.append(["Hour", "Actual speed (km/h)", "Free-flow speed used (km/h)", "Lanes", "Traffic volume (veh/h)",
                                 "Tolerance", "Free-flow reference day", "Volume status"])
            for item in volume_rows:
                inputs_sheet.append([f"{item['hour']:02d}:00-{item['hour'] + 1:02d}:00", item["actual_speed"], item["freeflow_speed"],
                                     item["lanes"], item["volume"], item["tolerance"], exports_svc._excel_safe_text(item["reference"]),
                                     exports_svc._excel_safe_text(item["state"])])
            exports_svc._style_excel_sheet(inputs_sheet)
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return flask_app.response_class(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="trafficlens-request-{rid}.xlsx"'},
    )
