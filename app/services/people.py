"""Persoonsfilter: Huishouden (totaal) / lid / Gezamenlijk."""

from dataclasses import dataclass

from flask import request
from flask_login import current_user
from sqlalchemy import Select, or_, select

from ..extensions import db
from ..models import Account, Transaction
from .household import members


@dataclass(frozen=True)
class PersonFilter:
    key: str  # 'household' | 'joint' | 'member:<id>'
    kind: str  # 'household' | 'joint' | 'member'
    member_id: int | None = None

    @property
    def is_household(self) -> bool:
        return self.kind == "household"


HOUSEHOLD = PersonFilter("household", "household")
JOINT = PersonFilter("joint", "joint")


def parse_person(key: str | None, valid_member_ids: set[int]) -> PersonFilter:
    if key == "joint":
        return JOINT
    if key and key.startswith("member:"):
        try:
            member_id = int(key.split(":", 1)[1])
        except ValueError:
            return HOUSEHOLD
        if member_id in valid_member_ids:
            return PersonFilter(key, "member", member_id)
    return HOUSEHOLD


def person_options() -> list[dict]:
    """Opties voor de pill-switcher (met kleur en label)."""
    options = [{"key": "household", "label": "Huishouden", "color": None}]
    options += [{"key": f"member:{m.id}", "label": m.name, "color": m.color} for m in members()]
    options.append({"key": "joint", "label": "Gezamenlijk", "color": "#0ea5e9"})
    return options


def current_person() -> PersonFilter:
    """?person= in de URL wint, daarna de opgeslagen voorkeur van de gebruiker."""
    valid = {m.id for m in members(include_archived=True)}
    raw = request.args.get("person")
    if raw is None and getattr(current_user, "is_authenticated", False):
        raw = current_user.preferences.get("person") or current_user.default_person_filter
    return parse_person(raw, valid)


def remember_person(key: str) -> None:
    valid = {m.id for m in members(include_archived=True)}
    chosen = parse_person(key, valid)
    current_user.set_preference("person", chosen.key)
    db.session.commit()


def owner_clause(person: PersonFilter, column=Account.owner_member_id):
    if person.kind == "member":
        return column == person.member_id
    if person.kind == "joint":
        return column.is_(None)
    return None


def person_account_ids(person: PersonFilter) -> Select:
    stmt = select(Account.id)
    clause = owner_clause(person)
    return stmt.where(clause) if clause is not None else stmt


def person_transaction_clause(person: PersonFilter):
    """Transacties van/naar rekeningen van deze persoon (None = geen beperking)."""
    if person.is_household:
        return None
    ids = person_account_ids(person)
    return or_(Transaction.account_id.in_(ids), Transaction.to_account_id.in_(ids))
