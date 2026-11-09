"""Vaste lasten en terugkerende inkomsten."""

import json
from datetime import date

from flask import Blueprint, abort, make_response, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import select

from ...extensions import db
from ...models import FREQUENCIES, RecurringRule
from ...services import access
from ...services import recurring as svc
from ...services import transactions as tx_svc
from ...services.household import household_id, members_by_id
from ...services.transactions import _int
from ...utils.dates import parse_date
from ...utils.money import MoneyError, format_number, parse_money
from ..transactions import form_context

bp = Blueprint("recurring", __name__, url_prefix="/vaste-lasten")

FREQ_LABELS = {
    "weekly": "Wekelijks",
    "monthly": "Maandelijks",
    "quarterly": "Per kwartaal",
    "yearly": "Jaarlijks",
}


def _hx(message: str, category: str = "success"):
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps(
        {"toast": {"message": message, "category": category}, "reload-page": {}, "close-modal": {}}
    )
    return resp


def _get(rule_id: int) -> RecurringRule:
    rule = db.session.get(RecurringRule, rule_id)
    if (
        rule is None
        or rule.household_id != household_id()
        or not access.can_see_account(rule.account_id)
    ):
        abort(404)
    return rule


@bp.get("/")
@login_required
def list_view():
    rules = svc.visible_rules()
    members = members_by_id()
    upcoming = svc.upcoming(30)
    per_member: dict[str, int] = {}
    total = 0
    for item in upcoming:
        if item.rule.type == "transfer":
            continue
        sign = 1 if item.rule.type == "income" else -1
        owner = members.get(item.rule.account.owner_member_id)
        key = owner.name if owner else "Gezamenlijk"
        per_member[key] = per_member.get(key, 0) + sign * item.rule.amount_cents
        total += sign * item.rule.amount_cents
    return render_template(
        "recurring/list.html",
        rules=rules,
        upcoming=upcoming,
        per_member=per_member,
        total=total,
        pending=svc.pending_confirmations(),
        members=members,
        freq=FREQ_LABELS,
        today=date.today(),
    )


def _form_ctx(rule: RecurringRule | None = None, form=None, errors=None):
    ctx = form_context(None, None, errors)
    values = {
        "type": "expense",
        "name": "",
        "amount": "",
        "frequency": "monthly",
        "interval": "1",
        "date": date.today().isoformat(),
        "end_date": "",
        "auto_create": "",
        "account_id": ctx["values"].get("account_id"),
    }
    if rule is not None:
        tags = []
        try:
            from ...models import Tag

            ids = json.loads(rule.tag_ids_json or "[]")
            tags = (
                [t.name for t in db.session.execute(select(Tag).where(Tag.id.in_(ids))).scalars()]
                if ids
                else []
            )
        except ValueError:
            pass
        values.update(
            type=rule.type,
            name=rule.name,
            amount=format_number(rule.amount_cents),
            frequency=rule.frequency,
            interval=str(rule.interval),
            date=rule.next_date.isoformat(),
            end_date=rule.end_date.isoformat() if rule.end_date else "",
            auto_create="1" if rule.auto_create else "",
            account_id=rule.account_id,
            to_account_id=rule.to_account_id,
            category_id=rule.category_id,
            description=rule.description,
            payee=rule.payee,
            notes=rule.notes,
            tags=", ".join(tags),
        )
    if form is not None:
        values.update(form.items())
    ctx.update(values=values, errors=errors or {}, rule=rule, freq=FREQ_LABELS)
    return ctx


def _clean(form, existing: RecurringRule | None):
    errors: dict[str, str] = {}
    data: dict = {}
    try:
        data = tx_svc.clean_form(form, current_user, existing=None)
    except tx_svc.TransactionError as exc:
        errors.update(exc.errors)
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Geef de vaste last een naam."
    frequency = form.get("frequency")
    if frequency not in FREQUENCIES:
        errors["frequency"] = "Kies een frequentie."
    interval = _int(form.get("interval")) or 0
    if not 1 <= interval <= 52:
        errors["interval"] = "Kies een interval tussen 1 en 52."
    end_date = None
    if (form.get("end_date") or "").strip():
        try:
            end_date = parse_date(form["end_date"])
        except ValueError as exc:
            errors["end_date"] = str(exc)
    if end_date and data.get("date") and end_date < data["date"]:
        errors["end_date"] = "De einddatum ligt vóór de startdatum."
    if errors:
        raise tx_svc.TransactionError(errors)
    return {
        "name": name[:120],
        "type": data["type"],
        "amount_cents": data["amount_cents"],
        "account_id": data["account_id"],
        "to_account_id": data["to_account_id"],
        "category_id": data["category_id"],
        "description": data["description"],
        "payee": data["payee"],
        "notes": data["notes"],
        "tag_ids_json": json.dumps([t.id for t in tx_svc.find_or_create_tags(data["tag_names"])]),
        "frequency": frequency,
        "interval": interval,
        "next_date": data["date"],
        "anchor_day": data["date"].day,
        "end_date": end_date,
        "auto_create": form.get("auto_create") == "1",
    }


@bp.get("/nieuw")
@login_required
def new_form():
    return render_template("recurring/form.html", **_form_ctx())


@bp.post("/")
@login_required
def create():
    try:
        data = _clean(request.form, None)
    except tx_svc.TransactionError as exc:
        return render_template(
            "recurring/form.html", **_form_ctx(form=request.form, errors=exc.errors)
        ), 422
    db.session.add(RecurringRule(household_id=household_id(), **data))
    db.session.commit()
    return _hx("Vaste last opgeslagen.")


@bp.get("/<int:rule_id>/bewerken")
@login_required
def edit_form(rule_id: int):
    return render_template("recurring/form.html", **_form_ctx(_get(rule_id)))


@bp.post("/<int:rule_id>")
@login_required
def update(rule_id: int):
    rule = _get(rule_id)
    try:
        data = _clean(request.form, rule)
    except tx_svc.TransactionError as exc:
        return render_template(
            "recurring/form.html", **_form_ctx(rule, request.form, exc.errors)
        ), 422
    for key, value in data.items():
        setattr(rule, key, value)
    rule.is_active = bool(not rule.end_date or rule.next_date <= rule.end_date)
    db.session.commit()
    return _hx("Vaste last bijgewerkt.")


@bp.post("/<int:rule_id>/verwijder")
@login_required
def delete(rule_id: int):
    db.session.delete(_get(rule_id))
    db.session.commit()
    return _hx("Vaste last verwijderd.")


@bp.post("/<int:rule_id>/actief")
@login_required
def toggle(rule_id: int):
    rule = _get(rule_id)
    rule.is_active = not rule.is_active
    db.session.commit()
    return _hx("Vaste last hervat." if rule.is_active else "Vaste last gepauzeerd.")


@bp.post("/<int:rule_id>/bevestig")
@login_required
def confirm(rule_id: int):
    rule = _get(rule_id)
    if rule.next_date > date.today() or not rule.is_active:
        return _hx("Deze termijn is nog niet aan de beurt.", "error")
    amount = None
    if (request.form.get("amount") or "").strip():
        try:
            amount = parse_money(request.form["amount"])
            if amount <= 0:
                raise MoneyError("Het bedrag moet groter zijn dan nul.")
        except MoneyError as exc:
            return _hx(str(exc), "error")
    svc.confirm(rule, current_user, amount_cents=amount)
    return _hx("Transactie aangemaakt.")


@bp.post("/<int:rule_id>/overslaan")
@login_required
def skip(rule_id: int):
    rule = _get(rule_id)
    if rule.next_date > date.today() or not rule.is_active:
        return _hx("Deze termijn is nog niet aan de beurt.", "error")
    svc.skip(rule)
    return _hx("Termijn overgeslagen.")
