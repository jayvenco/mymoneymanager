"""Toegang tot het (enige) huishouden en zijn leden."""

from flask import g
from sqlalchemy import select

from ..extensions import db
from ..models import Household, Member


def get_household() -> Household:
    if "household" not in g:
        g.household = db.session.execute(select(Household).order_by(Household.id)).scalars().first()
    return g.household


def household_id() -> int:
    return get_household().id


def members(*, include_archived: bool = False) -> list[Member]:
    stmt = select(Member).where(Member.household_id == household_id())
    if not include_archived:
        stmt = stmt.where(Member.is_archived.is_(False))
    return list(db.session.execute(stmt.order_by(Member.sort_order, Member.id)).scalars())


def members_by_id() -> dict[int, Member]:
    return {m.id: m for m in members(include_archived=True)}
