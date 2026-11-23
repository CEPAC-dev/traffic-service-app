"""Turning-movement studies: saving, reviewing and releasing them.

A study is one four-leg intersection with one or more periods (for example AM / MD / PM).
Each period holds what enters and leaves every leg. The estimate is computed whenever the
inputs are saved and is stored next to them, but the client only sees it after an admin
approves the study.

Status flow (same words as the rest of the site):
    Pending     the client (or an admin) just entered it
    Processing  an admin is working on it; the client can no longer edit
    Completed   an admin approved it; results are visible to the client; locked
    Rejected    an admin turned it down with a reply; the owner may fix it and resubmit

Every function here is one unit of work: it validates, writes, and commits.
"""
import datetime as dt
import json

from trafficlens.db import db
from trafficlens.services import audit as audit_svc
from trafficlens.services import common as common_svc
from trafficlens.services import turning_movement as tm

LEG_COUNT = 4
DEFAULT_LEGS = tm.FOUR_LEG_NAMES
MAX_PERIODS = 24
MAX_TITLE = 120
MAX_NAME = 40
METHOD = "ordered"         # the estimation method that produced stored results (older studies say "legacy")
EDITABLE_BY_OWNER = ("Pending", "Rejected")
LOCKED = ("Completed",)


class StudyError(ValueError):
    """The request cannot be carried out; the message is safe to show to the user."""


# ---------------------------------------------------------------- validation

def _text(value, what, limit, required=True):
    text = " ".join(str(value or "").split())
    if required and not text:
        raise StudyError(f"{what} is required.")
    if len(text) > limit:
        raise StudyError(f"{what} must be at most {limit} characters.")
    return text


def _clean_legs(legs):
    legs = list(DEFAULT_LEGS if legs is None else legs)
    if len(legs) != LEG_COUNT:
        raise StudyError(f"Give exactly {LEG_COUNT} leg names.")
    names = [_text(name, "A leg name", MAX_NAME) for name in legs]
    if len({name.lower() for name in names}) != LEG_COUNT:
        raise StudyError("Leg names must be different from each other.")
    return names


def _clean_turn_classes(turn_classes):
    if turn_classes is None:
        turn_classes = [dict(tm.CLASS_PRESETS["typical"]) for _ in range(LEG_COUNT)]
    if not isinstance(turn_classes, (list, tuple)) or len(turn_classes) != LEG_COUNT:
        raise StudyError("Give the turn classes of all four legs.")
    cleaned = []
    for leg in turn_classes:
        if not isinstance(leg, dict) or set(leg) != set(tm.TURN_TYPES):
            raise StudyError("Each leg needs a class for U-turn, left, through and right.")
        cleaned.append({kind: leg[kind] for kind in tm.TURN_TYPES})
    try:
        tm.classes_from_turn_classes(cleaned)
    except tm.TurningMovementError as exc:
        raise StudyError(str(exc)) from None
    return cleaned


def _normal_number(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _clean_periods(periods, legs, turn_classes):
    if not isinstance(periods, (list, tuple)) or not periods:
        raise StudyError("Add at least one period.")
    if len(periods) > MAX_PERIODS:
        raise StudyError(f"A study can have at most {MAX_PERIODS} periods.")
    classes = tm.classes_from_turn_classes(turn_classes)
    cleaned, seen = [], set()
    for number, period in enumerate(periods, start=1):
        if not isinstance(period, dict):
            raise StudyError(f"Period {number} is not valid.")
        label = _text(period.get("label"), f"The name of period {number}", MAX_NAME)
        if label.lower() in seen:
            raise StudyError(f'Two periods are both called "{label}".')
        seen.add(label.lower())
        raw_in, raw_out = period.get("inflows"), period.get("outflows")
        if not isinstance(raw_in, (list, tuple)) or not isinstance(raw_out, (list, tuple)):
            raise StudyError(f'Period "{label}": enter the inflow and outflow of every leg.')
        inflows = [_normal_number(v) for v in raw_in]
        outflows = [_normal_number(v) for v in raw_out]
        try:
            result = tm.estimate_turning_movements(inflows, outflows, classes, legs=legs)
        except tm.TurningMovementError as exc:
            raise StudyError(f'Period "{label}": {exc}') from None
        stored = result.to_dict()
        stored["method"] = METHOD
        stored["movements"] = tm.movements_by_leg(result)
        cleaned.append({"label": label, "inflows": inflows, "outflows": outflows,
                        "source": period.get("source"), "result": stored})
    return cleaned


def _clean_date(value):
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise StudyError("Choose a valid study date.") from None


# ---------------------------------------------------------------- reading

def _period_dict(row):
    return {"id": row["id"], "label": row["label"], "sort_order": row["sort_order"],
            "inflows": json.loads(row["inflows_json"]), "outflows": json.loads(row["outflows_json"]),
            "source": json.loads(row["source_json"]) if row["source_json"] else None,
            "result": json.loads(row["result_json"]) if row["result_json"] else None}


def get_study(study_id):
    """Everything about a study, including drafts and warnings (admin view). None when missing."""
    row = db().execute(
        "SELECT s.*, u.name AS owner_name, u.email AS owner_email FROM turning_movement_studies s "
        "JOIN users u ON u.id=s.user_id WHERE s.id=?", (study_id,)).fetchone()
    if not row:
        return None
    periods = db().execute(
        "SELECT * FROM turning_movement_periods WHERE study_id=? ORDER BY sort_order,id", (study_id,)).fetchall()
    study = {key: row[key] for key in row.keys() if not key.endswith("_json")}
    study["legs"] = json.loads(row["legs_json"])
    study["turn_classes"] = json.loads(row["classes_json"])
    study["periods"] = [_period_dict(p) for p in periods]
    study["warning_count"] = sum(len((p["result"] or {}).get("warnings") or []) for p in study["periods"])
    return study


def client_view(study):
    """What the owner may see: inputs always; estimates and warnings only once the study is Completed."""
    released = study["status"] == "Completed"
    view = {key: study[key] for key in ("id", "title", "study_date", "mode", "status", "legs", "turn_classes",
                                        "created_at", "updated_at", "sent_at")}
    view["reply"] = study["reply"] if study["status"] in ("Completed", "Rejected") else None
    view["results_visible"] = released
    view["periods"] = [
        {"label": p["label"], "inflows": p["inflows"], "outflows": p["outflows"],
         "result": {k: p["result"][k] for k in ("matrix", "rounded", "movements", "inflows", "outflows", "legs",
                                                "balanced_by_scaling", "method")} if released and p["result"] else None}
        for p in study["periods"]]
    return view


def list_studies(user_id=None, status=None):
    sql = ("SELECT s.id,s.title,s.status,s.mode,s.study_date,s.created_at,s.updated_at,s.user_id,u.name AS owner_name,"
           "(SELECT COUNT(*) FROM turning_movement_periods p WHERE p.study_id=s.id) AS period_count "
           "FROM turning_movement_studies s JOIN users u ON u.id=s.user_id")
    clauses, args = [], []
    if user_id is not None:
        clauses.append("s.user_id=?"); args.append(user_id)
    if status:
        clauses.append("s.status=?"); args.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return [dict(row) for row in db().execute(sql + " ORDER BY s.id DESC", args).fetchall()]


# ---------------------------------------------------------------- writing

def _write_periods(study_id, periods):
    db().execute("DELETE FROM turning_movement_periods WHERE study_id=?", (study_id,))
    for order, period in enumerate(periods):
        db().execute(
            "INSERT INTO turning_movement_periods(study_id,label,sort_order,inflows_json,outflows_json,source_json,result_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (study_id, period["label"], order, json.dumps(period["inflows"]), json.dumps(period["outflows"]),
             json.dumps(period["source"]) if period["source"] else None, json.dumps(period["result"])))


def create_study(user_id, title, periods, turn_classes=None, legs=None, study_date=None, mode="manual"):
    """Save a new study as Pending and return its id. Nothing is saved when anything is invalid."""
    if mode != "manual":
        raise StudyError("Only manually entered studies are available for now.")
    title = _text(title, "The title", MAX_TITLE)
    legs = _clean_legs(legs)
    turn_classes = _clean_turn_classes(turn_classes)
    cleaned = _clean_periods(periods, legs, turn_classes)
    study_date = _clean_date(study_date)
    now = common_svc._timestamp_now()
    try:
        cursor = db().execute(
            "INSERT INTO turning_movement_studies(user_id,title,study_date,mode,legs_json,classes_json,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'Pending',?,?)",
            (user_id, title, study_date, mode, json.dumps(legs, ensure_ascii=False), json.dumps(turn_classes), now, now))
        _write_periods(cursor.lastrowid, cleaned)
        audit_svc.audit("turning_movement_created", None, study_id=cursor.lastrowid, periods=len(cleaned))
        db().commit()
    except Exception:
        db().rollback()
        raise
    return cursor.lastrowid


def _require_study(study_id):
    study = get_study(study_id)
    if not study:
        raise StudyError("This study does not exist.")
    return study


def update_study(study_id, actor_id, actor_is_admin, title=None, periods=None, turn_classes=None, legs=None,
                 study_date=None, change_date=False):
    """Change a study's inputs and recompute every estimate.

    Admins can edit anything that is not Completed; an admin edit moves a Pending study to Processing.
    The owner can edit only while it is Pending or Rejected (a Rejected study goes back to Pending).
    """
    study = _require_study(study_id)
    if study["status"] in LOCKED:
        raise StudyError("A completed study cannot be edited. Reopen it first.")
    if not actor_is_admin and not (study["user_id"] == actor_id and study["status"] in EDITABLE_BY_OWNER):
        raise StudyError("This study can no longer be edited.")
    new_title = _text(title, "The title", MAX_TITLE) if title is not None else study["title"]
    new_legs = _clean_legs(legs if legs is not None else study["legs"])
    new_classes = _clean_turn_classes(turn_classes if turn_classes is not None else study["turn_classes"])
    new_periods = _clean_periods(
        periods if periods is not None else [{"label": p["label"], "inflows": p["inflows"], "outflows": p["outflows"],
                                              "source": p["source"]} for p in study["periods"]],
        new_legs, new_classes)
    new_date = _clean_date(study_date) if change_date else study["study_date"]
    if actor_is_admin:
        new_status = "Processing"            # an admin who touches it is working on it
    else:
        new_status = "Pending"               # the owner fixed a rejected study: back in the queue
    try:
        changed = db().execute(
            "UPDATE turning_movement_studies SET title=?,study_date=?,legs_json=?,classes_json=?,status=?,updated_at=? "
            "WHERE id=? AND status=?",
            (new_title, new_date, json.dumps(new_legs, ensure_ascii=False), json.dumps(new_classes), new_status,
             common_svc._timestamp_now(), study_id, study["status"])).rowcount
        if not changed:
            raise StudyError("The study changed while you were editing it. Reload and try again.")
        _write_periods(study_id, new_periods)
        audit_svc.audit("turning_movement_edited", None, study_id=study_id, by_admin=bool(actor_is_admin))
        db().commit()
    except Exception:
        db().rollback()
        raise


def _transition(study_id, allowed_from, set_clause, args, audit_action, **audit_details):
    placeholders = ",".join("?" for _ in allowed_from)
    changed = db().execute(
        f"UPDATE turning_movement_studies SET {set_clause} WHERE id=? AND status IN ({placeholders})",
        (*args, study_id, *allowed_from)).rowcount
    if not changed:
        db().rollback()
        raise StudyError("The study is no longer in a state where this can be done. Reload and check its status.")
    audit_svc.audit(audit_action, None, study_id=study_id, **audit_details)
    db().commit()


def approve_study(study_id, admin_id, reply=None, acknowledge_warnings=False):
    """Release the results to the client. Warnings must be acknowledged explicitly."""
    study = _require_study(study_id)
    if study["status"] == "Completed":
        raise StudyError("This study has already been approved.")
    if not study["periods"] or any(not p["result"] for p in study["periods"]):
        raise StudyError("Every period needs a computed result before approval.")
    if study["warning_count"] and not acknowledge_warnings:
        raise StudyError(f"This study has {study['warning_count']} warning(s). Review them and confirm before approving.")
    now = common_svc._timestamp_now()
    reply = _text(reply, "The reply", 2000, required=False) or None
    _transition(study_id, ("Pending", "Processing", "Rejected"),
                "status='Completed',reviewed_by=?,reviewed_at=?,sent_at=?,reply=?,reply_at=?,updated_at=?",
                (admin_id, now, now, reply, now if reply else None, now),
                "turning_movement_approved", warnings=study["warning_count"])


def reject_study(study_id, admin_id, reply):
    reply = _text(reply, "The reason", 2000)
    now = common_svc._timestamp_now()
    _transition(study_id, ("Pending", "Processing"),
                "status='Rejected',reviewed_by=?,reviewed_at=?,reply=?,reply_at=?,updated_at=?",
                (admin_id, now, reply, now, now), "turning_movement_rejected")


def reopen_study(study_id, admin_id):
    """Take an approved study back for corrections: results are hidden from the client again."""
    now = common_svc._timestamp_now()
    _transition(study_id, ("Completed",), "status='Processing',sent_at=NULL,updated_at=?", (now,),
                "turning_movement_reopened", by=admin_id)


def delete_study(study_id):
    if not db().execute("SELECT 1 FROM turning_movement_studies WHERE id=?", (study_id,)).fetchone():
        raise StudyError("This study does not exist.")
    db().execute("DELETE FROM turning_movement_periods WHERE study_id=?", (study_id,))
    db().execute("DELETE FROM turning_movement_studies WHERE id=?", (study_id,))
    audit_svc.audit("turning_movement_deleted", None, study_id=study_id)
    db().commit()
