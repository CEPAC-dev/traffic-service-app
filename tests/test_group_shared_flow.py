import json
import os
import tempfile
import unittest
from io import BytesIO
from unittest import mock

from openpyxl import load_workbook

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "shared.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")

import app as traffic


def link(offset=0.0):
    return {"coords": [[30.138239 + offset, 31.395698], [30.137423 + offset, 31.393946]],
            "path": [[30.138239 + offset, 31.395698], [30.137423 + offset, 31.393946]],
            "road_km": 0.19, "junctions": 0, "no_junctions": True,
            "point_c": [30.129997 + offset, 31.383281], "point_c_distance_km": 1.7}


PERIODS = [{"label": "AM", "start_time": "07:00:00", "end_time": "08:00:00"},
           {"label": "MD", "start_time": "12:00:00", "end_time": "13:00:00"},
           {"label": "PM", "start_time": "16:00:00", "end_time": "17:00:00"}]


class SharedFreeFlowTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            for name, email, role in (("Shared admin", "shared-admin@example.test", "admin"),
                                      ("Shared client", "shared-client@example.test", "client")):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,'x',?)", (name, email, role))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'shared-%'")}
            d.execute("UPDATE periods SET is_active=1")
            d.commit()
        self.admin = self.login(ids["shared-admin@example.test"], "admin", "Shared admin")
        self.client = self.login(ids["shared-client@example.test"], "client", "Shared client")

    def login(self, uid, role, name):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role=role, name=name)
        return client

    def order(self, links=1, services=("freeflow_time", "travel"), periods=PERIODS):
        body = {"mode": "multi_period" if links == 1 else "multi_link", "services": list(services), "date": "2026-10-24",
                "links": [link(i / 100) for i in range(links)], "periods": periods if links == 1 else periods[:1]}
        with mock.patch.object(traffic, "apply_network", return_value=None):
            response = self.client.post("/api/request-groups", json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def data(self, rid, service):
        with traffic.app.app_context():
            row = traffic.db().execute("SELECT data,status FROM requests WHERE id=?", (rid,)).fetchone()
        return (json.loads(row["data"] or "{}").get(service), row["status"])

    def test_one_owner_per_link_and_only_for_period_independent_services(self):
        a, b, c = self.order()["request_ids"]
        with traffic.app.app_context():
            rows = {rid: traffic.db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone() for rid in (a, b, c)}
            self.assertIsNone(traffic._shared_flow_leader(rows[a], "freeflow_time"))
            self.assertEqual(traffic._shared_flow_leader(rows[b], "freeflow_time"), a)
            self.assertEqual(traffic._shared_flow_leader(rows[c], "freeflow_time"), a)
            self.assertIsNone(traffic._shared_flow_leader(rows[b], "travel"))   # depends on the period

    def test_no_second_directions_request_can_be_prepared_for_a_follower(self):
        a, b, _ = self.order()["request_ids"]
        follower = self.admin.get(f"/admin/outscraper?source_request_id={b}&service=freeflow_time")
        self.assertEqual(follower.status_code, 409)
        self.assertIn(f"REQ-{a}".encode(), follower.data)
        self.assertNotEqual(self.admin.get(f"/admin/outscraper?source_request_id={a}&service=freeflow_time").status_code, 409)
        page = self.admin.get(f"/admin/requests/{b}").data
        self.assertIn(f"shared with REQ-{a}".encode(), page)
        self.assertNotIn(b"API Directions \xc2\xb7 Free flow time</a>", page)

    def test_owner_value_is_copied_to_the_other_periods(self):
        a, b, c = self.order()["request_ids"]
        self.assertEqual(self.admin.post(f"/api/requests/{a}/data/freeflow_time", json={"rows": [{"value": 3.5}], "note": "holiday run"}).status_code, 200)
        for follower in (b, c):
            saved, status = self.data(follower, "freeflow_time")
            self.assertEqual((saved["rows"][0]["value"], saved["source"], saved["shared_from"]), (3.5, "shared", a))
            self.assertEqual(status, "Processing")
        # the save-all path and later corrections propagate too
        self.assertEqual(self.admin.post(f"/api/requests/{a}/save-all", json={"services": {"freeflow_time": {"rows": [{"value": 4.25}], "note": ""}}}).status_code, 200)
        self.assertEqual(self.data(c, "freeflow_time")[0]["rows"][0]["value"], 4.25)
        # period-dependent services are left alone
        self.assertIsNone(self.data(b, "travel")[0])
        # clearing the owner's value clears the copies
        self.admin.post(f"/api/requests/{a}/data/freeflow_time", json={"rows": [], "note": ""})
        self.assertIsNone(self.data(b, "freeflow_time")[0])

    def test_each_link_has_its_own_owner(self):
        data = self.order(links=2, periods=PERIODS)
        self.assertEqual(len(data["request_ids"]), 2)   # two links x one period: nothing to share, both are owners
        with traffic.app.app_context():
            rows = [traffic.db().execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone() for rid in data["request_ids"]]
            self.assertTrue(all(traffic._shared_flow_leader(r, "freeflow_time") is None for r in rows))

    def test_admin_sees_one_row_per_order_and_a_complete_draft_report(self):
        data = self.order()
        gid, (a, b, c) = data["group_id"], data["request_ids"]
        dashboard = self.admin.get("/admin").data.decode()
        self.assertIn(f"<b>GRP-{gid}</b>", dashboard)
        self.assertNotIn(f"<b>REQ-{b}</b>", dashboard)
        page = self.admin.get(f"/admin/groups/{gid}").data.decode()
        self.assertIn("Free flow is requested once per link", page)
        self.assertIn("1</b> Directions API request instead of 3", page)
        for rid in (a, b, c):
            self.assertIn(f"REQ-{rid}", page)
        self.admin.post(f"/api/requests/{a}/data/freeflow_time", json={"rows": [{"value": 3.5}], "note": ""})
        report = self.admin.get(f"/groups/{gid}/export.xlsx")      # admin can export before everything is completed
        self.assertEqual(report.status_code, 200)
        workbook = load_workbook(BytesIO(report.data))
        summary = {row[0]: row[1] for row in workbook["Order Summary"].iter_rows(min_row=2, values_only=True)}
        self.assertIn("Draft", summary["Report state"])
        sources = [row[8] for row in workbook["Service Details"].iter_rows(min_row=2, values_only=True) if row[3] == "Free flow time"]
        self.assertEqual(sorted(sources), sorted([f"Shared from REQ-{a} (same link)"] * 2 + ["Admin"]))
        self.assertEqual(self.client.get(f"/groups/{gid}/export.xlsx").status_code, 404)   # client: only after sending

    def test_admin_can_delete_a_whole_order(self):
        gid = self.order()["group_id"]
        self.assertEqual(self.client.post(f"/api/groups/{gid}/delete").status_code, 403)
        self.assertEqual(self.admin.post(f"/api/groups/{gid}/delete").get_json()["deleted"], 3)
        with traffic.app.app_context():
            self.assertIsNone(traffic.db().execute("SELECT 1 FROM request_groups WHERE id=?", (gid,)).fetchone())


if __name__ == "__main__":
    unittest.main()
