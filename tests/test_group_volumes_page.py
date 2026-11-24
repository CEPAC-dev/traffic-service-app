"""The order page: one map with every link, and Traffic volumes of all links in one table."""
import json
import os
import re
import subprocess
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "group_volumes_page.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic

HERE = os.path.dirname(__file__)
PERIODS = [{"label": "AM", "start_time": "07:15:00", "end_time": "08:15:00"}, {"label": "PM", "start_time": "16:00:00", "end_time": "17:00:00"}]


def link(offset):
    return {"coords": [[30.138239 + offset, 31.395698], [30.137423 + offset, 31.393946]],
            "path": [[30.138239 + offset, 31.395698], [30.137423 + offset, 31.393946]], "road_km": 0.19, "junctions": 0,
            "no_junctions": True, "point_c": [30.129997 + offset, 31.383281], "point_c_distance_km": 1.7}


def inline_scripts(html):
    return [m for m in re.findall(r"<script>(.*?)</script>", html, re.S) if m.strip()]


def node_check(test, source):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(source)
    try:
        done = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
    except FileNotFoundError:
        test.skipTest("node is not installed")
    finally:
        os.unlink(handle.name)
    test.assertEqual(done.returncode, 0, done.stderr)


class GroupVolumesPageTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            for name, email, role in (("GV admin", "gv-admin@example.test", "admin"), ("GV client", "gv-client@example.test", "client")):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,'x',?)", (name, email, role))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'gv-%'")}
            d.execute("UPDATE periods SET is_active=1")
            d.commit()
        self.admin, self.client = self.login(ids["gv-admin@example.test"], "admin"), self.login(ids["gv-client@example.test"], "client")

    def login(self, uid, role):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role=role, name=role)
        return client

    def order(self, links=3, services=("volumes",), mode="multi_link", periods=PERIODS[:1], **extra):
        body = {"mode": mode, "services": list(services), "date": "2026-10-24", "links": [link(i * 0.01) for i in range(links)],
                "periods": periods}
        body.update(extra)
        response = self.client.post("/api/request-groups", json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["group_id"]

    def map_links(self, html):
        match = re.search(r"const links=(\[.*?\]);if\(!links\.length", html, re.S)
        self.assertIsNotNone(match, "the map data is missing")
        return json.loads(match.group(1))

    def test_the_admin_page_has_the_map_and_the_volumes_table(self):
        gid = self.order()
        html = self.admin.get(f"/admin/groups/{gid}").get_data(as_text=True)
        for text in ('id="groupMap"', 'id="panelVolumes"', 'id="gvTable"', "group_volumes.js", "gvSendApi", "gvSaveLanes", "gvApprove", "gvCostAck"):
            self.assertIn(text, html)

    def test_the_map_has_one_entry_per_link_not_per_period(self):
        gid = self.order(links=1, mode="multi_period", periods=PERIODS)
        html = self.admin.get(f"/admin/groups/{gid}").get_data(as_text=True)
        self.assertEqual([item["no"] for item in self.map_links(html)], [1])

    def test_all_the_links_of_the_order_are_on_the_map(self):
        gid = self.order(links=4)
        links = self.map_links(self.admin.get(f"/admin/groups/{gid}").get_data(as_text=True))
        self.assertEqual([item["no"] for item in links], [1, 2, 3, 4])

    def test_the_admin_map_shows_point_c_and_the_client_map_does_not(self):
        gid = self.order(links=2)
        admin_links = self.map_links(self.admin.get(f"/admin/groups/{gid}").get_data(as_text=True))
        client_links = self.map_links(self.client.get(f"/groups/{gid}").get_data(as_text=True))
        self.assertTrue(all("point_c" in item for item in admin_links))
        self.assertFalse(any("point_c" in item for item in client_links))

    def test_the_client_sees_the_map_but_not_the_admin_volumes_tools(self):
        gid = self.order()
        html = self.client.get(f"/groups/{gid}").get_data(as_text=True)
        self.assertIn('id="groupMap"', html)
        for text in ("id=\"panelVolumes\"", "gvSendApi", "group_volumes.js"):
            self.assertNotIn(text, html)

    def test_the_dialog_offers_reuse_but_never_ticks_it_for_the_admin(self):
        gid = self.order()
        html = self.admin.get(f"/admin/groups/{gid}").get_data(as_text=True)
        for text in ('id="gvReuseSpeeds"', 'id="gvReuseFreeflow"', "Earlier results can be reused"):
            self.assertIn(text, html)
        self.assertNotIn('id="gvReuseSpeeds" checked', html)
        self.assertNotIn('id="gvReuseFreeflow" checked', html)

    def test_an_order_without_volumes_has_no_volumes_tab(self):
        gid = self.order(services=("length",))
        html = self.admin.get(f"/admin/groups/{gid}").get_data(as_text=True)
        self.assertNotIn('id="panelVolumes"', html)
        self.assertNotIn("group_volumes.js", html)
        self.assertIn('id="groupMap"', html)

    def test_a_turning_movement_order_labels_the_links_by_leg(self):
        gid = self.order(links=8, turning_movement={"turn_classes": None})
        labels = [item["label"] for item in self.map_links(self.client.get(f"/groups/{gid}").get_data(as_text=True))]
        self.assertEqual(labels[0], "1 · East entry")
        self.assertEqual(labels[1], "2 · East exit")
        self.assertEqual(labels[-1], "8 · South exit")

    def test_the_table_lists_every_request_with_its_suggested_lanes(self):
        gid = self.order(links=3)
        html = self.admin.get(f"/admin/groups/{gid}").get_data(as_text=True)
        match = re.search(r"const children=(\[.*?\]),\$=id", html, re.S)
        children = json.loads(match.group(1))
        self.assertEqual([c["link_no"] for c in children], [1, 2, 3])
        self.assertTrue(all({"id", "coords", "point_c", "d1", "start_time", "end_time", "road_km"} <= set(c) for c in children))

    def test_every_script_on_the_admin_and_client_pages_is_valid_javascript(self):
        gid = self.order(links=8, turning_movement={"turn_classes": None})
        for client, path in ((self.admin, f"/admin/groups/{gid}"), (self.client, f"/groups/{gid}")):
            for source in inline_scripts(client.get(path).get_data(as_text=True)):
                node_check(self, source)

    def test_the_batch_module_passes_its_own_tests(self):
        try:
            done = subprocess.run(["node", os.path.join(HERE, "js", "group_volumes_test.js")], capture_output=True, text=True)
        except FileNotFoundError:
            self.skipTest("node is not installed")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
