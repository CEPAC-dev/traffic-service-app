"""Profile pages, profile photos and password change."""
import os
import re
import secrets
import sqlite3
from flask import Blueprint, abort, flash, redirect, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from trafficlens.config import ENGINEERING_SPECIALIZATIONS, PROFILE_JOB_TITLES, PROFILE_PHOTO_MAX_BYTES
from trafficlens.db import db
from trafficlens.runtime import flask_app
from trafficlens.security.decorators import admin_required, login_required
from trafficlens.services import user_profile as user_profile_svc

bp = Blueprint("profile", __name__)


@bp.get("/profile")
@login_required
def profile():
    if session.get("role") == "admin":
        return redirect(url_for("profile.admin_profile"))
    user = db().execute(
        "SELECT * FROM users WHERE id=? AND role='client'",
        (session["uid"],),
    ).fetchone()
    if not user:
        abort(404)
    return user_profile_svc._render_profile(user_profile_svc._profile_values(user))


@bp.get("/admin/profile")
@admin_required
def admin_profile():
    user = db().execute(
        "SELECT * FROM users WHERE id=? AND role='admin'",
        (session["uid"],),
    ).fetchone()
    if not user:
        abort(404)
    return user_profile_svc._render_profile(user_profile_svc._profile_values(user))


@bp.post("/profile/photo")
@login_required
def update_profile_photo():
    user_id = session["uid"]
    if (
        request.content_length is not None
        and request.content_length > PROFILE_PHOTO_MAX_BYTES + 128 * 1024
    ):
        flash("Profile photos must be 5 MB or smaller.", "error")
        return user_profile_svc._profile_redirect()
    user = db().execute(
        "SELECT profile_image FROM users WHERE id=? AND role=?",
        (user_id, session.get("role")),
    ).fetchone()
    if not user:
        abort(404)

    if request.form.get("remove_photo") == "1":
        old_filename = user["profile_image"]
        db().execute("UPDATE users SET profile_image=NULL WHERE id=?", (user_id,))
        db().commit()
        session.pop("profile_image", None)
        user_profile_svc._delete_profile_photo(old_filename, user_id)
        flash("Profile photo removed.", "success")
        return user_profile_svc._profile_redirect()

    try:
        image_data = user_profile_svc._process_profile_photo(request.files.get("photo"))
    except ValueError as exc:
        flash(str(exc), "error")
        return user_profile_svc._profile_redirect()

    photo_dir = user_profile_svc._profile_photo_directory()
    photo_dir.mkdir(parents=True, exist_ok=True)
    filename = f"user-{user_id}-{secrets.token_hex(16)}.jpg"
    final_path = photo_dir / filename
    temporary_path = photo_dir / f".{filename}.tmp"
    old_filename = user["profile_image"]
    try:
        temporary_path.write_bytes(image_data)
        os.replace(temporary_path, final_path)
        db().execute(
            "UPDATE users SET profile_image=? WHERE id=?",
            (filename, user_id),
        )
        db().commit()
    except OSError:
        temporary_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        flask_app.logger.exception("Unable to save profile photo for user %s", user_id)
        flash("The profile photo could not be saved. Please try again.", "error")
        return user_profile_svc._profile_redirect()
    except sqlite3.Error:
        db().rollback()
        temporary_path.unlink(missing_ok=True)
        final_path.unlink(missing_ok=True)
        flask_app.logger.exception("Unable to update profile photo record for user %s", user_id)
        flash("The profile photo could not be saved. Please try again.", "error")
        return user_profile_svc._profile_redirect()

    session["profile_image"] = True
    user_profile_svc._delete_profile_photo(old_filename, user_id)
    flash("Profile photo updated.", "success")
    return user_profile_svc._profile_redirect()


@bp.get("/profile/photo")
@login_required
def profile_photo():
    user = db().execute(
        "SELECT profile_image FROM users WHERE id=? AND role=?",
        (session["uid"], session.get("role")),
    ).fetchone()
    if not user or not user["profile_image"]:
        abort(404)
    filename = user["profile_image"]
    if not re.fullmatch(rf"user-{session['uid']}-[a-f0-9]{{32}}\.jpg", filename):
        flask_app.logger.error("Invalid profile image filename stored for user %s", session["uid"])
        abort(404)
    image_path = user_profile_svc._profile_photo_directory() / filename
    if not image_path.is_file():
        abort(404)
    response = send_file(image_path, mimetype="image/jpeg", conditional=True, max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.post("/profile")
@login_required
def update_profile():
    first_name = request.form.get("first_name", "").strip()
    last_name = request.form.get("last_name", "").strip()
    if not first_name and not last_name and request.form.get("name"):
        name_parts = request.form.get("name", "").strip().split(None, 1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""
    email = request.form.get("email", "").strip().lower()
    phone_number = request.form.get("phone_number", "").strip()
    job_title = request.form.get("job_title", "").strip()
    specialization = request.form.get("engineering_specialization", "").strip()
    company_name = request.form.get("company_name", "").strip()
    bio = request.form.get("bio", "").strip()
    values = {
        "first_name": first_name,
        "last_name": last_name,
        "name": f"{first_name} {last_name}".strip(),
        "email": email,
        "phone_number": phone_number,
        "job_title": job_title,
        "engineering_specialization": specialization,
        "company_name": company_name,
        "bio": bio,
    }
    errors = {}
    if not first_name or len(first_name) > 60:
        errors["first_name"] = "Enter your first name (up to 60 characters)."
    if not last_name or len(last_name) > 60:
        errors["last_name"] = "Enter your last name (up to 60 characters)."
    if (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        errors["email"] = "Enter a valid email address."
    phone_digits = "".join(char for char in phone_number if char.isdigit())
    if (
        not 7 <= len(phone_digits) <= 15
        or not re.fullmatch(r"\+?[0-9().\s-]+", phone_number)
    ):
        errors["phone_number"] = "Enter a valid international phone number (7–15 digits)."
    if job_title not in PROFILE_JOB_TITLES:
        errors["job_title"] = "Choose a job title from the list."
    if job_title == "Engineer":
        if specialization not in ENGINEERING_SPECIALIZATIONS:
            errors["engineering_specialization"] = "Choose an engineering specialization."
    else:
        values["engineering_specialization"] = ""
    if len(company_name) > 120:
        errors["company_name"] = "Company name must be 120 characters or fewer."
    if len(bio) > 500:
        errors["bio"] = "Description must be 500 characters or fewer."
    if errors:
        return user_profile_svc._render_profile(values, errors, 400)
    try:
        db().execute(
            "UPDATE users SET name=?,first_name=?,last_name=?,email=?,phone_number=?,"
            "job_title=?,engineering_specialization=?,company_name=?,bio=? WHERE id=?",
            (
                values["name"], first_name, last_name, email, phone_number,
                job_title, values["engineering_specialization"], company_name,
                bio, session["uid"],
            ),
        )
        db().commit()
    except sqlite3.IntegrityError:
        db().rollback()
        errors["email"] = "This email address is already in use."
        return user_profile_svc._render_profile(values, errors, 400)
    session["name"] = values["name"]
    flash("Profile updated.", "success")
    return user_profile_svc._profile_redirect()


@bp.post("/profile/password")
@login_required
def change_password():
    user = db().execute(
        "SELECT pw FROM users WHERE id=?", (session["uid"],)
    ).fetchone()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    if not user or not check_password_hash(user["pw"], current_password):
        flash("The current password is incorrect.", "error")
        return user_profile_svc._profile_redirect()
    if len(new_password) < 6:
        flash("The new password must be at least 6 characters.", "error")
        return user_profile_svc._profile_redirect()
    if new_password != confirm_password:
        flash("The new password and confirmation do not match.", "error")
        return user_profile_svc._profile_redirect()
    db().execute(
        "UPDATE users SET pw=? WHERE id=?",
        (generate_password_hash(new_password), session["uid"]),
    )
    db().commit()
    flash("Password changed successfully.", "success")
    return user_profile_svc._profile_redirect()
