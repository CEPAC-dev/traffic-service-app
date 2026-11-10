import json
import os
import re
import tempfile
import unittest
from unittest import mock

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "group_repeat.db"))
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


PERIODS = [
    {"label": "AM", "start_time": "07:15:00", "end_time": "08:15:00"},
    {"label": "MD", "start_time": "12:00:00", "end_time": "13:00:00"},
]


class RepeatGroupTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            for name, email, role in (("Repeat admin", "grprepeat-admin@example.test", "admin"),
                                      ("Repeat client", "grprepeat-client@example.test", "client"),
                                      ("Repeat other", "grprepeat-other@example.test", "client")):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,'x',?)", (name, email, role))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'grprepeat-%'")}
            d.execute("UPDATE periods SET is_active=1")
            d.commit()
        self.admin_id, self.client_id, self.other_id = (
            ids["grprepeat-admin@example.test"], ids["grprepeat-client@example.test"], ids["grprepeat-other@example.test"])

    def login(self, uid, role, name):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role=role, name=name)
        return client

    def make_group(self, client, **overrides):
        body = {"mode": "multi_link", "services": ["travel"], "date": "2026-10-24", "title": "Gulf Road",
                "links": [link(0), link(0.01)], "periods": [PERIODS[0]]}
        body.update(overrides)
        with mock.patch.object(traffic, "apply_network", return_value=None):
            return client.post("/api/request-groups", json=body).get_json()

    def complete_and_send(self, gid, ids):
        admin = self.login(self.admin_id, "admin", "Repeat admin")
        with traffic.app.app_context():
            d = traffic.db()
            for rid in ids:
                d.execute("UPDATE requests SET status='Completed' WHERE id=?", (rid,))
            d.commit()
        self.assertEqual(admin.post(f"/api/groups/{gid}/send").status_code, 200)

    def payload(self, html):
        match = re.search(r"const repeatGroup = (\{.*?\});\n", html.decode(), re.S)
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_completed_group_can_be_repeated_with_all_its_links_and_periods(self):
        client = self.login(self.client_id, "client", "Repeat client")
        data = self.make_group(client, mode="multi_period", links=[link(0)], periods=PERIODS)
        self.complete_and_send(data["group_id"], data["request_ids"])
        page = client.get(f"/study?repeat_group_id={data['group_id']}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(f"Repeating order GRP-{data['group_id']}".encode(), page.data)
        copy = self.payload(page.data)
        self.assertEqual(copy["mode"], "multi_period")
        self.assertEqual(copy["title"], "Gulf Road")
        self.assertEqual(copy["date"], "2026-10-24")
        self.assertEqual(copy["services"], ["travel"])
        self.assertEqual([(p["label"], p["start_time"], p["end_time"]) for p in copy["periods"]],
                         [("AM", "07:15:00", "08:15:00"), ("MD", "12:00:00", "13:00:00")])
        self.assertEqual(len(copy["links"]), 1)   # one link, not one per period
        self.assertEqual(copy["links"][0]["coords"], link(0)["coords"])

    def test_multi_link_group_copies_every_link_in_order(self):
        client = self.login(self.client_id, "client", "Repeat client")
        data = self.make_group(client)
        self.complete_and_send(data["group_id"], data["request_ids"])
        copy = self.payload(client.get(f"/study?repeat_group_id={data['group_id']}").data)
        self.assertEqual([x["coords"] for x in copy["links"]], [link(0)["coords"], link(0.01)["coords"]])

    def test_group_that_is_not_sent_yet_cannot_be_repeated(self):
        client = self.login(self.client_id, "client", "Repeat client")
        data = self.make_group(client)
        gid = data["group_id"]
        response = client.get(f"/study?repeat_group_id={gid}")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith(f"/groups/{gid}"))
        self.assertNotIn(b"Repeat / edit order", client.get(f"/groups/{gid}").data)

    def test_only_the_owner_can_repeat_a_group(self):
        client = self.login(self.client_id, "client", "Repeat client")
        data = self.make_group(client)
        self.complete_and_send(data["group_id"], data["request_ids"])
        other = self.login(self.other_id, "client", "Repeat other")
        self.assertEqual(other.get(f"/study?repeat_group_id={data['group_id']}").status_code, 404)
        self.assertEqual(client.get("/study?repeat_group_id=abc").status_code, 400)

    def test_repeat_buttons_appear_only_for_the_client_after_the_report_was_sent(self):
        client = self.login(self.client_id, "client", "Repeat client")
        admin = self.login(self.admin_id, "admin", "Repeat admin")
        data = self.make_group(client)
        gid = data["group_id"]
        self.complete_and_send(gid, data["request_ids"])
        self.assertIn(b"Repeat / edit order", client.get(f"/groups/{gid}").data)
        self.assertIn(f"repeat_group_id={gid}".encode(), client.get("/requests").data)
        self.assertNotIn(b"Repeat / edit order", admin.get(f"/admin/groups/{gid}").data)

    def test_repeated_order_is_a_new_group_and_the_original_is_untouched(self):
        client = self.login(self.client_id, "client", "Repeat client")
        data = self.make_group(client)
        self.complete_and_send(data["group_id"], data["request_ids"])
        copy = self.payload(client.get(f"/study?repeat_group_id={data['group_id']}").data)
        # the client edits the copy: new date, one link removed, one new link, another period and service
        body = {"mode": "multi_link", "title": copy["title"], "date": "2026-11-10", "services": ["travel", "speed"],
                "links": [link(0.03)] + [dict(x, no_junctions=True, point_c=x["point_c"]) for x in copy["links"][:1]],
                "periods": [PERIODS[1]]}
        with mock.patch.object(traffic, "apply_network", return_value=None):
            again = client.post("/api/request-groups", json=body).get_json()
        self.assertNotEqual(again["group_id"], data["group_id"])
        self.assertEqual(again["count"], 2)
        with traffic.app.app_context():
            d = traffic.db()
            original = d.execute("SELECT COUNT(*) FROM requests WHERE group_id=?", (data["group_id"],)).fetchone()[0]
            sent = d.execute("SELECT sent_at,study_date FROM request_groups WHERE id=?", (data["group_id"],)).fetchone()
            new = d.execute("SELECT study_date,services FROM request_groups WHERE id=?", (again["group_id"],)).fetchone()
        self.assertEqual(original, 2)
        self.assertIsNotNone(sent["sent_at"])
        self.assertEqual(sent["study_date"], "2026-10-24")
        self.assertEqual(new["study_date"], "2026-11-10")
        self.assertEqual(json.loads(new["services"]), ["travel", "speed"])


if __name__ == "__main__":
    unittest.main()
