"""Profile pages and profile photos."""
import io
import re
import warnings
from PIL import Image, ImageOps, UnidentifiedImageError
from flask import redirect, render_template, session, url_for
from pathlib import Path
from trafficlens.config import ENGINEERING_SPECIALIZATIONS, PROFILE_JOB_TITLES, PROFILE_PHOTO_MAX_BYTES, PROFILE_PHOTO_SIZE
from trafficlens.runtime import flask_app


def _profile_redirect():
    return redirect(url_for("profile.admin_profile" if session.get("role") == "admin" else "profile.profile"))


def _profile_values(user):
    values = dict(user)
    if not values.get("first_name") or not values.get("last_name"):
        parts = (values.get("name") or "").strip().split(None, 1)
        values["first_name"] = values.get("first_name") or (parts[0] if parts else "")
        values["last_name"] = values.get("last_name") or (parts[1] if len(parts) > 1 else "")
    for field in (
        "phone_number", "job_title", "engineering_specialization",
        "company_name", "bio",
    ):
        values[field] = values.get(field) or ""
    return values


def _profile_photo_directory():
    return Path(flask_app.instance_path) / "profile_images"


def _delete_profile_photo(filename, user_id):
    if not filename or not re.fullmatch(rf"user-{user_id}-[a-f0-9]{{32}}\.jpg", filename):
        return
    try:
        (_profile_photo_directory() / filename).unlink(missing_ok=True)
    except OSError:
        flask_app.logger.warning(
            "Could not remove superseded profile photo for user %s", user_id,
            exc_info=True,
        )


def _process_profile_photo(upload):
    if not upload or not upload.filename:
        raise ValueError("Choose a JPG, PNG, or WebP image to upload.")
    try:
        raw = upload.stream.read(PROFILE_PHOTO_MAX_BYTES + 1)
    finally:
        upload.close()
    if len(raw) > PROFILE_PHOTO_MAX_BYTES:
        raise ValueError("Profile photos must be 5 MB or smaller.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise ValueError("Use a JPG, PNG, or WebP image.")
                if source.width * source.height > 20_000_000:
                    raise ValueError("Choose an image with 20 megapixels or fewer.")
                source.verify()
            with Image.open(io.BytesIO(raw)) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail(PROFILE_PHOTO_SIZE, Image.Resampling.LANCZOS)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=88, optimize=True)
                return output.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("The selected image could not be opened. Choose a valid JPG, PNG, or WebP image.") from exc


def _render_profile(values, errors=None, status=200):
    return render_template(
        "profile.html",
        profile_user=values,
        profile_role="Admin" if session.get("role") == "admin" else "User",
        job_titles=PROFILE_JOB_TITLES,
        engineering_specializations=ENGINEERING_SPECIALIZATIONS,
        profile_errors=errors or {},
    ), status
