"""Turning-movement estimation for an intersection.

Given what enters and leaves each leg, and how likely each turn is, estimate how many
vehicles make every turn. This is the original TM MATLAB method, ported one-to-one:

  1. If total inflow and total outflow differ, scale both to their average.
  2. Share each leg's inflow across its turns in proportion to the class of each turn
     (1 = low, 2 = medium, 3 = high; 0 = the turn is not allowed), never giving a turn
     more than the outflow of the leg it goes to.
  3. Scale every column so the arrivals at a leg match its outflow.
  4. Scale every row so the departures from a leg match its inflow.

Step 4 comes last, so rows add up to the inflows exactly while columns only approximately
match the outflows (the result reports how far off they are). Nothing here touches Flask
or the database: the same function serves a manually entered intersection and one fed by
Volumes results.
"""
import math
import statistics
from dataclasses import dataclass

CLASS_LOW, CLASS_MEDIUM, CLASS_HIGH = 1, 2, 3
CLASS_LABELS = {CLASS_LOW: "Low", CLASS_MEDIUM: "Medium", CLASS_HIGH: "High"}
FOUR_LEG_NAMES = ("E", "N", "W", "S")   # counter-clockwise from east; row = from, column = to

# Turn types for a four-leg intersection with traffic driving on the right. Legs are listed
# counter-clockwise, so for a vehicle arriving from leg i: leg i again is a U-turn, the next leg
# counter-clockwise is a right turn, the one after is straight on, the last one is a left turn.
TURN_TYPES = ("u_turn", "left", "through", "right")
TURN_LABELS = {"u_turn": "U-turn", "left": "Left", "through": "Through", "right": "Right"}
_TURN_BY_STEP = {0: "u_turn", 1: "right", 2: "through", 3: "left"}   # step = (to - from) % 4

# Starting values for the class of each turn (0 = not allowed, 1 low, 2 medium, 3 high).
CLASS_PRESETS = {
    "typical": {"u_turn": CLASS_LOW, "left": CLASS_MEDIUM, "through": CLASS_HIGH, "right": CLASS_MEDIUM},
    "no_u_turns": {"u_turn": 0, "left": CLASS_MEDIUM, "through": CLASS_HIGH, "right": CLASS_MEDIUM},
}

IMBALANCE_WARNING = 0.10     # inflow and outflow totals more than 10% apart
COLUMN_GAP_WARNING = 0.05    # arrivals more than 5% away from a leg's outflow


class TurningMovementError(ValueError):
    """The input cannot be turned into a turning-movement estimate."""


@dataclass(frozen=True)
class TurningMovementResult:
    legs: tuple
    original_inflows: tuple
    original_outflows: tuple
    inflows: tuple            # after balancing
    outflows: tuple           # after balancing
    balanced_by_scaling: bool
    imbalance: float          # |in - out| / max(in, out) before balancing
    matrix: tuple             # exact values, row = from leg, column = to leg
    rounded: tuple            # same, rounded to whole vehicles
    column_gaps: tuple        # arrivals at each leg minus its (balanced) outflow
    row_std: tuple            # spread of each leg's turns (not an error estimate)
    warnings: tuple

    def to_dict(self):
        """A plain, JSON-friendly copy (tuples become lists)."""
        return {
            "legs": list(self.legs), "original_inflows": list(self.original_inflows),
            "original_outflows": list(self.original_outflows), "inflows": list(self.inflows),
            "outflows": list(self.outflows), "balanced_by_scaling": self.balanced_by_scaling,
            "imbalance": self.imbalance, "matrix": [list(row) for row in self.matrix],
            "rounded": [list(row) for row in self.rounded], "column_gaps": list(self.column_gaps),
            "row_std": list(self.row_std), "warnings": list(self.warnings),
        }


def _round_half_up(value):
    return int(math.floor(value + 0.5))


def _number(value, what):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TurningMovementError(f"{what} must be a number.")
    if not math.isfinite(value) or value < 0:
        raise TurningMovementError(f"{what} must be zero or more.")
    return float(value)


def _validate(inflows, outflows, classes, legs):
    n = len(inflows)
    if n < 2 or len(outflows) != n:
        raise TurningMovementError("Inflows and outflows need the same number of legs (at least 2).")
    if legs is None:
        legs = FOUR_LEG_NAMES if n == 4 else tuple(f"Leg {k + 1}" for k in range(n))
    if len(legs) != n:
        raise TurningMovementError("Give one name per leg.")
    inflows = [_number(v, f"Inflow of {legs[k]}") for k, v in enumerate(inflows)]
    outflows = [_number(v, f"Outflow of {legs[k]}") for k, v in enumerate(outflows)]
    if len(classes) != n or any(len(row) != n for row in classes):
        raise TurningMovementError(f"The class table must be {n} x {n}.")
    classes = [[_number(v, f"Class {legs[i]} to {legs[j]}") for j, v in enumerate(row)] for i, row in enumerate(classes)]
    if sum(inflows) <= 0 or sum(outflows) <= 0:
        raise TurningMovementError("Total inflow and total outflow must both be above zero.")
    for i in range(n):
        if inflows[i] > 0 and not any(c > 0 for c in classes[i]):
            raise TurningMovementError(f"Leg {legs[i]} has traffic entering but no allowed turn.")
    for j in range(n):
        if outflows[j] > 0 and not any(classes[i][j] > 0 for i in range(n)):
            raise TurningMovementError(f"Leg {legs[j]} has traffic leaving but no turn leads to it.")
    return tuple(legs), inflows, outflows, classes


def turn_type(from_leg, to_leg, legs=4):
    """Which kind of turn a vehicle makes going from one leg to another (four-leg intersections only)."""
    if legs != 4:
        raise TurningMovementError("Turn types are only defined for four-leg intersections.")
    return _TURN_BY_STEP[(to_leg - from_leg) % 4]


def classes_from_turn_classes(turn_classes):
    """Turn a per-leg {u_turn, left, through, right} table into the class matrix the method uses."""
    if len(turn_classes) != 4:
        raise TurningMovementError("Give the turn classes of all four legs.")
    matrix = []
    for i, leg in enumerate(turn_classes):
        row = []
        for j in range(4):
            kind = turn_type(i, j)
            value = leg.get(kind)
            if isinstance(value, bool) or value not in (0, 1, 2, 3):
                raise TurningMovementError(f"The class of the {TURN_LABELS[kind].lower()} turn must be 0, 1, 2 or 3.")
            row.append(value)
        matrix.append(row)
    return matrix


def movements_by_leg(result):
    """Rounded vehicle counts per leg and turn type, e.g. [{"u_turn": 177, "right": 269, ...}, ...]."""
    if len(result.legs) != 4:
        raise TurningMovementError("Turn types are only defined for four-leg intersections.")
    return [{turn_type(i, j): result.rounded[i][j] for j in range(4)} for i in range(4)]


def estimate_turning_movements(inflows, outflows, classes, legs=None):
    legs, inflows, outflows, classes = _validate(inflows, outflows, classes, legs)
    n = len(legs)
    original_inflows, original_outflows = tuple(inflows), tuple(outflows)
    total_in, total_out = sum(inflows), sum(outflows)
    imbalance = abs(total_in - total_out) / max(total_in, total_out)
    scaled = abs(total_in - total_out) > 1e-6
    if scaled:
        average = (total_in + total_out) / 2
        inflows = [v * average / total_in for v in inflows]
        outflows = [v * average / total_out for v in outflows]

    flow = [[0.0] * n for _ in range(n)]
    for i in range(n):
        weights = [c / max(sum(classes[i]), 1) for c in classes[i]]
        for j in range(n):
            if classes[i][j] > 0:
                flow[i][j] = min(inflows[i] * weights[j], outflows[j])
    for j in range(n):
        column = sum(flow[i][j] for i in range(n))
        if column > 0:
            for i in range(n):
                flow[i][j] *= outflows[j] / column
    for i in range(n):
        row = sum(flow[i])
        if row > 0:
            flow[i] = [v * inflows[i] / row for v in flow[i]]

    column_gaps = tuple(sum(flow[i][j] for i in range(n)) - outflows[j] for j in range(n))
    warnings = []
    if imbalance > IMBALANCE_WARNING:
        warnings.append(
            f"Total inflow ({total_in:.0f}) and total outflow ({total_out:.0f}) differ by {imbalance * 100:.0f}%; "
            "both were scaled to their average, so the counts used are not the ones entered.")
    for j, gap in enumerate(column_gaps):
        if outflows[j] > 0 and abs(gap) / outflows[j] > COLUMN_GAP_WARNING:
            warnings.append(f"Arrivals at {legs[j]} are {abs(gap) / outflows[j] * 100:.0f}% "
                            f"{'above' if gap > 0 else 'below'} its outflow.")
    return TurningMovementResult(
        legs=legs, original_inflows=original_inflows, original_outflows=original_outflows,
        inflows=tuple(inflows), outflows=tuple(outflows), balanced_by_scaling=scaled, imbalance=imbalance,
        matrix=tuple(tuple(row) for row in flow),
        rounded=tuple(tuple(_round_half_up(v) for v in row) for row in flow),
        column_gaps=column_gaps, row_std=tuple(statistics.stdev(row) for row in flow),
        warnings=tuple(warnings))
