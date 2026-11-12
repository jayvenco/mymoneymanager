"""Instellingen: profiel, wachtwoord, tweestapsverificatie, thema en over deze app."""

import base64
import io
import re

import pyotp
import qrcode
import qrcode.image.svg
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required

from ...extensions import db
from ...services import access
from ...services.household import get_household
from ...services.people import person_options, remember_person
from ...services.setup import validate_password
from ...themes import THEME_KEYS

bp = Blueprint("settings", __name__)


@bp.get("/instellingen")
@login_required
def index():
    return render_template("settings/index.html")


@bp.post("/instellingen/thema")
@bp.post("/settings/theme")
@login_required
def theme():
    value = request.form.get("theme")
    if value not in THEME_KEYS:
        abort(400)
    current_user.theme_preference = value
    db.session.commit()
    return "", 204


@bp.post("/persoon")
@login_required
def person():
    remember_person(request.form.get("person", "household"))
    target = request.form.get("next") or url_for("reports.overview")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("reports.overview")
    # ?person= in de doel-URL zou de nieuwe keuze overschrijven
    base, _, query = target.partition("?")
    params = [p for p in query.split("&") if p and not p.startswith("person=")]
    return redirect(base + ("?" + "&".join(params) if params else ""))


# --- profiel ------------------------------------------------------------------------------------


@bp.route("/instellingen/profiel", methods=["GET", "POST"])
@login_required
def profile():
    accounts = list(db.session.execute(access.visible_accounts(include_archived=False)).scalars())
    if request.method == "POST":
        theme_value = request.form.get("theme")
        if theme_value in THEME_KEYS:
            current_user.theme_preference = theme_value
        options = {o["key"] for o in person_options()}
        chosen = request.form.get("person")
        if chosen in options:
            current_user.default_person_filter = chosen
            remember_person(chosen)
        default_account = request.form.get("default_account")
        if (
            default_account
            and default_account.isdigit()
            and any(a.id == int(default_account) for a in accounts)
        ):
            current_user.set_preference("default_account", int(default_account))
        elif not default_account:
            current_user.set_preference("default_account", None)
        db.session.commit()
        flash("Profiel opgeslagen.", "success")
        return redirect(url_for("settings.profile"))
    return render_template(
        "settings/profile.html",
        accounts=accounts,
        person_opts=person_options(),
        default_account=current_user.preferences.get("default_account"),
    )


# --- wachtwoord ---------------------------------------------------------------------------------


@bp.route("/instellingen/wachtwoord", methods=["GET", "POST"])
@login_required
def password():
    error = None
    if request.method == "POST":
        if not current_user.check_password(request.form.get("current", "")):
            error = "Het huidige wachtwoord klopt niet."
        else:
            error = validate_password(request.form.get("new", ""), request.form.get("confirm", ""))
        if error is None:
            current_user.set_password(request.form["new"])
            db.session.commit()
            flash("Wachtwoord gewijzigd.", "success")
            return redirect(url_for("settings.index"))
    return render_template("settings/password.html", error=error), (422 if error else 200)


# --- 2FA ----------------------------------------------------------------------------------------


def _qr_data_uri(uri: str) -> str:
    img = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage, box_size=8)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


@bp.route("/instellingen/2fa", methods=["GET", "POST"])
@login_required
def two_factor():
    error = None
    if current_user.totp_secret:
        if request.method == "POST":
            if current_user.check_password(request.form.get("password", "")):
                current_user.totp_secret = None
                db.session.commit()
                flash("Tweestapsverificatie is uitgeschakeld.", "success")
                return redirect(url_for("settings.two_factor"))
            error = "Wachtwoord onjuist."
        return render_template("settings/two_factor.html", enabled=True, error=error)

    secret = session.get("totp_setup")
    if not secret or request.args.get("nieuw"):
        secret = session["totp_setup"] = pyotp.random_base32()
    if request.method == "POST":
        if pyotp.TOTP(secret).verify(
            re.sub(r"\s", "", request.form.get("code", "")), valid_window=1
        ):
            current_user.totp_secret = secret
            session.pop("totp_setup", None)
            db.session.commit()
            flash("Tweestapsverificatie is ingeschakeld.", "success")
            return redirect(url_for("settings.two_factor"))
        error = "Ongeldige code. Controleer de tijd op je telefoon en probeer opnieuw."
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=current_user.username, issuer_name="MyMoneyManager"
    )
    return render_template(
        "settings/two_factor.html", enabled=False, error=error, secret=secret, qr=_qr_data_uri(uri)
    ), (422 if error else 200)


# --- over ---------------------------------------------------------------------------------------


@bp.get("/instellingen/over")
@login_required
def about():
    cfg = current_app.config
    return render_template(
        "settings/about.html",
        version=cfg["APP_VERSION"],
        sha=cfg["GIT_SHA"],
        build_date=cfg["BUILD_DATE"],
        household=get_household(),
    )
