"""Transacties: lijst met filters, snel invoeren, inline bewerken, bulkacties, ongedaan maken."""

import json
from datetime import date

from flask import Blueprint, abort, make_response, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func, select

from ...extensions import db
from ...models import Account, Category, Tag, Transaction, TransactionTag
from ...services import access
from ...services import transactions as svc
from ...services.household import household_id, members_by_id
from ...services.people import current_person
from ...utils.dates import format_date
from ...utils.money import format_number

bp = Blueprint("transactions", __name__, url_prefix="/transacties")


# --- helpers


def _hx_response(
    message: str, *, category: str = "success", extra: dict | None = None, undo_url=None
):
    resp = make_response("", 204)
    toast = {"message": message, "category": category}
    if undo_url:
        toast["undoUrl"] = undo_url
    triggers = {"toast": toast, "tx-changed": {}, "close-modal": {}}
    triggers.update(extra or {})
    resp.headers["HX-Trigger"] = json.dumps(triggers)
    return resp


def get_tx_or_404(tx_id: int) -> Transaction:
    tx = access.get_visible_transaction(tx_id)
    if tx is None:
        abort(404)
    return tx


def category_tree(kind: str, *, include_id: int | None = None):
    stmt = (
        select(Category)
        .where(Category.household_id == household_id(), Category.kind == kind)
        .order_by(Category.sort_order, Category.name)
    )
    cats = [
        c for c in db.session.execute(stmt).scalars() if not c.is_archived or c.id == include_id
    ]
    parents = [c for c in cats if c.parent_id is None]
    result = []
    for p in parents:
        result.append((p, [c for c in cats if c.parent_id == p.id]))
    return result


def form_context(tx: Transaction | None = None, form=None, errors=None) -> dict:
    members = members_by_id()
    accounts = list(db.session.execute(access.visible_accounts(include_archived=False)).scalars())
    if tx is not None:
        for acc_id in (tx.account_id, tx.to_account_id):
            if acc_id and all(a.id != acc_id for a in accounts):
                acc = access.get_visible_account(acc_id)
                if acc:
                    accounts.append(acc)

    groups: list[tuple[str, list[Account]]] = []
    for m in sorted(members.values(), key=lambda m: m.sort_order):
        mine = [a for a in accounts if a.owner_member_id == m.id]
        if mine:
            groups.append((m.name, mine))
    joint = [a for a in accounts if a.owner_member_id is None]
    if joint:
        groups.append(("Gezamenlijk", joint))

    last = db.session.execute(
        select(Transaction.account_id)
        .where(Transaction.created_by_user_id == current_user.id)
        .order_by(Transaction.id.desc())
        .limit(1)
    ).scalar()
    default_account = current_user.preferences.get("default_account") or last
    if default_account and all(a.id != default_account for a in accounts):
        default_account = None

    values = {}
    if tx is not None:
        values = {
            "type": tx.type,
            "amount": format_number(tx.amount_cents),
            "date": tx.date.isoformat(),
            "account_id": tx.account_id,
            "to_account_id": tx.to_account_id,
            "category_id": tx.category_id,
            "description": tx.description,
            "payee": tx.payee,
            "notes": tx.notes,
            "tags": ", ".join(t.name for t in tx.tags),
        }
    else:
        values = {
            "type": "expense",
            "date": date.today().isoformat(),
            "account_id": default_account,
        }
    if form is not None:
        values.update({k: v for k, v in form.items()})
        if values.get("date") and "-" in values["date"] and len(values["date"].split("-")[0]) != 4:
            try:
                d = svc.parse_date(values["date"])
                values["date"] = d.isoformat()
            except ValueError:
                pass
    selected_category = svc._int(values.get("category_id"))
    return {
        "tx": tx,
        "values": values,
        "errors": errors or {},
        "account_groups": groups,
        "expense_categories": category_tree("expense", include_id=selected_category),
        "income_categories": category_tree("income", include_id=selected_category),
        "all_tags": [
            t.name
            for t in db.session.execute(
                select(Tag).where(Tag.household_id == household_id()).order_by(Tag.name)
            ).scalars()
        ],
    }


def row_context(tx: Transaction) -> dict:
    person = current_person()
    return {
        "tx": tx,
        "members": members_by_id(),
        "person": person,
        "info": svc.describe_for_person(tx, person),
    }


# --- list


@bp.get("/")
@login_required
def list_view():
    person = current_person()
    filters = svc.parse_filters(request.args, person)
    page = svc.run_query(filters)
    members = members_by_id()
    ctx = {
        "page": page,
        "f": filters,
        "members": members,
        "person": person,
        "infos": {tx.id: svc.describe_for_person(tx, person) for tx in page.items},
        "query": {k: v for k, v in request.args.items() if k not in {"page"}},
    }
    if request.headers.get("HX-Request") and not request.headers.get("HX-Boosted"):
        return render_template("transactions/_results.html", **ctx)
    accounts = list(db.session.execute(access.visible_accounts()).scalars())
    tags = db.session.execute(
        select(Tag, func.count(TransactionTag.c.transaction_id))
        .outerjoin(TransactionTag, TransactionTag.c.tag_id == Tag.id)
        .where(Tag.household_id == household_id())
        .group_by(Tag.id)
        .order_by(Tag.name)
    ).all()
    categories = [
        (p, children)
        for kind in ("expense", "income")
        for p, children in category_tree(kind, include_id=filters.category_id)
    ]
    return render_template(
        "transactions/list.html",
        **ctx,
        accounts=accounts,
        tags=[t for t, _n in tags],
        categories=categories,
        form_ctx=form_context(),
    )


# --- create / edit


@bp.get("/nieuw")
@login_required
def new_form():
    return render_template("transactions/form.html", **form_context())


@bp.post("/")
@login_required
def create():
    try:
        data = svc.clean_form(request.form, current_user)
    except svc.TransactionError as exc:
        return render_template(
            "transactions/form.html", **form_context(form=request.form, errors=exc.errors)
        ), 422
    svc.create_transaction(data, current_user)
    return _hx_response("Transactie opgeslagen.")


@bp.get("/<int:tx_id>/bewerken")
@login_required
def edit_form(tx_id: int):
    return render_template("transactions/form.html", **form_context(get_tx_or_404(tx_id)))


@bp.post("/<int:tx_id>")
@login_required
def update(tx_id: int):
    tx = get_tx_or_404(tx_id)
    try:
        data = svc.clean_form(request.form, current_user, existing=tx)
    except svc.TransactionError as exc:
        return render_template(
            "transactions/form.html", **form_context(tx, form=request.form, errors=exc.errors)
        ), 422
    svc.update_transaction(tx, data, current_user)
    return _hx_response("Transactie bijgewerkt.")


# --- inline


@bp.get("/<int:tx_id>/rij")
@login_required
def row(tx_id: int):
    return render_template("transactions/_row.html", **row_context(get_tx_or_404(tx_id)))


@bp.get("/<int:tx_id>/inline")
@login_required
def inline_form(tx_id: int):
    tx = get_tx_or_404(tx_id)
    ctx = row_context(tx)
    ctx["categories"] = (
        category_tree(tx.type, include_id=tx.category_id) if tx.type != "transfer" else []
    )
    ctx["amount_value"] = format_number(tx.amount_cents)
    return render_template("transactions/_row_edit.html", **ctx)


@bp.post("/<int:tx_id>/inline")
@login_required
def inline_save(tx_id: int):
    tx = get_tx_or_404(tx_id)
    merged = {
        "type": tx.type,
        "account_id": tx.account_id,
        "to_account_id": tx.to_account_id,
        "category_id": tx.category_id,
        "payee": tx.payee,
        "notes": tx.notes,
        "tags": ", ".join(t.name for t in tx.tags),
        "amount": format_number(tx.amount_cents),
        "date": format_date(tx.date),
        "description": tx.description,
    }
    merged.update(
        {
            k: request.form[k]
            for k in ("date", "description", "amount", "category_id")
            if k in request.form
        }
    )
    try:
        data = svc.clean_form(merged, current_user, existing=tx)
    except svc.TransactionError as exc:
        ctx = row_context(tx)
        ctx.update(
            categories=category_tree(tx.type, include_id=tx.category_id)
            if tx.type != "transfer"
            else [],
            amount_value=request.form.get("amount", ""),
            errors=exc.errors,
        )
        return render_template("transactions/_row_edit.html", **ctx), 422
    svc.update_transaction(tx, data, current_user)
    resp = make_response(render_template("transactions/_row.html", **row_context(tx)))
    resp.headers["HX-Trigger"] = json.dumps(
        {"toast": {"message": "Transactie bijgewerkt.", "category": "success"}, "tx-totals": {}}
    )
    return resp


# --- duplicate / delete / undo


@bp.post("/<int:tx_id>/dupliceer")
@login_required
def duplicate(tx_id: int):
    svc.duplicate_transaction(get_tx_or_404(tx_id), current_user)
    return _hx_response("Transactie gedupliceerd (datum: vandaag).")


@bp.post("/<int:tx_id>/verwijder")
@login_required
def delete(tx_id: int):
    entry = svc.delete_transaction(get_tx_or_404(tx_id), current_user)
    from flask import url_for

    return _hx_response(
        "Transactie verwijderd.", undo_url=url_for("transactions.undo", audit_id=entry.id)
    )


@bp.post("/ongedaan/<int:audit_id>")
@login_required
def undo(audit_id: int):
    tx = svc.restore_deleted(audit_id, current_user)
    if tx is None:
        return _hx_response("Herstellen is niet meer mogelijk.", category="error")
    return _hx_response("Transactie hersteld.")


# --- bulk


@bp.post("/bulk")
@login_required
def bulk():
    ids = [i for i in (svc._int(v) for v in request.form.getlist("ids")) if i]
    if not ids:
        return _hx_response("Selecteer eerst een of meer transacties.", category="error")
    action = request.form.get("action", "")
    value = request.form.get("value")
    changed = svc.bulk_apply(ids, action, value, current_user)
    noun = "transactie" if changed == 1 else "transacties"
    return _hx_response(f"{changed} {noun} bijgewerkt.", extra={"bulk-done": {}})


# --- detail


@bp.get("/<int:tx_id>")
@login_required
def detail(tx_id: int):
    tx = get_tx_or_404(tx_id)
    labels = {
        "date": "Datum",
        "amount_cents": "Bedrag",
        "type": "Type",
        "account_id": "Rekening",
        "to_account_id": "Tegenrekening",
        "category_id": "Categorie",
        "description": "Omschrijving",
        "payee": "Begunstigde",
        "notes": "Notities",
        "tags": "Tags",
    }
    accounts = {a.id: a.name for a in db.session.execute(select(Account)).scalars()}
    categories = {c.id: c.name for c in db.session.execute(select(Category)).scalars()}

    def show(key, value):
        if value is None or value == "" or value == []:
            return "—"
        if key == "amount_cents":
            from ...utils.money import format_eur

            return format_eur(value)
        if key in {"account_id", "to_account_id"}:
            return accounts.get(value, f"#{value}")
        if key == "category_id":
            return categories.get(value, f"#{value}")
        if key == "tags":
            return ", ".join(value)
        if key == "date":
            return format_date(date.fromisoformat(value))
        return str(value)

    entries = []
    for entry in svc.history(tx.id):
        before = json.loads(entry.before_json) if entry.before_json else {}
        after = json.loads(entry.after_json) if entry.after_json else {}
        changes = [
            (labels[k], show(k, before.get(k)), show(k, after.get(k)))
            for k in labels
            if before.get(k) != after.get(k)
        ]
        entries.append({"entry": entry, "changes": changes})
    return render_template("transactions/detail.html", **row_context(tx), history=entries)


@bp.get("/tags/zoek")
@login_required
def tag_search():
    q = (request.args.get("q") or "").strip().lower()
    stmt = select(Tag.name).where(Tag.household_id == household_id()).order_by(Tag.name).limit(20)
    if q:
        stmt = stmt.where(func.lower(Tag.name).like(f"%{q}%"))
    return {"tags": list(db.session.execute(stmt).scalars())}
