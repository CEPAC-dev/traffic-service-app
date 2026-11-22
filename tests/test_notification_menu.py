import os
import tempfile
import unittest
from unittest import mock

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "notifications.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")

import app as traffic
from trafficlens.db import db
from trafficlens import study_notifications
from trafficlens.services import network_links as network_links_svc, notifications as notifications_svc
from trafficlens.services import turning_movement_studies as studies


class NotificationMenuTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            for name, email, role in (
                ("Menu owner", "menu-owner@example.test", "client"),
                ("Menu other", "menu-other@example.test", "client"),
                ("Menu admin", "menu-admin@example.test", "admin"),
            ):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,?,?)", (name, email, "x", role))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'menu-%'")}
            self.owner_id, self.other_id, self.admin_id = (
                ids["menu-owner@example.test"], ids["menu-other@example.test"], ids["menu-admin@example.test"]
            )
            d.execute("DELETE FROM notifications WHERE user_id IN (?,?,?)", (self.owner_id, self.other_id, self.admin_id))
            d.commit()
        self.owner = self.login(self.owner_id, "Menu owner")
        self.other = self.login(self.other_id, "Menu other")
        self.admin = self.login(self.admin_id, "Menu admin", role="admin")
        body = {"mode": "single", "services": ["travel"], "date": "2026-10-24", "links": [{
            "coords": [[30.138239, 31.395698], [30.137423, 31.393946]],
            "path": [[30.138239, 31.395698], [30.137423, 31.393946]], "road_km": 0.19, "junctions": 0,
            "no_junctions": True, "point_c": [30.129997, 31.383281], "point_c_distance_km": 1.7}],
            "periods": [{"label": "AM", "start_time": "07:00:00", "end_time": "08:00:00"}]}
        with mock.patch.object(network_links_svc, "apply_network", return_value=None):
            self.request_id = self.owner.post("/api/request-groups", json=body).get_json()["id"]
        with traffic.app.app_context():
            for owner, n in ((self.owner_id, 3), (self.other_id, 1)):
                for i in range(n):
                    notifications_svc._create_notification(owner, self.request_id, "admin_reply", f"Title {i}", f"Message {i}")
            traffic.db().commit()

    def login(self, uid, name, role="client"):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role=role, name=name)
        return client

    def items(self, client=None):
        return (client or self.owner).get("/api/notifications").get_json()

    def test_bell_is_a_dropdown_with_its_controls(self):
        page = self.owner.get("/requests").data
        for marker in (b'id="notifToggle"', b'id="notifPanel"', b'id="notifReadAll"', b'id="notifClear"', b'id="notifRefresh"'):
            self.assertIn(marker, page)

    def test_list_shows_only_own_notifications_with_open_links(self):
        data = self.items()
        self.assertEqual((data["unread"], data["total"], len(data["notifications"])), (3, 3, 3))
        self.assertTrue(all(n["url"].endswith("/open") and not n["read"] for n in data["notifications"]))
        self.assertEqual(self.items(self.other)["total"], 1)

    def test_opening_marks_read_then_mark_unread_and_back(self):
        first = self.items()["notifications"][0]
        self.assertEqual(self.owner.get(first["url"]).status_code, 302)
        self.assertEqual(self.items()["unread"], 2)
        response = self.owner.post(f"/api/notifications/{first['id']}/read", json={"read": False})
        self.assertEqual(response.get_json()["unread"], 3)
        self.assertEqual(self.owner.post(f"/api/notifications/{first['id']}/read", json={"read": True}).get_json()["unread"], 2)
        self.assertEqual(self.owner.post(f"/api/notifications/{first['id']}/read", json={"read": "yes"}).status_code, 400)

    def test_mark_all_read_delete_and_clear_are_scoped_to_the_user(self):
        self.assertEqual(self.owner.post("/api/notifications/read-all").get_json()["unread"], 0)
        self.assertEqual(self.items(self.other)["unread"], 1)
        victim = self.items()["notifications"][0]["id"]
        self.assertEqual(self.other.post(f"/api/notifications/{victim}/delete").status_code, 404)
        self.assertEqual(self.other.post(f"/api/notifications/{victim}/read", json={"read": False}).status_code, 404)
        self.assertEqual(self.owner.post(f"/api/notifications/{victim}/delete").status_code, 200)
        self.assertEqual(self.items()["total"], 2)
        self.assertEqual(self.owner.post("/api/notifications/clear").get_json()["unread"], 0)
        self.assertEqual(self.items()["total"], 0)
        self.assertEqual(self.items(self.other)["total"], 1)

    def test_full_page_has_actions_and_requires_login(self):
        page = self.owner.get("/notifications").data
        self.assertIn(b'id="pageReadAll"', page)
        self.assertIn(b'data-act="delete"', page)
        self.assertNotEqual(traffic.app.test_client().get("/api/notifications").status_code, 200)

    def test_request_notifications_redirect_to_role_specific_request_pages(self):
        client_notification = self.items()["notifications"][0]
        client_open = self.owner.get(client_notification["url"])
        self.assertEqual(client_open.status_code, 302)
        self.assertTrue(client_open.headers["Location"].endswith(f"/results/{self.request_id}"))

        with traffic.app.app_context():
            notifications_svc._create_notification(
                self.admin_id, self.request_id, "new_request", "New request", "A request needs review."
            )
            db().commit()
        with traffic.app.app_context():
            admin_notification_id = db().execute(
                "SELECT id FROM notifications WHERE user_id=? AND request_id=? ORDER BY id DESC LIMIT 1",
                (self.admin_id, self.request_id),
            ).fetchone()["id"]
        admin_open = self.admin.get(f"/notifications/{admin_notification_id}/open")
        self.assertEqual(admin_open.status_code, 302)
        self.assertTrue(admin_open.headers["Location"].endswith(f"/admin/requests/{self.request_id}"))

    def test_group_notifications_redirect_to_role_specific_group_pages(self):
        with traffic.app.app_context():
            group_id = db().execute(
                "INSERT INTO request_groups(user_id,title,created,study_date,services,mode,sent_at) "
                "VALUES(?, 'Grouped order', '2026-01-01', '2026-01-01', '[]', 'multi_period', '2026-01-02')",
                (self.owner_id,),
            ).lastrowid
            request_id = db().execute(
                "INSERT INTO requests(user_id,group_id) VALUES(?,?)", (self.owner_id, group_id)
            ).lastrowid
            notifications_svc._create_notification(
                self.owner_id, request_id, "group_ready", "Group ready", "The grouped results are ready."
            )
            notifications_svc._create_notification(
                self.admin_id, request_id, "new_request", "New grouped request", "A grouped order needs review."
            )
            db().commit()
            client_notification_id = db().execute(
                "SELECT id FROM notifications WHERE user_id=? AND request_id=?",
                (self.owner_id, request_id),
            ).fetchone()["id"]
            admin_notification_id = db().execute(
                "SELECT id FROM notifications WHERE user_id=? AND request_id=?",
                (self.admin_id, request_id),
            ).fetchone()["id"]

        client_open = self.owner.get(f"/notifications/{client_notification_id}/open")
        admin_open = self.admin.get(f"/notifications/{admin_notification_id}/open")
        self.assertEqual(client_open.status_code, 302)
        self.assertTrue(client_open.headers["Location"].endswith(f"/groups/{group_id}"))
        self.assertEqual(admin_open.status_code, 302)
        self.assertTrue(admin_open.headers["Location"].endswith(f"/admin/groups/{group_id}"))

    def test_study_notifications_open_and_appear_in_device_feed_for_both_roles(self):
        with traffic.app.app_context():
            study_id = studies.create_study(
                self.owner_id, "Notification study",
                [{"label": "AM", "inflows": [10, 20, 30, 40], "outflows": [10, 20, 30, 40]}],
            )
            study_notifications.notify_study(
                self.owner_id, study_id, "tm_result", "Results ready", "Study results are ready."
            )
            study_notifications.notify_study(
                self.admin_id, study_id, "tm_submitted", "Study submitted", "A study needs review."
            )
            db().commit()
            client_notification_id = db().execute(
                "SELECT id FROM notifications WHERE user_id=? AND study_id=?",
                (self.owner_id, study_id),
            ).fetchone()["id"]
            admin_notification_id = db().execute(
                "SELECT id FROM notifications WHERE user_id=? AND study_id=?",
                (self.admin_id, study_id),
            ).fetchone()["id"]

        for client, notification_id in (
            (self.owner, client_notification_id), (self.admin, admin_notification_id)
        ):
            opened = client.get(f"/notifications/{notification_id}/open")
            self.assertEqual(opened.status_code, 302)
            self.assertTrue(opened.headers["Location"].endswith(f"/turning-movement/{study_id}"))
            feed = client.get("/api/notifications/device-feed?after=0")
            self.assertEqual(feed.status_code, 200)
            self.assertTrue(any(item["url"] == f"/turning-movement/{study_id}"
                                for item in feed.get_json()["notifications"]))

    def test_device_feed_and_open_handle_missing_notification_references(self):
        with traffic.app.app_context():
            missing_request_id = db().execute("SELECT COALESCE(MAX(id), 0) + 1 FROM requests").fetchone()[0]
            missing_study_id = db().execute(
                "SELECT COALESCE(MAX(id), 0) + 1 FROM turning_movement_studies"
            ).fetchone()[0]
            feed_after = max(121, db().execute("SELECT COALESCE(MAX(id), 0) FROM notifications").fetchone()[0])
            db().execute(
                "INSERT INTO notifications(id,user_id,request_id,kind,title,message,created_at) "
                "VALUES(?,?,?,'legacy','Missing request','The request no longer exists.','2026-01-01')",
                (feed_after + 1, self.owner_id, missing_request_id),
            )
            missing_request_notification_id = feed_after + 1
            db().execute(
                "INSERT INTO notifications(id,user_id,study_id,kind,title,message,created_at) "
                "VALUES(?,?,?,'tm_result','Missing study','The study no longer exists.','2026-01-01')",
                (feed_after + 2, self.owner_id, missing_study_id),
            )
            missing_study_notification_id = feed_after + 2
            db().execute(
                "INSERT INTO notifications(user_id,request_id,kind,title,message,created_at) "
                "VALUES(?,?,'legacy','Other request','This request belongs to someone else.','2026-01-01')",
                (self.other_id, self.request_id),
            )
            other_owner_notification_id = db().execute("SELECT last_insert_rowid()").fetchone()[0]
            db().commit()

        for notification_id in (missing_request_notification_id, missing_study_notification_id):
            opened = self.owner.get(f"/notifications/{notification_id}/open")
            self.assertEqual(opened.status_code, 302)
            self.assertTrue(opened.headers["Location"].endswith("/notifications"))

        other_owner_open = self.other.get(f"/notifications/{other_owner_notification_id}/open")
        self.assertEqual(other_owner_open.status_code, 302)
        self.assertTrue(other_owner_open.headers["Location"].endswith("/notifications"))

        feed = self.owner.get(f"/api/notifications/device-feed?after={feed_after}")
        self.assertEqual(feed.status_code, 200)
        destinations = {item["id"]: item["url"] for item in feed.get_json()["notifications"]}
        for notification_id in (missing_request_notification_id, missing_study_notification_id):
            self.assertEqual(destinations[notification_id], "/notifications")


if __name__ == "__main__":
    unittest.main()
