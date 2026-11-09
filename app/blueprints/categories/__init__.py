"""Categorieën en tags beheren (huishoudbreed)."""

import json
import re

from flask import Blueprint, abort, make_response, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func, select, update

from ...extensions import db
from ...models import Category, Tag, Transaction, TransactionTag
from ...services.household import household_id
from ...services.transactions import _int, write_audit

bp = Blueprint("categories", __name__, url_prefix="/categorieen")

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _hx(message: str, category: str = "success"):
    resp = make_response("", 204)
    resp.headers["HX-Trigger"] = json.dumps(
        {"toast": {"message": message, "category": category}, "reload-page": {}, "close-modal": {}}
    )
    return resp


def _cat(category_id: int) -> Category:
    cat = db.session.get(Category, category_id)
    if cat is None or cat.household_id != household_id():
        abort(404)
    return cat


def _usage_counts() -> dict[int, int]:
    rows = db.session.execute(
        select(Transaction.category_id, func.count()).group_by(Transaction.category_id)
    ).all()
    return {cid: n for cid, n in rows if cid}


def _tree(kind: str) -> list[tuple[Category, list[Category]]]:
    cats = list(
        db.session.execute(
            select(Category)
            .where(Category.household_id == household_id(), Category.kind == kind)
            .order_by(Category.sort_order, Category.name)
        ).scalars()
    )
    return [(p, [c for c in cats if c.parent_id == p.id]) for p in cats if p.parent_id is None]


@bp.get("/")
@login_required
def list_view():
    return render_template(
        "categories/list.html",
        trees={"expense": _tree("expense"), "income": _tree("income")},
        usage=_usage_counts(),
    )


def _form(category: Category | None = None, form=None, errors=None, kind=None, parent_id=None):
    values = {
        "name": "",
        "color": "#64748b",
        "icon": "tag",
        "kind": kind or "expense",
        "parent_id": parent_id,
    }
    if category is not None:
        values.update(
            name=category.name,
            color=category.color,
            icon=category.icon,
            kind=category.kind,
            parent_id=category.parent_id,
        )
    if form is not None:
        values.update(form.items())
    parents = (
        [p for p, _c in _tree(values["kind"])] if values["kind"] in {"expense", "income"} else []
    )
    return render_template(
        "categories/form.html",
        category=category,
        values=values,
        errors=errors or {},
        parents=[p for p in parents if category is None or p.id != category.id],
        icons=ICON_CHOICES,
    )


ICON_CHOICES = [
    "home",
    "utensils",
    "shirt",
    "ticket",
    "trending-up",
    "shopping-bag",
    "banknote",
    "plus-circle",
    "coins",
    "tag",
    "wallet",
    "piggy-bank",
    "landmark",
    "repeat",
    "target",
    "calendar",
]


@bp.get("/nieuw")
@login_required
def new_form():
    parent_id = _int(request.args.get("parent"))
    parent = db.session.get(Category, parent_id) if parent_id else None
    kind = (
        parent.kind
        if parent
        else (
            request.args.get("kind")
            if request.args.get("kind") in {"income", "expense"}
            else "expense"
        )
    )
    resp = _form(kind=kind, parent_id=parent.id if parent else None)
    return resp


def _apply(category: Category, form) -> dict[str, str]:
    errors: dict[str, str] = {}
    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Vul een naam in."
    elif len(name) > 80:
        errors["name"] = "Maximaal 80 tekens."
    color = form.get("color") or "#64748b"
    if not _COLOR.match(color):
        errors["color"] = "Ongeldige kleur."
    icon = form.get("icon") if form.get("icon") in ICON_CHOICES else "tag"
    parent_id = _int(form.get("parent_id"))
    parent = None
    if parent_id:
        parent = db.session.get(Category, parent_id)
        if parent is None or parent.household_id != household_id() or parent.parent_id is not None:
            errors["parent_id"] = "Ongeldige hoofdcategorie."
        elif category.id and parent.id == category.id:
            errors["parent_id"] = "Een categorie kan niet onder zichzelf vallen."
    if errors:
        return errors
    category.name = name
    category.color = color
    category.icon = icon
    if parent is not None:
        category.parent_id = parent.id
        category.kind = parent.kind
    else:
        category.parent_id = None
    return errors


@bp.post("/")
@login_required
def create():
    kind = request.form.get("kind")
    if kind not in {"income", "expense"}:
        abort(400)
    category = Category(household_id=household_id(), kind=kind, sort_order=999)
    errors = _apply(category, request.form)
    if errors:
        return _form(None, request.form, errors), 422
    db.session.add(category)
    db.session.flush()
    write_audit("category", category.id, "create", None, {"name": category.name}, current_user)
    db.session.commit()
    return _hx("Categorie aangemaakt.")


@bp.get("/<int:category_id>/bewerken")
@login_required
def edit_form(category_id: int):
    return _form(_cat(category_id))


@bp.post("/<int:category_id>")
@login_required
def update_view(category_id: int):
    category = _cat(category_id)
    if category.is_system:
        abort(403)
    errors = _apply(category, request.form)
    if errors:
        db.session.rollback()
        return _form(category, request.form, errors), 422
    db.session.commit()
    return _hx("Categorie bijgewerkt.")


@bp.post("/<int:category_id>/archiveer")
@login_required
def archive(category_id: int):
    category = _cat(category_id)
    category.is_archived = not category.is_archived
    for child in category.children:
        child.is_archived = category.is_archived
    db.session.commit()
    return _hx("Categorie gearchiveerd." if category.is_archived else "Categorie hersteld.")


@bp.post("/volgorde")
@login_required
def reorder():
    for position, category_id in enumerate(
        i for i in (_int(v) for v in request.form.getlist("ids")) if i
    ):
        category = db.session.get(Category, category_id)
        if category is not None and category.household_id == household_id():
            category.sort_order = position
    db.session.commit()
    return "", 204


@bp.get("/<int:category_id>/verwijderen")
@login_required
def delete_form(category_id: int):
    category = _cat(category_id)
    if category.is_system:
        abort(403)
    ids = [category.id, *[c.id for c in category.children]]
    count = db.session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.category_id.in_(ids))
    ).scalar_one()
    targets = [
        c
        for c in db.session.execute(
            select(Category)
            .where(
                Category.household_id == household_id(),
                Category.kind == category.kind,
                Category.id.not_in(ids),
                Category.is_archived.is_(False),
            )
            .order_by(Category.sort_order, Category.name)
        ).scalars()
    ]
    return render_template(
        "categories/delete.html", category=category, count=count, targets=targets, error=None
    )


@bp.post("/<int:category_id>/verwijderen")
@login_required
def delete(category_id: int):
    category = _cat(category_id)
    if category.is_system:
        abort(403)
    ids = [category.id, *[c.id for c in category.children]]
    count = db.session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.category_id.in_(ids))
    ).scalar_one()
    if count:
        target = db.session.get(Category, _int(request.form.get("target")) or 0)
        if (
            target is None
            or target.household_id != household_id()
            or target.id in ids
            or target.kind != category.kind
        ):
            return _hx(
                "Kies een categorie waarnaartoe de transacties verplaatst worden.", "error"
            ), 200
        db.session.execute(
            update(Transaction)
            .where(Transaction.category_id.in_(ids))
            .values(category_id=target.id)
        )
        from ...models import Budget, RecurringRule

        db.session.execute(
            update(RecurringRule)
            .where(RecurringRule.category_id.in_(ids))
            .values(category_id=target.id)
        )
        db.session.execute(Budget.__table__.delete().where(Budget.category_id.in_(ids)))
    else:
        from ...models import Budget, RecurringRule

        db.session.execute(Budget.__table__.delete().where(Budget.category_id.in_(ids)))
        db.session.execute(
            update(RecurringRule).where(RecurringRule.category_id.in_(ids)).values(category_id=None)
        )
    for child in list(category.children):
        db.session.delete(child)
    db.session.delete(category)
    db.session.commit()
    return _hx("Categorie verwijderd.")


# --------------------------------------------------------------------------- tags


@bp.get("/tags")
@login_required
def tags_view():
    rows = db.session.execute(
        select(Tag, func.count(TransactionTag.c.transaction_id))
        .outerjoin(TransactionTag, TransactionTag.c.tag_id == Tag.id)
        .where(Tag.household_id == household_id())
        .group_by(Tag.id)
        .order_by(func.lower(Tag.name))
    ).all()
    return render_template("categories/tags.html", tags=rows)


def _tag(tag_id: int) -> Tag:
    tag = db.session.get(Tag, tag_id)
    if tag is None or tag.household_id != household_id():
        abort(404)
    return tag


@bp.get("/tags/<int:tag_id>/bewerken")
@login_required
def tag_form(tag_id: int):
    tag = _tag(tag_id)
    others = db.session.execute(
        select(Tag)
        .where(Tag.household_id == household_id(), Tag.id != tag.id)
        .order_by(func.lower(Tag.name))
    ).scalars()
    return render_template("categories/tag_form.html", tag=tag, others=list(others), errors={})


@bp.post("/tags/<int:tag_id>")
@login_required
def tag_update(tag_id: int):
    tag = _tag(tag_id)
    name = " ".join((request.form.get("name") or "").split())[:60]
    color = request.form.get("color") or tag.color
    errors = {}
    if not name:
        errors["name"] = "Vul een naam in."
    elif db.session.execute(
        select(Tag.id).where(
            Tag.household_id == household_id(),
            func.lower(Tag.name) == name.lower(),
            Tag.id != tag.id,
        )
    ).first():
        errors["name"] = "Deze tag bestaat al. Gebruik 'samenvoegen' om ze te combineren."
    if not _COLOR.match(color):
        errors["color"] = "Ongeldige kleur."
    if errors:
        others = db.session.execute(select(Tag).where(Tag.id != tag.id)).scalars()
        return render_template(
            "categories/tag_form.html", tag=tag, others=list(others), errors=errors
        ), 422
    tag.name, tag.color = name, color
    db.session.commit()
    return _hx("Tag bijgewerkt.")


@bp.post("/tags/<int:tag_id>/samenvoegen")
@login_required
def tag_merge(tag_id: int):
    source = _tag(tag_id)
    target = _tag(_int(request.form.get("target")) or 0)
    if target.id == source.id:
        return _hx("Kies een andere tag.", "error")
    existing = set(
        db.session.execute(
            select(TransactionTag.c.transaction_id).where(TransactionTag.c.tag_id == target.id)
        ).scalars()
    )
    moving = [
        tid
        for tid in db.session.execute(
            select(TransactionTag.c.transaction_id).where(TransactionTag.c.tag_id == source.id)
        ).scalars()
        if tid not in existing
    ]
    for tid in moving:
        db.session.execute(TransactionTag.insert().values(transaction_id=tid, tag_id=target.id))
    db.session.execute(TransactionTag.delete().where(TransactionTag.c.tag_id == source.id))
    db.session.delete(source)
    db.session.commit()
    return _hx(f"Tag samengevoegd met '{target.name}'.")


@bp.post("/tags/<int:tag_id>/verwijder")
@login_required
def tag_delete(tag_id: int):
    tag = _tag(tag_id)
    db.session.execute(TransactionTag.delete().where(TransactionTag.c.tag_id == tag.id))
    db.session.delete(tag)
    db.session.commit()
    return _hx("Tag verwijderd.")
