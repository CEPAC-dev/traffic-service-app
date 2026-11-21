"""A stand-in for the Flask application object that any module can import.

app.py creates the real application and registers it with bind_app(). Code that
used to refer to the global `app` (logging, config, background threads that need
an application context) uses `flask_app` instead, which always points at it.
"""
from werkzeug.local import LocalProxy

_state = {"app": None}


def bind_app(app):
    _state["app"] = app


flask_app = LocalProxy(lambda: _state["app"])
