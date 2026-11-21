"""The service catalog (lanes, speed, travel...), site settings, and conversion of request rows to dictionaries."""
import json
import network
from trafficlens.config import FORMS, HIDDEN_SERVICES_BY_DEFAULT, SERVICES, SERVICE_DEFAULTS, _f
from trafficlens.db import db
from trafficlens.services import common as common_svc


def _service_ready(service, entry):
    entry = entry or {}
    if entry.get("rows"):
        return True
    if service == "volumes":
        return False
    # A Directions API placeholder note ("awaiting administrator review") is not a result.
    if entry.get("source") == "outscraper":
        return False
    return bool(entry.get("note"))


def _all_requested_services_ready(requested_services, data):
    return bool(requested_services) and all(
        _service_ready(service, data.get(service)) for service in requested_services
    )


def to_dict(r):
    d = dict(r)
    for k in ("services", "bbox", "coords"): d[k] = json.loads(d[k])
    d["services"] = [s for s in d["services"] if s in service_catalog()]
    if len(d["coords"]) > 2: d["coords"] = [d["coords"][0], d["coords"][-1]]
    d["data"] = json.loads(d.get("data") or "{}")
    d["path"] = json.loads(d.get("path") or "null") or d["coords"]   # road shape; falls back to a straight line
    d["hours"] = json.loads(d.get("hours") or "[]")
    d["point_c"] = json.loads(d["point_c"]) if d.get("point_c") else None
    d["hours_label"] = common_svc.fmt_hours(d["hours"])
    d["time_range_label"] = (
        f"{d['start_time']}–{d['end_time']}"
        if d.get("start_time") and d.get("end_time") else d["hours_label"]
    )
    d.pop("results", None)
    filled = [s for s in d["services"] if d["data"].get(s, {}).get("rows") or d["data"].get(s, {}).get("note")]
    d["progress"] = [len(filled), len(d["services"])]
    return d


def clean_rows(service, rows):
    form = forms_catalog().get(service)
    if not form: return []
    hourly = form["kind"] == "hourly"; out = []
    for row in (rows or [])[: 24 if hourly else 1]:
        if not isinstance(row, dict): continue
        if form.get("value_type") == "mixed":
            value = row.get("value")
            if value is None or not str(value).strip(): continue
            c = {"value": str(value).strip()[:200]}
        else:
            try: c = {"value": float(row.get("value"))}
            except (TypeError, ValueError): continue
        if hourly:
            try: h = int(row.get("hour"))
            except (TypeError, ValueError): continue
            if not 0 <= h <= 23: continue
            c["hour"] = h
        out.append(c)
    return out


def svinfo():
    over = {r["key"]: r["description"] for r in db().execute("SELECT key, description FROM service_info")}
    catalog, info = service_catalog(), {}
    for key, service in catalog.items():
        default = SERVICE_DEFAULTS.get(key, {"icon": "\U0001F9E9", "desc": service.get("description", "Data provided by the administrator.")})
        info[key] = {"icon": default["icon"], "desc": over.get(key) or default["desc"]}
    return info


def service_catalog():
    saved = get_setting("service_catalog", {}) or {}
    catalog = {}
    for key, name in SERVICES.items():
        config = saved.get(key, {})
        form = FORMS[key]
        catalog[key] = {
            "name": name, "unit": form["unit"], "kind": form["kind"],
            "value_type": "number", "custom": False,
            "enabled": config.get("enabled", key not in HIDDEN_SERVICES_BY_DEFAULT),
            "description": SERVICE_DEFAULTS[key]["desc"],
        }
    for key, config in saved.items():
        if not key.startswith("custom_") or not isinstance(config, dict): continue
        catalog[key] = {
            "name": str(config.get("name") or "Custom service"),
            "unit": str(config.get("unit") or ""),
            "kind": config.get("kind") if config.get("kind") in ("static", "hourly") else "static",
            "value_type": "mixed", "custom": True,
            "enabled": bool(config.get("enabled", True)),
            "description": str(config.get("description") or "Data entered by the administrator."),
        }
    return catalog


def active_services():
    return {key: service["name"] for key, service in service_catalog().items() if service["enabled"]}


def all_service_names():
    return {key: service["name"] for key, service in service_catalog().items()}


def forms_catalog():
    forms = {key: dict(form) for key, form in FORMS.items()}
    for key, service in service_catalog().items():
        if service["custom"]:
            forms[key] = _f(service["kind"], service["name"], service["unit"], f"Enter {service['name']} data for this link.")
            forms[key]["value_type"] = "mixed"
        else:
            forms[key]["value_type"] = "number"
    return forms


def get_setting(k, default=None):
    r = db().execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return json.loads(r["value"]) if r else default


def set_setting(k, v):
    db().execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
    db().commit()


def net_services():
    """Services the static road network can answer without a date/time-specific API request."""
    cfg = get_setting("network", {})
    if not (network.HAVE_GEO and cfg.get("file")): return []
    out = ["length"]
    if cfg.get("lanes"): out.append("lanes")
    if cfg.get("capacity"): out.append("capacity")
    return out
