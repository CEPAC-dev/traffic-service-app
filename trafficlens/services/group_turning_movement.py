"""Turning movements of a grouped order whose 8 links are the entries and exits of a four-leg intersection.

The client marks every link as an entry or an exit. Links come in pairs, one pair per leg, and the legs
are always East, North, West, South in that order: links 1 and 2 are East, 3 and 4 North, 5 and 6 West,
7 and 8 South. By default the first link of a pair is the entry and the second the exit; the client may
swap a pair. The Traffic volumes results of the 8 links (summed over the hours of the period) become the
inflows and outflows of the four legs, and the ratings the client chose decide how they turn.

Only a Several-links-one-period order with exactly 8 links and Traffic volumes can carry the option.
"""
import json

from trafficlens.db import db
from trafficlens.services import audit as audit_svc
from trafficlens.services import common as common_svc
from trafficlens.services import turning_movement as tm
from trafficlens.services.turning_movement_studies import StudyError, _clean_turn_classes

LINK_COUNT = 8
LEG_NAMES = ("East", "North", "West", "South")
ROLES = ("in", "out")
PENDING, PROCESSING, COMPLETED = "Pending", "Processing", "Completed"
READY_VOLUME_STATUSES = ("ready_for_review", "sent")


class GroupTurningError(ValueError):
    """The option or action is not possible; the message is safe to show to the user."""


def default_roles():
    """Entry, exit, entry, exit, ... in the order the links were added."""
    return [ROLES[i % 2] for i in range(LINK_COUNT)]


def clean_spec(raw, mode, link_count, services):
    """Validate what the client sent with the order. Returns ``{"roles": [...], "turn_classes": [...]}``."""
    if mode != "multi_link" or link_count != LINK_COUNT:
        raise GroupTurningError(f"Turning movements need an order of exactly {LINK_COUNT} links for one period.")
    if "volumes" not in services:
        raise GroupTurningError("Turning movements are built from Traffic volumes: select Traffic volumes too.")
    raw = raw if isinstance(raw, dict) else {}
    roles = raw.get("roles")
    roles = default_roles() if roles is None else roles
    if not isinstance(roles, (list, tuple)) or len(roles) != LINK_COUNT or any(r not in ROLES for r in roles):
        raise GroupTurningError("Mark every link as an entry or an exit.")
    for leg, name in enumerate(LEG_NAMES):
        if sorted(roles[2 * leg:2 * leg + 2]) != sorted(ROLES):
            raise GroupTurningError(
                f"{name} needs one entry and one exit (Links {2 * leg + 1} and {2 * leg + 2}).")
    try:
        classes = _clean_turn_classes(raw.get("turn_classes"))
    except StudyError as exc:
        raise GroupTurningError(str(exc)) from None
    matrix = tm.classes_from_turn_classes(classes)
    for leg, name in enumerate(LEG_NAMES):
        if not any(matrix[leg]):
            raise GroupTurningError(f"{name} has every turn marked not allowed: allow at least one turn from it.")
        if not any(row[leg] for row in matrix):
            raise GroupTurningError(f"No turn leads to {name}: allow at least one turn into it.")
    return {"roles": list(roles), "turn_classes": classes}


def create(group_id, spec):
    now = common_svc._timestamp_now()
    db().execute(
        "INSERT INTO group_turning_movement(group_id,roles_json,classes_json,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)", (group_id, json.dumps(spec["roles"]), json.dumps(spec["turn_classes"]), PENDING, now, now))


def get(group_id):
    """The stored option of an order, or None when the order did not ask for turning movements."""
    row = db().execute("SELECT * FROM group_turning_movement WHERE group_id=?", (group_id,)).fetchone()
    if not row:
        return None
    return {"group_id": row["group_id"], "roles": json.loads(row["roles_json"]),
            "turn_classes": json.loads(row["classes_json"]), "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"], "approved_at": row["approved_at"]}


def _link_volumes(children):
    """Per link (in the order they were added): the request, the volumes status and the summed volume."""
    links = {}
    for child in children:
        links.setdefault(child["link_no"], child)
    rows = []
    for number in sorted(links):
        child = links[number]
        flow = db().execute("SELECT status,result_json FROM volumes_workflows WHERE request_id=?", (child["id"],)).fetchone()
        status = flow["status"] if flow else "missing"
        volume = None
        if flow and flow["status"] in READY_VOLUME_STATUSES and flow["result_json"]:
            hourly = (json.loads(flow["result_json"]) or {}).get("hourly") or []
            if hourly:
                volume = sum(float(item["value"]) for item in hourly)
        rows.append({"link_no": number, "request_id": child["id"], "volume_status": status, "volume": volume})
    return rows


def legs_overview(option, children):
    """What the admin sees before calculating: every leg with its entry and exit link and their volumes."""
    links = _link_volumes(children)
    legs = []
    for leg, name in enumerate(LEG_NAMES):
        pair = links[2 * leg:2 * leg + 2]
        entry = next((x for x, role in zip(pair, option["roles"][2 * leg:2 * leg + 2]) if role == "in"), None)
        exit_ = next((x for x, role in zip(pair, option["roles"][2 * leg:2 * leg + 2]) if role == "out"), None)
        legs.append({"name": name, "entry": entry, "exit": exit_})
    ready = len(links) == LINK_COUNT and all(x["volume"] is not None for x in links)
    approved = all(x["volume_status"] == "sent" for x in links)
    return {"legs": legs, "ready": ready, "volumes_approved": ready and approved}


def calculate(group_id, children, turn_classes=None):
    """Run the estimate from the order's volumes, store it and return the stored option."""
    option = get(group_id)
    if not option:
        raise GroupTurningError("This order did not ask for turning movements.")
    overview = legs_overview(option, children)
    if not overview["ready"]:
        raise GroupTurningError("Turning movements need the Traffic volumes of all 8 links to be calculated first.")
    classes = option["turn_classes"] if turn_classes is None else _clean_classes_for_group(turn_classes)
    inflows = [leg["entry"]["volume"] for leg in overview["legs"]]
    outflows = [leg["exit"]["volume"] for leg in overview["legs"]]
    try:
        result = tm.estimate_turning_movements(inflows, outflows, tm.classes_from_turn_classes(classes), legs=LEG_NAMES)
    except tm.TurningMovementError as exc:
        raise GroupTurningError(str(exc)) from None
    stored = result.to_dict()
    stored["movements"] = tm.movements_by_leg(result)
    stored["volumes_approved"] = overview["volumes_approved"]
    stored["links"] = [{"leg": leg["name"], "entry_link": leg["entry"]["link_no"], "exit_link": leg["exit"]["link_no"]}
                       for leg in overview["legs"]]
    now = common_svc._timestamp_now()
    db().execute("UPDATE group_turning_movement SET classes_json=?,result_json=?,status=?,approved_at=NULL,updated_at=? WHERE group_id=?",
                 (json.dumps(classes), json.dumps(stored), PROCESSING, now, group_id))
    db().commit()
    audit_svc.audit("group_turning_movement_calculated", None, group_id=group_id)
    return get(group_id)


def _clean_classes_for_group(turn_classes):
    try:
        return _clean_turn_classes(turn_classes)
    except StudyError as exc:
        raise GroupTurningError(str(exc)) from None


def approve(group_id):
    option = get(group_id)
    if not option:
        raise GroupTurningError("This order did not ask for turning movements.")
    if not option["result"]:
        raise GroupTurningError("Calculate the turning movements before approving them.")
    now = common_svc._timestamp_now()
    db().execute("UPDATE group_turning_movement SET status=?,approved_at=?,updated_at=? WHERE group_id=?",
                 (COMPLETED, now, now, group_id))
    db().commit()
    audit_svc.audit("group_turning_movement_approved", None, group_id=group_id)
    return get(group_id)


def client_view(option):
    """What the owner sees: the inputs always, the estimate only once it is approved. No warnings, no spread."""
    if not option:
        return None
    released = option["status"] == COMPLETED and option["result"]
    view = {"status": option["status"], "turn_classes": option["turn_classes"], "roles": option["roles"],
            "released": bool(released), "result": None}
    if released:
        view["result"] = {key: option["result"][key] for key in ("legs", "inflows", "outflows", "movements", "rounded", "balanced_by_scaling")}
    return view
