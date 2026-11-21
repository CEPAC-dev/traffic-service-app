"""Admin dashboard and site management: requests list, users, services, periods, network files."""
import datetime as dt
import json
import network
import os
from flask import Blueprint, jsonify, render_template, request, session
from trafficlens.config import STATUSES
from trafficlens.db import db
from trafficlens.security.decorators import admin_required
from trafficlens.services import catalog as catalog_svc, request_groups as request_groups_svc

bp = Blueprint("admin", __name__)


@bp.post("/api/periods")
@admin_required
def add_period():
    label = " ".join(str((request.get_json(silent=True) or {}).get("label") or "").split())[:12].upper()
    if not label:
        return jsonify(error="Enter a period name, e.g. EVE"), 400
    if db().execute("SELECT 1 FROM periods WHERE label=?", (label,)).fetchone():
        return jsonify(error="This period already exists"), 409
    order = db().execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM periods").fetchone()[0]
    db().execute("INSERT INTO periods(label,sort_order) VALUES(?,?)", (label, order)); db().commit()
    return jsonify(ok=True)


@bp.post("/api/periods/<int:pid>/availability")
@admin_required
def set_period_availability(pid):
    enabled = (request.get_json(silent=True) or {}).get("enabled")
    if not isinstance(enabled, bool):
        return jsonify(error="Invalid"), 400
    if not enabled and db().execute("SELECT COUNT(*) FROM periods WHERE is_active=1 AND id!=?", (pid,)).fetchone()[0] == 0:
        return jsonify(error="At least one period must stay available"), 409
    if not db().execute("UPDATE periods SET is_active=? WHERE id=?", (int(enabled), pid)).rowcount:
        return jsonify(error="Period not found"), 404
    db().commit()
    return jsonify(ok=True)


@bp.route("/admin")
@admin_required
def admin():
    rows = db().execute("SELECT r.*, u.name AS client FROM requests r JOIN users u ON u.id=r.user_id ORDER BY r.id DESC").fetchall()
    reqs = [catalog_svc.to_dict(r) for r in rows]
    review_request_ids = {
        row["request_id"]
        for row in db().execute(
            "SELECT w.request_id FROM request_directions_workflows w JOIN requests r ON r.id=w.request_id "
            "WHERE w.status='ready' AND r.status NOT IN ('Completed','Rejected') "
            "UNION SELECT v.request_id FROM volumes_workflows v JOIN requests r ON r.id=v.request_id "
            "WHERE v.status='ready_for_review' AND r.status NOT IN ('Completed','Rejected')"
        ).fetchall()
    }
    for item in reqs:
        item["waiting_for_review"] = item["id"] in review_request_ids
        item["dashboard_status"] = "Waiting for review" if item["waiting_for_review"] else item["status"]
    entries, seen_groups = [], set()
    group_rows = {row["id"]: row for row in db().execute(
        "SELECT g.*, u.name AS client FROM request_groups g JOIN users u ON u.id=g.user_id").fetchall()}
    for item in reqs:   # newest first; a grouped order appears once, at the position of its newest request
        group_id = item.get("group_id")
        if not group_id or group_id not in group_rows:
            entries.append({"kind": "request", "r": item}); continue
        if group_id in seen_groups:
            continue
        seen_groups.add(group_id)
        group_row = group_rows[group_id]
        children = [c for c in reqs if c.get("group_id") == group_id]
        status = request_groups_svc._group_status(group_row, children)
        needs_review = any(c["waiting_for_review"] for c in children) or (not group_row["sent_at"] and request_groups_svc._group_ready_to_send(children))
        entries.append({"kind": "group", "g": {
            "id": group_id, "title": group_row["title"], "client": group_row["client"], "created": group_row["created"],
            "services": [x for x in json.loads(group_row["services"]) if x in catalog_svc.service_catalog()],
            "count": len(children), "links": len({json.dumps(c["coords"]) for c in children}),
            "periods": sorted({c["period_label"] for c in children if c.get("period_label")}),
            "status": status, "waiting_for_review": needs_review,
            "dashboard_status": "Ready to send" if (needs_review and not any(c["waiting_for_review"] for c in children)) else
                                ("Waiting for review" if needs_review else status)}})
    today = dt.date.today(); counts = {}
    for r in reqs: counts[r["created"]] = counts.get(r["created"], 0) + 1
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    stats = {"total": len(reqs),
             "clients": db().execute("SELECT COUNT(*) FROM users WHERE role='client'").fetchone()[0],
             "by_status": {s: sum(1 for r in reqs if r["status"] == s) for s in STATUSES},
             "sent_to_users": sum(1 for r in reqs if r["status"] == "Completed"),
             "waiting_review": len(review_request_ids),
             "by_service": {v: sum(1 for r in reqs if k in r["services"]) for k, v in catalog_svc.all_service_names().items()},
             "by_hour": {"labels": [f"{h:02d}" for h in range(24)], "values": [sum(1 for r in reqs if h in r["hours"]) for h in range(24)]},
             "per_day": {"labels": days, "values": [counts.get(d, 0) for d in days]}}
    period_rows = [dict(x) for x in db().execute("SELECT * FROM periods ORDER BY sort_order,id").fetchall()]
    users = [dict(u) for u in db().execute(
        "SELECT u.id,u.name,u.email,u.role,COUNT(r.id) AS n,COALESCE(SUM(r.status='Completed'),0) AS done,MAX(r.created) AS last "
        "FROM users u LEFT JOIN requests r ON r.user_id=u.id GROUP BY u.id ORDER BY n DESC, u.id").fetchall()]
    return render_template("admin.html", reqs=reqs, entries=entries, stats=stats, users=users, statuses=STATUSES, periods=period_rows,
                           net={"have_geo": network.HAVE_GEO, "cfg": catalog_svc.get_setting("network", {}), "services": catalog_svc.net_services()})


@bp.post("/api/requests/<int:rid>/delete")
@admin_required
def delete_request(rid):
    deleted = request_groups_svc._delete_requests([rid])
    if not deleted:
        return jsonify(error="Request not found"), 404
    return jsonify(ok=True, deleted=deleted)


@bp.post("/api/requests/delete-all")
@admin_required
def delete_all_requests():
    payload = request.get_json(silent=True) or {}
    if payload.get("confirmation") != "DELETE_ALL_REQUESTS":
        return jsonify(error="Explicit confirmation is required to delete all requests."), 400
    deleted = request_groups_svc._delete_requests()
    return jsonify(ok=True, deleted=deleted)


@bp.post("/api/users/<int:uid>/role")
@admin_required
def set_role(uid):
    role = (request.get_json(force=True) or {}).get("role")
    if role not in ("admin", "client") or not db().execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone(): return jsonify(error="Invalid"), 400
    if uid == session["uid"]: return jsonify(error="You cannot change your own role"), 400
    db().execute("UPDATE users SET role=? WHERE id=?", (role, uid)); db().commit()
    return jsonify(ok=True, role=role)


@bp.post("/api/services/<key>")
@admin_required
def save_service_info(key):
    catalog = catalog_svc.service_catalog()
    if key not in catalog: return jsonify(error="Invalid"), 400
    text = str((request.get_json(force=True) or {}).get("description") or "").strip()[:500]
    if text:
        db().execute("INSERT INTO service_info(key,description) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET description=excluded.description", (key, text))
    else:
        db().execute("DELETE FROM service_info WHERE key=?", (key,))
    db().commit()
    return jsonify(ok=True, description=text or catalog[key]["description"])


@bp.post("/api/services/<key>/availability")
@admin_required
def set_service_availability(key):
    catalog = catalog_svc.service_catalog()
    if key not in catalog: return jsonify(error="Invalid service"), 404
    enabled = (request.get_json(force=True) or {}).get("enabled")
    if not isinstance(enabled, bool): return jsonify(error="Choose whether the service is enabled"), 400
    saved = catalog_svc.get_setting("service_catalog", {}) or {}
    saved.setdefault(key, {})["enabled"] = enabled
    catalog_svc.set_setting("service_catalog", saved)
    return jsonify(ok=True, enabled=enabled)


@bp.post("/api/services/custom")
@admin_required
def create_custom_service():
    j = request.get_json(force=True) or {}
    name = str(j.get("name") or "").strip()
    unit = str(j.get("unit") or "").strip()
    kind = j.get("kind")
    if not name or len(name) > 80: return jsonify(error="Enter a service name (up to 80 characters)"), 400
    if len(unit) > 24: return jsonify(error="The unit must be 24 characters or fewer"), 400
    if kind not in ("static", "hourly"): return jsonify(error="Choose whether the service is per link or per hour"), 400
    saved = catalog_svc.get_setting("service_catalog", {}) or {}
    key = "custom_" + os.urandom(8).hex()
    saved[key] = {"name": name, "unit": unit, "kind": kind, "enabled": True,
                  "description": "Data entered by the administrator."}
    catalog_svc.set_setting("service_catalog", saved)
    return jsonify(ok=True, key=key, name=name)


@bp.post("/api/network/upload")
@admin_required
def net_upload():
    if not network.HAVE_GEO: return jsonify(error="geopandas is not installed. Run: pip install geopandas pyogrio"), 400
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(network.EXTS): return jsonify(error="Upload a .zip (Shapefile), .geojson or .gpkg file"), 400
    os.makedirs(network.NET_DIR, exist_ok=True)
    for old in os.listdir(network.NET_DIR): os.remove(os.path.join(network.NET_DIR, old))
    path = os.path.join(network.NET_DIR, "network" + os.path.splitext(f.filename)[1].lower()); f.save(path)
    try: meta = network.inspect(path)
    except Exception as e:
        os.remove(path); return jsonify(error=f"Could not read the file: {e}"), 400
    old = catalog_svc.get_setting("network", {})
    cfg = {"file": path, "filename": f.filename, "uploaded": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "tol": old.get("tol", 25), "min_cov": old.get("min_cov", 80),
           "speed_unit": old.get("speed_unit", "km/h"), **meta}
    guessed = network.guess(meta["columns"])
    for k in ("lanes", "speed", "capacity", "road_name"):
        cfg[k] = old.get(k) if old.get(k) in meta["columns"] else guessed.get(k, "")
    catalog_svc.set_setting("network", cfg)
    return jsonify(ok=True, features=meta["features"])


@bp.post("/api/network/config")
@admin_required
def net_config():
    cfg, j = catalog_svc.get_setting("network", {}), request.get_json(force=True) or {}
    if not cfg.get("file"): return jsonify(error="Upload a network file first"), 400
    for k in ("lanes", "speed", "capacity", "road_name"):
        v = j.get(k) or ""
        if v and v not in cfg["columns"]: return jsonify(error=f"Unknown column: {v}"), 400
        cfg[k] = v
    cfg["speed_unit"] = j.get("speed_unit") if j.get("speed_unit") in network.UNITS else "km/h"
    try: cfg["tol"], cfg["min_cov"] = max(5, min(200, float(j.get("tol") or 25))), max(10, min(100, float(j.get("min_cov") or 80)))
    except (TypeError, ValueError): return jsonify(error="Distance and coverage must be numbers"), 400
    catalog_svc.set_setting("network", cfg)
    return jsonify(ok=True, services=catalog_svc.net_services())
