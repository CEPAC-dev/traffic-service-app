import json
import os
import re
import tempfile
import unittest
from unittest import mock

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "data_interval.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic


def link(offset=0.0):
    return {
        "coords": [[30.138239 + offset, 31.395698], [30.137423 + offset, 31.393946]],
        "path": [[30.138239 + offset, 31.395698], [30.137423 + offset, 31.393946]],
        "road_km": 0.19, "junctions": 0, "no_junctions": True,
        "point_c": [30.129997 + offset, 31.383281], "point_c_distance_km": 1.7,
    }


PERIOD = {"label": "AM", "start_time": "07:15:00", "end_time": "08:15:00"}


class DataIntervalTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES('Interval client','interval-client@example.test','x','client')")
            d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES('Interval admin','interval-admin@example.test','x','admin')")
            self.uid = d.execute("SELECT id FROM users WHERE email='interval-client@example.test'").fetchone()["id"]
            self.admin_id = d.execute("SELECT id FROM users WHERE email='interval-admin@example.test'").fetchone()["id"]
            d.execute("UPDATE periods SET is_active=1")
            d.commit()
        self.client = traffic.app.test_client()
        with self.client.session_transaction() as session:
            session.update(uid=self.uid, role="client", name="Interval client")

    def order(self, **extra):
        body = {"mode": "single", "services": ["travel"], "date": "2026-10-24", "title": "Interval road",
                "links": [link()], "periods": [PERIOD]}
        body.update(extra)
        with mock.patch.object(traffic, "apply_network", return_value=None):
            return self.client.post("/api/request-groups", json=body)

    def stored_interval(self, request_id):
        with traffic.app.app_context():
            return traffic.db().execute("SELECT data_interval FROM requests WHERE id=?", (request_id,)).fetchone()[0]

    def test_standard_interval_is_15_minutes_when_not_chosen(self):
        response = self.order()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_interval(response.get_json()["id"]), 15)

    def test_client_can_choose_another_interval(self):
        response = self.order(data_interval=30)
        self.assertEqual(self.stored_interval(response.get_json()["id"]), 30)

    def test_unsupported_interval_is_rejected(self):
        for bad in (7, "abc", -5, 0, True, 90):
            self.assertEqual(self.order(data_interval=bad).status_code, 400, bad)

    def test_every_request_of_a_group_gets_the_chosen_interval(self):
        response = self.order(mode="multi_link", links=[link(), link(0.01)], data_interval=5)
        self.assertEqual(response.status_code, 200)
        with traffic.app.app_context():
            values = {r[0] for r in traffic.db().execute(
                "SELECT data_interval FROM requests WHERE group_id=?", (response.get_json()["group_id"],))}
        self.assertEqual(values, {5})

    def test_order_page_shows_the_interval_choice_select_all_and_bold_service_names(self):
        page = self.client.get("/study")
        self.assertEqual(page.status_code, 200)
        html = page.data.decode()
        for minutes in traffic.DATA_INTERVALS:
            self.assertIn(f'name="data_interval" value="{minutes}"', html)
        self.assertRegex(html, r'value="15" checked')
        self.assertIn('id="svcAll"', html)
        self.assertIn("<b>Free flow speed</b>", html)
        self.assertIn("<b>Free flow time</b>", html)
        self.assertIn("nearest to your study date", html)

    def test_repeat_copies_the_interval(self):
        response = self.order(mode="multi_link", links=[link(), link(0.01)], data_interval=30)
        gid = response.get_json()["group_id"]
        admin = traffic.app.test_client()
        with admin.session_transaction() as session:
            session.update(uid=self.admin_id, role="admin", name="Interval admin")
        with traffic.app.app_context():
            d = traffic.db()
            d.execute("UPDATE requests SET status='Completed' WHERE group_id=?", (gid,)); d.commit()
        self.assertEqual(admin.post(f"/api/groups/{gid}/send").status_code, 200)
        page = self.client.get(f"/study?repeat_group_id={gid}")
        copy = json.loads(re.search(r"const repeatGroup = (\{.*?\});\n", page.data.decode(), re.S).group(1))
        self.assertEqual(copy["data_interval"], 30)

    def test_travel_directions_prefill_uses_the_chosen_interval_and_old_requests_keep_one_minute(self):
        chosen = self.order(data_interval=30).get_json()["id"]
        with traffic.app.app_context():
            d = traffic.db()
            legacy = self.order().get_json()["id"]
            d.execute("UPDATE requests SET data_interval=NULL WHERE id=?", (legacy,)); d.commit()
            prefill = lambda rid: traffic._outscraper_prefill_from_request(
                d.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone(), "travel")["prefill_config"]
            self.assertEqual(prefill(chosen)["interval"], 30)
            self.assertEqual(prefill(legacy)["interval"], 1)


if __name__ == "__main__":
    unittest.main()
