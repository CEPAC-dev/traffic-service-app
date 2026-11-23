"""Grouped requests (one study date, many links) and flows shared between services."""
import datetime as dt
import json
import math
from flask import abort, session
from trafficlens.config import DEFAULT_DATA_INTERVAL, SHARED_FLOW_SERVICES
from trafficlens.db import db
from trafficlens.services import catalog as catalog_svc
from trafficlens.services import common as common_svc


def _repeat_group_payload(gid):
    """Everything needed to prefill a new order from a completed grouped order (None until its report was sent)."""
    row = db().execute("SELECT * FROM request_groups WHERE id=? AND user_id=?", (gid, session["uid"])).fetchone()
    if not row:
        abort(404)
    if not row["sent_at"]:
        return None
    enabled = catalog_svc.active_services()
    periods, links, seen_periods, seen_links = [], [], set(), set()
    data_interval = DEFAULT_DATA_INTERVAL
    for child in _group_children(gid):
        data_interval = child.get("data_interval") or data_interval
        label = child.get("period_label")
        if label and label not in seen_periods and child.get("start_time") and child.get("end_time"):
            seen_periods.add(label)
            periods.append({"label": label, "start_time": child["start_time"], "end_time": child["end_time"]})
        key = json.dumps(child["coords"])
        if key not in seen_links:
            seen_links.add(key)
            links.append({"coords": child["coords"], "path": child["path"], "road_km": child["road_km"],
                          "junctions": child["junctions"], "point_c": child["point_c"],
                          "point_c_distance_km": child["point_c_distance_km"]})
    return {"id": gid, "title": row["title"] or "", "date": row["study_date"], "mode": row["mode"],
            "services": [x for x in json.loads(row["services"]) if x in enabled],
            "periods": periods, "links": links, "data_interval": data_interval}


def _flow_is_shared(services, service):
    """Free flow time/speed describe the link, not the study period, so one API result serves every period of a link.
    The only exception is free-flow speed requested together with Volumes: that A-to-C window depends on the period."""
    return service in SHARED_FLOW_SERVICES and not (service == "freeflow_speed" and "volumes" in services)


def _link_siblings(request_row):
    """Requests of the same grouped order that cover the same link (one per period), oldest first."""
    if not request_row["group_id"]:
        return []
    key = json.loads(request_row["coords"] or "[]")
    rows = db().execute("SELECT * FROM requests WHERE group_id=? ORDER BY id", (request_row["group_id"],)).fetchall()
    return [row for row in rows if json.loads(row["coords"] or "[]") == key]


def _shared_flow_leader(request_row, service):
    """Id of the sibling that owns the free-flow API result for this link; None if this request owns it or it is not shared."""
    services = json.loads(request_row["services"] or "[]")
    if not request_row["group_id"] or service not in services or not _flow_is_shared(services, service):
        return None
    owners = [row["id"] for row in _link_siblings(request_row)
              if service in json.loads(row["services"] or "[]") and _flow_is_shared(json.loads(row["services"] or "[]"), service)]
    return owners[0] if owners and owners[0] != request_row["id"] else None


def _propagate_shared_services(rid, saved_services):
    """Copy the owner's confirmed free-flow values to the other periods of the same link, so the API is paid for once."""
    row = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not row or not row["group_id"]:
        return 0
    owner_services = json.loads(row["services"] or "[]")
    shared = [name for name in saved_services if _flow_is_shared(owner_services, name) and _shared_flow_leader(row, name) is None]
    if not shared:
        return 0
    owner_data, stamp, touched = json.loads(row["data"] or "{}"), dt.datetime.now().strftime("%Y-%m-%d %H:%M"), 0
    for sibling in _link_siblings(row):
        if sibling["id"] == rid:
            continue
        sibling_services = json.loads(sibling["services"] or "[]")
        sibling_data, changed = json.loads(sibling["data"] or "{}"), False
        for name in shared:
            if name not in sibling_services or not _flow_is_shared(sibling_services, name):
                continue
            source = owner_data.get(name) or {}
            rows = source.get("rows") or []
            if rows:
                value = {"rows": json.loads(json.dumps(rows)), "note": source.get("note") or "", "source": "shared",
                         "shared_from": rid, "updated": stamp}
                for key in ("holiday_context", "reference_context", "outscraper_job_id"):
                    if source.get(key):
                        value[key] = source[key]
                sibling_data[name], changed = value, True
            elif (sibling_data.get(name) or {}).get("source") == "shared":
                sibling_data.pop(name, None); changed = True
        if changed:
            db().execute("UPDATE requests SET data=?, status=CASE WHEN status='Pending' THEN 'Processing' ELSE status END WHERE id=?",
                         (json.dumps(sibling_data, ensure_ascii=False), sibling["id"]))
            touched += 1
    db().commit()
    return touched


def _clean_group_period(raw, active_labels):
    if not isinstance(raw, dict):
        return None, "Invalid period."
    label = str(raw.get("label") or "").strip()
    if label not in active_labels:
        return None, f"Period '{label[:20]}' is not available."
    try:
        start, end = common_svc._parse_local_time(raw.get("start_time")), common_svc._parse_local_time(raw.get("end_time"))
    except ValueError:
        return None, f"Period {label}: choose a valid start and end time (whole seconds only)."
    start_s = start.hour * 3600 + start.minute * 60 + start.second
    end_s = end.hour * 3600 + end.minute * 60 + end.second
    if end_s <= start_s:
        return None, f"Period {label}: end time must be later than start time."
    return {"label": label, "start_time": start.strftime("%H:%M:%S"), "end_time": end.strftime("%H:%M:%S"),
            "hours": list(range(start_s // 3600, (end_s - 1) // 3600 + 1))}, None


def _clean_group_link(raw, services, number):
    name = f"Link {number}"
    if not isinstance(raw, dict):
        return None, f"{name}: invalid data."
    try:
        coords = raw.get("coords") or []
        if len(coords) != 2:
            return None, f"{name}: select the start and the end of the link."
        coords = [[round(float(c[0]), 6), round(float(c[1]), 6)] for c in coords]
        path = [[round(float(p[0]), 6), round(float(p[1]), 6)] for p in (raw.get("path") or [])[:2000]]
        road_km = round(float(raw["road_km"]), 2) if raw.get("road_km") is not None else None
        junctions = int(raw["junctions"]) if raw.get("junctions") is not None else None
    except (TypeError, ValueError, IndexError, KeyError):
        return None, f"{name}: the road route could not be verified. Adjust points A and B and add the link again."
    if junctions is None or junctions < 0 or len(path) < 2:
        return None, f"{name}: the road route could not be verified for intersections."
    ack = junctions > 0 and raw.get("accept_intersection_quality_impact") is True
    if junctions == 0 and raw.get("no_junctions") is not True:
        return None, f"{name}: confirm that the link has no intersections, entrances or exits."
    if junctions > 0 and not ack:
        return None, f"{name}: confirm that you accept the possible intersection quality impact."
    point_c = point_c_distance_km = None
    if {"speed", "travel", "volumes"}.intersection(services):
        try:
            rc = raw.get("point_c")
            if not isinstance(rc, list) or len(rc) != 2:
                raise ValueError
            point_c = [round(float(rc[0]), 6), round(float(rc[1]), 6)]
            if not (-90 <= point_c[0] <= 90 and -180 <= point_c[1] <= 180):
                raise ValueError
            point_c_distance_km = float(raw.get("point_c_distance_km"))
            if not math.isfinite(point_c_distance_km) or point_c_distance_km <= 1.5:
                raise ValueError
        except (TypeError, ValueError):
            return None, f"{name}: a valid road point C more than 1.5 km from A is required for Time, Speed or Volumes."
    lats, lngs = [c[0] for c in coords], [c[1] for c in coords]
    return {"coords": coords, "path": path, "road_km": road_km, "junctions": junctions, "ack": ack,
            "point_c": point_c, "point_c_distance_km": point_c_distance_km,
            "bbox": [min(lats), min(lngs), max(lats), max(lngs)]}, None


def _group_children(gid):
    rows = db().execute("SELECT * FROM requests WHERE group_id=? ORDER BY id", (gid,)).fetchall()
    return [catalog_svc.to_dict(row) for row in rows]


def _group_status(group_row, children):
    if group_row["sent_at"]:
        return "Completed"
    active = [c for c in children if c["status"] != "Rejected"]
    if not active:
        return "Rejected"
    if any(c["status"] in ("Processing", "Completed") for c in active):
        return "Processing"
    return "Pending"


def _group_ready_to_send(children):
    active = [c for c in children if c["status"] != "Rejected"]
    return bool(active) and all(c["status"] == "Completed" for c in active)


def _group_card(group_row):
    children = _group_children(group_row["id"])
    return {"id": group_row["id"], "title": group_row["title"], "created": group_row["created"],
            "d1": group_row["study_date"], "services": [x for x in json.loads(group_row["services"]) if x in catalog_svc.service_catalog()],
            "count": len(children), "periods": sorted({c["period_label"] for c in children if c.get("period_label")}),
            "links": len({json.dumps(c["coords"]) for c in children}),
            "status": _group_status(group_row, children), "sent": bool(group_row["sent_at"])}


def _load_group(gid, admin_view):
    row = db().execute(
        "SELECT g.*, u.name AS client, u.email AS email FROM request_groups g JOIN users u ON u.id=g.user_id WHERE g.id=?",
        (gid,)).fetchone()
    if not row or (not admin_view and row["user_id"] != session["uid"]):
        abort(404)
    children = _group_children(gid)
    # label every child by its link and period, preserving the order links were added
    link_index = {}
    for c in children:
        key = json.dumps(c["coords"])
        link_index.setdefault(key, len(link_index) + 1)
        c["link_no"] = link_index[key]
    return row, children


def _delete_requests(request_ids=None):
    d = db()
    d.execute("BEGIN IMMEDIATE")
    if request_ids is None:
        rows = d.execute("SELECT id FROM requests").fetchall()
    else:
        rows = d.execute(
            f"SELECT id FROM requests WHERE id IN ({','.join('?' for _ in request_ids)})",
            request_ids,
        ).fetchall() if request_ids else []
    ids = [row["id"] for row in rows]
    if not ids:
        d.commit()
        return 0
    placeholders = ",".join("?" for _ in ids)
    d.execute(f"DELETE FROM notifications WHERE request_id IN ({placeholders})", ids)
    d.execute(f"DELETE FROM volume_freeflow_attempts WHERE request_id IN ({placeholders})", ids)
    d.execute(f"DELETE FROM volumes_workflows WHERE request_id IN ({placeholders})", ids)
    d.execute(f"DELETE FROM request_directions_workflows WHERE request_id IN ({placeholders})", ids)
    d.execute(f"DELETE FROM requests WHERE id IN ({placeholders})", ids)
    d.execute("DELETE FROM group_turning_movement WHERE group_id NOT IN (SELECT DISTINCT group_id FROM requests WHERE group_id IS NOT NULL)")
    d.execute("DELETE FROM request_groups WHERE id NOT IN (SELECT DISTINCT group_id FROM requests WHERE group_id IS NOT NULL)")
    d.commit()
    return len(ids)
