"""Budgetten: maandoverzicht, instellen en kopiëren."""

import json
import re

from flask import Blueprint, abort, make_response, render_template, request
from flask_login import login_required
from sqlalchemy import select

from ...extensions import db
from ...models import Budget, Category
from ...services import budgets as svc
from ...services.household import get_household, household_id, members
from ...services.people import current_person
from ...services.periods import financial_month, shift_ym
from ...services.transactions import _int
from ...utils.dates import month_label
from ...utils.money import MoneyError, format_number, parse_money

bp = Blueprint("budgets", __name__, url_prefix="/budgetten")
_YM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _ym() -> str:
    raw = request.args.get("maand", "")
    return raw if _YM.match(raw) else svc.current_ym()


def _hx(message: str, category: str = "success"):
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps(
        {"toast": {"message": message, "category": category}, "reload-page": {}, "close-modal": {}}
    )
    return resp


@bp.get("/")
@login_required
def overview():
    ym = _ym()
    person = current_person()
    lines = svc.budget_lines(ym, person)
    period = financial_month(ym, get_household().financial_month_start_day)
    total_available = sum(line.available_cents for line in lines)
    total_spent = sum(line.spent_cents for line in lines)
    return render_template(
        "budgets/overview.html",
        lines=lines,
        ym=ym,
        label=month_label(ym),
        prev_ym=shift_ym(ym, -1),
        next_ym=shift_ym(ym, 1),
        period=period,
        is_current=ym == svc.current_ym(),
        total_available=total_available,
        total_spent=total_spent,
        person=person,
    )


def _form_ctx(budget: Budget | None = None, form=None, errors=None, ym: str | None = None):
    values = {
        "category_id": "",
        "member_id": "",
        "amount": "",
        "rollover": "",
        "scope": "default",
    }
    if budget is not None:
        values.update(
            category_id=budget.category_id,
            member_id=budget.member_id or "",
            amount=format_number(budget.amount_cents),
            rollover="1" if budget.rollover else "",
            scope="month" if budget.month else "default",
        )
    if form is not None:
        values.update(form.items())
    cats = list(
        db.session.execute(
            select(Category)
            .where(
                Category.household_id == household_id(),
                Category.kind == "expense",
                Category.is_archived.is_(False),
                Category.exclude_from_reports.is_(False),
            )
            .order_by(Category.sort_order, Category.name)
        ).scalars()
    )
    tree = [(p, [c for c in cats if c.parent_id == p.id]) for p in cats if p.parent_id is None]
    return {
        "budget": budget,
        "values": values,
        "errors": errors or {},
        "tree": tree,
        "members": members(),
        "ym": ym or svc.current_ym(),
        "label": month_label(ym or svc.current_ym()),
    }


def _validate(form, ym: str, existing: Budget | None):
    errors: dict[str, str] = {}
    category = db.session.get(Category, _int(form.get("category_id")) or 0)
    if category is None or category.household_id != household_id() or category.kind != "expense":
        errors["category_id"] = "Kies een uitgavencategorie."
    member_id = _int(form.get("member_id"))
    if member_id is not None and member_id not in {m.id for m in members(include_archived=True)}:
        errors["member_id"] = "Ongeldig lid."
    try:
        amount = parse_money(form.get("amount", ""))
        if amount < 0:
            raise MoneyError("Het bedrag mag niet negatief zijn.")
    except MoneyError as exc:
        errors["amount"] = str(exc)
        amount = 0
    month = ym if form.get("scope") == "month" else None
    if not errors:
        clash = db.session.execute(
            select(Budget).where(
                Budget.household_id == household_id(),
                Budget.category_id == category.id,
                Budget.member_id == member_id
                if member_id is not None
                else Budget.member_id.is_(None),
                Budget.month == month if month else Budget.month.is_(None),
            )
        ).scalar_one_or_none()
        if clash is not None and (existing is None or clash.id != existing.id):
            errors["category_id"] = (
                "Voor deze categorie en periode bestaat al een budget; bewerk dat budget."
            )
    return errors, category, member_id, amount, month


@bp.get("/nieuw")
@login_required
def new_form():
    return render_template("budgets/form.html", **_form_ctx(ym=_ym()))


@bp.post("/")
@login_required
def create():
    ym = _ym()
    errors, category, member_id, amount, month = _validate(request.form, ym, None)
    if errors:
        return render_template(
            "budgets/form.html", **_form_ctx(form=request.form, errors=errors, ym=ym)
        ), 422
    db.session.add(
        Budget(
            household_id=household_id(),
            category_id=category.id,
            member_id=member_id,
            month=month,
            amount_cents=amount,
            rollover=request.form.get("rollover") == "1",
        )
    )
    db.session.commit()
    return _hx("Budget opgeslagen.")


def _get(budget_id: int) -> Budget:
    budget = db.session.get(Budget, budget_id)
    if budget is None or budget.household_id != household_id():
        abort(404)
    return budget


@bp.get("/<int:budget_id>/bewerken")
@login_required
def edit_form(budget_id: int):
    return render_template("budgets/form.html", **_form_ctx(_get(budget_id), ym=_ym()))


@bp.post("/<int:budget_id>")
@login_required
def update(budget_id: int):
    budget = _get(budget_id)
    ym = _ym()
    errors, category, member_id, amount, month = _validate(request.form, budget.month or ym, budget)
    if errors:
        return render_template(
            "budgets/form.html", **_form_ctx(budget, request.form, errors, ym)
        ), 422
    budget.category_id, budget.member_id, budget.amount_cents = category.id, member_id, amount
    budget.rollover = request.form.get("rollover") == "1"
    budget.month = month
    db.session.commit()
    return _hx("Budget bijgewerkt.")


@bp.post("/<int:budget_id>/verwijder")
@login_required
def delete(budget_id: int):
    db.session.delete(_get(budget_id))
    db.session.commit()
    return _hx("Budget verwijderd.")


@bp.post("/kopieer")
@login_required
def copy():
    ym = request.form.get("maand", "")
    if not _YM.match(ym):
        abort(400)
    created = svc.copy_previous_month(ym)
    if not created:
        return _hx("Er was niets om te kopiëren.", "info")
    return _hx(f"{created} budget(ten) gekopieerd van vorige maand.")
