"""Huishouden beheren (beheerder): naam, financiële maand, leden en logins."""

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import select

from ...extensions import db
from ...models import Member, User
from ...services.household import get_household, household_id, members
from ...services.setup import validate_password
from .. import admin_required

bp = Blueprint("household", __name__, url_prefix="/instellingen/huishouden")

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_USERNAME = re.compile(r"^[a-z0-9_.-]{3,40}$")


def _member(member_id: int) -> Member:
    member = db.session.get(Member, member_id)
    if member is None or member.household_id != household_id():
        abort(404)
    return member


@bp.get("/")
@admin_required
def index():
    return render_template(
        "household/index.html", household=get_household(), members=members(include_archived=True)
    )


@bp.post("/")
@admin_required
def save():
    household = get_household()
    name = (request.form.get("name") or "").strip()
    try:
        start_day = int(request.form.get("start_day", ""))
    except ValueError:
        start_day = 0
    if not name or not 1 <= start_day <= 28:
        flash("Vul een naam in en kies een startdag tussen 1 en 28.", "error")
    else:
        household.name, household.financial_month_start_day = name[:120], start_day
        db.session.commit()
        flash("Huishouden opgeslagen.", "success")
    return redirect(url_for("household.index"))


@bp.post("/leden")
@admin_required
def add_member():
    name = (request.form.get("name") or "").strip()
    color = request.form.get("color") or "#64748b"
    if not name:
        flash("Vul een naam in.", "error")
    else:
        order = (
            db.session.execute(
                select(db.func.coalesce(db.func.max(Member.sort_order), -1))
            ).scalar_one()
            + 1
        )
        db.session.add(
            Member(
                household_id=household_id(),
                name=name[:80],
                color=color if _COLOR.match(color) else "#64748b",
                avatar_initials=Member.initials_for(name),
                sort_order=order,
            )
        )
        db.session.commit()
        flash(f"{name} is toegevoegd.", "success")
    return redirect(url_for("household.index"))


@bp.post("/leden/<int:member_id>")
@admin_required
def update_member(member_id: int):
    member = _member(member_id)
    name = (request.form.get("name") or "").strip()
    color = request.form.get("color") or member.color
    if not name:
        flash("Vul een naam in.", "error")
    else:
        member.name = name[:80]
        member.avatar_initials = Member.initials_for(name)
        member.color = color if _COLOR.match(color) else member.color
        db.session.commit()
        flash("Lid bijgewerkt.", "success")
    return redirect(url_for("household.index"))


@bp.post("/leden/<int:member_id>/archiveer")
@admin_required
def archive_member(member_id: int):
    member = _member(member_id)
    if not member.is_archived and len([m for m in members() if m.id != member.id]) == 0:
        flash("Er moet minimaal één actief lid overblijven.", "error")
    elif member.user is not None and member.user.is_admin:
        flash("Het lid van de beheerder kan niet worden gearchiveerd.", "error")
    else:
        member.is_archived = not member.is_archived
        db.session.commit()
        flash("Lid gearchiveerd." if member.is_archived else "Lid hersteld.", "success")
    return redirect(url_for("household.index"))


@bp.post("/leden/<int:member_id>/login")
@admin_required
def create_login(member_id: int):
    member = _member(member_id)
    username = (request.form.get("username") or "").strip().lower()
    password = request.form.get("password", "")
    error = None
    if member.user is not None:
        error = "Dit lid heeft al een login."
    elif not _USERNAME.match(username):
        error = "Gebruikersnaam: 3–40 tekens (letters, cijfers, . _ -)."
    elif db.session.execute(select(User.id).where(User.username == username)).first():
        error = "Deze gebruikersnaam is al in gebruik."
    else:
        error = validate_password(password)
    if error:
        flash(error, "error")
    else:
        user = User(username=username, member_id=member.id, is_admin=False)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash(f"Login '{username}' aangemaakt voor {member.name}.", "success")
    return redirect(url_for("household.index"))


@bp.post("/leden/<int:member_id>/login/wachtwoord")
@admin_required
def reset_password(member_id: int):
    member = _member(member_id)
    error = validate_password(request.form.get("password", ""))
    if member.user is None:
        error = "Dit lid heeft geen login."
    if error:
        flash(error, "error")
    else:
        member.user.set_password(request.form["password"])
        db.session.commit()
        flash("Wachtwoord opnieuw ingesteld.", "success")
    return redirect(url_for("household.index"))


@bp.post("/leden/<int:member_id>/login/verwijder")
@admin_required
def remove_login(member_id: int):
    member = _member(member_id)
    user = member.user
    if user is None:
        flash("Dit lid heeft geen login.", "error")
    elif user.is_admin:
        flash("De beheerder-login kan niet worden verwijderd.", "error")
    else:
        # Verwijs gemaakte-door-verwijzingen weg; de transacties zelf blijven bestaan.
        db.session.execute(
            db.text(
                'UPDATE "transaction" SET created_by_user_id = NULL WHERE created_by_user_id = :u'
            ),
            {"u": user.id},
        )
        db.session.execute(
            db.text(
                'UPDATE "transaction" SET updated_by_user_id = NULL WHERE updated_by_user_id = :u'
            ),
            {"u": user.id},
        )
        db.session.execute(
            db.text("UPDATE audit_log SET user_id = NULL WHERE user_id = :u"), {"u": user.id}
        )
        db.session.delete(user)
        db.session.commit()
        flash(f"Login van {member.name} verwijderd. De gegevens blijven bestaan.", "success")
    return redirect(url_for("household.index"))
