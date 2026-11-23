"""Turning movements as part of a grouped order: 8 links, Traffic volumes in, a section on the order page out."""
import json
import os
import subprocess
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "group_tm.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic
from trafficlens.services import group_turning_movement as gtm
from trafficlens.services import request_groups as request_groups_svc

PERIOD = {"label": "AM", "start_time": "07:15:00", "end_time": "08:15:00"}
# Volumes of links 1..8: East in/out, North in/out, West in/out, South in/out.
VOLUMES = [2070, 1320, 2700, 1870, 2170, 2570, 1770, 2950]
RATINGS = [dict(u_turn=1, left=1, through=3, right=2), dict(u_turn=2, left=1, through=2, right=3),
           dict(u_turn=3, left=3, through=1, right=2), dict(u_turn=2, left=3, through=3, right=1)]


def link(offset):
    return {"coords": [[30.138239 + offset, 31.395698], [30.137423 + offset, 31.393946]],
            "path": [[30.138239 + offset, 31.395698], [30.137423 + offset, 31.393946]],
            "road_km": 0.19, "junctions": 0, "no_junctions": True,
            "point_c": [30.129997 + offset, 31.383281], "point_c_distance_km": 1.7}


class GroupTurningMovementTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            for name, email, role in (("TM admin", "gtm-admin@example.test", "admin"), ("TM client", "gtm-client@example.test", "client"),
                                      ("TM other", "gtm-other@example.test", "client")):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,'x',?)", (name, email, role))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'gtm-%'")}
            d.execute("UPDATE periods SET is_active=1")
            d.commit()
        self.admin_id, self.client_id, self.other_id = (ids["gtm-admin@example.test"], ids["gtm-client@example.test"], ids["gtm-other@example.test"])
        self.admin, self.client = self.login(self.admin_id, "admin"), self.login(self.client_id, "client")

    def login(self, uid, role):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role=role, name=role)
        return client

    def order(self, client=None, **overrides):
        body = {"mode": "multi_link", "services": ["volumes"], "date": "2026-10-24", "title": "Gulf junction",
                "links": [link(i * 0.01) for i in range(8)], "periods": [PERIOD],
                "turning_movement": {"turn_classes": RATINGS}}
        body.update(overrides)
        response = (client or self.client).post("/api/request-groups", json=body)
        return response

    def made(self, **overrides):
        response = self.order(**overrides)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def finish_volumes(self, request_ids, values=VOLUMES, status="sent", hours=1):
        with traffic.app.app_context():
            d = traffic.db()
            for rid, value in zip(request_ids, values):
                hourly = [{"hour": 7 + k, "value": value / hours, "tolerance": 5.0} for k in range(hours)]
                d.execute("UPDATE volumes_workflows SET status=?, result_json=? WHERE request_id=?",
                          (status, json.dumps({"hourly": hourly}), rid))
            d.commit()

    # ------------------------------------------------------------ the option itself
    def test_the_option_is_stored_with_the_default_roles(self):
        made = self.made()
        with traffic.app.app_context():
            option = gtm.get(made["group_id"])
        self.assertEqual(option["roles"], ["in", "out"] * 4)
        self.assertEqual(option["status"], "Pending")
        self.assertEqual(option["turn_classes"], RATINGS)

    def test_it_needs_exactly_eight_links(self):
        for count in (3, 7, 9):
            response = self.order(links=[link(i * 0.01) for i in range(count)])
            self.assertEqual(response.status_code, 400)
            self.assertIn("exactly 8 links", response.get_json()["error"])

    def test_it_needs_traffic_volumes_and_a_single_period(self):
        self.assertIn("Traffic volumes", self.order(services=["travel"]).get_json()["error"])
        self.assertEqual(self.order(mode="multi_period").status_code, 400)

    def test_every_pair_needs_one_entry_and_one_exit(self):
        roles = ["in", "in"] + ["in", "out"] * 3
        response = self.order(turning_movement={"roles": roles, "turn_classes": RATINGS})
        self.assertEqual(response.status_code, 400)
        self.assertIn("East needs one entry and one exit", response.get_json()["error"])

    def test_the_ratings_are_validated(self):
        bad = [dict(r) for r in RATINGS]
        bad[0]["left"] = 7
        self.assertEqual(self.order(turning_movement={"turn_classes": bad}).status_code, 400)
        bad[0].update(left=0, u_turn=0, through=0, right=0)
        self.assertEqual(self.order(turning_movement={"turn_classes": bad}).status_code, 400)

    def test_an_order_without_the_option_has_no_section(self):
        made = self.made(turning_movement=None)
        with traffic.app.app_context():
            self.assertIsNone(gtm.get(made["group_id"]))
        page = self.admin.get(f"/admin/groups/{made['group_id']}")
        self.assertNotIn("groupTurning", page.get_data(as_text=True))

    # ------------------------------------------------------------ calculating
    def test_it_cannot_be_calculated_before_all_volumes_exist(self):
        made = self.made()
        self.finish_volumes(made["request_ids"][:7], VOLUMES[:7])
        response = self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("all 8 links", response.get_json()["error"])

    def test_the_volumes_become_the_inflows_and_outflows_of_the_legs(self):
        made = self.made()
        self.finish_volumes(made["request_ids"])
        response = self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        self.assertEqual(response.status_code, 200, response.get_json())
        with traffic.app.app_context():
            option = gtm.get(made["group_id"])
        result = option["result"]
        self.assertEqual(result["legs"], ["East", "North", "West", "South"])
        self.assertEqual(result["original_inflows"], [2070, 2700, 2170, 1770])
        self.assertEqual(result["original_outflows"], [1320, 1870, 2570, 2950])
        self.assertEqual([sum(row.values()) for row in result["movements"]], [2070, 2700, 2170, 1770])
        self.assertEqual(option["status"], "Processing")
        self.assertTrue(result["volumes_approved"])

    def test_a_swapped_pair_swaps_entry_and_exit(self):
        roles = ["out", "in"] + ["in", "out"] * 3
        made = self.made(turning_movement={"roles": roles, "turn_classes": RATINGS})
        self.finish_volumes(made["request_ids"])
        self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        with traffic.app.app_context():
            result = gtm.get(made["group_id"])["result"]
        self.assertEqual(result["original_inflows"][0], 1320)
        self.assertEqual(result["original_outflows"][0], 2070)

    def test_hourly_volumes_of_the_period_are_added_up(self):
        made = self.made()
        self.finish_volumes(made["request_ids"], hours=2)
        self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        with traffic.app.app_context():
            self.assertEqual(gtm.get(made["group_id"])["result"]["original_inflows"], [2070, 2700, 2170, 1770])

    def test_volumes_still_in_review_can_be_used_but_are_flagged(self):
        made = self.made()
        self.finish_volumes(made["request_ids"], status="ready_for_review")
        self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        with traffic.app.app_context():
            self.assertFalse(gtm.get(made["group_id"])["result"]["volumes_approved"])

    def test_the_admin_can_change_the_ratings_before_calculating(self):
        made = self.made()
        self.finish_volumes(made["request_ids"])
        changed = [dict(r) for r in RATINGS]
        changed[0] = dict(u_turn=0, left=1, through=3, right=2)
        self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={"turn_classes": changed})
        with traffic.app.app_context():
            option = gtm.get(made["group_id"])
        self.assertEqual(option["turn_classes"], changed)
        self.assertEqual(option["result"]["movements"][0]["u_turn"], 0)

    def test_every_leg_keeps_its_rating_order(self):
        made = self.made()
        self.finish_volumes(made["request_ids"])
        self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        with traffic.app.app_context():
            movements = gtm.get(made["group_id"])["result"]["movements"]
        for row, ratings in zip(movements, RATINGS):
            for a in ratings:
                for b in ratings:
                    if ratings[a] > ratings[b]:
                        self.assertGreater(row[a], row[b])

    # ------------------------------------------------------------ who sees what
    def test_only_an_admin_can_calculate_or_approve(self):
        made = self.made()
        self.finish_volumes(made["request_ids"])
        for action in ("calculate", "approve"):
            self.assertEqual(self.client.post(f"/api/groups/{made['group_id']}/turning/{action}", json={}).status_code, 403)

    def test_it_cannot_be_approved_before_it_is_calculated(self):
        made = self.made()
        response = self.admin.post(f"/api/groups/{made['group_id']}/turning/approve", json={})
        self.assertEqual(response.status_code, 400)

    def test_the_admin_sees_the_section_with_the_ratings_table_on_the_order_page(self):
        made = self.made()
        self.finish_volumes(made["request_ids"])
        self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        html = self.admin.get(f"/admin/groups/{made['group_id']}").get_data(as_text=True)
        for text in ("panelTurning", "Turning movements (vehicles)", "Ratings used", "tm-rate-3", "3 High", "1 Low", "tmApprove"):
            self.assertIn(text, html)

    def test_the_client_sees_the_ratings_at_once_and_the_results_only_after_approval(self):
        made = self.made()
        self.finish_volumes(made["request_ids"])
        self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        page = f"/groups/{made['group_id']}"
        before = self.client.get(page).get_data(as_text=True)
        self.assertIn("3 High", before)
        self.assertIn("being prepared", before)
        self.assertNotIn("Turning movements (vehicles)", before)
        self.assertNotIn("Method:", before)
        self.assertEqual(self.admin.post(f"/api/groups/{made['group_id']}/turning/approve", json={}).status_code, 200)
        after = self.client.get(page).get_data(as_text=True)
        self.assertIn("Turning movements (vehicles)", after)
        self.assertNotIn("being prepared", after)
        self.assertNotIn("Check before approving", after)
        self.assertNotIn("Method:", after)

    def test_the_excel_report_has_a_turning_movement_sheet_with_the_ratings(self):
        from io import BytesIO
        from openpyxl import load_workbook
        made = self.made()
        self.finish_volumes(made["request_ids"])
        self.admin.post(f"/api/groups/{made['group_id']}/turning/calculate", json={})
        book = load_workbook(BytesIO(self.admin.get(f"/groups/{made['group_id']}/export.xlsx").data))
        rows = [[cell for cell in row if cell is not None] for row in book["Turning Movement"].iter_rows(values_only=True)]
        self.assertEqual(rows[0], ["From leg", "U-turn", "Left", "Through", "Right", "Total"])
        self.assertEqual(rows[1][-1], 2070)
        self.assertIn(["East", 1, 1, 3, 2], rows)

    def test_someone_elses_order_is_not_visible(self):
        made = self.made()
        stranger = self.login(self.other_id, "client")
        self.assertEqual(stranger.get(f"/groups/{made['group_id']}").status_code, 404)

    def test_deleting_the_order_removes_its_turning_movement(self):
        made = self.made()
        with traffic.app.app_context():
            request_groups_svc._delete_requests(made["request_ids"])
            self.assertIsNone(gtm.get(made["group_id"]))

    # ------------------------------------------------------------ the order form
    def test_the_order_form_script_is_valid_javascript(self):
        html = self.client.get("/study").get_data(as_text=True)
        self.assertIn("stepTurning", html)
        start = html.rindex("<script>")
        script = html[start + len("<script>"):html.index("</script>", start)]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script)
        try:
            checked = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest("node is not installed")
        finally:
            os.unlink(handle.name)
        self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
