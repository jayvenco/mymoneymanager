"""Transacties: validatie, aanmaken/wijzigen/verwijderen met audit-log, filters en bulkacties."""

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import aliased

from ..extensions import db
from ..models import TX_TYPES, Account, AuditLog, Category, Tag, Transaction
from ..utils.dates import parse_date
from ..utils.money import MoneyError, parse_money
from . import access
from .household import household_id
from .people import PersonFilter, person_transaction_clause

PAGE_SIZE = 50


class TransactionError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


# --- audit


def serialize(tx: Transaction) -> dict[str, Any]:
    return {
        "date": tx.date.isoformat(),
        "amount_cents": tx.amount_cents,
        "type": tx.type,
        "account_id": tx.account_id,
        "to_account_id": tx.to_account_id,
        "category_id": tx.category_id,
        "description": tx.description,
        "payee": tx.payee,
        "notes": tx.notes,
        "tags": sorted(t.name for t in tx.tags),
    }


def write_audit(
    entity: str,
    entity_id: int,
    action: str,
    before: dict | None,
    after: dict | None,
    user,
) -> AuditLog:
    entry = AuditLog(
        household_id=household_id(),
        user_id=getattr(user, "id", None),
        entity=entity,
        entity_id=entity_id,
        action=action,
        before_json=json.dumps(before, ensure_ascii=False) if before is not None else None,
        after_json=json.dumps(after, ensure_ascii=False) if after is not None else None,
    )
    db.session.add(entry)
    return entry


def history(tx_id: int) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity == "transaction", AuditLog.entity_id == tx_id)
        .order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
    )
    return list(db.session.execute(stmt).scalars())


# --- tags


def parse_tag_names(raw: str | None) -> list[str]:
    seen: dict[str, str] = {}
    for part in (raw or "").split(","):
        name = " ".join(part.split())[:60]
        if name:
            seen.setdefault(name.lower(), name)
    return list(seen.values())[:20]


def find_or_create_tags(names: list[str]) -> list[Tag]:
    tags: list[Tag] = []
    for name in names:
        tag = db.session.execute(
            select(Tag).where(
                Tag.household_id == household_id(), func.lower(Tag.name) == name.lower()
            )
        ).scalar_one_or_none()
        if tag is None:
            tag = Tag(household_id=household_id(), name=name)
            db.session.add(tag)
            db.session.flush()
        tags.append(tag)
    return tags


# --- validation


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def clean_form(form, user, existing: Transaction | None = None) -> dict[str, Any]:
    """Valideert transactiegegevens volledig server-side. Gooit TransactionError."""
    errors: dict[str, str] = {}
    data: dict[str, Any] = {}

    tx_type = form.get("type")
    if tx_type not in TX_TYPES:
        errors["type"] = "Kies een geldig type."
    data["type"] = tx_type

    try:
        amount = parse_money(form.get("amount", ""))
        if amount <= 0:
            errors["amount"] = "Het bedrag moet groter zijn dan nul."
        data["amount_cents"] = amount
    except MoneyError as exc:
        errors["amount"] = str(exc)

    try:
        data["date"] = parse_date(form.get("date", ""))
    except ValueError as exc:
        errors["date"] = str(exc)

    def usable_account(raw, field_name: str, label: str, current_id: int | None):
        account_id = _int(raw)
        if account_id is None:
            errors[field_name] = f"Kies {label}."
            return None
        account = access.get_visible_account(account_id)
        if account is None:
            errors[field_name] = f"Ongeldige {label}."
            return None
        if account.is_archived and account.id != current_id:
            errors[field_name] = f"{label.capitalize()} is gearchiveerd."
            return None
        return account

    account = usable_account(
        form.get("account_id"), "account_id", "rekening", existing.account_id if existing else None
    )
    data["account_id"] = account.id if account else None

    data["to_account_id"] = None
    data["category_id"] = None
    if tx_type == "transfer":
        target = usable_account(
            form.get("to_account_id"),
            "to_account_id",
            "tegenrekening",
            existing.to_account_id if existing else None,
        )
        if target is not None:
            if account is not None and target.id == account.id:
                errors["to_account_id"] = "Kies een andere rekening dan de bronrekening."
            data["to_account_id"] = target.id
    elif tx_type in {"income", "expense"}:
        category_id = _int(form.get("category_id"))
        category = db.session.get(Category, category_id) if category_id else None
        if category is None or category.household_id != household_id():
            errors["category_id"] = "Kies een categorie."
        elif category.kind != tx_type:
            errors["category_id"] = "Deze categorie past niet bij het type."
        elif category.is_archived and (existing is None or existing.category_id != category.id):
            errors["category_id"] = "Deze categorie is gearchiveerd."
        else:
            data["category_id"] = category.id

    description = (form.get("description") or "").strip()
    payee = (form.get("payee") or "").strip()
    if len(description) > 255:
        errors["description"] = "Maximaal 255 tekens."
    if len(payee) > 120:
        errors["payee"] = "Maximaal 120 tekens."
    data.update(description=description, payee=payee, notes=(form.get("notes") or "").strip())
    data["tag_names"] = parse_tag_names(form.get("tags"))

    if errors:
        raise TransactionError(errors)
    return data


# --- CRUD


def _apply(tx: Transaction, data: dict[str, Any]) -> None:
    for key in (
        "type",
        "amount_cents",
        "date",
        "account_id",
        "to_account_id",
        "category_id",
        "description",
        "payee",
        "notes",
    ):
        setattr(tx, key, data[key])
    tx.tags = find_or_create_tags(data["tag_names"])


def create_transaction(data: dict[str, Any], user) -> Transaction:
    tx = Transaction(household_id=household_id(), created_by_user_id=getattr(user, "id", None))
    tx.updated_by_user_id = getattr(user, "id", None)
    _apply(tx, data)
    db.session.add(tx)
    db.session.flush()
    write_audit("transaction", tx.id, "create", None, serialize(tx), user)
    db.session.commit()
    return tx


def update_transaction(tx: Transaction, data: dict[str, Any], user) -> Transaction:
    before = serialize(tx)
    _apply(tx, data)
    tx.updated_by_user_id = getattr(user, "id", None)
    tx.updated_at = datetime.now().replace(microsecond=0)
    db.session.flush()
    after = serialize(tx)
    if before != after:
        write_audit("transaction", tx.id, "update", before, after, user)
    db.session.commit()
    return tx


def delete_transaction(tx: Transaction, user) -> AuditLog:
    before = serialize(tx)
    tx_id = tx.id
    db.session.delete(tx)
    entry = write_audit("transaction", tx_id, "delete", before, None, user)
    db.session.commit()
    return entry


def restore_deleted(audit_id: int, user) -> Transaction | None:
    """Maakt een zojuist verwijderde transactie opnieuw aan (functie achter 'Ongedaan maken')."""
    entry = db.session.get(AuditLog, audit_id)
    if (
        entry is None
        or entry.action != "delete"
        or entry.entity != "transaction"
        or not entry.before_json
    ):
        return None
    if entry.user_id != getattr(user, "id", None):
        return None
    if db.session.execute(
        select(AuditLog.id).where(
            AuditLog.entity == "transaction",
            AuditLog.entity_id == entry.entity_id,
            AuditLog.id > entry.id,
        )
    ).first():
        return None  # al hersteld
    before = json.loads(entry.before_json)
    if not (access.can_see_account(before["account_id"], user)):
        return None
    tx = Transaction(
        household_id=household_id(),
        created_by_user_id=entry.user_id,
        updated_by_user_id=entry.user_id,
    )
    data = {
        "type": before["type"],
        "amount_cents": before["amount_cents"],
        "date": date.fromisoformat(before["date"]),
        "account_id": before["account_id"],
        "to_account_id": before["to_account_id"],
        "category_id": before["category_id"],
        "description": before["description"],
        "payee": before["payee"],
        "notes": before["notes"],
        "tag_names": before["tags"],
    }
    _apply(tx, data)
    db.session.add(tx)
    db.session.flush()
    write_audit("transaction", tx.id, "create", None, serialize(tx), user)
    db.session.commit()
    return tx


def duplicate_transaction(tx: Transaction, user) -> Transaction:
    data = serialize(tx)
    data["date"] = date.today()
    data["tag_names"] = data.pop("tags")
    return create_transaction(data, user)


# --- bulk


def bulk_apply(ids: list[int], action: str, value: str | None, user) -> int:
    """Voert een bulkactie uit op zichtbare transacties. Geeft het aantal gewijzigde rijen."""
    txs = [t for t in (access.get_visible_transaction(i, user) for i in ids) if t is not None]
    changed = 0
    for tx in txs:
        before = serialize(tx)
        if action == "delete":
            delete_transaction(tx, user)
            changed += 1
            continue
        if action == "category":
            category = db.session.get(Category, _int(value) or 0)
            if (
                category is None
                or category.household_id != household_id()
                or tx.type not in {"income", "expense"}
                or category.kind != tx.type
            ):
                continue
            tx.category_id = category.id
        elif action in {"add_tag", "remove_tag"}:
            names = parse_tag_names(value)
            if not names:
                continue
            if action == "add_tag":
                tx.tags = list({t.id: t for t in [*tx.tags, *find_or_create_tags(names)]}.values())
            else:
                lowered = {n.lower() for n in names}
                tx.tags = [t for t in tx.tags if t.name.lower() not in lowered]
        elif action == "account":
            account = access.get_visible_account(_int(value) or 0)
            if account is None or account.is_archived or account.id == tx.to_account_id:
                continue
            tx.account_id = account.id
        else:
            continue
        tx.updated_by_user_id = getattr(user, "id", None)
        db.session.flush()
        after = serialize(tx)
        if before != after:
            write_audit("transaction", tx.id, "update", before, after, user)
            changed += 1
    db.session.commit()
    return changed


# --- query


@dataclass
class TxFilters:
    person: PersonFilter
    account_id: int | None = None
    date_from: date | None = None
    date_to: date | None = None
    category_id: int | None = None
    tag_id: int | None = None
    type: str | None = None
    min_cents: int | None = None
    max_cents: int | None = None
    q: str = ""
    sort: str = "date"
    direction: str = "desc"
    page: int = 1
    raw: dict[str, str] = field(default_factory=dict)


SORTS = {"date", "amount", "description", "category", "account"}


def parse_filters(args, person: PersonFilter) -> TxFilters:
    def parse_d(name: str) -> date | None:
        try:
            return parse_date(args.get(name, "")) if args.get(name) else None
        except ValueError:
            return None

    def parse_m(name: str) -> int | None:
        try:
            return abs(parse_money(args[name])) if args.get(name) else None
        except MoneyError:
            return None

    sort = args.get("sort", "date")
    return TxFilters(
        person=person,
        account_id=_int(args.get("account")),
        date_from=parse_d("from"),
        date_to=parse_d("to"),
        category_id=_int(args.get("category")),
        tag_id=_int(args.get("tag")),
        type=args.get("type") if args.get("type") in TX_TYPES else None,
        min_cents=parse_m("min"),
        max_cents=parse_m("max"),
        q=(args.get("q") or "").strip()[:100],
        sort=sort if sort in SORTS else "date",
        direction="asc" if args.get("dir") == "asc" else "desc",
        page=max(1, _int(args.get("page")) or 1),
        raw={k: v for k, v in args.items() if k != "page" and v},
    )


def category_ids_with_children(category_id: int) -> list[int]:
    children = db.session.execute(
        select(Category.id).where(Category.parent_id == category_id)
    ).scalars()
    return [category_id, *children]


def build_query(f: TxFilters, user=None) -> Select:
    stmt = access.visible_transactions(user).where(Transaction.household_id == household_id())
    clause = person_transaction_clause(f.person)
    if clause is not None:
        stmt = stmt.where(clause)
    if f.account_id:
        stmt = stmt.where(
            or_(Transaction.account_id == f.account_id, Transaction.to_account_id == f.account_id)
        )
    if f.date_from:
        stmt = stmt.where(Transaction.date >= f.date_from)
    if f.date_to:
        stmt = stmt.where(Transaction.date <= f.date_to)
    if f.category_id:
        stmt = stmt.where(Transaction.category_id.in_(category_ids_with_children(f.category_id)))
    if f.tag_id:
        from ..models import TransactionTag

        stmt = stmt.where(
            Transaction.id.in_(
                select(TransactionTag.c.transaction_id).where(TransactionTag.c.tag_id == f.tag_id)
            )
        )
    if f.type:
        stmt = stmt.where(Transaction.type == f.type)
    if f.min_cents is not None:
        stmt = stmt.where(Transaction.amount_cents >= f.min_cents)
    if f.max_cents is not None:
        stmt = stmt.where(Transaction.amount_cents <= f.max_cents)
    if f.q:
        like = f"%{f.q.lower().replace('%', '').replace('_', '')}%"
        stmt = stmt.where(
            or_(
                func.lower(Transaction.description).like(like),
                func.lower(Transaction.payee).like(like),
                func.lower(Transaction.notes).like(like),
            )
        )
    return stmt


def ordered(stmt: Select, f: TxFilters) -> Select:
    if f.sort == "category":
        cat = aliased(Category)
        stmt = stmt.outerjoin(cat, cat.id == Transaction.category_id)
        column = func.lower(cat.name)
    elif f.sort == "account":
        acc = aliased(Account)
        stmt = stmt.join(acc, acc.id == Transaction.account_id)
        column = func.lower(acc.name)
    else:
        column = {
            "date": Transaction.date,
            "amount": Transaction.amount_cents,
            "description": func.lower(Transaction.description),
        }[f.sort]
    column = column.asc() if f.direction == "asc" else column.desc()
    return stmt.order_by(
        column, Transaction.id.desc() if f.direction == "desc" else Transaction.id.asc()
    )


@dataclass
class TxPage:
    items: list[Transaction]
    total: int
    page: int
    pages: int
    sum_income: int
    sum_expense: int


def run_query(f: TxFilters, user=None) -> TxPage:
    base = build_query(f, user)
    sub = base.with_only_columns(Transaction.id).subquery()
    total = db.session.execute(select(func.count()).select_from(sub)).scalar_one()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(f.page, pages)
    items = list(
        db.session.execute(
            ordered(base, f).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
        ).scalars()
    )
    sums = dict(
        db.session.execute(
            select(Transaction.type, func.sum(Transaction.amount_cents))
            .where(Transaction.id.in_(select(sub.c.id)))
            .group_by(Transaction.type)
        ).all()
    )
    return TxPage(
        items, total, page, pages, int(sums.get("income") or 0), int(sums.get("expense") or 0)
    )


def describe_for_person(tx: Transaction, person: PersonFilter) -> dict[str, Any]:
    """Weergave-info: label voor overboekingen vanuit het gekozen persoonsperspectief."""
    label = None
    if tx.type == "transfer":
        if person.is_household:
            label = "Overboeking"
        else:

            def mine(account: Account | None) -> bool:
                if account is None:
                    return False
                return (
                    account.owner_member_id is None
                    if person.kind == "joint"
                    else account.owner_member_id == person.member_id
                )

            src, dst = mine(tx.account), mine(tx.to_account)
            label = (
                "Interne overboeking"
                if src and dst
                else "Uitgaande overboeking"
                if src
                else "Inkomende overboeking"
            )
    return {"transfer_label": label}
