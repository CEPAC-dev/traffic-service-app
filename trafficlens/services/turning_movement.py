"""Turning-movement estimation for an intersection.

Given what enters and leaves each leg, and how likely each turn is, estimate how many
vehicles make every turn. Two methods are available:

``"ordered"`` (the default) lets the rating of each turn decide the order of the counts.
For every leg (row) it guarantees, exactly:

  * the turns of a leg add up to the leg's inflow (after the inflow/outflow balancing);
  * a turn rated 0 (not allowed) carries 0 vehicles;
  * a turn with a higher rating carries clearly more vehicles than a turn with a lower
    rating from the same leg: at least MIN_RATING_RATIO (1.25) times as many, so the two
    are never equal and never reversed (equal ratings are free to differ);
  * no count is negative.

Within those hard rules it picks the matrix closest to "inflow shared in proportion to the
ratings" (1 : 2 : 3) whose arrivals at each leg are close to that leg's outflow. It is a small
convex problem: minimise ``sum_i |F_i - S_i|^2 / inflow_i + LAMBDA * |arrivals - outflows|^2``
(flows in units of the total inflow), where S is the proportional share, so a small leg keeps
its rating proportions as faithfully as a big one. Each row
is projected onto the set of ratings-respecting rows (exactly, by trying every way of tying
turns together), and the outflow match is enforced through accelerated dual ascent, so every
iterate already satisfies the hard rules. When the outflows cannot be met without breaking
a rating the ratings win, and the result says how far each leg is off (``column_gaps`` and
``warnings``).

``"legacy"`` is the original TM MATLAB method, ported one-to-one and kept for reproducing
old results:

  1. If total inflow and total outflow differ, scale both to their average.
  2. Share each leg's inflow across its turns in proportion to the class of each turn
     (1 = low, 2 = medium, 3 = high; 0 = the turn is not allowed), never giving a turn
     more than the outflow of the leg it goes to.
  3. Scale every column so the arrivals at a leg match its outflow.
  4. Scale every row so the departures from a leg match its inflow.

Its steps 3 and 4 multiply every column by a different factor, which reorders the turns
inside a row, so it does not respect the ratings; that is why "ordered" replaced it.

Nothing here touches Flask or the database: the same function serves a manually entered
intersection and one fed by Volumes results.
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

METHOD_ORDERED, METHOD_LEGACY = "ordered", "legacy"
ORDERED_MAX_LEGS = 6         # the exact row projection tries every way of tying turns together
MIN_RATING_RATIO = 1.25      # a turn carries at least this many times the vehicles of any lower-rated turn
OUTFLOW_WEIGHT = 30.0        # how strongly "ordered" pulls arrivals towards the outflows (gaps stay ~1-3%)
_MAX_ITERATIONS = 4000
_CONVERGED = 1e-12           # in units of the intersection's total inflow


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
    method: str = METHOD_ORDERED

    def to_dict(self):
        """A plain, JSON-friendly copy (tuples become lists)."""
        return {
            "legs": list(self.legs), "original_inflows": list(self.original_inflows),
            "original_outflows": list(self.original_outflows), "inflows": list(self.inflows),
            "outflows": list(self.outflows), "balanced_by_scaling": self.balanced_by_scaling,
            "imbalance": self.imbalance, "matrix": [list(row) for row in self.matrix],
            "rounded": [list(row) for row in self.rounded], "column_gaps": list(self.column_gaps),
            "row_std": list(self.row_std), "warnings": list(self.warnings),
            "method": self.method,
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


# ------------------------------------------------------------ ordered method

def _set_partitions(items):
    """Every way of splitting ``items`` into groups (the Bell numbers: 52 for 5 items)."""
    if not items:
        yield []
        return
    first, rest = items[0], items[1:]
    for partition in _set_partitions(rest):
        yield [[first]] + partition
        for k in range(len(partition)):
            yield partition[:k] + [[first] + partition[k]] + partition[k + 1:]


def _row_partitions(allowed):
    """Groupings of a row's allowed turns, plus the marker -1 for "pinned at zero"."""
    return list(_set_partitions([-1] + list(allowed)))


def _row_scales(ratings):
    """Weight of each turn: ``MIN_RATING_RATIO`` to the power of its rank among the row's ratings.

    A turn is required to carry at least ``MIN_RATING_RATIO`` times as many vehicles as any
    turn of the next lower rating. Dividing every count by its weight turns that into a plain
    "not smaller" order, which the row projection below handles exactly.
    """
    levels = sorted({r for r in ratings if r > 0})
    return [MIN_RATING_RATIO ** levels.index(r) if r > 0 else 1.0 for r in ratings]


def _project_row(target, ratings, scales, allowed, total, partitions):
    """The row closest to ``target`` that adds up to ``total``, is not negative and gives every
    turn at least ``MIN_RATING_RATIO`` times the vehicles of any turn with a lower rating.

    Work with ``y = count / scale`` so the rule becomes "higher rating has y at least as big".
    At the optimum some turns are tied in y (their rule is active) and some sit at zero.
    Trying every grouping, giving each group the value that fits it best and keeping the
    closest grouping that respects the order, is therefore exact.
    """
    best, best_distance = None, None
    for partition in partitions:
        free = [group for group in partition if -1 not in group]
        if not free:
            continue
        weight = [sum(scales[k] ** 2 for k in g) for g in free]
        size = [sum(scales[k] for k in g) for g in free]
        mean = [sum(scales[k] * target[k] for k in g) / w for g, w in zip(free, weight)]
        multiplier = (total - sum(sz * m for sz, m in zip(size, mean))) / sum(sz * sz / w for sz, w in zip(size, weight))
        value_of, feasible = {}, True
        for g, w, sz, m in zip(free, weight, size, mean):
            value = m + multiplier * sz / w
            if value < -1e-12:
                feasible = False
                break
            for k in g:
                value_of[k] = max(value, 0.0)
        if not feasible:
            continue
        for group in partition:
            if -1 in group:
                for k in group:
                    if k != -1:
                        value_of[k] = 0.0
        for a in allowed:
            if any(ratings[a] > ratings[b] and value_of[a] < value_of[b] for b in allowed):
                feasible = False
                break
        if not feasible:
            continue
        counts = {k: value_of[k] * scales[k] for k in allowed}
        distance = sum((counts[k] - target[k]) ** 2 for k in allowed)
        if best_distance is None or distance < best_distance - 1e-15:
            best, best_distance = counts, distance
    return best


def _ordered_flow(inflows, outflows, classes):
    """Unrounded matrix for the "ordered" method (see the module docstring)."""
    n = len(inflows)
    if n > ORDERED_MAX_LEGS:
        raise TurningMovementError(
            f"The ordered method supports up to {ORDERED_MAX_LEGS} legs; this intersection has {n}.")
    scale = sum(inflows)
    ins = [v / scale for v in inflows]
    outs = [v / scale for v in outflows]
    seed = [[ins[i] * c / max(sum(classes[i]), 1e-300) for c in classes[i]] for i in range(n)]
    allowed = [[j for j in range(n) if classes[i][j] > 0] for i in range(n)]
    partitions = [_row_partitions(a) for a in allowed]
    scales = [_row_scales(classes[i]) for i in range(n)]

    def rows_for(shift):
        flow = [[0.0] * n for _ in range(n)]
        for i in range(n):
            if ins[i] <= 0:
                continue
            target = [0.0] * n
            for j in allowed[i]:
                target[j] = seed[i][j] - shift[j] * ins[i]
            values = _project_row(target, classes[i], scales[i], allowed[i], ins[i], partitions[i])
            for j, value in values.items():
                if j >= 0:
                    flow[i][j] = value
        return flow

    lipschitz = 1.0 + 1.0 / OUTFLOW_WEIGHT      # inflows add up to 1 once divided by the total
    q = (1.0 / OUTFLOW_WEIGHT) / lipschitz
    momentum = (1 - math.sqrt(q)) / (1 + math.sqrt(q))
    price = [0.0] * n          # dual variable of each leg's outflow equation
    probe = price[:]
    for _ in range(_MAX_ITERATIONS):
        flow = rows_for(probe)
        gradient = [sum(flow[i][j] for i in range(n)) - outs[j] - probe[j] / OUTFLOW_WEIGHT
                    for j in range(n)]
        updated = [probe[j] + gradient[j] / lipschitz for j in range(n)]
        probe = [updated[j] + momentum * (updated[j] - price[j]) for j in range(n)]
        moved = max(abs(updated[j] - price[j]) for j in range(n))
        price = updated
        if moved < _CONVERGED:
            break
    flow = rows_for(price)
    return [[value * scale for value in row] for row in flow]


def _respects_order(row, ratings):
    return all(row[a] >= row[b] for a in range(len(row)) for b in range(len(row))
               if ratings[a] > ratings[b])


def _round_row_ordered(row, ratings, total):
    """Whole vehicles that add up to ``total`` and keep the rating order of ``row``.

    Floors first, then hands the missing vehicles out by largest fraction, skipping any hand-out
    that would put a lower-rated turn above a higher-rated one. Giving a vehicle to a top-rated
    turn is always allowed, so the loop always finishes.
    """
    n = len(row)
    counts = [int(math.floor(v + 1e-9)) if ratings[j] > 0 else 0 for j, v in enumerate(row)]
    missing = total - sum(counts)
    order = sorted((j for j in range(n) if ratings[j] > 0),
                   key=lambda j: (-(row[j] - counts[j]), -ratings[j]))
    top = max((ratings[j] for j in range(n)), default=0)
    while missing > 0:
        placed = False
        for j in order:
            if missing == 0:
                break
            trial = counts[:]
            trial[j] += 1
            if _respects_order(trial, ratings):
                counts, missing, placed = trial, missing - 1, True
        if not placed:
            j = next(k for k in order if ratings[k] == top)
            counts[j] += 1
            missing -= 1
    while missing < 0:     # float noise only: take back from the lowest-rated, largest count
        j = min((k for k in range(n) if counts[k] > 0), key=lambda k: (ratings[k], -counts[k]))
        trial = counts[:]
        trial[j] -= 1
        counts = trial
        missing += 1
    return tuple(counts)


# ------------------------------------------------------------ legacy method

def _legacy_flow(inflows, outflows, classes):
    n = len(inflows)
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
    return flow


def estimate_turning_movements(inflows, outflows, classes, legs=None, method=METHOD_ORDERED):
    if method not in (METHOD_ORDERED, METHOD_LEGACY):
        raise TurningMovementError(f'Unknown method "{method}".')
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

    if method == METHOD_LEGACY:
        flow = _legacy_flow(inflows, outflows, classes)
        rounded = tuple(tuple(_round_half_up(v) for v in row) for row in flow)
    else:
        flow = _ordered_flow(inflows, outflows, classes)
        rounded = tuple(_round_row_ordered(flow[i], classes[i], _round_half_up(inflows[i]))
                        for i in range(n))

    column_gaps = tuple(sum(flow[i][j] for i in range(n)) - outflows[j] for j in range(n))
    warnings = []
    if imbalance > IMBALANCE_WARNING:
        warnings.append(
            f"Total inflow ({total_in:.0f}) and total outflow ({total_out:.0f}) differ by {imbalance * 100:.0f}%; "
            "both were scaled to their average, so the counts used are not the ones entered.")
    for j, gap in enumerate(column_gaps):
        if outflows[j] > 0 and abs(gap) / outflows[j] > COLUMN_GAP_WARNING:
            text = (f"Arrivals at {legs[j]} are {abs(gap) / outflows[j] * 100:.0f}% "
                    f"{'above' if gap > 0 else 'below'} its outflow.")
            if method == METHOD_ORDERED:
                text += (" The ratings were kept, so the outflow could not be matched; "
                         f"review the ratings of the turns into {legs[j]} or its outflow.")
            warnings.append(text)
    return TurningMovementResult(
        legs=legs, original_inflows=original_inflows, original_outflows=original_outflows,
        inflows=tuple(inflows), outflows=tuple(outflows), balanced_by_scaling=scaled, imbalance=imbalance,
        matrix=tuple(tuple(row) for row in flow), rounded=rounded,
        column_gaps=column_gaps, row_std=tuple(statistics.stdev(row) for row in flow),
        warnings=tuple(warnings), method=method)
