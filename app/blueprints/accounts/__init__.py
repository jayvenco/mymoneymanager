"""Rekeningen: overzicht per eigenaar, beheer, saldo controleren en saldoverloop."""

import json
import re
from datetime import date, timedelta

from flask import Blueprint, abort, make_response, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import select

from ...extensions import db
from ...models import ACCOUNT_TYPES, Account, Category
from ...services import access
from ...services import transactions as tx_svc
from ...services.balances import account_balance, account_balances, balance_series
from ...services.household import household_id, members, members_by_id
from ...services.people import current_person
from ...utils.dates import format_date, parse_date
from ...utils.money import MoneyError, format_eur, format_number, parse_money

bp = Blueprint("accounts", __name__, url_prefix="/rekeningen")

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
ICONS = {
    "checking": "wallet",
    "savings": "piggy-bank",
    "investment": "trending-up",
    "cash": "coins",
}


def can_manage(account: Account | None, owner_member_id: int | None = None) -> bool:
    """Beheerder beheert alles; anderen alleen hun eigen rekeningen."""
    if current_user.is_admin:
        return True
    owner = account.owner_member_id if account is not None else owner_member_id
    return owner is not None and owner == current_user.member_id


def _get_account(account_id: int) -> Account:
    account = access.get_visible_account(account_id)
    if account is None:
        abort(404)
    return account


def _toast(message: str, category: str = "success", **extra):
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps(
        {
            "toast": {"message": message, "category": category},
            "accounts-changed": {},
            "close-modal": {},
            **extra,
        }
    )
    return resp


def _sparkline(values: list[int], width: int = 96, height: int = 28) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    step = width / (len(values) - 1)
    return " ".join(
        f"{i * step:.1f},{height - 2 - (v - lo) / span * (height - 4):.1f}"
        for i, v in enumerate(values)
    )


# --- list


@bp.get("/")
@login_required
def list_view():
    person = current_person()
    all_members = members()
    show_archived = request.args.get("archief") == "1"
    accounts = list(
        db.session.execute(access.visible_accounts(include_archived=show_archived)).scalars()
    )
    balances = account_balances([a.id for a in accounts])
    today = date.today()

    groups = []
    for m in all_members:
        groups.append(
            {
                "member": m,
                "title": m.name,
                "accounts": [a for a in accounts if a.owner_member_id == m.id],
            }
        )
    groups.append(
        {
            "member": None,
            "title": "Gezamenlijk",
            "accounts": [a for a in accounts if a.owner_member_id is None],
        }
    )
    if person.kind == "member":
        groups = [
            g for g in groups if g["member"] is not None and g["member"].id == person.member_id
        ]
    elif person.kind == "joint":
        groups = [g for g in groups if g["member"] is None]

    sparks = {}
    for g in groups:
        g["accounts"] = [a for a in g["accounts"] if show_archived or not a.is_archived]
        g["subtotal"] = sum(balances.get(a.id, 0) for a in g["accounts"] if not a.is_archived)
        for a in g["accounts"]:
            series = balance_series(a.id, today - timedelta(days=29), today)
            sparks[a.id] = _sparkline([v for _d, v in series])
    groups = [g for g in groups if g["accounts"] or person.is_household and g["member"] is not None]
    total = sum(g["subtotal"] for g in groups)
    return render_template(
        "accounts/list.html",
        groups=groups,
        balances=balances,
        sparks=sparks,
        total=total,
        show_archived=show_archived,
        can_manage=can_manage,
    )


# --- create / edit


def _form_ctx(account: Account | None = None, form=None, errors=None) -> dict:
    values = {
        "name": "",
        "bank": "",
        "iban_last4": "",
        "type": "checking",
        "visibility": "shared",
        "opening_balance": "0,00",
        "opening_date": date.today().isoformat(),
        "color": "#64748b",
        "owner": str(current_user.member_id)
        if current_user.member_id and not current_user.is_admin
        else "joint",
    }
    if account is not None:
        values.update(
            name=account.name,
            bank=account.bank,
            iban_last4=account.iban_last4,
            type=account.type,
            visibility=account.visibility,
            opening_balance=format_number(account.opening_balance_cents),
            opening_date=account.opening_date.isoformat(),
            color=account.color,
            owner="joint" if account.owner_member_id is None else str(account.owner_member_id),
        )
    if form is not None:
        values.update({k: v for k, v in form.items()})
    owners = [
        (str(m.id), m.name)
        for m in members()
        if current_user.is_admin or m.id == current_user.member_id
    ]
    if current_user.is_admin:
        owners.append(("joint", "Gezamenlijk"))
    return {
        "account": account,
        "values": values,
        "errors": errors or {},
        "owners": owners,
        "types": [
            ("checking", "Betaalrekening"),
            ("savings", "Spaarrekening"),
            ("investment", "Beleggen"),
            ("cash", "Contant"),
        ],
    }


def _clean(form, existing: Account | None) -> dict:
    errors: dict[str, str] = {}
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Vul een naam in."
    if len(name) > 120:
        errors["name"] = "Maximaal 120 tekens."
    acc_type = form.get("type")
    if acc_type not in ACCOUNT_TYPES:
        errors["type"] = "Kies een type."
    visibility = "private" if form.get("visibility") == "private" else "shared"

    owner_raw = form.get("owner", "")
    owner_id: int | None
    if owner_raw == "joint":
        owner_id = None
    else:
        owner_id = tx_svc._int(owner_raw)
        if owner_id not in members_by_id():
            errors["owner"] = "Kies een eigenaar."
    if not errors.get("owner") and not can_manage(None, owner_id) and not current_user.is_admin:
        errors["owner"] = "Je kunt alleen je eigen rekeningen beheren."

    try:
        balance = parse_money(form.get("opening_balance") or "0")
    except MoneyError as exc:
        errors["opening_balance"] = str(exc)
        balance = 0
    try:
        opening = parse_date(form.get("opening_date") or "")
    except ValueError as exc:
        errors["opening_date"] = str(exc)
        opening = date.today()
    iban = (
        re.sub(r"\D*", "", form.get("iban_last4") or "")[-4:]
        if (form.get("iban_last4") or "").strip()
        else ""
    )
    raw_iban = (form.get("iban_last4") or "").strip()
    if raw_iban and not re.fullmatch(r"[A-Za-z0-9]{1,4}", raw_iban):
        errors["iban_last4"] = "Vul maximaal de laatste 4 tekens in."
    color = form.get("color") or "#64748b"
    if not _COLOR.match(color):
        color = "#64748b"
    if errors:
        raise tx_svc.TransactionError(errors)
    return {
        "name": name,
        "bank": (form.get("bank") or "").strip()[:80],
        "iban_last4": raw_iban[:4] if raw_iban else iban,
        "type": acc_type,
        "visibility": visibility,
        "owner_member_id": owner_id,
        "opening_balance_cents": balance,
        "opening_date": opening,
        "color": color,
        "icon": ICONS[acc_type],
    }


@bp.get("/nieuw")
@login_required
def new_form():
    return render_template("accounts/form.html", **_form_ctx())


@bp.post("/")
@login_required
def create():
    try:
        data = _clean(request.form, None)
    except tx_svc.TransactionError as exc:
        return render_template(
            "accounts/form.html", **_form_ctx(form=request.form, errors=exc.errors)
        ), 422
    order = (
        db.session.execute(
            select(db.func.coalesce(db.func.max(Account.sort_order), -1))
        ).scalar_one()
        + 1
    )
    account = Account(household_id=household_id(), sort_order=order, **data)
    db.session.add(account)
    db.session.flush()
    tx_svc.write_audit("account", account.id, "create", None, _snapshot(account), current_user)
    db.session.commit()
    return _toast("Rekening aangemaakt.")


def _snapshot(account: Account) -> dict:
    return {
        "name": account.name,
        "owner_member_id": account.owner_member_id,
        "type": account.type,
        "visibility": account.visibility,
        "opening_balance_cents": account.opening_balance_cents,
        "opening_date": account.opening_date.isoformat(),
        "is_archived": account.is_archived,
    }


@bp.get("/<int:account_id>/bewerken")
@login_required
def edit_form(account_id: int):
    account = _get_account(account_id)
    if not can_manage(account):
        abort(403)
    return render_template("accounts/form.html", **_form_ctx(account))


@bp.post("/<int:account_id>")
@login_required
def update(account_id: int):
    account = _get_account(account_id)
    if not can_manage(account):
        abort(403)
    try:
        data = _clean(request.form, account)
    except tx_svc.TransactionError as exc:
        return render_template(
            "accounts/form.html", **_form_ctx(account, request.form, exc.errors)
        ), 422
    before = _snapshot(account)
    for key, value in data.items():
        setattr(account, key, value)
    tx_svc.write_audit("account", account.id, "update", before, _snapshot(account), current_user)
    db.session.commit()
    return _toast("Rekening bijgewerkt.")


@bp.post("/<int:account_id>/archiveer")
@login_required
def archive(account_id: int):
    account = _get_account(account_id)
    if not can_manage(account):
        abort(403)
    account.is_archived = not account.is_archived
    db.session.commit()
    return _toast("Rekening gearchiveerd." if account.is_archived else "Rekening hersteld.")


@bp.post("/volgorde")
@login_required
def reorder():
    ids = [i for i in (tx_svc._int(v) for v in request.form.getlist("ids")) if i]
    for position, account_id in enumerate(ids):
        account = access.get_visible_account(account_id)
        if account is not None and can_manage(account):
            account.sort_order = position
    db.session.commit()
    return "", 204


# --- detail


@bp.get("/<int:account_id>")
@login_required
def detail(account_id: int):
    account = _get_account(account_id)
    today = date.today()
    start = max(account.opening_date, today - timedelta(days=365))
    series = balance_series(account.id, start, today)
    step = max(1, len(series) // 120)
    sampled = series[::step]
    if series and sampled[-1][0] != series[-1][0]:
        sampled.append(series[-1])
    stmt = (
        access.visible_transactions()
        .where(
            (tx_svc.Transaction.account_id == account.id)
            | (tx_svc.Transaction.to_account_id == account.id)
        )
        .order_by(tx_svc.Transaction.date.desc(), tx_svc.Transaction.id.desc())
        .limit(25)
    )
    recent = list(db.session.execute(stmt).scalars())
    person = current_person()
    return render_template(
        "accounts/detail.html",
        account=account,
        balance=account_balance(account.id),
        chart={
            "labels": [format_date(d) for d, _v in sampled],
            "values": [v / 100 for _d, v in sampled],
        },
        recent=recent,
        members=members_by_id(),
        person=person,
        infos={t.id: tx_svc.describe_for_person(t, person) for t in recent},
        can_manage=can_manage(account),
    )


# --- saldo controleren


def _reconcile_ctx(account: Account, **extra) -> dict:
    return {
        "account": account,
        "today": date.today().isoformat(),
        "values": {},
        "errors": {},
        **extra,
    }


@bp.get("/<int:account_id>/controleren")
@login_required
def reconcile_form(account_id: int):
    return render_template("accounts/reconcile.html", **_reconcile_ctx(_get_account(account_id)))


@bp.post("/<int:account_id>/controleren")
@login_required
def reconcile(account_id: int):
    account = _get_account(account_id)
    errors: dict[str, str] = {}
    try:
        actual = parse_money(request.form.get("actual", ""))
    except MoneyError as exc:
        errors["actual"] = str(exc)
        actual = 0
    try:
        on = parse_date(request.form.get("date", ""))
    except ValueError as exc:
        errors["date"] = str(exc)
        on = date.today()
    if errors:
        return render_template(
            "accounts/reconcile.html", **_reconcile_ctx(account, values=request.form, errors=errors)
        ), 422

    booked = account_balance(account.id, on)
    diff = actual - booked
    if request.form.get("action") != "correct":
        return render_template(
            "accounts/reconcile.html",
            **_reconcile_ctx(
                account, values=request.form, booked=booked, actual=actual, diff=diff, on=on
            ),
        )
    if diff == 0:
        return _toast("Het saldo klopt al; geen correctie nodig.")
    kind = "income" if diff > 0 else "expense"
    category = (
        db.session.execute(
            select(Category).where(
                Category.household_id == household_id(),
                Category.is_system.is_(True),
                Category.kind == kind,
            )
        )
        .scalars()
        .first()
    )
    data = {
        "type": kind,
        "amount_cents": abs(diff),
        "date": on,
        "account_id": account.id,
        "to_account_id": None,
        "category_id": category.id if category else None,
        "description": f"Saldocorrectie ({format_eur(booked)} → {format_eur(actual)})",
        "payee": "",
        "notes": "Aangemaakt via Saldo controleren.",
        "tag_names": [],
    }
    tx_svc.create_transaction(data, current_user)
    return _toast(f"Correctie van {format_eur(abs(diff))} aangemaakt.")
