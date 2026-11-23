"""The rating of a turn decides the order of the counts, for every leg and every period.

Rules checked here (ratings: 0 not allowed, 1 low, 2 medium, 3 high), per leg:
  * a turn rated 0 carries exactly 0 vehicles;
  * a higher-rated turn never carries fewer vehicles than a lower-rated one;
  * equal ratings may differ freely;
  * the turns of a leg add up to the leg's inflow, and nothing is negative.
"""
import random
import unittest

from trafficlens.services.turning_movement import (
    MIN_RATING_RATIO, TURN_TYPES, TurningMovementError, classes_from_turn_classes, estimate_turning_movements,
    movements_by_leg,
)

# The four legs of the intersection that exposed the problem (E, N, W, S), by turn type.
SCREENSHOT_TURNS = [
    dict(u_turn=1, left=1, through=3, right=2),   # E
    dict(u_turn=2, left=1, through=2, right=3),   # N
    dict(u_turn=3, left=3, through=1, right=2),   # W
    dict(u_turn=2, left=3, through=3, right=1),   # S
]
# What each leg sent and received in the results that looked wrong (same totals, so it is balanced).
SCREENSHOT_INFLOWS = [346, 34, 280, 46434]
SCREENSHOT_OUTFLOWS = [10174, 10196, 12185, 14539]
ALL_LEGS = ("E", "N", "W", "S")


def assert_ratings_respected(test, row, ratings, tolerance=0.0, ratio=1.0):
    for a in range(len(row)):
        if ratings[a] == 0:
            test.assertEqual(row[a], 0)
        test.assertGreaterEqual(row[a], 0)
        for b in range(len(row)):
            if ratings[a] > ratings[b]:
                test.assertGreaterEqual(row[a] + tolerance, ratio * row[b],
                                        f"rating {ratings[a]} has {row[a]} but rating {ratings[b]} has {row[b]}")


class FourLegScreenshotTests(unittest.TestCase):
    """The failing example from the screen: every leg now keeps its rating order."""

    @classmethod
    def setUpClass(cls):
        cls.classes = classes_from_turn_classes(SCREENSHOT_TURNS)
        cls.result = estimate_turning_movements(SCREENSHOT_INFLOWS, SCREENSHOT_OUTFLOWS, cls.classes, legs=ALL_LEGS)
        cls.turns = movements_by_leg(cls.result)

    def check_leg(self, index):
        ratings = SCREENSHOT_TURNS[index]
        assert_ratings_respected(self, [self.turns[index][k] for k in TURN_TYPES], [ratings[k] for k in TURN_TYPES])
        assert_ratings_respected(self, self.result.matrix[index], self.classes[index], tolerance=1e-6, ratio=MIN_RATING_RATIO)
        self.assertEqual(sum(self.result.rounded[index]), SCREENSHOT_INFLOWS[index])
        self.assertAlmostEqual(sum(self.result.matrix[index]), SCREENSHOT_INFLOWS[index], places=6)

    def test_leg_e(self):
        # was: Left (Low) 155 > Through (High) 98
        self.check_leg(0)
        self.assertGreaterEqual(self.turns[0]["through"], self.turns[0]["right"])
        self.assertGreaterEqual(self.turns[0]["right"], max(self.turns[0]["left"], self.turns[0]["u_turn"]))

    def test_leg_n(self):
        # was: Right (High) 6 < Through (Medium) 20
        self.check_leg(1)
        self.assertGreaterEqual(self.turns[1]["right"], self.turns[1]["through"])
        self.assertGreaterEqual(self.turns[1]["right"], self.turns[1]["u_turn"])

    def test_leg_w(self):
        # was: Right (Medium) 126 > both High turns (27 and 51)
        self.check_leg(2)
        self.assertGreaterEqual(min(self.turns[2]["u_turn"], self.turns[2]["left"]), self.turns[2]["right"])
        self.assertGreaterEqual(self.turns[2]["right"], self.turns[2]["through"])

    def test_leg_s(self):
        # was: U-turn (Medium) 14,238 > Left and Through (High)
        self.check_leg(3)
        self.assertGreaterEqual(min(self.turns[3]["left"], self.turns[3]["through"]), self.turns[3]["u_turn"])
        self.assertGreaterEqual(self.turns[3]["u_turn"], self.turns[3]["right"])

    def test_leg_totals_are_kept(self):
        self.assertEqual([sum(row) for row in self.result.rounded], SCREENSHOT_INFLOWS)

    def test_arrivals_and_departures_still_balance(self):
        self.assertAlmostEqual(sum(self.result.column_gaps), 0, places=6)

    def test_the_outflows_that_cannot_be_met_are_reported_not_hidden(self):
        # S cannot be fed 14,539 vehicles without making its Medium U-turn bigger than a High turn.
        self.assertTrue(any("Arrivals at" in text for text in self.result.warnings))


class RatingRuleTests(unittest.TestCase):
    def test_a_rating_of_zero_carries_exactly_zero(self):
        classes = [[2, 0, 3, 1], [0, 1, 2, 3], [2, 3, 0, 1], [1, 0, 1, 3]]
        result = estimate_turning_movements([144, 2000, 2500, 250], [222, 251, 255, 258], classes)
        for i, j in ((0, 1), (1, 0), (2, 2), (3, 1)):
            self.assertEqual(result.matrix[i][j], 0)
            self.assertEqual(result.rounded[i][j], 0)

    def test_equal_ratings_are_not_forced_to_be_equal(self):
        result = estimate_turning_movements([1000, 1000, 1000], [1500, 1200, 300], [[3, 3, 1], [3, 3, 1], [3, 3, 1]])
        # the outflows ask for more at the first leg than at the second, so the two High turns differ
        self.assertNotEqual(result.rounded[0][0], result.rounded[0][1])
        assert_ratings_respected(self, result.matrix[0], [3, 3, 1], tolerance=1e-6)

    def test_higher_rating_gets_at_least_as_many_in_a_two_leg_case(self):
        result = estimate_turning_movements([1000, 1000], [1000, 1000], [[1, 3], [3, 1]])
        self.assertGreater(result.rounded[0][1], result.rounded[0][0])
        self.assertGreater(result.rounded[1][0], result.rounded[1][1])

    def test_a_leg_with_no_inflow_sends_nothing(self):
        result = estimate_turning_movements([0, 500, 500], [300, 400, 300], [[1, 2, 3]] * 3)
        self.assertEqual(result.rounded[0], (0, 0, 0))

    def test_a_ratings_conflict_with_the_outflow_keeps_the_ratings(self):
        result = estimate_turning_movements([300, 100], [100, 300], [[3, 1], [1, 3]], legs=("North", "South"))
        for row, ratings in zip(result.matrix, ([3, 1], [1, 3])):
            assert_ratings_respected(self, row, ratings, tolerance=1e-6)
        self.assertEqual(sum(result.rounded[0]), 300)
        self.assertTrue(result.warnings)

    def test_each_period_is_independent(self):
        classes = classes_from_turn_classes(SCREENSHOT_TURNS)
        am = estimate_turning_movements([2070, 2700, 2170, 1770], [1320, 1870, 2570, 2950], classes)
        pm = estimate_turning_movements([144, 2000, 2500, 250], [222, 251, 255, 258], classes)
        again = estimate_turning_movements([2070, 2700, 2170, 1770], [1320, 1870, 2570, 2950], classes)
        self.assertEqual(am.rounded, again.rounded)
        for result in (am, pm):
            for row, ratings in zip(result.rounded, classes):
                assert_ratings_respected(self, row, ratings)

    def test_the_ordered_method_is_limited_to_six_legs(self):
        n = 7
        with self.assertRaises(TurningMovementError):
            estimate_turning_movements([10] * n, [10] * n, [[1] * n] * n)

    def test_unknown_method_is_rejected(self):
        with self.assertRaises(TurningMovementError):
            estimate_turning_movements([1, 2], [1, 2], [[1, 1], [1, 1]], method="magic")

    def test_the_result_says_which_method_made_it(self):
        self.assertEqual(estimate_turning_movements([5, 5], [5, 5], [[1, 2], [2, 1]]).to_dict()["method"], "ordered")


class StrictOrderTests(unittest.TestCase):
    """The form's ratings E 1/1/3/2, N 2/1/2/3, W 3/3/1/2, S 2/3/3/1 (U-turn/Left/Through/Right)."""
    TURNS = [dict(u_turn=1, left=1, through=3, right=2), dict(u_turn=2, left=1, through=2, right=3),
             dict(u_turn=3, left=3, through=1, right=2), dict(u_turn=2, left=3, through=3, right=1)]

    def test_a_higher_rating_always_gives_strictly_more_vehicles(self):
        result = estimate_turning_movements([2070, 2700, 2170, 1770], [1320, 1870, 2570, 2950],
                                            classes_from_turn_classes(self.TURNS), legs=ALL_LEGS)
        for leg, (movements, ratings) in enumerate(zip(movements_by_leg(result), self.TURNS)):
            for a in TURN_TYPES:
                for b in TURN_TYPES:
                    if ratings[a] > ratings[b]:
                        self.assertGreater(movements[a], movements[b], (ALL_LEGS[leg], a, b))
                        self.assertGreaterEqual(movements[a], MIN_RATING_RATIO * movements[b] - 1,
                                                (ALL_LEGS[leg], a, b))
            self.assertEqual(sum(movements.values()), (2070, 2700, 2170, 1770)[leg])

    def test_the_numbers_for_that_example(self):
        result = estimate_turning_movements([2070, 2700, 2170, 1770], [1320, 1870, 2570, 2950],
                                            classes_from_turn_classes(self.TURNS), legs=ALL_LEGS)
        self.assertEqual(movements_by_leg(result), [
            dict(u_turn=430, left=430, through=672, right=538),
            dict(u_turn=271, left=217, through=983, right=1229),
            dict(u_turn=640, left=640, through=378, right=512),
            dict(u_turn=418, left=522, through=522, right=308)])


class RandomIntersectionTests(unittest.TestCase):
    def test_ratings_hold_for_hundreds_of_random_intersections(self):
        rng = random.Random(2026)
        checked = 0
        for _ in range(150):
            n = 4
            inflows = [rng.choice([0, rng.randint(1, 60), rng.randint(100, 3000), rng.randint(5000, 50000)]) for _ in range(n)]
            outflows = [rng.randint(1, 20000) for _ in range(n)]
            classes = [[rng.choice([0, 1, 2, 3, 3]) for _ in range(n)] for _ in range(n)]
            try:
                result = estimate_turning_movements(inflows, outflows, classes)
            except TurningMovementError:
                continue
            checked += 1
            for i in range(n):
                assert_ratings_respected(self, result.matrix[i], classes[i], tolerance=1e-6, ratio=MIN_RATING_RATIO)
                assert_ratings_respected(self, result.rounded[i], classes[i])
                self.assertAlmostEqual(sum(result.matrix[i]), result.inflows[i], places=5)
                self.assertEqual(sum(result.rounded[i]), int(result.inflows[i] + 0.5))
            self.assertAlmostEqual(sum(result.column_gaps), 0, places=4)
        self.assertGreater(checked, 60)


if __name__ == "__main__":
    unittest.main()
