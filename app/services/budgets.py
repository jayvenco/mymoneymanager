"""Budgetten per financiële maand, met standaardbedragen, overrides en rollover."""

from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select

from ..extensions import db
from ..models import Account, Budget, Category, Transaction
from . import access
from .household import get_household, household_id
from .people import PersonFilter
from .periods import Period, fin_month_expr, financial_month, months_between, shift_ym, ym_of

ROLLOVER_LOOKBACK_MONTHS = 12


@dataclass
class BudgetLine:
    budget: Budget
    category: Category
    base_cents: int
    carry_cents: int
    available_cents: int
    spent_cents: int
    remaining_cents: int
    pct: int
    state: str  # ok | warn | over
    per_day_cents: int | None


def children_map() -> dict[int, list[int]]:
    rows = db.session.execute(
        select(Category.id, Category.parent_id).where(
            Category.household_id == household_id(), Category.parent_id.isnot(None)
        )
    ).all()
    result: dict[int, list[int]] = {}
    for cid, pid in rows:
        result.setdefault(pid, []).append(cid)
    return result


def spent_map(
    ym_from: str,
    ym_to: str,
    *,
    user=None,
    member_id: int | None = None,
    joint_only: bool = False,
) -> dict[tuple[int, str], int]:
    """Uitgaven per (categorie, financiële maand), inclusief doorrekening naar hoofdcategorieën."""
    start_day = get_household().financial_month_start_day
    first = financial_month(ym_from, start_day).start
    last = financial_month(ym_to, start_day).end
    month = fin_month_expr(Transaction.date, start_day)
    stmt = (
        select(Transaction.category_id, month.label("ym"), func.sum(Transaction.amount_cents))
        .join(Category, Category.id == Transaction.category_id)
        .join(Account, Account.id == Transaction.account_id)
        .where(
            access.transaction_visibility_clause(user),
            Transaction.household_id == household_id(),
            Transaction.type == "expense",
            Category.exclude_from_reports.is_(False),
            Transaction.date >= first,
            Transaction.date <= last,
        )
        .group_by(Transaction.category_id, month)
    )
    if member_id is not None:
        stmt = stmt.where(Account.owner_member_id == member_id)
    elif joint_only:
        stmt = stmt.where(Account.owner_member_id.is_(None))

    raw: dict[tuple[int, str], int] = {}
    for category_id, ym, total in db.session.execute(stmt):
        raw[(category_id, ym)] = int(total or 0)

    parents = {
        row.id: row.parent_id
        for row in db.session.execute(
            select(Category.id, Category.parent_id).where(Category.household_id == household_id())
        )
    }
    result = dict(raw)
    for (category_id, ym), total in raw.items():
        parent = parents.get(category_id)
        if parent:
            result[(parent, ym)] = result.get((parent, ym), 0) + total
    return result


def budgets_in_range(ym_from: str, ym_to: str) -> list[Budget]:
    return list(
        db.session.execute(
            select(Budget)
            .join(Category, Category.id == Budget.category_id)
            .where(
                Budget.household_id == household_id(),
                (Budget.month.is_(None)) | ((Budget.month >= ym_from) & (Budget.month <= ym_to)),
            )
            .order_by(Category.sort_order, Category.name)
        ).scalars()
    )


def _amount_for(budgets: list[Budget], category_id: int, member_id: int | None, ym: str):
    """Override voor die maand, anders de standaard. Geeft het Budget-object of None."""
    default = override = None
    for b in budgets:
        if b.category_id == category_id and b.member_id == member_id:
            if b.month == ym:
                override = b
            elif b.month is None:
                default = b
    return override or default


def _state(pct: int) -> str:
    return "over" if pct > 100 else "warn" if pct >= 80 else "ok"


def budget_lines(
    ym: str,
    person: PersonFilter,
    *,
    user=None,
    today: date | None = None,
) -> list[BudgetLine]:
    today = today or date.today()
    start_day = get_household().financial_month_start_day
    period: Period = financial_month(ym, start_day)
    first_ym = shift_ym(ym, -ROLLOVER_LOOKBACK_MONTHS)
    # Rollover telt pas vanaf de maand waarin de eerste (zichtbare) transactie staat.
    earliest = db.session.execute(
        select(func.min(Transaction.date)).where(access.transaction_visibility_clause(user))
    ).scalar()
    roll_from = max(first_ym, ym_of(earliest, start_day)) if earliest else ym
    all_budgets = budgets_in_range(first_ym, ym)
    categories = {
        c.id: c
        for c in db.session.execute(
            select(Category).where(Category.household_id == household_id())
        ).scalars()
    }

    # Welke budgetten tonen we voor dit persoonsfilter?
    if person.kind == "member":
        keys = {
            (b.category_id, b.member_id) for b in all_budgets if b.member_id == person.member_id
        }
        spend_kwargs = {"member_id": person.member_id}
    else:
        keys = {(b.category_id, b.member_id) for b in all_budgets if b.member_id is None}
        spend_kwargs = {"joint_only": True} if person.kind == "joint" else {}

    # Uitgaven voor de gewenste scope; bij 'huishouden' ook per lid voor ledenbudgetten.
    spent_cache: dict[int | None, dict[tuple[int, str], int]] = {}

    def spent_for(member_id: int | None):
        if member_id not in spent_cache:
            kwargs = {"member_id": member_id} if member_id is not None else spend_kwargs
            spent_cache[member_id] = spent_map(first_ym, ym, user=user, **kwargs)
        return spent_cache[member_id]

    lines: list[BudgetLine] = []
    for category_id, member_id in sorted(
        keys, key=lambda k: (categories[k[0]].sort_order, categories[k[0]].name)
    ):
        current = _amount_for(all_budgets, category_id, member_id, ym)
        if current is None:
            continue
        spent_all = spent_for(member_id if person.is_household or person.kind == "member" else None)
        carry = 0
        for prev in months_between(roll_from, shift_ym(ym, -1)):
            prev_budget = _amount_for(all_budgets, category_id, member_id, prev)
            if prev_budget is None:
                carry = 0
                continue
            available = prev_budget.amount_cents + (carry if prev_budget.rollover else 0)
            carry = available - spent_all.get((category_id, prev), 0)
        carry_in = carry if current.rollover else 0
        available = current.amount_cents + carry_in
        spent = spent_all.get((category_id, ym), 0)
        remaining = available - spent
        pct = int(round(spent * 100 / available)) if available > 0 else (100 if spent == 0 else 999)
        per_day = None
        if remaining > 0:
            if period.contains(today):
                per_day = remaining // ((period.end - today).days + 1)
            elif today < period.start:
                per_day = remaining // period.days
        lines.append(
            BudgetLine(
                budget=current,
                category=categories[category_id],
                base_cents=current.amount_cents,
                carry_cents=carry_in,
                available_cents=available,
                spent_cents=spent,
                remaining_cents=remaining,
                pct=pct,
                state=_state(pct),
                per_day_cents=per_day,
            )
        )
    return lines


def copy_previous_month(ym: str) -> int:
    """Kopieert de effectieve budgetten van vorige maand als overrides naar `ym`.

    Budgetten die voor `ym` al een eigen override hebben, blijven ongemoeid.
    """
    prev = shift_ym(ym, -1)
    budgets = budgets_in_range(prev, ym)
    keys = {(b.category_id, b.member_id) for b in budgets}
    created = 0
    for category_id, member_id in keys:
        if any(
            b.month == ym and b.category_id == category_id and b.member_id == member_id
            for b in budgets
        ):
            continue
        source = _amount_for(budgets, category_id, member_id, prev)
        if source is None:
            continue
        db.session.add(
            Budget(
                household_id=household_id(),
                category_id=category_id,
                member_id=member_id,
                month=ym,
                amount_cents=source.amount_cents,
                rollover=source.rollover,
            )
        )
        created += 1
    db.session.commit()
    return created


def current_ym(today: date | None = None) -> str:
    return ym_of(today or date.today(), get_household().financial_month_start_day)
