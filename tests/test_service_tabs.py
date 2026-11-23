"""Every requested service gets its own button; only that service's section is shown."""
import os
import re
import subprocess
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "tabs.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic

PERIOD = {"label": "AM", "start_time": "07:15:00", "end_time": "08:15:00"}
LINK = {"coords": [[30.138239, 31.395698], [30.137423, 31.393946]],
        "path": [[30.138239, 31.395698], [30.137423, 31.393946]], "road_km": 0.19, "junctions": 0, "no_junctions": True,
        "point_c": [30.129997, 31.383281], "point_c_distance_km": 1.7}


def last_script(html):
    start = html.rindex("<script>")
    return html[start + len("<script>"):html.index("</script>", start)]


def js_is_valid(test, script):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
        handle.write(script)
    try:
        # the page scripts use top-level template values, so only the syntax is checked
        done = subprocess.run(["node", "--check", handle.name], capture_output=True, text=True)
    except FileNotFoundError:
        test.skipTest("node is not installed")
    finally:
        os.unlink(handle.name)
    test.assertEqual(done.returncode, 0, done.stderr)


class ServiceTabsTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            d = traffic.db()
            for name, email, role in (("Tabs admin", "tabs-admin@example.test", "admin"), ("Tabs client", "tabs-client@example.test", "client")):
                d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES(?,?,'x',?)", (name, email, role))
            ids = {r["email"]: r["id"] for r in d.execute("SELECT id,email FROM users WHERE email LIKE 'tabs-%'")}
            d.execute("UPDATE periods SET is_active=1")
            d.commit()
        self.admin, self.client = self.login(ids["tabs-admin@example.test"], "admin"), self.login(ids["tabs-client@example.test"], "client")

    def login(self, uid, role):
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role=role, name=role)
        return client

    def order(self, services):
        body = {"mode": "single", "services": services, "date": "2026-10-24", "links": [LINK], "periods": [PERIOD]}
        response = self.client.post("/api/request-groups", json=body)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["id"]

    def test_the_admin_page_has_one_button_per_service(self):
        rid = self.order(["speed", "travel", "volumes", "length"])
        html = self.admin.get(f"/admin/requests/{rid}").get_data(as_text=True)
        self.assertIn('id="svcTabs"', html)
        self.assertEqual(sorted(re.findall(r'<button type="button" role="tab" data-svc="(\w+)"', html)),
                         ["length", "speed", "travel", "volumes"])

    def test_each_workflow_panel_belongs_to_its_services(self):
        rid = self.order(["speed", "volumes"])
        html = self.admin.get(f"/admin/requests/{rid}").get_data(as_text=True)
        self.assertIn('data-for="volumes" id="volumesWorkflow"', html)
        self.assertIn('data-for="travel speed volumes" id="requestDirectionsWorkflow"', html)

    def test_the_admin_page_script_is_valid_javascript(self):
        rid = self.order(["speed", "volumes"])
        js_is_valid(self, last_script(self.admin.get(f"/admin/requests/{rid}").get_data(as_text=True)))

    def test_the_client_result_page_gets_buttons_and_a_valid_script(self):
        rid = self.order(["speed", "length"])
        html = self.admin.get(f"/results/{rid}").get_data(as_text=True)  # same page the client sees once released
        self.assertIn("service-tabs", html)
        self.assertIn("sec.dataset.service=s", html)
        js_is_valid(self, last_script(html))


if __name__ == "__main__":
    unittest.main()
