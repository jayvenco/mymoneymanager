"""Rapportages: het startscherm van de app."""

from flask import Blueprint, render_template
from flask_login import login_required

from ...extensions import db
from ...models import Household, Member

bp = Blueprint("reports", __name__)


@bp.route("/")
@login_required
def overview():
    household = db.session.execute(db.select(Household)).scalars().first()
    members = db.session.execute(db.select(Member).order_by(Member.sort_order)).scalars().all()
    return render_template("reports/overview.html", household=household, members=members)
