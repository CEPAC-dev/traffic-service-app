"""Access-control decorators for routes."""
from functools import wraps

from flask import abort, redirect, request, session, url_for


def login_required(f):
    """Send visitors who are not signed in to the login page."""
    @wraps(f)
    def w(*a, **k):
        if "uid" not in session: return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def admin_required(f):
    """Signed-in admins only; everyone else gets 403."""
    @wraps(f)
    @login_required
    def w(*a, **k):
        if session.get("role") != "admin": abort(403)
        return f(*a, **k)
    return w
