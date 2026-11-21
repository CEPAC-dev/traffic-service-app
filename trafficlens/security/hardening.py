"""One call (init_security) that switches on the site-wide protections."""
from werkzeug.middleware.proxy_fix import ProxyFix

from trafficlens.security.csrf import verify_same_origin
from trafficlens.security.settings import behind_proxy, is_production, secure_cookies

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _add_security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def init_security(app):
    if is_production() and app.config.get("EPHEMERAL_SECRET_KEY"):
        raise RuntimeError("SECRET_KEY must be configured when TRAFFICLENS_ENV=production.")
    if behind_proxy():
        # Trust one proxy hop for the client IP, the scheme (https) and the original Host.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies(),
    )
    app.before_request(verify_same_origin)
    app.after_request(_add_security_headers)
