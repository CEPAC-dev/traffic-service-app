import json
import os
import tempfile
import unittest
from unittest import mock

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "freeflow_confirm.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic

# 200 m take 30 s on the A-to-B road -> 24 km/h and 0.5 min
RESPONSE = {"status": "Success", "data": [[{"road_distance_timing(meters:seconds)": {"0": 0, "100": 10, "200": 30}}]]}


class FreeflowConfirmTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES('Freeflow admin','freeflow-admin@example.test','x','admin')")
            self.admin_id = d.execute("SELECT id FROM users WHERE email='freeflow-admin@example.test'").fetchone()["id"]
            d.commit()
        self.admin = traffic.app.test_client()
        with self.admin.session_transaction() as session:
            session.update(uid=self.admin_id, role="admin", name="Freeflow admin")

    def request_with(self, service):
        with traffic.app.app_context():
            cursor = traffic.db().execute(
                "INSERT INTO requests(user_id,created,d1,d2,services,area,bbox,coords,status,type,hours,path,road_km,junctions,start_time,end_time) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.admin_id, "2026-10-18", "2026-10-18", "2026-10-18", json.dumps([service]), "Free flow route",
                 "[29.3,47.9,29.4,48.0]", "[[29.3,47.9],[29.4,48.0]]", "Pending", "link", "[7]",
                 "[[29.3,47.9],[29.35,47.95],[29.4,48.0]]", 0.2, 0, "07:00:00", "08:00:00"))
            traffic.db().commit()
            return cursor.lastrowid

    def complete_job(self, request_id, service, **context):
        job_id = f"freeflow-confirm-{service}-{request_id}"
        context = {"source_request_id": request_id, "service": service, "study_date": "2026-10-18",
                   "country_code": "EG", "country_name": "Egypt", "timezone": "Africa/Cairo",
                   "departure_time": 1792310400, **context}
        with traffic.app.app_context():
            now = traffic._timestamp_now()
            traffic.db().execute(
                "INSERT INTO outscraper_jobs(id,created_at,updated_at,config_json,config_hash,approval_token_hash) VALUES(?,?,?,?,?,?)",
                (job_id, now, now, json.dumps({"trafficlens_context": context}), "hash", "token"))
            traffic.db().execute(
                "INSERT INTO outscraper_job_routes(job_id,route_index,origin,destination,raw_response_json) VALUES(?,1,'A','B',?)",
                (job_id, json.dumps(RESPONSE)))
            traffic._persist_holiday_result_context(job_id)
            return json.loads(traffic.db().execute("SELECT data FROM requests WHERE id=?", (request_id,)).fetchone()["data"])

    def test_free_flow_speed_without_volumes_gets_the_api_value_to_confirm(self):
        rid = self.request_with("freeflow_speed")
        data = self.complete_job(rid, "freeflow_speed", reference_type="weekly_weekend",
                                 weekend_date="2026-10-16", weekend_day="Friday")
        self.assertAlmostEqual(data["freeflow_speed"]["suggested_speed"], 24.0)
        self.assertEqual(data["freeflow_speed"]["rows"] if "rows" in data["freeflow_speed"] else [], [])
        self.assertIn("Suggested A-to-B free flow speed: 24.00 km/h", data["freeflow_speed"]["note"])

    def test_free_flow_time_still_gets_its_suggestion(self):
        rid = self.request_with("freeflow_time")
        data = self.complete_job(rid, "freeflow_time", reference_type="public_holiday",
                                 holiday_date="2026-10-06", holiday_name="Armed Forces Day")
        self.assertAlmostEqual(data["freeflow_time"]["suggested_minutes"], 0.5)

    def test_admin_page_offers_the_confirm_button_for_both_free_flow_services(self):
        rid = self.request_with("freeflow_speed")
        self.complete_job(rid, "freeflow_speed", reference_type="weekly_weekend",
                          weekend_date="2026-10-16", weekend_day="Friday")
        page = self.admin.get(f"/admin/requests/{rid}")
        self.assertEqual(page.status_code, 200)
        html = page.data.decode()
        self.assertIn("Confirm API value", html)
        self.assertIn("saved.suggested_speed", html)
        self.assertIn("suggested_minutes", html)

    def test_speed_is_not_suggested_when_the_response_has_no_timing_profile(self):
        rid = self.request_with("freeflow_speed")
        job_id = f"freeflow-confirm-empty-{rid}"
        with traffic.app.app_context():
            now = traffic._timestamp_now()
            traffic.db().execute(
                "INSERT INTO outscraper_jobs(id,created_at,updated_at,config_json,config_hash,approval_token_hash) VALUES(?,?,?,?,?,?)",
                (job_id, now, now, json.dumps({"trafficlens_context": {"source_request_id": rid, "service": "freeflow_speed"}}), "h", "t"))
            traffic.db().execute(
                "INSERT INTO outscraper_job_routes(job_id,route_index,origin,destination,raw_response_json) VALUES(?,1,'A','B',?)",
                (job_id, json.dumps({"status": "Success", "data": [[{"duration": "10 mins"}]]})))
            traffic._persist_holiday_result_context(job_id)
            data = json.loads(traffic.db().execute("SELECT data FROM requests WHERE id=?", (rid,)).fetchone()["data"])
        self.assertNotIn("suggested_speed", data["freeflow_speed"])


if __name__ == "__main__":
    unittest.main()
