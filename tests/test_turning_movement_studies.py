"""Turning-movement studies: saving, editing rights, review and release."""
import json
import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "tm_studies.db"))
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic
from trafficlens.db import db
from trafficlens.services import turning_movement as tm
from trafficlens.services import turning_movement_studies as studies
from trafficlens.services.turning_movement_studies import StudyError

# The example from the original MATLAB script, written per turn type (legs E, N, W, S).
HER_TURNS = [dict(u_turn=1, right=1, through=3, left=2), dict(u_turn=1, left=2, right=2, through=3),
             dict(u_turn=1, left=3, through=3, right=2), dict(u_turn=1, right=2, through=3, left=3)]
BALANCED = {"label": "AM", "inflows": [2070, 2700, 2170, 1770], "outflows": [1320, 1870, 2570, 2950]}
UNBALANCED = {"label": "PM", "inflows": [144, 2000, 2500, 250], "outflows": [222, 251, 255, 258]}


class StudyTestCase(unittest.TestCase):
    def setUp(self):
        self.context = traffic.app.app_context()
        self.context.push()
        for table in ("turning_movement_periods", "turning_movement_studies", "audit_log"):
            db().execute(f"DELETE FROM {table}")
        db().execute("DELETE FROM users WHERE email LIKE '%@tm.test'")
        for name, role in (("Admin", "admin"), ("Alice", "client"), ("Bob", "client")):
            db().execute("INSERT INTO users(name,email,pw,role) VALUES(?,?,?,?)", (name, f"{name.lower()}@tm.test", "x", role))
        db().commit()
        ids = {r["name"]: r["id"] for r in db().execute("SELECT id,name FROM users WHERE email LIKE '%@tm.test'")}
        self.admin, self.alice, self.bob = ids["Admin"], ids["Alice"], ids["Bob"]

    def tearDown(self):
        self.context.pop()

    def make(self, periods=None, user=None, **extra):
        return studies.create_study(user or self.alice, "Main St / 5th Ave", periods or [dict(BALANCED)],
                                    turn_classes=extra.pop("turn_classes", HER_TURNS), **extra)

    def count(self, table="turning_movement_studies"):
        return db().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class TurnTypeTests(unittest.TestCase):
    def test_turn_types_follow_the_counter_clockwise_leg_order(self):
        self.assertEqual([tm.turn_type(0, j) for j in range(4)], ["u_turn", "right", "through", "left"])
        self.assertEqual([tm.turn_type(1, j) for j in range(4)], ["left", "u_turn", "right", "through"])
        self.assertEqual([tm.turn_type(3, j) for j in range(4)], ["right", "through", "left", "u_turn"])

    def test_turn_types_exist_only_for_four_legs(self):
        with self.assertRaises(tm.TurningMovementError):
            tm.turn_type(0, 1, legs=3)

    def test_per_turn_classes_rebuild_the_original_class_matrix(self):
        self.assertEqual(tm.classes_from_turn_classes(HER_TURNS), [[1, 1, 3, 2], [2, 1, 2, 3], [3, 3, 1, 2], [2, 3, 3, 1]])

    def test_presets_are_complete_and_valid(self):
        for preset in tm.CLASS_PRESETS.values():
            self.assertEqual(set(preset), set(tm.TURN_TYPES))
            tm.classes_from_turn_classes([preset] * 4)

    def test_bad_class_values_are_rejected(self):
        for bad in (4, -1, 1.5, "2", None, True):
            leg = dict(u_turn=1, left=2, through=3, right=bad)
            with self.assertRaises(tm.TurningMovementError):
                tm.classes_from_turn_classes([leg] * 4)


class CreateStudyTests(StudyTestCase):
    def test_a_new_study_is_pending_and_estimates_are_computed(self):
        study = studies.get_study(self.make())
        self.assertEqual((study["status"], study["mode"], study["legs"]), ("Pending", "manual", ["E", "N", "W", "S"]))
        result = study["periods"][0]["result"]
        self.assertEqual(result["rounded"][0], [177, 269, 901, 723])
        self.assertEqual(result["movements"][0], {"u_turn": 177, "right": 269, "through": 901, "left": 723})
        self.assertEqual(result["method"], "legacy")

    def test_defaults_give_a_typical_class_for_every_leg(self):
        study_id = studies.create_study(self.alice, "Defaults", [dict(BALANCED)])
        self.assertEqual(studies.get_study(study_id)["turn_classes"], [tm.CLASS_PRESETS["typical"]] * 4)

    def test_several_periods_keep_their_order(self):
        study = studies.get_study(self.make([dict(BALANCED), dict(UNBALANCED, label="MD"), dict(BALANCED, label="PM")]))
        self.assertEqual([p["label"] for p in study["periods"]], ["AM", "MD", "PM"])

    def test_whole_number_floats_are_stored_as_integers(self):
        period = dict(BALANCED, inflows=[2070.0, 2700.0, 2170.0, 1770.0])
        self.assertEqual(studies.get_study(self.make([period]))["periods"][0]["inflows"], [2070, 2700, 2170, 1770])

    def test_custom_leg_names_and_date(self):
        study = studies.get_study(self.make(legs=["East", "North", "West", "South"], study_date="2026-11-21"))
        self.assertEqual((study["legs"], study["study_date"]), (["East", "North", "West", "South"], "2026-11-21"))

    def test_creating_is_written_to_the_audit_log(self):
        study_id = self.make()
        row = db().execute("SELECT details FROM audit_log WHERE action='turning_movement_created'").fetchone()
        self.assertEqual(json.loads(row["details"])["study_id"], study_id)

    def test_invalid_input_saves_nothing(self):
        bad_cases = [
            dict(title=""), dict(title="x" * 121), dict(legs=["A", "B", "C"]), dict(legs=["A", "a", "C", "D"]),
            dict(study_date="21/11/2026"), dict(mode="volumes"),
            dict(periods=[]), dict(periods=[dict(BALANCED), dict(BALANCED)]), dict(periods=[dict(BALANCED, label=" ")]),
            dict(periods=[dict(BALANCED, inflows=[1, 2, 3])]), dict(periods=[dict(BALANCED, inflows=[-1, 2, 3, 4])]),
            dict(periods=[dict(BALANCED, inflows="many")]), dict(periods=["AM"]),
            dict(periods=[dict(BALANCED, label=str(k)) for k in range(25)]),
            dict(turn_classes=HER_TURNS[:3]), dict(turn_classes=[dict(u_turn=1, left=2, through=3)] * 4),
            dict(turn_classes=[dict(u_turn=0, left=0, through=0, right=0)] + HER_TURNS[1:]),
        ]
        for case in bad_cases:
            with self.subTest(case):
                args = dict(user_id=self.alice, title="T", periods=[dict(BALANCED)], turn_classes=HER_TURNS)
                args.update(case)
                with self.assertRaises(StudyError):
                    studies.create_study(**args)
        self.assertEqual((self.count(), self.count("turning_movement_periods")), (0, 0))

    def test_a_bad_period_is_named_in_the_message(self):
        with self.assertRaises(StudyError) as caught:
            self.make([dict(BALANCED), dict(label="Night", inflows=[0, 0, 0, 0], outflows=[1, 1, 1, 1])])
        self.assertIn('Period "Night"', str(caught.exception))


class EditingRightsTests(StudyTestCase):
    def test_owner_can_edit_while_pending_and_estimates_follow(self):
        study_id = self.make()
        studies.update_study(study_id, self.alice, False, periods=[dict(UNBALANCED)], title="Renamed")
        study = studies.get_study(study_id)
        self.assertEqual((study["title"], study["status"], study["periods"][0]["label"]), ("Renamed", "Pending", "PM"))
        self.assertTrue(study["periods"][0]["result"]["warnings"])

    def test_someone_else_cannot_edit(self):
        study_id = self.make()
        with self.assertRaises(StudyError):
            studies.update_study(study_id, self.bob, False, title="Hijack")
        self.assertEqual(studies.get_study(study_id)["title"], "Main St / 5th Ave")

    def test_an_admin_edit_moves_it_to_processing_and_locks_the_owner_out(self):
        study_id = self.make()
        studies.update_study(study_id, self.admin, True, turn_classes=[dict(u_turn=0, left=2, through=3, right=2)] * 4)
        self.assertEqual(studies.get_study(study_id)["status"], "Processing")
        self.assertEqual(studies.get_study(study_id)["periods"][0]["result"]["movements"][0]["u_turn"], 0)
        with self.assertRaises(StudyError):
            studies.update_study(study_id, self.alice, False, title="Too late")

    def test_changing_only_the_classes_keeps_the_entered_numbers(self):
        study_id = self.make()
        studies.update_study(study_id, self.admin, True, turn_classes=[dict(tm.CLASS_PRESETS["no_u_turns"])] * 4)
        period = studies.get_study(study_id)["periods"][0]
        self.assertEqual((period["inflows"], period["outflows"]), (BALANCED["inflows"], BALANCED["outflows"]))

    def test_invalid_edit_leaves_the_study_untouched(self):
        study_id = self.make()
        with self.assertRaises(StudyError):
            studies.update_study(study_id, self.alice, False, title="Fine", periods=[dict(BALANCED, inflows=[1, 2])])
        self.assertEqual(studies.get_study(study_id)["title"], "Main St / 5th Ave")

    def test_date_changes_only_when_asked(self):
        study_id = self.make(study_date="2026-11-21")
        studies.update_study(study_id, self.alice, False, title="Same date")
        self.assertEqual(studies.get_study(study_id)["study_date"], "2026-11-21")
        studies.update_study(study_id, self.alice, False, study_date="", change_date=True)
        self.assertIsNone(studies.get_study(study_id)["study_date"])

    def test_missing_study(self):
        with self.assertRaises(StudyError):
            studies.update_study(999999, self.admin, True, title="x")


class ReviewTests(StudyTestCase):
    def test_approval_releases_the_study_and_locks_it(self):
        study_id = self.make()
        studies.approve_study(study_id, self.admin, reply="Looks good")
        study = studies.get_study(study_id)
        self.assertEqual((study["status"], study["reviewed_by"], study["reply"]), ("Completed", self.admin, "Looks good"))
        self.assertTrue(study["sent_at"])
        with self.assertRaises(StudyError):
            studies.update_study(study_id, self.admin, True, title="Edit after approval")
        with self.assertRaises(StudyError):
            studies.approve_study(study_id, self.admin)

    def test_warnings_must_be_acknowledged_before_approval(self):
        study_id = self.make([dict(UNBALANCED)])
        with self.assertRaises(StudyError) as caught:
            studies.approve_study(study_id, self.admin)
        self.assertIn("warning", str(caught.exception))
        self.assertEqual(studies.get_study(study_id)["status"], "Pending")
        studies.approve_study(study_id, self.admin, acknowledge_warnings=True)
        self.assertEqual(studies.get_study(study_id)["status"], "Completed")

    def test_rejection_needs_a_reason_and_the_owner_can_fix_and_resubmit(self):
        study_id = self.make()
        with self.assertRaises(StudyError):
            studies.reject_study(study_id, self.admin, "  ")
        studies.reject_study(study_id, self.admin, "Counts look swapped")
        self.assertEqual(studies.get_study(study_id)["status"], "Rejected")
        studies.update_study(study_id, self.alice, False, periods=[dict(BALANCED, outflows=[1320, 1870, 2950, 2570])])
        self.assertEqual(studies.get_study(study_id)["status"], "Pending")

    def test_a_completed_study_cannot_be_rejected_but_can_be_reopened(self):
        study_id = self.make()
        studies.approve_study(study_id, self.admin)
        with self.assertRaises(StudyError):
            studies.reject_study(study_id, self.admin, "Too late")
        studies.reopen_study(study_id, self.admin)
        study = studies.get_study(study_id)
        self.assertEqual((study["status"], study["sent_at"]), ("Processing", None))
        self.assertFalse(studies.client_view(study)["results_visible"])
        studies.update_study(study_id, self.admin, True, title="Corrected")

    def test_reopen_only_works_on_completed_studies(self):
        with self.assertRaises(StudyError):
            studies.reopen_study(self.make(), self.admin)

    def test_every_step_is_audited(self):
        study_id = self.make()
        studies.update_study(study_id, self.admin, True, title="x")
        studies.approve_study(study_id, self.admin)
        studies.reopen_study(study_id, self.admin)
        actions = [r["action"] for r in db().execute("SELECT action FROM audit_log ORDER BY id")]
        self.assertEqual(actions, ["turning_movement_created", "turning_movement_edited",
                                   "turning_movement_approved", "turning_movement_reopened"])


class ClientViewTests(StudyTestCase):
    def test_results_and_warnings_stay_hidden_until_approval(self):
        study_id = self.make([dict(UNBALANCED)])
        view = studies.client_view(studies.get_study(study_id))
        self.assertFalse(view["results_visible"])
        self.assertIsNone(view["periods"][0]["result"])
        self.assertEqual(view["periods"][0]["inflows"], UNBALANCED["inflows"])
        self.assertNotIn("warnings", json.dumps(view))

    def test_results_appear_after_approval_without_admin_only_details(self):
        study_id = self.make()
        studies.approve_study(study_id, self.admin)
        view = studies.client_view(studies.get_study(study_id))
        self.assertTrue(view["results_visible"])
        self.assertEqual(view["periods"][0]["result"]["rounded"][1], [414, 314, 703, 1269])
        self.assertNotIn("row_std", view["periods"][0]["result"])
        self.assertNotIn("warnings", view["periods"][0]["result"])

    def test_the_reason_for_a_rejection_is_visible_but_not_a_pending_draft_reply(self):
        study_id = self.make()
        self.assertIsNone(studies.client_view(studies.get_study(study_id))["reply"])
        studies.reject_study(study_id, self.admin, "Please recount")
        self.assertEqual(studies.client_view(studies.get_study(study_id))["reply"], "Please recount")


class ListAndDeleteTests(StudyTestCase):
    def test_listing_filters_by_owner_and_status(self):
        mine, theirs = self.make(), self.make(user=self.bob)
        studies.approve_study(theirs, self.admin)
        self.assertEqual({s["id"] for s in studies.list_studies()}, {mine, theirs})
        self.assertEqual([s["id"] for s in studies.list_studies(user_id=self.alice)], [mine])
        self.assertEqual([s["id"] for s in studies.list_studies(status="Completed")], [theirs])
        self.assertEqual(studies.list_studies(user_id=self.alice)[0]["period_count"], 1)

    def test_deleting_removes_the_study_and_its_periods(self):
        study_id = self.make([dict(BALANCED), dict(BALANCED, label="PM")])
        studies.delete_study(study_id)
        self.assertEqual((self.count(), self.count("turning_movement_periods")), (0, 0))
        with self.assertRaises(StudyError):
            studies.delete_study(study_id)

    def test_a_missing_study_is_none(self):
        self.assertIsNone(studies.get_study(424242))


if __name__ == "__main__":
    unittest.main()
