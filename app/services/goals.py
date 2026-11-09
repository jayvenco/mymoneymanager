"""Spaardoelen: voortgang, benodigde maandelijkse inleg en status."""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select

from ..extensions import db
from ..models import SavingsGoal
from . import access
from .balances import account_balance, account_balances
from .household import household_id
from .people import PersonFilter


@dataclass
class GoalProgress:
    goal: SavingsGoal
    current_cents: int
    pct: int
    remaining_cents: int
    months_left: int | None
    needed_monthly_cents: int | None
    actual_monthly_cents: int
    status: str  # behaald | op_schema | achter | geen_deadline


def months_until(target: date, today: date) -> int:
    months = (target.year - today.year) * 12 + (target.month - today.month)
    if target.day < today.day:
        months -= 1
    return max(1, months)


def evaluate(goal: SavingsGoal, current: int, today: date | None = None) -> GoalProgress:
    today = today or date.today()
    remaining = max(0, goal.target_cents - current)
    pct = min(100, int(current * 100 / goal.target_cents)) if goal.target_cents > 0 else 0
    ninety = today - timedelta(days=90)
    actual = (current - account_balance(goal.account_id, ninety)) // 3 if goal.account_id else 0
    if current >= goal.target_cents:
        return GoalProgress(goal, current, 100, 0, None, 0, actual, "behaald")
    if goal.target_date is None:
        return GoalProgress(goal, current, pct, remaining, None, None, actual, "geen_deadline")
    if goal.target_date <= today:
        return GoalProgress(goal, current, pct, remaining, 0, remaining, actual, "achter")
    months = months_until(goal.target_date, today)
    needed = -(-remaining // months)  # naar boven afgerond
    return GoalProgress(
        goal,
        current,
        pct,
        remaining,
        months,
        needed,
        actual,
        "op_schema" if actual >= needed else "achter",
    )


def visible_goals(person: PersonFilter, user=None) -> list[SavingsGoal]:
    stmt = select(SavingsGoal).where(
        SavingsGoal.household_id == household_id(),
        SavingsGoal.account_id.in_(access.visible_account_ids(user)),
    )
    if person.kind == "member":
        stmt = stmt.where(SavingsGoal.member_id == person.member_id)
    elif person.kind == "joint":
        stmt = stmt.where(SavingsGoal.member_id.is_(None))
    return list(
        db.session.execute(
            stmt.order_by(
                SavingsGoal.target_date.is_(None), SavingsGoal.target_date, SavingsGoal.id
            )
        ).scalars()
    )


def progress_for(person: PersonFilter, user=None, today: date | None = None) -> list[GoalProgress]:
    goals = visible_goals(person, user)
    balances = account_balances({g.account_id for g in goals})
    return [evaluate(g, balances.get(g.account_id, 0), today) for g in goals]
