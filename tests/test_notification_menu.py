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


class NotificationMenuTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            for name, email in (("Menu owner", "menu-owner@example.test"), ("Menu other", "menu-other@example.test")):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,'x','client')", (name, email))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'menu-%'")}
            self.owner_id, self.other_id = ids["menu-owner@example.test"], ids["menu-other@example.test"]
            d.execute("DELETE FROM notifications WHERE user_id IN (?,?)", (self.owner_id, self.other_id))
            d.commit()
        self.owner = self.login(self.owner_id, "Menu owner")
        self.other = self.login(self.other_id, "Menu other")
        body = {"mode": "single", "services": ["travel"], "date": "2026-10-24", "links": [{
            "coords": [[30.138239, 31.395698], [30.137423, 31.393946]],
            "path": [[30.138239, 31.395698], [30.137423, 31.393946]], "road_km": 0.19, "junctions": 0,
            "no_junctions": True, "point_c": [30.129997, 31.383281], "point_c_distance_km": 1.7}],
            "periods": [{"label": "AM", "start_time": "07:00:00", "end_time": "08:00:00"}]}
        with mock.patch.object(traffic, "apply_network", return_value=None):
            self.request_id = self.owner.post("/api/request-groups", json=body).get_json()["id"]
        with traffic.app.app_context():
            for owner, n in ((self.owner_id, 3), (self.other_id, 1)):
                for i in range(n):
                    traffic._create_notification(owner, self.request_id, "admin_reply", f"Title {i}", f"Message {i}")
            traffic.db().commit()

    def login(self, uid, name):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role="client", name=name)
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


if __name__ == "__main__":
    unittest.main()
