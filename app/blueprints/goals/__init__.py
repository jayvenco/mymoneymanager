"""Spaardoelen."""

import json
import re
from datetime import date

from flask import Blueprint, abort, make_response, render_template, request
from flask_login import login_required

from ...extensions import db
from ...models import SavingsGoal
from ...services import access
from ...services import goals as svc
from ...services.household import household_id, members
from ...services.people import current_person
from ...services.transactions import _int
from ...utils.dates import parse_date
from ...utils.money import MoneyError, format_number, parse_money

bp = Blueprint("goals", __name__, url_prefix="/spaardoelen")
_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hx(message: str, category: str = "success"):
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps(
        {"toast": {"message": message, "category": category}, "reload-page": {}, "close-modal": {}}
    )
    return resp


@bp.get("/")
@login_required
def list_view():
    items = svc.progress_for(current_person())
    return render_template("goals/list.html", items=items, today=date.today())


def _form_ctx(goal: SavingsGoal | None = None, form=None, errors=None):
    values = {
        "name": "",
        "account_id": "",
        "member_id": "",
        "target": "",
        "target_date": "",
        "color": "#10b981",
    }
    if goal is not None:
        values.update(
            name=goal.name,
            account_id=goal.account_id,
            member_id=goal.member_id or "",
            target=format_number(goal.target_cents),
            target_date=goal.target_date.isoformat() if goal.target_date else "",
            color=goal.color,
        )
    if form is not None:
        values.update(form.items())
    accounts = [
        a for a in db.session.execute(access.visible_accounts(include_archived=False)).scalars()
    ]
    return {
        "goal": goal,
        "values": values,
        "errors": errors or {},
        "accounts": accounts,
        "members": members(),
    }


def _validate(form):
    errors: dict[str, str] = {}
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Vul een naam in."
    account = access.get_visible_account(_int(form.get("account_id")) or 0)
    if account is None:
        errors["account_id"] = "Kies een rekening."
    member_id = _int(form.get("member_id"))
    if member_id is not None and member_id not in {m.id for m in members(include_archived=True)}:
        errors["member_id"] = "Ongeldig lid."
    try:
        target = parse_money(form.get("target", ""))
        if target <= 0:
            errors["target"] = "Het doelbedrag moet groter zijn dan nul."
    except MoneyError as exc:
        errors["target"] = str(exc)
        target = 0
    target_date = None
    if (form.get("target_date") or "").strip():
        try:
            target_date = parse_date(form["target_date"])
        except ValueError as exc:
            errors["target_date"] = str(exc)
    color = form.get("color") if _COLOR.match(form.get("color") or "") else "#10b981"
    return errors, {
        "name": name[:120],
        "account_id": account.id if account else None,
        "member_id": member_id,
        "target_cents": target,
        "target_date": target_date,
        "color": color,
    }


def _get(goal_id: int) -> SavingsGoal:
    goal = db.session.get(SavingsGoal, goal_id)
    if (
        goal is None
        or goal.household_id != household_id()
        or not access.can_see_account(goal.account_id)
    ):
        abort(404)
    return goal


@bp.get("/nieuw")
@login_required
def new_form():
    return render_template("goals/form.html", **_form_ctx())


@bp.post("/")
@login_required
def create():
    errors, data = _validate(request.form)
    if errors:
        return render_template(
            "goals/form.html", **_form_ctx(form=request.form, errors=errors)
        ), 422
    db.session.add(SavingsGoal(household_id=household_id(), **data))
    db.session.commit()
    return _hx("Spaardoel aangemaakt.")


@bp.get("/<int:goal_id>/bewerken")
@login_required
def edit_form(goal_id: int):
    return render_template("goals/form.html", **_form_ctx(_get(goal_id)))


@bp.post("/<int:goal_id>")
@login_required
def update(goal_id: int):
    goal = _get(goal_id)
    errors, data = _validate(request.form)
    if errors:
        return render_template("goals/form.html", **_form_ctx(goal, request.form, errors)), 422
    for key, value in data.items():
        setattr(goal, key, value)
    db.session.commit()
    return _hx("Spaardoel bijgewerkt.")


@bp.post("/<int:goal_id>/verwijder")
@login_required
def delete(goal_id: int):
    db.session.delete(_get(goal_id))
    db.session.commit()
    return _hx("Spaardoel verwijderd.")
