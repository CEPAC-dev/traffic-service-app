"""Turning-movement studies: the client's entry form, the owner's view and the admin's review."""
import re
from collections import Counter
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for

from trafficlens import study_notifications as notes
from trafficlens.services import turning_movement as tm
from trafficlens.services import turning_movement_studies as studies
from trafficlens.services.turning_movement_forms import parse_study_form
from trafficlens.services.turning_movement_studies import StudyError

bp = Blueprint("turning", __name__, url_prefix="/turning-movement")
STATUSES = ("Pending", "Processing", "Completed", "Rejected")
PRESET_NAMES = tuple(tm.CLASS_PRESETS)


def _login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            return redirect(url_for("public.login"))
        return view(*args, **kwargs)
    return wrapper


def _admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("uid"):
            return redirect(url_for("public.login"))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def _is_admin():
    return session.get("role") == "admin"


def _load(study_id):
    """The study, or 404. Someone else's study looks the same as a missing one."""
    study = studies.get_study(study_id)
    if not study or (not _is_admin() and study["user_id"] != session["uid"]):
        abort(404)
    return study


def _can_edit(study):
    if study["status"] in studies.LOCKED:
        return False
    return _is_admin() or (study["user_id"] == session["uid"] and study["status"] in studies.EDITABLE_BY_OWNER)


# ------------------------------------------------------------ values shown in the form

def _blank_period(label=""):
    return {"label": label, "inflows": [""] * 4, "outflows": [""] * 4}


def _preset_classes(name):
    return [dict(tm.CLASS_PRESETS[name]) for _ in range(4)]


def _default_values():
    return {"title": "", "study_date": "", "preset": "typical", "legs": list(tm.FOUR_LEG_NAMES),
            "classes": _preset_classes("typical"), "periods": [_blank_period("AM")]}


def _values_from_study(study):
    preset = next((name for name in PRESET_NAMES if study["turn_classes"] == _preset_classes(name)), "custom")
    return {"title": study["title"], "study_date": study["study_date"] or "", "preset": preset,
            "legs": study["legs"], "classes": study["turn_classes"],
            "periods": [{"label": p["label"], "inflows": p["inflows"], "outflows": p["outflows"]}
                        for p in study["periods"]]}


def _values_from_form(form):
    """Whatever was typed, unchecked, so the form can be shown again after an error."""
    preset = form.get("preset") or "typical"
    classes = []
    for i in range(4):
        defaults = tm.CLASS_PRESETS.get(preset, tm.CLASS_PRESETS["typical"])
        classes.append({kind: form.get(f"class_{i}_{kind}", defaults[kind]) for kind in tm.TURN_TYPES})
    periods = [{"label": form.get(f"period_label_{k}", ""),
                "inflows": [form.get(f"in_{k}_{i}", "") for i in range(4)],
                "outflows": [form.get(f"out_{k}_{i}", "") for i in range(4)]}
               for k in form.getlist("period_ids") if str(k).isdigit()]
    return {"title": form.get("title", ""), "study_date": form.get("study_date", ""), "preset": preset,
            "legs": [form.get(f"leg_name_{i}") or d for i, d in enumerate(tm.FOUR_LEG_NAMES)],
            "classes": classes, "periods": periods or [_blank_period()]}


def _form_page(values, study=None, status=200):
    return render_template("turning_movement_form.html", values=values, study=study,
                           turn_types=tm.TURN_TYPES, turn_labels=tm.TURN_LABELS, presets=PRESET_NAMES), status


def _diagram(shown):
    """What the drawing may use. For a client the movements exist only after approval, so nothing leaks."""
    return {"legs": shown["legs"],
            "periods": [{"label": p["label"], "inflows": p["inflows"], "outflows": p["outflows"],
                         "movements": (p.get("result") or {}).get("movements")} for p in shown["periods"]]}


# ------------------------------------------------------------ pages

@bp.get("/")
@_login_required
def index():
    status = request.args.get("status")
    status = status if status in STATUSES else None
    everything = studies.list_studies(None if _is_admin() else session["uid"])
    counts = Counter(row["status"] for row in everything)
    rows = [row for row in everything if not status or row["status"] == status]
    return render_template("turning_movement_list.html", rows=rows, status=status, statuses=STATUSES,
                           counts=counts, total=len(everything), is_admin=_is_admin())


@bp.route("/new", methods=["GET", "POST"])
@_login_required
def new():
    if request.method == "GET":
        return _form_page(_default_values())
    try:
        study_id = studies.create_study(session["uid"], **parse_study_form(request.form))
    except StudyError as exc:
        flash(str(exc), "error")
        return _form_page(_values_from_form(request.form), status=400)
    notes.notify_admins_new_study(study_id, session.get("name"))
    flash("Sent for review. You will be notified when the results are ready.", "success")
    return redirect(url_for("turning.view", study_id=study_id))


@bp.get("/<int:study_id>")
@_login_required
def view(study_id):
    study = _load(study_id)
    admin = _is_admin()
    shown = study if admin else studies.client_view(study)
    return render_template("turning_movement_view.html", study=shown, diagram=_diagram(shown), is_admin=admin,
                           can_edit=_can_edit(study), turn_types=tm.TURN_TYPES, turn_labels=tm.TURN_LABELS)


def _released(study_id):
    """The study as this person may see it, or None while there are no results they may see.
    Exports always start from this, so a client can never download what is still hidden from them."""
    study = _load(study_id)
    shown = study if _is_admin() else studies.client_view(study)
    return shown if any(p.get("result") for p in shown["periods"]) else None


def _not_ready(study_id):
    flash("The results are not ready yet.", "error")
    return redirect(url_for("turning.view", study_id=study_id))


@bp.get("/<int:study_id>/export.xlsx")
@_login_required
def export_xlsx(study_id):
    shown = _released(study_id)
    if shown is None:
        return _not_ready(study_id)
    try:
        from trafficlens.services import turning_movement_export as export
    except ImportError:
        flash("Excel export needs the openpyxl package. Run: pip install openpyxl", "error")
        return redirect(url_for("turning.view", study_id=study_id))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    data = export.build_workbook(shown, stamp, include_internal=_is_admin())
    slug = re.sub(r"[^A-Za-z0-9]+", "-", shown["title"]).strip("-")[:40] or "study"
    import io
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f"TM-{study_id}-{slug}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.get("/<int:study_id>/report")
@_login_required
def report(study_id):
    shown = _released(study_id)
    if shown is None:
        return _not_ready(study_id)
    return render_template("turning_movement_report.html", study=shown, diagram=_diagram(shown), is_admin=_is_admin(),
                           turn_types=tm.TURN_TYPES, turn_labels=tm.TURN_LABELS,
                           report_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"))


@bp.route("/<int:study_id>/edit", methods=["GET", "POST"])
@_login_required
def edit(study_id):
    study = _load(study_id)
    if not _can_edit(study):
        flash("This study can no longer be edited.", "error")
        return redirect(url_for("turning.view", study_id=study_id))
    if request.method == "GET":
        return _form_page(_values_from_study(study), study=study)
    try:
        args = parse_study_form(request.form)
        studies.update_study(study_id, session["uid"], _is_admin(), title=args["title"], periods=args["periods"],
                             turn_classes=args["turn_classes"], legs=args["legs"], study_date=args["study_date"],
                             change_date=True)
    except StudyError as exc:
        flash(str(exc), "error")
        return _form_page(_values_from_form(request.form), study=study, status=400)
    if not _is_admin():
        notes.notify_admins_new_study(study_id, session.get("name"), resubmitted=study["status"] == "Rejected")
    flash("Changes saved.", "success")
    return redirect(url_for("turning.view", study_id=study_id))


def _admin_action(study_id, action, success, notify=None):
    study = _load(study_id)
    try:
        action(study)
    except StudyError as exc:
        flash(str(exc), "error")
        return redirect(url_for("turning.view", study_id=study_id))
    if notify:
        notes.notify_owner(study["user_id"], study_id, *notify(study))
    flash(success, "success")
    return redirect(url_for("turning.view", study_id=study_id))


@bp.post("/<int:study_id>/approve")
@_admin_required
def approve(study_id):
    return _admin_action(
        study_id,
        lambda s: studies.approve_study(study_id, session["uid"], request.form.get("reply"),
                                        acknowledge_warnings=request.form.get("acknowledge") == "1"),
        "Approved. The client can see the results now.",
        lambda s: ("tm_result", "Turning-movement results are ready", f'The results of "{s["title"]}" are ready.'))


@bp.post("/<int:study_id>/reject")
@_admin_required
def reject(study_id):
    return _admin_action(
        study_id, lambda s: studies.reject_study(study_id, session["uid"], request.form.get("reply")),
        "Rejected. The client can fix the counts and send the study again.",
        lambda s: ("tm_result", "Turning-movement study rejected", f'"{s["title"]}" needs changes. Open it to read why.'))


@bp.post("/<int:study_id>/reopen")
@_admin_required
def reopen(study_id):
    return _admin_action(study_id, lambda s: studies.reopen_study(study_id, session["uid"]),
                         "Reopened. The client cannot see the results until you approve again.")


@bp.post("/<int:study_id>/delete")
@_admin_required
def delete(study_id):
    _load(study_id)
    try:
        studies.delete_study(study_id)
    except StudyError as exc:
        flash(str(exc), "error")
        return redirect(url_for("turning.view", study_id=study_id))
    notes.forget_study(study_id)
    flash("Study deleted.", "success")
    return redirect(url_for("turning.index"))
