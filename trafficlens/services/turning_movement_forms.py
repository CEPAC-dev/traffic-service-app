"""Turns the fields of the turning-movement HTML form into the arguments of
turning_movement_studies.create_study / update_study. Pure functions: no database, no Flask.

Field names the form must use (k = 0,1,2... one per period, i = 0..3 one per leg):
    title, study_date, preset                      ("typical" | "no_u_turns" | "custom")
    leg_name_i                                     leg names (optional, default E N W S)
    class_i_<turn>                                 0-3, only read when preset == "custom"
    period_ids                                     repeated hidden field listing the k values in order
    period_label_k, in_k_i, out_k_i                period name, inflow and outflow of each leg
"""
from trafficlens.services import turning_movement as tm
from trafficlens.services.turning_movement_studies import StudyError

LEG_COUNT = 4


def _number(raw, what):
    text = str(raw if raw is not None else "").strip().replace(",", "")
    if not text:
        raise StudyError(f"{what} is empty.")
    try:
        value = float(text)
    except ValueError:
        raise StudyError(f"{what} must be a number.") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise StudyError(f"{what} must be a finite number.")
    return int(value) if value.is_integer() else value


def _turn_classes(form, preset):
    if preset in tm.CLASS_PRESETS:
        return [dict(tm.CLASS_PRESETS[preset]) for _ in range(LEG_COUNT)]
    if preset != "custom":
        raise StudyError("Choose a class preset or enter the classes yourself.")
    classes = []
    for i in range(LEG_COUNT):
        leg = {}
        for kind in tm.TURN_TYPES:
            raw = str(form.get(f"class_{i}_{kind}", "")).strip()
            if raw not in ("0", "1", "2", "3"):
                raise StudyError(f"Leg {i + 1}: the class of the {kind.replace('_', '-')} turn must be 0, 1, 2 or 3.")
            leg[kind] = int(raw)
        classes.append(leg)
    return classes


def parse_study_form(form, max_periods=24):
    """Returns the keyword arguments for create_study / update_study (without user ids)."""
    legs = [(form.get(f"leg_name_{i}") or default).strip() or default
            for i, default in enumerate(tm.FOUR_LEG_NAMES)]
    ids = [value for value in form.getlist("period_ids") if str(value).isdigit()]
    if not ids:
        raise StudyError("Add at least one period.")
    if len(ids) > max_periods:
        raise StudyError(f"A study can have at most {max_periods} periods.")
    periods = []
    for k in ids:
        label = (form.get(f"period_label_{k}") or "").strip()
        name = f'Period "{label}"' if label else f"Period {len(periods) + 1}"
        periods.append({
            "label": label,
            "inflows": [_number(form.get(f"in_{k}_{i}"), f"{name}: inflow of leg {i + 1}") for i in range(LEG_COUNT)],
            "outflows": [_number(form.get(f"out_{k}_{i}"), f"{name}: outflow of leg {i + 1}") for i in range(LEG_COUNT)],
        })
    return {
        "title": form.get("title", ""),
        "study_date": (form.get("study_date") or "").strip() or None,
        "legs": legs,
        "turn_classes": _turn_classes(form, (form.get("preset") or "typical").strip()),
        "periods": periods,
    }
