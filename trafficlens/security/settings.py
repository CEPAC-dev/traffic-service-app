"""Environment switches that decide how strict the app must be.

TRAFFICLENS_ENV=production turns on the strict rules (secret key required, admin
password required, secure cookies, debug impossible). Anything else is development.
Everything is read when called, never at import time, so tests can change it.
"""
import os


def environment():
    return os.environ.get("TRAFFICLENS_ENV", "development").strip().lower()


def is_production():
    return environment() == "production"


def debug_enabled():
    """Flask debug mode: opt-in with TRAFFICLENS_DEBUG=1, and never in production."""
    return os.environ.get("TRAFFICLENS_DEBUG", "0").strip() == "1" and not is_production()


def secure_cookies():
    """Send the session cookie over HTTPS only. Default: on in production.

    Set TRAFFICLENS_COOKIE_SECURE=0 only while the site is served over plain HTTP
    (for example a first test on a bare IP address); logins fail over HTTP otherwise.
    """
    override = os.environ.get("TRAFFICLENS_COOKIE_SECURE", "").strip()
    if override:
        return override != "0"
    return is_production()


def trusted_origin_hosts():
    """Extra hosts (comma-separated) allowed to send POST requests, besides the site's own host."""
    raw = os.environ.get("TRAFFICLENS_TRUSTED_ORIGINS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


def behind_proxy():
    """True when a reverse proxy (nginx...) sits in front of the app and sets X-Forwarded-* headers.

    Turn this on only when that is really the case: otherwise any visitor could forge
    those headers and pretend to come from another IP address.
    """
    return os.environ.get("TRAFFICLENS_BEHIND_PROXY", "0").strip() == "1"
