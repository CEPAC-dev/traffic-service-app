"""Login throttling, reverse-proxy support, and the login page itself."""
import os
import tempfile
import unittest
from unittest import mock

_database_dir = tempfile.TemporaryDirectory()
os.environ.setdefault("TRAFFICLENS_DB", os.path.join(_database_dir.name, "rate_limit.db"))
os.environ.setdefault("SECRET_KEY", "unit-test-session-secret-that-is-long-enough")
os.environ.setdefault("TRAFFICLENS_OUTSCRAPER_AUTO_POLL", "0")
os.environ.setdefault("TRAFFICLENS_MAINTENANCE", "0")

import app as traffic
from flask import Flask
from werkzeug.security import generate_password_hash
from trafficlens.security import rate_limit
from trafficlens.security.hardening import init_security

PASSWORD = "correct-horse-battery"


def make_user(email):
    with traffic.app.app_context():
        connection = traffic.db()
        connection.execute("DELETE FROM users WHERE email=?", (email,))
        connection.execute("INSERT INTO users(name,email,pw,role) VALUES(?,?,?,?)",
                           ("Tester", email, generate_password_hash(PASSWORD), "client"))
        connection.commit()


class LoginThrottleTests(unittest.TestCase):
    def setUp(self):
        with traffic.app.app_context():
            traffic.db().execute("DELETE FROM login_attempts")
            traffic.db().commit()
        make_user("alice@example.com")
        make_user("bob@example.com")
        self.client = traffic.app.test_client()

    def attempt(self, email, password, ip="198.51.100.10"):
        return self.client.post("/login", data={"email": email, "password": password},
                                environ_base={"REMOTE_ADDR": ip})

    def test_wrong_passwords_are_rejected_normally_until_the_limit(self):
        for _ in range(rate_limit.MAX_FAILURES_PER_EMAIL):
            response = self.attempt("alice@example.com", "wrong")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Wrong email or password", response.data)

    def test_email_is_locked_after_too_many_failures_even_with_the_right_password(self):
        for _ in range(rate_limit.MAX_FAILURES_PER_EMAIL):
            self.attempt("alice@example.com", "wrong")
        response = self.attempt("alice@example.com", PASSWORD)
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"Too many failed sign-in attempts", response.data)
        self.assertGreater(int(response.headers["Retry-After"]), 0)

    def test_other_accounts_are_not_affected(self):
        for _ in range(rate_limit.MAX_FAILURES_PER_EMAIL):
            self.attempt("alice@example.com", "wrong")
        response = self.attempt("bob@example.com", PASSWORD)
        self.assertEqual(response.status_code, 302)

    def test_a_successful_sign_in_clears_that_accounts_failures(self):
        for _ in range(rate_limit.MAX_FAILURES_PER_EMAIL - 1):
            self.attempt("alice@example.com", "wrong")
        self.assertEqual(self.attempt("alice@example.com", PASSWORD).status_code, 302)
        for _ in range(rate_limit.MAX_FAILURES_PER_EMAIL - 1):
            self.assertEqual(self.attempt("alice@example.com", "wrong").status_code, 200)

    def test_the_lock_lifts_when_the_window_has_passed(self):
        for _ in range(rate_limit.MAX_FAILURES_PER_EMAIL):
            self.attempt("alice@example.com", "wrong")
        later = rate_limit._now() + rate_limit.WINDOW_SECONDS + 5
        with mock.patch.object(rate_limit, "_now", return_value=later):
            self.assertEqual(self.attempt("alice@example.com", PASSWORD).status_code, 302)

    def test_one_ip_guessing_many_accounts_is_blocked(self):
        for number in range(rate_limit.MAX_FAILURES_PER_IP):
            self.attempt(f"nobody{number}@example.com", "wrong", ip="203.0.113.50")
        response = self.attempt("bob@example.com", PASSWORD, ip="203.0.113.50")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self.attempt("bob@example.com", PASSWORD, ip="203.0.113.51").status_code, 302)

    def test_successful_sign_in_does_not_reset_the_ip_counter(self):
        for number in range(rate_limit.MAX_FAILURES_PER_IP):
            self.attempt(f"nobody{number}@example.com", "wrong", ip="203.0.113.60")
        self.assertEqual(self.attempt("bob@example.com", PASSWORD, ip="203.0.113.60").status_code, 429)

    def test_old_failures_are_pruned(self):
        self.attempt("alice@example.com", "wrong")
        later = rate_limit._now() + rate_limit.WINDOW_SECONDS + 5
        with mock.patch.object(rate_limit, "_now", return_value=later):
            self.attempt("alice@example.com", "wrong")
        with traffic.app.app_context():
            count = traffic.db().execute("SELECT COUNT(*) FROM login_attempts").fetchone()[0]
        self.assertEqual(count, 1)

    def test_login_page_does_not_advertise_demo_credentials(self):
        page = self.client.get("/login").get_data(as_text=True)
        self.assertNotIn("admin123", page)
        self.assertNotIn("admin@demo.com", page)


class ProxySupportTests(unittest.TestCase):
    def _client_for(self, env):
        fake = Flask(__name__)
        fake.config["EPHEMERAL_SECRET_KEY"] = False
        with mock.patch.dict(os.environ, env):
            init_security(fake)

        @fake.get("/whoami")
        def whoami():
            from flask import request
            return f"{request.remote_addr}|{request.scheme}|{request.host}"
        return fake.test_client()

    HEADERS = {"X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "site.example.com"}

    def test_forwarded_headers_are_trusted_only_when_enabled(self):
        trusted = self._client_for({"TRAFFICLENS_BEHIND_PROXY": "1"}).get("/whoami", headers=self.HEADERS)
        self.assertEqual(trusted.get_data(as_text=True), "203.0.113.7|https|site.example.com")
        ignored = self._client_for({"TRAFFICLENS_BEHIND_PROXY": "0"}).get("/whoami", headers=self.HEADERS)
        self.assertNotIn("203.0.113.7", ignored.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
