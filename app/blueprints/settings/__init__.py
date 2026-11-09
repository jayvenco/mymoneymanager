"""Instellingen: hub, thema en persoonsfilter (uitgebreid in latere fasen)."""

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from ...extensions import db
from ...services.people import remember_person

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
    if value not in {"light", "dark", "system"}:
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
