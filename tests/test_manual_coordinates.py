import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "manual_coords.db"))
os.environ.setdefault("OUTSCRAPER_API_KEY", "unit-test-api-key")
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic


class ManualCoordinateTests(unittest.TestCase):
    def test_order_page_lets_the_client_type_a_and_b(self):
        with traffic.app.app_context():
            d = traffic.db()
            d.execute("INSERT OR IGNORE INTO users(name,email,pw,role) VALUES('Coord client','coord-client@example.test','x','client')")
            uid = d.execute("SELECT id FROM users WHERE email='coord-client@example.test'").fetchone()["id"]
            d.commit()
        client = traffic.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=uid, role="client", name="Coord client")
        page = client.get("/study")
        self.assertEqual(page.status_code, 200)
        for needle in (b'id="manualA"', b'id="manualB"', b'id="drawManual"', b"Draw link from coordinates"):
            self.assertIn(needle, page.data)


if __name__ == "__main__":
    unittest.main()
