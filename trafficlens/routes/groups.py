"""Grouped requests: creating, viewing, sending and exporting them."""
import datetime as dt
import io
import json
import sqlite3
from flask import Blueprint, abort, jsonify, redirect, render_template, request, session, url_for
from openpyxl import Workbook
from trafficlens.config import GROUP_MODES, MAX_GROUP_LINKS, SERVICES, SHARED_FLOW_SERVICES
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.security.decorators import admin_required, login_required
from trafficlens.services import catalog as catalog_svc, common as common_svc, exports as exports_svc, network_links as network_links_svc, notifications as notifications_svc, request_groups as request_groups_svc, volumes as volumes_svc

bp = Blueprint("groups", __name__)


@bp.post("/api/request-groups")
@login_required
def create_request_group():
    j = request.get_json(force=True, silent=True) or {}
    enabled = catalog_svc.active_services()
    sv = list(dict.fromkeys(x for x in j.get("services", []) if x in enabled))
    if not sv:
        return jsonify(error="Choose at least one service"), 400
    try: day = dt.date.fromisoformat(str(j.get("date") or "")).isoformat()
    except ValueError: return jsonify(error="Choose a valid date"), 400
    data_interval, interval_error = common_svc._clean_data_interval(j.get("data_interval"))
    if interval_error:
        return jsonify(error=interval_error), 400
    mode = j.get("mode")
    if mode not in GROUP_MODES:
        return jsonify(error="Choose the order type first"), 400
    raw_links, raw_periods = j.get("links"), j.get("periods")
    if not isinstance(raw_links, list) or not isinstance(raw_periods, list):
        return jsonify(error="Add at least one link and one period"), 400
    if not raw_links:
        return jsonify(error="Add at least one link"), 400
    if not raw_periods:
        return jsonify(error="Choose at least one period"), 400
    if len(raw_links) > MAX_GROUP_LINKS:
        return jsonify(error=f"An order can contain at most {MAX_GROUP_LINKS} links"), 400
    if mode in ("single", "multi_period") and len(raw_links) != 1:
        return jsonify(error="This order type allows exactly one link"), 400
    if mode in ("single", "multi_link") and len(raw_periods) != 1:
        return jsonify(error="This order type allows exactly one period"), 400
    active_labels = {row["label"] for row in db().execute("SELECT label FROM periods WHERE is_active=1").fetchall()}
    periods, seen = [], set()
    for raw in raw_periods:
        period, error = request_groups_svc._clean_group_period(raw, active_labels)
        if error: return jsonify(error=error), 400
        if period["label"] in seen: return jsonify(error=f"Period {period['label']} was chosen twice"), 400
        seen.add(period["label"]); periods.append(period)
    links = []
    for number, raw in enumerate(raw_links, start=1):
        link, error = request_groups_svc._clean_group_link(raw, sv, number)
        if error: return jsonify(error=error), 400
        links.append(link)
    title = common_svc._clean_request_title(j.get("title"))
    now_day, total = dt.date.today().isoformat(), len(links) * len(periods)
    d = db()
    group_id = None
    try:
        if total > 1:
            group_id = d.execute(
                "INSERT INTO request_groups(user_id,title,created,study_date,services,mode) VALUES(?,?,?,?,?,?)",
                (session["uid"], title, now_day, day, json.dumps(sv), mode)).lastrowid
        request_ids = []
        for number, link in enumerate(links, start=1):
            for period in periods:
                child_title = title if total == 1 else common_svc._clean_request_title(
                    " · ".join(x for x in (title, f"Link {number}", period["label"]) if x))
                cur = d.execute(
                    "INSERT INTO requests(user_id,created,d1,d2,services,area,bbox,coords,type,hours,path,road_km,junctions,start_time,end_time,point_c,point_c_distance_km,title,junction_quality_ack,junction_quality_ack_at,group_id,period_label,data_interval) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session["uid"], now_day, day, day, json.dumps(sv),
                     f"Link {number} A to B (~{link['road_km']} km by road)" if total > 1 else f"Link A to B (~{link['road_km']} km by road)",
                     json.dumps(link["bbox"]), json.dumps(link["coords"]), "link", json.dumps(period["hours"]),
                     json.dumps(link["path"]), link["road_km"], link["junctions"], period["start_time"], period["end_time"],
                     json.dumps(link["point_c"]) if link["point_c"] else None, link["point_c_distance_km"],
                     child_title, int(link["ack"]), common_svc._timestamp_now() if link["ack"] else None, group_id, period["label"], data_interval))
                request_ids.append(cur.lastrowid)
                if "volumes" in sv:
                    now = common_svc._timestamp_now()
                    d.execute("INSERT INTO volumes_workflows(request_id,status,created_at,updated_at) VALUES(?,'waiting_for_inputs',?,?)",
                              (cur.lastrowid, now, now))
        if session.get("role") != "admin":
            if group_id:
                for admin_user in d.execute("SELECT id FROM users WHERE role='admin'").fetchall():
                    notifications_svc._create_notification(admin_user["id"], request_ids[0], "new_request", "New grouped request",
                        f"{session.get('name', 'A client')} submitted order GRP-{group_id}: {len(links)} link(s) x {len(periods)} period(s) = {total} requests.")
            else:
                notifications_svc._notify_admins_new_request(request_ids[0], session.get("name", "A client"))
        d.commit()
    except sqlite3.Error:
        d.rollback()
        flask_app.logger.exception("Could not save the grouped order")
        return jsonify(error="The order could not be saved. Nothing was created; please try again."), 500
    for rid in request_ids:
        try: network_links_svc.apply_network(rid)
        except Exception as e: flask_app.logger.warning("network lookup failed: %s", e)
    status = d.execute("SELECT status FROM requests WHERE id=?", (request_ids[0],)).fetchone()["status"]
    return jsonify(group_id=group_id, request_ids=request_ids, count=total, id=request_ids[0], status=status)


@bp.get("/groups/<int:gid>")
@login_required
def client_group(gid):
    if session.get("role") == "admin":
        return redirect(url_for("groups.admin_group", gid=gid))
    row, children = request_groups_svc._load_group(gid, admin_view=False)
    return render_template("group.html", g=row, children=children, card=request_groups_svc._group_card(row), admin_view=False,
                           ready=request_groups_svc._group_ready_to_send(children))


@bp.get("/admin/groups/<int:gid>")
@admin_required
def admin_group(gid):
    row, children = request_groups_svc._load_group(gid, admin_view=True)
    period_order = list(dict.fromkeys(c["period_label"] for c in children if c.get("period_label")))
    matrix = {}
    for c in children:
        matrix.setdefault(c["link_no"], {})[c.get("period_label")] = c
    shared = []
    for name in SHARED_FLOW_SERVICES:
        holders = [c for c in children if name in c["services"] and request_groups_svc._flow_is_shared(c["services"], name)]
        if not holders:
            continue
        owners = {}
        for c in holders:
            owners.setdefault(c["link_no"], c["id"])
        if len(holders) > len(owners):
            shared.append({"service": name, "api_calls": len(owners), "without": len(holders), "owners": owners})
    return render_template("group.html", g=row, children=children, card=request_groups_svc._group_card(row), admin_view=True,
                           ready=request_groups_svc._group_ready_to_send(children), matrix=matrix, period_order=period_order, shared=shared)


@bp.post("/api/groups/<int:gid>/delete")
@admin_required
def delete_group(gid):
    ids = [r["id"] for r in db().execute("SELECT id FROM requests WHERE group_id=?", (gid,)).fetchall()]
    if not ids and not db().execute("SELECT 1 FROM request_groups WHERE id=?", (gid,)).fetchone():
        return jsonify(error="Order not found"), 404
    request_groups_svc._delete_requests(ids)
    db().execute("DELETE FROM request_groups WHERE id=?", (gid,)); db().commit()
    return jsonify(ok=True, deleted=len(ids))


@bp.post("/api/groups/<int:gid>/send")
@admin_required
def send_group_report(gid):
    row = db().execute("SELECT * FROM request_groups WHERE id=?", (gid,)).fetchone()
    if not row:
        return jsonify(error="Order not found"), 404
    if row["sent_at"]:
        return jsonify(error="This report was already sent."), 409
    children = request_groups_svc._group_children(gid)
    if not request_groups_svc._group_ready_to_send(children):
        return jsonify(error="Every request in the order must be Completed (or Rejected) before the report can be sent."), 409
    stamp = common_svc._timestamp_now()
    db().execute("UPDATE request_groups SET sent_at=? WHERE id=?", (stamp, gid))
    first = children[0]["id"]
    notifications_svc._create_notification(row["user_id"], first, "group_ready", "Your report is ready",
                         f"All results for order GRP-{gid} are ready in one report.")
    db().commit()
    return jsonify(ok=True, sent_at=stamp)


@bp.get("/groups/<int:gid>/export.xlsx")
@login_required
def export_group_excel(gid):
    admin_view = session.get("role") == "admin"
    row, children = request_groups_svc._load_group(gid, admin_view=admin_view)
    if not admin_view and not row["sent_at"]:
        abort(404)
    forms = catalog_svc.forms_catalog()
    services = [x for x in json.loads(row["services"]) if x in catalog_svc.service_catalog()]
    workbook = Workbook()
    summary = workbook.active
    summary.title = "Order Summary"
    summary.append(["Field", "Value"])
    link_count = len({json.dumps(c["coords"]) for c in children})
    for label, value in (
        ("Order ID", f"GRP-{gid}"), ("Name", row["title"] or ""), ("Client", row["client"]),
        ("Status", request_groups_svc._group_status(row, children)),
        ("Report state", "Sent to client" if row["sent_at"] else "Draft - not sent to the client yet"),
        ("Study date", row["study_date"]),
        ("Links", link_count), ("Periods", ", ".join(sorted({c["period_label"] for c in children if c.get("period_label")}))),
        ("Requests in this order", len(children)),
        ("Requested services", ", ".join(SERVICES.get(x, x) for x in services)),
    ):
        summary.append([exports_svc._excel_safe_text(label), value if isinstance(value, (int, float)) else exports_svc._excel_safe_text(value)])
    exports_svc._style_excel_sheet(summary)

    links_sheet = workbook.create_sheet("Links and Periods")
    links_sheet.append(["Request", "Link", "Period", "Time range", "Status", "Point A", "Point B", "Point C", "Road length (km)", "Admin note"])
    for c in children:
        show_c = admin_view or c.get("show_point_c")
        links_sheet.append([
            f"REQ-{c['id']}", f"Link {c['link_no']}", exports_svc._excel_safe_text(c.get("period_label") or ""),
            exports_svc._excel_safe_text(c["time_range_label"]), c["status"],
            ", ".join(map(str, c["coords"][0])), ", ".join(map(str, c["coords"][-1])),
            ", ".join(map(str, c["point_c"])) if (c.get("point_c") and show_c) else "", c.get("road_km"),
            exports_svc._excel_safe_text(c.get("reply") or "")])
    exports_svc._style_excel_sheet(links_sheet)

    details = workbook.create_sheet("Service Details")
    details.append(["Link", "Period", "Time range", "Service", "Hour", "Value", "Tolerance", "Unit", "Source", "Notes"])
    for c in children:
        if c["status"] == "Rejected":
            continue
        for service in c["services"]:
            saved = c["data"].get(service, {})
            unit = (forms.get(service) or {}).get("unit", "")
            source = ("Road network" if saved.get("source") == "network" else "MATLAB" if saved.get("source") == "matlab"
                      else f"Shared from REQ-{saved.get('shared_from')} (same link)" if saved.get("source") == "shared"
                      else "Admin" if (saved.get("rows") or saved.get("note")) else "")
            rows = saved.get("rows") or [{}]
            for service_row in rows:
                hour = service_row.get("hour")
                hour_label = f"{int(hour):02d}:00-{int(hour) + 1:02d}:00" if hour is not None else ""
                value = service_row.get("value")
                if value is not None and not isinstance(value, (int, float)) or isinstance(value, bool):
                    value = exports_svc._excel_safe_text(value)
                tol = service_row.get("tolerance")
                if tol is not None and not isinstance(tol, (int, float)):
                    tol = exports_svc._excel_safe_text(tol)
                details.append([f"Link {c['link_no']}", exports_svc._excel_safe_text(c.get("period_label") or ""), exports_svc._excel_safe_text(c["time_range_label"]),
                                exports_svc._excel_safe_text(SERVICES.get(service, service)), hour_label, value, tol,
                                exports_svc._excel_safe_text(unit), exports_svc._excel_safe_text(source), exports_svc._excel_safe_text(saved.get("note") or "")])
    exports_svc._style_excel_sheet(details)

    if admin_view:
        volume_rows = []
        for c in children:
            if c["status"] == "Rejected" or "volumes" not in c["services"]:
                continue
            for item in volumes_svc._volume_input_rows(c["id"]):
                volume_rows.append([f"Link {c['link_no']}", exports_svc._excel_safe_text(c.get("period_label") or ""), f"{item['hour']:02d}:00-{item['hour'] + 1:02d}:00",
                                    item["actual_speed"], item["freeflow_speed"], item["lanes"], item["volume"], item["tolerance"],
                                    exports_svc._excel_safe_text(item["reference"]), exports_svc._excel_safe_text(item["state"])])
        if volume_rows:
            inputs_sheet = workbook.create_sheet("Volume Inputs")
            inputs_sheet.append(["Link", "Period", "Hour", "Actual speed (km/h)", "Free-flow speed used (km/h)", "Lanes",
                                 "Traffic volume (veh/h)", "Tolerance", "Free-flow reference day", "Volume status"])
            for item in volume_rows:
                inputs_sheet.append(item)
            exports_svc._style_excel_sheet(inputs_sheet)
    reference_rows, reference_seen = [], set()
    for c in children:
        if c["status"] == "Rejected":
            continue
        for service in c["services"]:
            context = ((c["data"].get(service) or {}).get("reference_context") or (c["data"].get(service) or {}).get("holiday_context"))
            if not context or (c["link_no"], service) in reference_seen:
                continue
            reference_seen.add((c["link_no"], service))
            reference_rows.append([
                f"Link {c['link_no']}", exports_svc._excel_safe_text(SERVICES.get(service, service)),
                exports_svc._excel_safe_text("Weekly weekend" if (context.get("reference_type") == "weekly_weekend" or context.get("weekend_date")) else "Public holiday"),
                exports_svc._excel_safe_text(context.get("weekend_date") or context.get("holiday_date") or ""),
                exports_svc._excel_safe_text(context.get("weekend_day") or context.get("holiday_name") or ""),
                exports_svc._excel_safe_text(f'{context.get("country_name", "")} ({context.get("country_code", "")})'),
                exports_svc._excel_safe_text(context.get("timezone", "")), "07:00 local time"])
    if reference_rows:
        references = workbook.create_sheet("Free Flow References")
        references.append(["Link", "Service", "Reference type", "Date", "Day / holiday", "Country", "Time zone", "Departure"])
        for item in reference_rows:
            references.append(item)
        exports_svc._style_excel_sheet(references)
    route_rows = []
    for c in children:
        directions = (c["data"].get("travel") or c["data"].get("speed") or {}).get("directions_result")
        if directions and c["status"] != "Rejected":
            route_rows.append([f"Link {c['link_no']}", exports_svc._excel_safe_text(c.get("period_label") or ""),
                               directions.get("distance_meters"), directions.get("time_seconds"), directions.get("speed_kmh")])
    if route_rows:
        routes = workbook.create_sheet("A-to-B Results")
        routes.append(["Link", "Period", "A-to-B distance (m)", "A-to-B time (s)", "A-to-B speed (km/h)"])
        for item in route_rows:
            routes.append(item)
        exports_svc._style_excel_sheet(routes)
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return flask_app.response_class(
        output.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="trafficlens-order-{gid}.xlsx"'},
    )
