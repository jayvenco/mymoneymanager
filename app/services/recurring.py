"""Terugkerende transacties (vaste lasten, salaris, abonnementen)."""

import calendar
import json
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select

from ..extensions import db
from ..models import RecurringRule, Transaction
from . import access
from . import transactions as tx_svc
from .household import household_id
from .periods import add_months_clamped

MAX_CATCH_UP = 36  # maximaal aantal ingehaalde termijnen per regel per run


def advance(current: date, frequency: str, interval: int, anchor_day: int | None = None) -> date:
    """Volgende termijn. Maandelijkse termijnen blijven op de oorspronkelijke dag (clamped)."""
    if frequency == "weekly":
        return current + timedelta(days=7 * interval)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[frequency] * interval
    nxt = add_months_clamped(current.replace(day=1), months)
    day = anchor_day or current.day
    return nxt.replace(day=min(day, calendar.monthrange(nxt.year, nxt.month)[1]))


def rule_tx_data(rule: RecurringRule, on: date) -> dict:
    try:
        tag_ids = json.loads(rule.tag_ids_json or "[]")
    except ValueError:
        tag_ids = []
    from ..models import Tag

    names = (
        [t.name for t in db.session.execute(select(Tag).where(Tag.id.in_(tag_ids))).scalars()]
        if tag_ids
        else []
    )
    return {
        "type": rule.type,
        "amount_cents": rule.amount_cents,
        "date": on,
        "account_id": rule.account_id,
        "to_account_id": rule.to_account_id,
        "category_id": rule.category_id,
        "description": rule.description or rule.name,
        "payee": rule.payee,
        "notes": rule.notes,
        "tag_names": names,
    }


def _exists(rule: RecurringRule, on: date) -> bool:
    return (
        db.session.execute(
            select(Transaction.id).where(
                Transaction.recurring_rule_id == rule.id, Transaction.date == on
            )
        ).first()
        is not None
    )


def _create(rule: RecurringRule, on: date, user=None) -> Transaction | None:
    if _exists(rule, on):
        return None
    data = rule_tx_data(rule, on)
    tx = tx_svc.create_transaction(data, user)
    tx.recurring_rule_id = rule.id
    db.session.commit()
    return tx


def _step(rule: RecurringRule) -> None:
    rule.next_date = advance(rule.next_date, rule.frequency, rule.interval, rule.anchor_day)
    if rule.end_date and rule.next_date > rule.end_date:
        rule.is_active = False


def generate_due(today: date | None = None) -> int:
    """Maakt vervallen termijnen aan (auto_create); geeft het aantal nieuwe transacties."""
    today = today or date.today()
    created = 0
    rules = db.session.execute(
        select(RecurringRule).where(
            RecurringRule.household_id == household_id(),
            RecurringRule.is_active.is_(True),
            RecurringRule.auto_create.is_(True),
            RecurringRule.next_date <= today,
        )
    ).scalars()
    for rule in list(rules):
        for _ in range(MAX_CATCH_UP):
            if not rule.is_active or rule.next_date > today:
                break
            if _create(rule, rule.next_date):
                created += 1
            _step(rule)
            db.session.commit()
    return created


@dataclass
class Pending:
    rule: RecurringRule
    due: date


def visible_rules(user=None, *, active_only: bool = False) -> list[RecurringRule]:
    ids = access.visible_account_ids(user)
    stmt = select(RecurringRule).where(
        RecurringRule.household_id == household_id(),
        RecurringRule.account_id.in_(ids),
    )
    if active_only:
        stmt = stmt.where(RecurringRule.is_active.is_(True))
    return list(
        db.session.execute(stmt.order_by(RecurringRule.next_date, RecurringRule.id)).scalars()
    )


def pending_confirmations(today: date | None = None, user=None) -> list[Pending]:
    today = today or date.today()
    return [
        Pending(rule, rule.next_date)
        for rule in visible_rules(user, active_only=True)
        if not rule.auto_create and rule.next_date <= today
    ]


def confirm(rule: RecurringRule, user, *, amount_cents: int | None = None) -> Transaction | None:
    on = rule.next_date
    if amount_cents:
        original = rule.amount_cents
        rule.amount_cents = amount_cents
        tx = _create(rule, on, user)
        rule.amount_cents = original
    else:
        tx = _create(rule, on, user)
    _step(rule)
    db.session.commit()
    return tx


def skip(rule: RecurringRule) -> None:
    _step(rule)
    db.session.commit()


@dataclass
class Upcoming:
    rule: RecurringRule
    due: date


def upcoming(days: int = 30, today: date | None = None, user=None) -> list[Upcoming]:
    """Verwachte termijnen binnen `days` dagen (inclusief nog te bevestigen)."""
    today = today or date.today()
    horizon = today + timedelta(days=days)
    result: list[Upcoming] = []
    for rule in visible_rules(user, active_only=True):
        due = rule.next_date
        guard = 0
        while due <= horizon and guard < 60:
            if not rule.end_date or due <= rule.end_date:
                result.append(Upcoming(rule, due))
            due = advance(due, rule.frequency, rule.interval, rule.anchor_day)
            guard += 1
    return sorted(result, key=lambda u: (u.due, u.rule.id))
