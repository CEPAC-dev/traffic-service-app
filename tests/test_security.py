"""Security hardening: debug switch, production rules, cross-site POSTs, response headers."""
import os
import tempfile
import unittest
from unittest import mock

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "security.db"))
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic
from trafficlens.security import settings


class SettingsTests(unittest.TestCase):
    def test_debug_is_off_unless_asked_for(self):
        with mock.patch.dict(os.environ, {"TRAFFICLENS_DEBUG": "", "TRAFFICLENS_ENV": ""}):
            self.assertFalse(settings.debug_enabled())
        with mock.patch.dict(os.environ, {"TRAFFICLENS_DEBUG": "1", "TRAFFICLENS_ENV": "development"}):
            self.assertTrue(settings.debug_enabled())

    def test_debug_is_impossible_in_production(self):
        with mock.patch.dict(os.environ, {"TRAFFICLENS_DEBUG": "1", "TRAFFICLENS_ENV": "production"}):
            self.assertFalse(settings.debug_enabled())

    def test_cookies_are_secure_in_production_unless_overridden(self):
        with mock.patch.dict(os.environ, {"TRAFFICLENS_ENV": "production", "TRAFFICLENS_COOKIE_SECURE": ""}):
            self.assertTrue(settings.secure_cookies())
        with mock.patch.dict(os.environ, {"TRAFFICLENS_ENV": "production", "TRAFFICLENS_COOKIE_SECURE": "0"}):
            self.assertFalse(settings.secure_cookies())
        with mock.patch.dict(os.environ, {"TRAFFICLENS_ENV": "development", "TRAFFICLENS_COOKIE_SECURE": ""}):
            self.assertFalse(settings.secure_cookies())

    def test_session_cookie_flags_are_set(self):
        self.assertTrue(traffic.app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(traffic.app.config["SESSION_COOKIE_SAMESITE"], "Lax")


class ProductionStartupTests(unittest.TestCase):
    def _run_init_on_empty_database(self, env):
        original = traffic.app.config["DATABASE"]
        with tempfile.TemporaryDirectory() as folder:
            traffic.app.config["DATABASE"] = os.path.join(folder, "empty.db")
            try:
                with mock.patch.dict(os.environ, env):
                    traffic.init()
            finally:
                traffic.app.config["DATABASE"] = original

    def test_production_refuses_to_invent_an_admin_password(self):
        with self.assertRaises(RuntimeError):
            self._run_init_on_empty_database({"TRAFFICLENS_ENV": "production", "TRAFFICLENS_ADMIN_PASSWORD": ""})

    def test_production_refuses_a_short_admin_password(self):
        with self.assertRaises(RuntimeError):
            self._run_init_on_empty_database({"TRAFFICLENS_ENV": "production", "TRAFFICLENS_ADMIN_PASSWORD": "short"})

    def test_production_requires_a_configured_secret_key(self):
        from trafficlens.security.hardening import init_security
        fake_app = mock.Mock()
        fake_app.config = {"EPHEMERAL_SECRET_KEY": True}
        with mock.patch.dict(os.environ, {"TRAFFICLENS_ENV": "production"}):
            with self.assertRaises(RuntimeError):
                init_security(fake_app)


class RequestProtectionTests(unittest.TestCase):
    def setUp(self):
        self.client = traffic.app.test_client()

    def test_cross_site_post_is_blocked(self):
        response = self.client.post("/login", data={"email": "a@b.c", "password": "x"},
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_cross_site_referer_is_blocked_when_origin_is_missing(self):
        response = self.client.post("/login", data={"email": "a@b.c", "password": "x"},
                                    headers={"Referer": "https://evil.example/page"})
        self.assertEqual(response.status_code, 403)

    def test_same_site_post_is_allowed(self):
        response = self.client.post("/login", data={"email": "a@b.c", "password": "x"},
                                    headers={"Origin": "http://localhost"})
        self.assertNotEqual(response.status_code, 403)

    def test_post_without_origin_headers_is_allowed(self):
        response = self.client.post("/login", data={"email": "a@b.c", "password": "x"})
        self.assertNotEqual(response.status_code, 403)

    def test_trusted_origin_can_be_added_by_configuration(self):
        with mock.patch.dict(os.environ, {"TRAFFICLENS_TRUSTED_ORIGINS": "portal.example.com"}):
            response = self.client.post("/login", data={"email": "a@b.c", "password": "x"},
                                        headers={"Origin": "https://portal.example.com"})
        self.assertNotEqual(response.status_code, 403)

    def test_get_requests_are_never_blocked_by_origin(self):
        response = self.client.get("/login", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 200)

    def test_security_headers_are_on_every_response(self):
        response = self.client.get("/login")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertIn("Referrer-Policy", response.headers)
        self.assertIn("Permissions-Policy", response.headers)
        missing = self.client.get("/no-such-page")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.headers["X-Content-Type-Options"], "nosniff")


if __name__ == "__main__":
    unittest.main()
