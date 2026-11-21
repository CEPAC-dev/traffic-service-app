"""Matching a request's links to the road network and suggesting lane counts."""
import datetime as dt
import json
import network
from trafficlens.config import MAX_VOLUME_LANES
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.services import catalog as catalog_svc


def _network_lane_suggestion(lookup_result):
    """A usable lane count from a road-network lookup, or None (0 lanes in the file means 'unknown')."""
    try:
        lanes = int(((lookup_result or {}).get("values", {}).get("lanes") or {}).get("value"))
    except (TypeError, ValueError):
        return None
    return lanes if 1 <= lanes <= MAX_VOLUME_LANES else None


def _backfill_suggested_lanes(rid, path_json):
    """Requests created before lane suggestions existed: look the link up once. 0 = looked up, nothing usable."""
    cfg = catalog_svc.get_setting("network", {})
    if not (network.HAVE_GEO and cfg.get("file")):
        return None
    try:
        lanes = _network_lane_suggestion(network.lookup(cfg, json.loads(path_json or "[]")))
    except Exception as exc:  # a bad network file must never break the admin page
        flask_app.logger.warning("lane suggestion lookup failed for request %s: %s", rid, exc)
        return None
    db().execute("UPDATE volumes_workflows SET suggested_lanes=? WHERE request_id=?", (lanes or 0, rid))
    db().commit()
    return lanes


def apply_network(rid):
    """Fill the services the network can answer. Pending request: all answered -> Completed, some answered -> Processing."""
    cfg = catalog_svc.get_setting("network", {})
    if not (network.HAVE_GEO and cfg.get("file")): return None
    row = db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    d = catalog_svc.to_dict(row)
    res = network.lookup(cfg, d["path"])
    if not res: return None
    data, filled, now = d["data"], [], dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for s in d["services"]:
        if s in {"freeflow_speed", "freeflow_time"}:
            continue
        v, cur = res["values"].get(s), d["data"].get(s) or {}
        if v and not (cur.get("rows") and cur.get("source") != "network"):     # never overwrite what the admin entered
            data[s] = {"rows": [{"value": v["value"]}], "note": v["note"], "source": "network", "updated": now}
            filled.append(s)
    if "volumes" in d["services"]:
        suggested_lanes = _network_lane_suggestion(res)
        if suggested_lanes:
            db().execute("UPDATE volumes_workflows SET suggested_lanes=? WHERE request_id=?", (suggested_lanes, rid))
    status, reply, reply_at = row["status"], row["reply"], row["reply_at"]
    if status == "Pending" and filled:
        answered = [s for s in d["services"] if data.get(s, {}).get("rows")]
        names = lambda L: ", ".join(catalog_svc.service_catalog()[x]["name"] for x in L)
        if len(answered) == len(d["services"]):
            status, msg = "Completed", "Your results are ready. All the data you requested was taken from our road network."
        else:
            waiting = [s for s in d["services"] if s not in answered]
            status, msg = "Processing", f"The data for {names(answered)} is ready from our road network and you can view it now. We are preparing {names(waiting)} and will update this request when it is ready."
        if not reply: reply, reply_at = msg, now
    db().execute("UPDATE requests SET data=?, status=?, reply=?, reply_at=? WHERE id=?", (json.dumps(data), status, reply, reply_at, rid)); db().commit()
    return {"filled": filled, "coverage": res["coverage"], "status": status}
