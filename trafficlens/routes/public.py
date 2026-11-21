"""Home page, sign-up, sign-in and sign-out."""
import sqlite3
from flask import Blueprint, flash, make_response, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from trafficlens.db import db
from trafficlens.security import rate_limit as login_throttle

bp = Blueprint("public", __name__)


@bp.route("/")
def index():
    if session.get("role") == "admin":
        return redirect(url_for("admin.admin"))
    if session.get("uid"):
        return redirect(url_for("client.dashboard"))
    return render_template("index.html")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        n, e, p = (request.form.get(k, "").strip() for k in ("name", "email", "password"))
        if not (n and e and len(p) >= 6): flash("Fill all fields. Password needs at least 6 characters."); return render_template("auth.html", mode="register")
        try:
            db().execute("INSERT INTO users(name,email,pw) VALUES(?,?,?)", (n, e.lower(), generate_password_hash(p))); db().commit()
        except sqlite3.IntegrityError:
            flash("This email is already registered."); return render_template("auth.html", mode="register")
        flash("Account created. Sign in to continue."); return redirect(url_for("public.login"))
    return render_template("auth.html", mode="register")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        wait = login_throttle.seconds_until_allowed(email, request.remote_addr)
        if wait:
            flash(f"Too many failed sign-in attempts. Try again in {-(-wait // 60)} minute(s).")
            blocked = make_response(render_template("auth.html", mode="login"), 429)
            blocked.headers["Retry-After"] = str(wait)
            return blocked
        u = db().execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if u and check_password_hash(u["pw"], request.form.get("password", "")):
            login_throttle.clear(email)
            session.clear(); session.update(
                uid=u["id"], name=u["name"], role=u["role"],
                profile_image=bool(u["profile_image"]),
            )
            nxt = request.args.get("next")
            return redirect(nxt if nxt and nxt.startswith("/") else url_for("admin.admin" if u["role"] == "admin" else "client.dashboard"))
        login_throttle.record_failure(email, request.remote_addr)
        flash("Wrong email or password.")
    return render_template("auth.html", mode="login")


@bp.route("/logout")
def logout(): session.clear(); return redirect(url_for("public.index"))
