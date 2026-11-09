"""Inloggen, uitloggen en tweestapsverificatie (TOTP)."""

import time
from datetime import datetime
from urllib.parse import urlparse

import pyotp
from flask import Blueprint, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from ...extensions import db, limiter
from ...models import User

bp = Blueprint("auth", __name__)

PENDING_2FA_SECONDS = 300


def _safe_next(target: str | None) -> str:
    if not target:
        return url_for("reports.overview")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return url_for("reports.overview")
    return target


def _finish_login(user: User, remember: bool) -> None:
    session.permanent = True
    login_user(user, remember=remember)
    user.last_login = datetime.now().replace(microsecond=0)
    db.session.commit()


@bp.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("reports.overview"))
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        remember = request.form.get("remember") == "on"
        user = db.session.execute(
            db.select(User).where(User.username == username)
        ).scalar_one_or_none()
        if user is None:
            User.burn_password_check(password)
        if user is not None and user.check_password(password):
            if user.totp_secret:
                session["pending_2fa"] = {
                    "uid": user.id,
                    "remember": remember,
                    "next": request.args.get("next"),
                    "ts": time.time(),
                }
                return redirect(url_for("auth.two_factor"))
            _finish_login(user, remember)
            return redirect(_safe_next(request.args.get("next")))
        error = "Onjuiste gebruikersnaam of wachtwoord."
    return render_template("auth/login.html", error=error), (401 if error else 200)


@bp.route("/login/2fa", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def two_factor():
    pending = session.get("pending_2fa")
    if not pending or time.time() - pending["ts"] > PENDING_2FA_SECONDS:
        session.pop("pending_2fa", None)
        return redirect(url_for("auth.login"))
    user = db.session.get(User, pending["uid"])
    if user is None or not user.totp_secret:
        session.pop("pending_2fa", None)
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        code = (request.form.get("code") or "").replace(" ", "")
        if pyotp.TOTP(user.totp_secret).verify(code, valid_window=1):
            session.pop("pending_2fa", None)
            _finish_login(user, bool(pending["remember"]))
            return redirect(_safe_next(pending.get("next")))
        error = "Ongeldige code. Probeer het opnieuw."
    return render_template("auth/two_factor.html", error=error), (401 if error else 200)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for("auth.login"))
