"""Setup-wizard: alleen beschikbaar zolang er geen gebruikers bestaan."""

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for
from flask_login import login_user

from ...services.setup import run_setup, setup_needed

bp = Blueprint("setup", __name__)


@bp.route("/setup", methods=["GET", "POST"])
def index():
    if not setup_needed():
        abort(404)
    errors: dict[str, str] = {}
    if request.method == "POST":
        result = run_setup(request.form)
        if result.ok and result.user is not None:
            current_app.extensions["mmm_setup_done"] = True
            login_user(result.user)
            return redirect(url_for("reports.overview"))
        errors = result.errors
    return render_template("setup/wizard.html", errors=errors, form=request.form)
