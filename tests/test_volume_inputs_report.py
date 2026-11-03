import json
import os
import tempfile
import unittest
from io import BytesIO
from unittest import mock

from openpyxl import load_workbook

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "volume-inputs.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")

import app as traffic

LINK = {"coords": [[30.138239, 31.395698], [30.137423, 31.393946]],
        "path": [[30.138239, 31.395698], [30.137423, 31.393946]], "road_km": 0.19, "junctions": 0,
        "no_junctions": True, "point_c": [30.129997, 31.383281], "point_c_distance_km": 1.7}
PERIODS = [{"label": "AM", "start_time": "07:00:00", "end_time": "09:00:00"},
           {"label": "PM", "start_time": "16:00:00", "end_time": "17:00:00"}]


class VolumeInputsReportTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            for name, email, role in (("Vol admin", "vol-admin@example.test", "admin"), ("Vol client", "vol-client@example.test", "client")):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,'x',?)", (name, email, role))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'vol-%'")}
            d.execute("UPDATE periods SET is_active=1"); d.commit()
        self.admin = self.login(ids["vol-admin@example.test"], "admin", "Vol admin")
        self.client = self.login(ids["vol-client@example.test"], "client", "Vol client")
        body = {"mode": "multi_period", "services": ["volumes"], "date": "2026-10-24", "links": [LINK], "periods": PERIODS}
        with mock.patch.object(traffic, "apply_network", return_value=None):
            data = self.client.post("/api/request-groups", json=body).get_json()
        self.gid, self.ids = data["group_id"], data["request_ids"]
        with traffic.app.app_context():
            d = traffic.db()
            for rid, hours in zip(self.ids, ([7, 8], [16])):
                speeds = [{"hour": h, "speed_kmh": 40.0 + h} for h in hours]
                result = {"hourly": [{"hour": h, "value": 1200.4 + h, "tolerance": 55.55} for h in hours],
                          "inputs": {"lanes": 3, "freeflow_speed": 72.5, "actual_speed_by_hour": speeds,
                                     "freeflow_context": {"weekend_day": "Friday", "weekend_date": "2026-10-16"}}}
                d.execute("UPDATE volumes_workflows SET status='sent', lanes=3, freeflow_speed=72.5, actual_result_json=?, result_json=? WHERE request_id=?",
                          (json.dumps({"hourly": speeds}), json.dumps(result), rid))
                rows = [{"hour": h, "value": 1200 + h, "tolerance": 55.6} for h in hours]
                d.execute("UPDATE requests SET status='Completed', data=? WHERE id=?",
                          (json.dumps({"volumes": {"rows": rows, "note": "", "source": "matlab"}}), rid))
            d.commit()

    def login(self, uid, role, name):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role=role, name=name)
        return client

    def test_admin_order_report_lists_speed_and_free_flow_speed_used_for_volumes(self):
        report = self.admin.get(f"/groups/{self.gid}/export.xlsx")
        self.assertEqual(report.status_code, 200)
        sheet = load_workbook(BytesIO(report.data))["Volume Inputs"]
        header = [c.value for c in sheet[1]]
        self.assertEqual(header[3:6], ["Actual speed (km/h)", "Free-flow speed used (km/h)", "Lanes"])
        rows = [list(r) for r in sheet.iter_rows(min_row=2, values_only=True)]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0][:8], ["Link 1", "AM", "07:00-08:00", 47.0, 72.5, 3, 1207, 55.6])
        self.assertEqual({r[4] for r in rows}, {72.5})
        self.assertEqual(rows[0][8], "Friday 2026-10-16")
        self.assertEqual(rows[2][1], "PM")

    def test_admin_request_report_has_the_inputs_but_the_client_copy_does_not(self):
        admin_book = load_workbook(BytesIO(self.admin.get(f"/results/{self.ids[0]}/export.xlsx").data))
        self.assertIn("Volume Inputs", admin_book.sheetnames)
        self.assertEqual([c.value for c in admin_book["Volume Inputs"][2]][:5], ["07:00-08:00", 47.0, 72.5, 3, 1207])
        with traffic.app.app_context():
            traffic.db().execute("UPDATE request_groups SET sent_at='2026-10-31 10:00:00' WHERE id=?", (self.gid,)); traffic.db().commit()
        client_single = load_workbook(BytesIO(self.client.get(f"/results/{self.ids[0]}/export.xlsx").data))
        client_group = load_workbook(BytesIO(self.client.get(f"/groups/{self.gid}/export.xlsx").data))
        self.assertNotIn("Volume Inputs", client_single.sheetnames)
        self.assertNotIn("Volume Inputs", client_group.sheetnames)


if __name__ == "__main__":
    unittest.main()
