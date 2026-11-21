"""Cross-site request forgery protection by checking where a request came from.

Browsers always send an Origin header (or at least Referer) when a page on another
site submits a POST to us. A forged request therefore announces a foreign host and
is refused here. Requests with neither header do not come from a browser page, and
without a browser there is no logged-in cookie to abuse, so they pass.

The session cookie is also SameSite=Lax (see hardening.py), which stops browsers
from attaching it to cross-site POSTs in the first place. The two layers back each other up.
"""
from urllib.parse import urlsplit

from flask import abort, request

from trafficlens.security.settings import trusted_origin_hosts

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _host_of(url):
    parts = urlsplit(url)
    return parts.netloc.lower() if parts.scheme in ("http", "https") else ""


def verify_same_origin():
    if request.method in SAFE_METHODS:
        return
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return
    host = _host_of(source)
    if host and (host == request.host.lower() or host in trusted_origin_hosts()):
        return
    abort(403, "Cross-site request blocked.")
