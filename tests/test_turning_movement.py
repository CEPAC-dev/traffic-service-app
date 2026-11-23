"""Turning-movement estimation, checked against the original MATLAB script."""
import json
import math
import os
import unittest

from trafficlens.services.turning_movement import (
    CLASS_HIGH, CLASS_LOW, FOUR_LEG_NAMES, TurningMovementError, estimate_turning_movements,
)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "turning_movement_reference.json")

# E, N, W, S: row = leg traffic comes from, column = leg it goes to; 1 low, 2 medium, 3 high
EXAMPLE_INFLOWS = [2070, 2700, 2170, 1770]
EXAMPLE_OUTFLOWS = [1320, 1870, 2570, 2950]
EXAMPLE_CLASSES = [[1, 1, 3, 2], [2, 1, 2, 3], [3, 3, 1, 2], [2, 3, 3, 1]]


def example():
    return estimate_turning_movements(EXAMPLE_INFLOWS, EXAMPLE_OUTFLOWS, EXAMPLE_CLASSES)


class MatchesTheOriginalScriptTests(unittest.TestCase):
    def test_every_reference_case_matches(self):
        with open(FIXTURES, encoding="utf-8") as handle:
            cases = json.load(handle)["cases"]
        self.assertGreaterEqual(len(cases), 10)
        for case in cases:
            with self.subTest(case["name"]):
                result = estimate_turning_movements(case["inflows"], case["outflows"], case["classes"], method="legacy")
                for got_row, want_row in zip(result.matrix, case["expected_matrix"]):
                    for got, want in zip(got_row, want_row):
                        self.assertAlmostEqual(got, want, places=6)
                for got, want in zip(result.row_std, case["expected_row_std"]):
                    self.assertAlmostEqual(got, want, places=6)

    def test_the_documented_example_rounds_to_known_values(self):
        legacy = estimate_turning_movements(EXAMPLE_INFLOWS, EXAMPLE_OUTFLOWS, EXAMPLE_CLASSES, method="legacy")
        self.assertEqual(legacy.rounded, ((177, 269, 901, 723), (414, 314, 703, 1269),
                                          (488, 741, 276, 665), (258, 589, 658, 264)))

    def test_the_ordered_method_gives_known_values_for_the_same_example(self):
        self.assertEqual(example().rounded, ((32, 284, 974, 780), (355, 284, 703, 1358),
                                             (654, 719, 274, 523), (316, 585, 616, 253)))


class ResultShapeTests(unittest.TestCase):
    def test_each_leg_departs_exactly_its_inflow(self):
        result = example()
        for row, inflow in zip(result.matrix, EXAMPLE_INFLOWS):
            self.assertAlmostEqual(sum(row), inflow, places=6)

    def test_nothing_is_negative_and_blocked_turns_stay_zero(self):
        result = estimate_turning_movements([144, 2000, 2500, 250], [222, 251, 255, 258],
                                            [[2, 0, 3, 1], [0, 1, 2, 3], [2, 3, 0, 1], [1, 0, 1, 3]])
        self.assertTrue(all(v >= 0 for row in result.matrix for v in row))
        for i, j in ((0, 1), (1, 0), (2, 2), (3, 1)):
            self.assertEqual(result.matrix[i][j], 0)

    def test_arrivals_and_departures_total_the_same(self):
        result = example()
        self.assertAlmostEqual(sum(result.column_gaps), 0, places=6)

    def test_a_high_class_turn_gets_more_than_a_low_class_turn_from_the_same_leg(self):
        result = estimate_turning_movements([1000, 1000], [1000, 1000], [[CLASS_LOW, CLASS_HIGH], [CLASS_HIGH, CLASS_LOW]])
        self.assertLess(result.matrix[0][0], result.matrix[0][1])

    def test_leg_names_default_to_east_north_west_south(self):
        self.assertEqual(example().legs, FOUR_LEG_NAMES)
        three = estimate_turning_movements([10, 10, 10], [10, 10, 10], [[1, 2, 3]] * 3)
        self.assertEqual(three.legs, ("Leg 1", "Leg 2", "Leg 3"))

    def test_inputs_are_not_modified(self):
        inflows, outflows = list(EXAMPLE_INFLOWS), list(EXAMPLE_OUTFLOWS)
        classes = [list(row) for row in EXAMPLE_CLASSES]
        estimate_turning_movements(inflows, outflows, classes)
        self.assertEqual((inflows, outflows, classes), (EXAMPLE_INFLOWS, EXAMPLE_OUTFLOWS, EXAMPLE_CLASSES))

    def test_a_leg_with_no_inflow_sends_nothing(self):
        result = estimate_turning_movements([0, 500, 500], [300, 400, 300], [[1, 1, 1]] * 3)
        self.assertEqual(result.matrix[0], (0.0, 0.0, 0.0))


class BalancingTests(unittest.TestCase):
    def test_balanced_totals_are_left_alone(self):
        result = example()
        self.assertFalse(result.balanced_by_scaling)
        self.assertEqual(result.inflows, tuple(float(v) for v in EXAMPLE_INFLOWS))
        self.assertEqual(result.warnings, ())

    def test_small_mismatch_is_scaled_to_the_average_without_a_warning(self):
        result = estimate_turning_movements([1000, 1000], [1020, 1020], [[1, 2], [2, 1]])
        self.assertTrue(result.balanced_by_scaling)
        self.assertAlmostEqual(sum(result.inflows), 2020)
        self.assertAlmostEqual(sum(result.outflows), 2020)
        self.assertEqual(result.warnings, ())

    def test_large_mismatch_is_flagged_and_the_entered_counts_are_kept(self):
        result = estimate_turning_movements([144, 2000, 2500, 250], [222, 251, 255, 258],
                                            [[2, 0, 3, 1], [0, 1, 2, 3], [2, 3, 0, 1], [1, 0, 1, 3]])
        self.assertAlmostEqual(result.imbalance, 1 - 986 / 4894)
        self.assertEqual(result.original_inflows, (144.0, 2000.0, 2500.0, 250.0))
        self.assertAlmostEqual(sum(result.inflows), 2940)
        self.assertTrue(any("differ by 80%" in text for text in result.warnings))


class ColumnGapTests(unittest.TestCase):
    def test_the_legacy_method_can_miss_an_outflow_and_says_so(self):
        result = estimate_turning_movements([300, 100], [100, 300], [[3, 1], [1, 3]], legs=("North", "South"),
                                            method="legacy")
        self.assertAlmostEqual(result.column_gaps[0] / result.outflows[0], 0.161, places=3)
        self.assertEqual(len(result.warnings), 2)
        self.assertIn("North", result.warnings[0])

    def test_the_ordered_method_keeps_the_ratings_when_an_outflow_cannot_be_met(self):
        # North sends 300 with High on its own leg and Low to South; South sends 100 with High on itself.
        # Meeting the outflows (100 North, 300 South) would need the Low turn to carry more than the High one.
        result = estimate_turning_movements([300, 100], [100, 300], [[3, 1], [1, 3]], legs=("North", "South"))
        self.assertEqual(result.rounded, ((167, 133), (0, 100)))
        self.assertEqual(len(result.warnings), 2)
        self.assertIn("North", result.warnings[0])
        self.assertIn("ratings were kept", result.warnings[0])


class InvalidInputTests(unittest.TestCase):
    def check(self, inflows, outflows, classes, text, **extra):
        with self.assertRaises(TurningMovementError) as caught:
            estimate_turning_movements(inflows, outflows, classes, **extra)
        self.assertIn(text, str(caught.exception))

    def test_different_numbers_of_legs(self):
        self.check([1, 2, 3], [1, 2], [[1] * 3] * 3, "same number of legs")

    def test_a_single_leg_is_not_an_intersection(self):
        self.check([5], [5], [[1]], "at least 2")

    def test_class_table_must_be_square(self):
        self.check([1, 2], [1, 2], [[1, 2]], "2 x 2")

    def test_negative_and_non_numeric_counts(self):
        self.check([-1, 2], [1, 2], [[1, 1], [1, 1]], "zero or more")
        self.check([1, math.nan], [1, 2], [[1, 1], [1, 1]], "zero or more")
        self.check([1, "2"], [1, 2], [[1, 1], [1, 1]], "must be a number")
        self.check([True, 2], [1, 2], [[1, 1], [1, 1]], "must be a number")

    def test_zero_totals(self):
        self.check([0, 0], [1, 2], [[1, 1], [1, 1]], "above zero")

    def test_leg_with_traffic_but_no_allowed_turn(self):
        self.check([10, 10], [10, 10], [[0, 0], [1, 1]], "Leg 1 has traffic entering but no allowed turn", legs=("Leg 1", "Leg 2"))

    def test_leg_that_no_turn_leads_to(self):
        self.check([10, 10], [10, 10], [[1, 0], [1, 0]], "Leg 2 has traffic leaving but no turn leads to it", legs=("Leg 1", "Leg 2"))

    def test_wrong_number_of_leg_names(self):
        self.check([1, 2], [1, 2], [[1, 1], [1, 1]], "one name per leg", legs=("A",))


if __name__ == "__main__":
    unittest.main()
