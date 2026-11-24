"""Reusing an earlier, already paid, Directions result: only when the admin asks, only when it really fits."""
import json
import os
import tempfile
import unittest
from unittest import mock

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "reuse.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic
from trafficlens.services import common as common_svc
from trafficlens.services import holiday_calendar as holiday_calendar_svc
from trafficlens.services import result_reuse as reuse

PERIOD = {"label": "AM", "start_time": "07:15:00", "end_time": "08:15:00"}
LINK = {"coords": [[30.138239, 31.395698], [30.137423, 31.393946]], "path": [[30.138239, 31.395698], [30.137423, 31.393946]],
        "road_km": 0.19, "junctions": 0, "no_junctions": True, "point_c": [30.129997, 31.383281], "point_c_distance_km": 1.7}
OTHER_LINK = dict(LINK, coords=[[30.148239, 31.395698], [30.147423, 31.393946]], point_c=[30.139997, 31.383281],
                  path=[[30.148239, 31.395698], [30.147423, 31.393946]])


class ResultReuseTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(holiday_calendar_svc, "_route_holiday_location", return_value=("EG", "Egypt", "Africa/Cairo"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with traffic.app.app_context():
            d = traffic.db()
            for table in ("outscraper_job_reuse", "volume_freeflow_attempts", "request_directions_workflows", "outscraper_job_routes", "outscraper_jobs"):
                d.execute(f"DELETE FROM {table}")
            d.execute("UPDATE volumes_workflows SET freeflow_job_id=NULL")
            for name, email, role in (("RR admin", "rr-admin@example.test", "admin"), ("RR client", "rr-client@example.test", "client")):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,'x',?)", (name, email, role))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'rr-%'")}
            d.execute("UPDATE periods SET is_active=1")
            d.commit()
        self.admin_id = ids["rr-admin@example.test"]
        self.admin, self.client = self.login(self.admin_id, "admin"), self.login(ids["rr-client@example.test"], "client")

    def login(self, uid, role):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role=role, name=role)
        return client

    def order(self, link=LINK, date="2026-10-24", period=PERIOD):
        body = {"mode": "single", "services": ["volumes"], "date": date, "links": [link], "periods": [period]}
        response = self.client.post("/api/request-groups", json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["request_ids"][0]

    def earlier_job(self, request_id, window=("07:00:00", "09:00:00"), hours=(7, 8), profile=None, link=LINK, with_workflow=True):
        """A completed Directions job for `request_id` as the Scraper would have left it."""
        profile = profile or {"0": 0, "190": 12, "1700": 160}
        with traffic.app.test_request_context(), traffic.app.app_context():
            row = traffic.db().execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            source = reuse.catalog_svc.to_dict(row)
            location = reuse.directions_svc._request_directions_location(row)
            source = dict(source, start_time=window[0], end_time=window[1])
            departure, finish = reuse.new_window(source, location["timezone"])
            a, b, c = (reuse._format(p) for p in (link["coords"][0], link["coords"][1], link["point_c"]))
            config = {"routes": [{"origin": a, "destination": c}], "departure_time": departure, "finish_time": finish, "interval": 30,
                      "travel_mode": "car", "language": "en", "region": "EG",
                      "trafficlens_request": {"source_request_id": request_id, "point_a": a, "point_b": b, "point_c": c,
                                              "road_ab_meters": 190.0, "interval_minutes": 30, "country_code": "EG",
                                              "country_name": "Egypt", "timezone": location["timezone"]}}
            records = [{"timestamp": departure + 900 * k, "road_distance_timing(meters:seconds)": profile}
                       for k in range((finish - departure) // 900 + 1) if departure + 900 * k <= finish]
            job_id, now = f"earlier-{request_id}-{window[0]}", common_svc._timestamp_now()
            d = traffic.db()
            d.execute("INSERT INTO outscraper_jobs(id,created_at,updated_at,config_json,config_hash,approval_token_hash,approval_status,"
                      "execution_status,completed_at) VALUES(?,?,?,?,?,?,'approved','completed',?)",
                      (job_id, now, now, json.dumps(config), "hash", "token", now))
            d.execute("INSERT INTO outscraper_job_routes(job_id,route_index,origin,destination,execution_status,raw_response_json) "
                      "VALUES(?,1,?,?,'completed',?)", (job_id, a, c, json.dumps({"status": "Success", "data": [records]})))
            if with_workflow:
                d.execute("INSERT INTO request_directions_workflows(job_id,request_id,status,interval_seconds,interval_minutes,result_json,created_at,updated_at) "
                          "VALUES(?,?,'ready',1800,30,'{}',?,?)", (job_id, request_id, now, now))
            d.commit()
        return job_id

    def candidates(self, request_id):
        response = self.admin.get(f"/api/requests/{request_id}/reuse-candidates")
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()

    # ------------------------------------------------------------ actual speed: finding candidates
    def test_an_earlier_result_with_a_wider_window_is_offered_as_a_candidate(self):
        old, new = self.order(), self.order()
        job = self.earlier_job(old)
        found = self.candidates(new)["directions"]
        self.assertEqual([c["source_job_id"] for c in found], [job])
        self.assertEqual(found[0]["source_request_id"], old)
        self.assertFalse(found[0]["exact_window"])

    def test_a_result_for_exactly_the_same_window_is_marked_exact_and_comes_first(self):
        old_wide, old_exact, new = self.order(), self.order(), self.order()
        wide = self.earlier_job(old_wide)
        exact = self.earlier_job(old_exact, window=("07:15:00", "08:15:00"))
        found = self.candidates(new)["directions"]
        self.assertEqual([c["source_job_id"] for c in found][0], exact)
        self.assertEqual({c["source_job_id"] for c in found}, {wide, exact})

    def test_nothing_is_offered_for_a_different_road(self):
        old, new = self.order(), self.order(link=OTHER_LINK)
        self.earlier_job(old)
        self.assertEqual(self.candidates(new)["directions"], [])

    def test_nothing_is_offered_when_the_old_window_does_not_cover_the_new_one(self):
        old, new = self.order(), self.order()
        self.earlier_job(old, window=("07:30:00", "08:00:00"))
        self.assertEqual(self.candidates(new)["directions"], [])

    def test_nothing_is_offered_when_a_selected_hour_has_no_data(self):
        old, new = self.order(), self.order()
        self.earlier_job(old, window=("08:00:00", "09:00:00"))
        self.assertEqual(self.candidates(new)["directions"], [])

    def test_nothing_is_offered_when_the_route_profile_does_not_reach_the_new_point_b(self):
        old, new = self.order(), self.order()
        self.earlier_job(old, profile={"0": 0, "100": 8})
        self.assertEqual(self.candidates(new)["directions"], [])

    def test_a_request_never_reuses_its_own_job(self):
        only = self.order()
        self.earlier_job(only)
        self.assertEqual(self.candidates(only)["directions"], [])

    def test_failed_jobs_are_ignored(self):
        failed_owner, new = self.order(), self.order()
        job = self.earlier_job(failed_owner)
        with traffic.app.app_context():
            traffic.db().execute("UPDATE request_directions_workflows SET status='failed' WHERE job_id=?", (job,))
            traffic.db().commit()
        self.assertEqual(self.candidates(new)["directions"], [])

    # ------------------------------------------------------------ actual speed: reusing
    def test_reusing_builds_the_hourly_speeds_for_the_new_request_without_a_new_api_request(self):
        old, new = self.order(), self.order()
        job = self.earlier_job(old)
        before = self.jobs_count()
        response = self.admin.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": job})
        self.assertEqual(response.status_code, 200, response.get_json())
        with traffic.app.app_context():
            workflow = traffic.db().execute("SELECT * FROM request_directions_workflows WHERE request_id=?", (new,)).fetchone()
            copy = traffic.db().execute("SELECT * FROM outscraper_jobs WHERE id=?", (workflow["job_id"],)).fetchone()
            mark = traffic.db().execute("SELECT * FROM outscraper_job_reuse WHERE request_id=?", (new,)).fetchone()
            audit = traffic.db().execute("SELECT * FROM audit_log WHERE request_id=? AND action='api_result_reused'", (new,)).fetchone()
        self.assertEqual(workflow["status"], "ready")
        result = json.loads(workflow["result_json"])
        self.assertEqual([item["hour"] for item in result["hourly"]], [7, 8])
        self.assertAlmostEqual(result["hourly"][0]["speed_kmh"], 190 / 12 * 3.6, places=3)
        self.assertEqual(result["source_request_id"], new)
        self.assertEqual((copy["execution_status"], copy["approval_status"]), ("completed", "approved"))
        self.assertEqual((mark["source_job_id"], mark["source_request_id"], mark["reused_by"]), (job, old, self.admin_id))
        self.assertIsNotNone(audit)
        self.assertEqual(self.jobs_count(), before + 1)    # a local copy, not a provider request

    def test_the_earlier_request_is_left_untouched(self):
        old, new = self.order(), self.order()
        job = self.earlier_job(old)
        self.admin.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": job})
        with traffic.app.app_context():
            rows = traffic.db().execute("SELECT job_id,status,result_json FROM request_directions_workflows WHERE request_id=?", (old,)).fetchall()
        self.assertEqual([(r["job_id"], r["status"], r["result_json"]) for r in rows], [(job, "ready", "{}")])

    def test_it_cannot_be_reused_twice_or_over_an_active_request(self):
        old, new = self.order(), self.order()
        job = self.earlier_job(old)
        self.assertEqual(self.admin.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": job}).status_code, 200)
        again = self.admin.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": job})
        self.assertEqual(again.status_code, 409)
        self.assertIn("already has a Directions request", again.get_json()["error"])

    def test_a_job_that_is_not_a_candidate_is_refused(self):
        old, new = self.order(), self.order(link=OTHER_LINK)
        job = self.earlier_job(old)
        response = self.admin.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": job})
        self.assertEqual(response.status_code, 409)

    def test_only_an_admin_can_list_or_reuse(self):
        old, new = self.order(), self.order()
        job = self.earlier_job(old)
        self.assertEqual(self.client.get(f"/api/requests/{new}/reuse-candidates").status_code, 403)
        self.assertEqual(self.client.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": job}).status_code, 403)
        self.assertEqual(self.admin.post(f"/api/requests/{new}/directions/reuse", json={}).status_code, 400)

    def test_the_marks_say_which_requests_use_a_reused_result(self):
        old, new = self.order(), self.order()
        job = self.earlier_job(old)
        self.admin.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": job})
        with traffic.app.app_context():
            self.assertEqual(reuse.reuse_marks([old, new]), {new: {"directions": old}})

    # ------------------------------------------------------------ free-flow speed
    def earlier_freeflow(self, request_id, accepted=1, link=LINK, profile=None):
        profile = profile or {"0": 0, "190": 10, "1700": 140}
        with traffic.app.test_request_context(), traffic.app.app_context():
            row = traffic.db().execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            config = reuse._freeflow_prefill(row)
            config["routes"] = [{"origin": reuse._format(link["coords"][0]), "destination": reuse._format(link["point_c"])}]
            departure, finish = config["departure_time"], config["finish_time"]
            records = [{"timestamp": departure + 900 * k, "road_distance_timing(meters:seconds)": profile} for k in range(4)]
            job_id, now = f"ff-{request_id}", common_svc._timestamp_now()
            d = traffic.db()
            d.execute("INSERT INTO outscraper_jobs(id,created_at,updated_at,config_json,config_hash,approval_token_hash,approval_status,"
                      "execution_status,completed_at) VALUES(?,?,?,?,?,?,'approved','completed',?)",
                      (job_id, now, now, json.dumps(config), "hash", "token", now))
            d.execute("INSERT INTO outscraper_job_routes(job_id,route_index,origin,destination,execution_status,raw_response_json) "
                      "VALUES(?,1,?,?,'completed',?)", (job_id, config["routes"][0]["origin"], config["routes"][0]["destination"],
                                                         json.dumps({"status": "Success", "data": [records]})))
            d.execute("INSERT INTO volume_freeflow_attempts(job_id,request_id,submitted_at,status,accepted) VALUES(?,?,?,?,?)",
                      (job_id, request_id, now, "accepted" if accepted else "needs_resend", accepted))
            d.commit()
        return job_id

    def test_an_accepted_free_flow_for_the_same_route_and_weekend_is_offered(self):
        old, new = self.order(), self.order()
        job = self.earlier_freeflow(old)
        found = self.candidates(new)["freeflow"]
        self.assertEqual([(c["source_job_id"], c["source_request_id"]) for c in found], [(job, old)])

    def test_a_free_flow_that_was_not_accepted_or_is_for_another_road_is_not_offered(self):
        old, other, new = self.order(), self.order(link=OTHER_LINK), self.order()
        self.earlier_freeflow(old, accepted=0)
        self.earlier_freeflow(other, link=OTHER_LINK)
        self.assertEqual(self.candidates(new)["freeflow"], [])

    def test_a_free_flow_for_another_weekend_is_not_offered(self):
        old, new = self.order(), self.order(date="2026-12-02")
        self.earlier_freeflow(old)
        self.assertEqual(self.candidates(new)["freeflow"], [])

    def test_reusing_a_free_flow_copies_the_job_and_reads_the_speed_for_the_new_request(self):
        old, new = self.order(), self.order()
        job = self.earlier_freeflow(old)
        before = self.jobs_count()
        response = self.admin.post(f"/api/requests/{new}/volumes/freeflow-reuse", json={"source_job_id": job})
        self.assertEqual(response.status_code, 200, response.get_json())
        with traffic.app.app_context():
            workflow = traffic.db().execute("SELECT * FROM volumes_workflows WHERE request_id=?", (new,)).fetchone()
            attempt = traffic.db().execute("SELECT * FROM volume_freeflow_attempts WHERE request_id=?", (new,)).fetchone()
            mark = traffic.db().execute("SELECT * FROM outscraper_job_reuse WHERE request_id=? AND kind='freeflow'", (new,)).fetchone()
        result = json.loads(workflow["freeflow_result_json"])
        self.assertAlmostEqual(result["selected_speed_kmh"], 190 / 10 * 3.6, places=3)
        self.assertEqual(workflow["freeflow_job_id"], attempt["job_id"])
        self.assertEqual(attempt["status"], "waiting_for_actual_speed")   # still compared with this request's own actual speed
        self.assertEqual(mark["source_job_id"], job)
        self.assertEqual(self.jobs_count(), before + 1)

    def test_a_free_flow_reuse_is_checked_against_the_actual_speed_of_the_new_request(self):
        old, new = self.order(), self.order()
        freeflow = self.earlier_freeflow(old)
        speeds = self.earlier_job(old)          # actual speed 190 m in 12 s = 57 km/h, slower than 68 km/h free flow
        self.admin.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": speeds})
        self.admin.post(f"/api/requests/{new}/volumes/freeflow-reuse", json={"source_job_id": freeflow})
        with traffic.app.app_context():
            workflow = traffic.db().execute("SELECT * FROM volumes_workflows WHERE request_id=?", (new,)).fetchone()
        self.assertEqual(workflow["freeflow_accepted"], 1)
        self.assertGreater(workflow["freeflow_speed"], 57)

    def test_a_slower_free_flow_than_the_actual_speed_is_not_accepted(self):
        old, new = self.order(), self.order()
        freeflow = self.earlier_freeflow(old, profile={"0": 0, "190": 20, "1700": 280})   # 34 km/h
        speeds = self.earlier_job(old)
        self.admin.post(f"/api/requests/{new}/directions/reuse", json={"source_job_id": speeds})
        self.admin.post(f"/api/requests/{new}/volumes/freeflow-reuse", json={"source_job_id": freeflow})
        with traffic.app.app_context():
            workflow = traffic.db().execute("SELECT * FROM volumes_workflows WHERE request_id=?", (new,)).fetchone()
        self.assertEqual(workflow["freeflow_accepted"], 0)
        self.assertIn("Needs re-send", workflow["error"])

    def jobs_count(self):
        with traffic.app.app_context():
            return traffic.db().execute("SELECT COUNT(*) FROM outscraper_jobs").fetchone()[0]


if __name__ == "__main__":
    unittest.main()
