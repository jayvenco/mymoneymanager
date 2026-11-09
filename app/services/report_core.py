"""Rapportage-rekenlaag: periodes, vergelijkingen en SQL-aggregaties.

Alle bedragen zijn integer cents. Overboekingen tellen NOOIT mee als inkomsten of uitgaven; ze
worden apart (en per persoon benoemd als in-/uitgaand) getoond. Correctiecategorieën
(`exclude_from_reports`) vallen standaard buiten alle rapporten.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import aliased

from ..extensions import db
from ..models import Account, Category, Transaction
from . import access
from .household import get_household, household_id
from .people import HOUSEHOLD, PersonFilter, owner_clause
from .periods import (
    Period,
    add_months_clamped,
    fin_month_expr,
    financial_month,
    financial_month_for,
    months_between,
    shift_ym,
    ym_of,
)

PERIOD_CHOICES = [
    ("this_month", "Deze maand"),
    ("last_month", "Vorige maand"),
    ("this_quarter", "Dit kwartaal"),
    ("this_year", "Dit jaar"),
    ("last_year", "Vorig jaar"),
    ("last_12m", "Laatste 12 maanden"),
    ("custom", "Aangepast"),
]
COMPARE_CHOICES = [
    ("none", "Geen vergelijking"),
    ("prev", "Vorige periode"),
    ("yoy", "Zelfde periode vorig jaar"),
]


@dataclass(frozen=True)
class ReportParams:
    period_key: str
    period: Period
    compare: str
    compare_period: Period | None
    person: PersonFilter
    hide_one_off: bool = False
    today: date = field(default_factory=date.today)

    @property
    def start_day(self) -> int:
        return get_household().financial_month_start_day

    def query(self) -> dict[str, str]:
        """Parameters voor deelbare URL's."""
        q = {"periode": self.period_key, "vergelijk": self.compare, "person": self.person.key}
        if self.period_key == "custom":
            q["van"], q["tot"] = self.period.start.isoformat(), self.period.end.isoformat()
        if self.hide_one_off:
            q["losse"] = "uit"
        return q


# --- periodes -----------------------------------------------------------------------------------


def resolve_period(
    key: str,
    start_day: int,
    today: date,
    custom_from: date | None = None,
    custom_to: date | None = None,
) -> Period:
    ym = ym_of(today, start_day)
    year, month = (int(p) for p in ym.split("-"))
    if key == "last_month":
        p = financial_month(shift_ym(ym, -1), start_day)
        return Period(p.start, p.end, "Vorige maand")
    if key == "this_quarter":
        first_month = ((month - 1) // 3) * 3 + 1
        return Period(
            financial_month(f"{year}-{first_month:02d}", start_day).start,
            financial_month(f"{year}-{first_month + 2:02d}", start_day).end,
            "Dit kwartaal",
        )
    if key in {"this_year", "last_year"}:
        y = year if key == "this_year" else year - 1
        return Period(
            financial_month(f"{y}-01", start_day).start,
            financial_month(f"{y}-12", start_day).end,
            "Dit jaar" if key == "this_year" else "Vorig jaar",
        )
    if key == "last_12m":
        return Period(
            financial_month(shift_ym(ym, -11), start_day).start,
            financial_month(ym, start_day).end,
            "Laatste 12 maanden",
        )
    if key == "custom" and custom_from and custom_to and custom_from <= custom_to:
        return Period(custom_from, custom_to, "Aangepast")
    p = financial_month(ym, start_day)
    return Period(p.start, p.end, "Deze maand")


def _aligned_months(period: Period, start_day: int) -> int | None:
    first = financial_month_for(period.start, start_day)
    last = financial_month_for(period.end, start_day)
    if first.start == period.start and last.end == period.end:
        return len(months_between(first.label, last.label))
    return None


def comparison_period(period: Period, mode: str, start_day: int) -> Period | None:
    if mode not in {"prev", "yoy"}:
        return None
    months = _aligned_months(period, start_day)
    if months is not None:
        shift = -months if mode == "prev" else -12
        first = shift_ym(ym_of(period.start, start_day), shift)
        last = shift_ym(ym_of(period.end, start_day), shift)
        return Period(financial_month(first, start_day).start, financial_month(last, start_day).end)
    if mode == "prev":
        length = period.days
        return Period(period.start - timedelta(days=length), period.start - timedelta(days=1))
    return Period(add_months_clamped(period.start, -12), add_months_clamped(period.end, -12))


def months_in(period: Period, start_day: int) -> list[str]:
    return months_between(ym_of(period.start, start_day), ym_of(period.end, start_day))


# --- basisquery ---------------------------------------------------------------------------------


def _visible_user(user=None):
    return user


def base_query(
    params: ReportParams,
    period: Period,
    cols,
    *,
    types=("income", "expense"),
    person: PersonFilter | None = None,
    user=None,
    one_off_only: bool = False,
    respect_one_off_toggle: bool = True,
):
    """SELECT over zichtbare transacties met alle standaardfilters; `cols` zijn de kolommen."""
    parent = aliased(Category)
    stmt = (
        select(*cols)
        .select_from(Transaction)
        .join(Account, Account.id == Transaction.account_id)
        .outerjoin(Category, Category.id == Transaction.category_id)
        .outerjoin(parent, parent.id == Category.parent_id)
        .where(
            access.transaction_visibility_clause(user),
            Transaction.household_id == household_id(),
            Transaction.date >= period.start,
            Transaction.date <= period.end,
            Transaction.type.in_(types),
            or_(Category.id.is_(None), Category.exclude_from_reports.is_(False)),
        )
    )
    chosen = person if person is not None else params.person
    clause = owner_clause(chosen)
    if clause is not None:
        stmt = stmt.where(clause)
    one_off = or_(Category.is_one_off.is_(True), parent.is_one_off.is_(True))
    if one_off_only:
        stmt = stmt.where(one_off)
    elif params.hide_one_off and respect_one_off_toggle:
        stmt = stmt.where(~and_(Category.id.isnot(None), one_off))
    return stmt


def _sum_amount():
    return func.coalesce(func.sum(Transaction.amount_cents), 0)


# --- kerncijfers --------------------------------------------------------------------------------


@dataclass
class Totals:
    income: int = 0
    expense: int = 0
    deposits: int = 0  # stortingen op spaarrekeningen (overboekingen), apart van inkomsten/uitgaven

    @property
    def net(self) -> int:
        return self.income - self.expense

    @property
    def savings_rate(self) -> float | None:
        return (self.net / self.income * 100) if self.income > 0 else None


def totals(
    params: ReportParams, period: Period, *, person: PersonFilter | None = None, user=None
) -> Totals:
    rows = db.session.execute(
        base_query(
            params, period, [Transaction.type, _sum_amount()], person=person, user=user
        ).group_by(Transaction.type)
    )
    result = Totals()
    for tx_type, amount in rows:
        if tx_type == "income":
            result.income = int(amount)
        else:
            result.expense = int(amount)
    result.deposits = savings_deposits(params, period, person=person, user=user)
    return result


def savings_deposits(
    params: ReportParams, period: Period, *, person: PersonFilter | None = None, user=None
) -> int:
    """Overboekingen naar spaarrekeningen vanaf niet-spaarrekeningen, per eigenaar van de spaarrekening."""
    dest = aliased(Account)
    src = aliased(Account)
    stmt = (
        select(func.coalesce(func.sum(Transaction.amount_cents), 0))
        .select_from(Transaction)
        .join(src, src.id == Transaction.account_id)
        .join(dest, dest.id == Transaction.to_account_id)
        .where(
            access.transaction_visibility_clause(user),
            Transaction.household_id == household_id(),
            Transaction.type == "transfer",
            Transaction.date >= period.start,
            Transaction.date <= period.end,
            dest.type == "savings",
            src.type != "savings",
        )
    )
    clause = owner_clause(person if person is not None else params.person, dest.owner_member_id)
    if clause is not None:
        stmt = stmt.where(clause)
    return int(db.session.execute(stmt).scalar_one())


def transfer_net(params: ReportParams, period: Period, user=None) -> tuple[int, int]:
    """(inkomend, uitgaand) overboekingen vanuit het perspectief van het persoonsfilter."""
    if params.person.is_household:
        return 0, 0
    src, dest = aliased(Account), aliased(Account)
    base = (
        select(func.coalesce(func.sum(Transaction.amount_cents), 0))
        .select_from(Transaction)
        .join(src, src.id == Transaction.account_id)
        .join(dest, dest.id == Transaction.to_account_id)
        .where(
            access.transaction_visibility_clause(user),
            Transaction.household_id == household_id(),
            Transaction.type == "transfer",
            Transaction.date >= period.start,
            Transaction.date <= period.end,
        )
    )
    mine_src = owner_clause(params.person, src.owner_member_id)
    mine_dest = owner_clause(params.person, dest.owner_member_id)
    incoming = db.session.execute(base.where(mine_dest, ~mine_src)).scalar_one()
    outgoing = db.session.execute(base.where(mine_src, ~mine_dest)).scalar_one()
    return int(incoming), int(outgoing)


def delta(
    current: int | float, previous: int | float | None
) -> tuple[int | float, float | None] | None:
    if previous is None:
        return None
    diff = current - previous
    pct = (diff / abs(previous) * 100) if previous else None
    return diff, pct


def balance_total(params: ReportParams, as_of: date, user=None) -> int:
    from .balances import account_balances

    stmt = access.visible_accounts(user, include_archived=False)
    clause = owner_clause(params.person)
    if clause is not None:
        stmt = stmt.where(clause)
    ids = [a.id for a in db.session.execute(stmt).scalars()]
    # Rekeningen die nog niet bestonden tellen niet mee.
    existing = {
        a.id
        for a in db.session.execute(select(Account).where(Account.id.in_(ids))).scalars()
        if a.opening_date <= as_of
    }
    return sum(v for k, v in account_balances(ids, as_of).items() if k in existing)


# --- reeksen ------------------------------------------------------------------------------------


def monthly_income_expense(
    params: ReportParams, ym_from: str, ym_to: str, *, person: PersonFilter | None = None, user=None
) -> dict[str, tuple[int, int]]:
    """{ym: (inkomsten, uitgaven)} per financiële maand."""
    start_day = params.start_day
    window = Period(
        financial_month(ym_from, start_day).start, financial_month(ym_to, start_day).end
    )
    month = fin_month_expr(Transaction.date, start_day)
    rows = db.session.execute(
        base_query(
            params,
            window,
            [month.label("ym"), Transaction.type, _sum_amount()],
            person=person,
            user=user,
        ).group_by(month, Transaction.type)
    )
    result = {ym: (0, 0) for ym in months_between(ym_from, ym_to)}
    for ym, tx_type, amount in rows:
        income, expense = result.get(ym, (0, 0))
        result[ym] = (
            (income + int(amount), expense)
            if tx_type == "income"
            else (income, expense + int(amount))
        )
    return result


def category_totals(
    params: ReportParams,
    period: Period,
    kind: str,
    *,
    person: PersonFilter | None = None,
    user=None,
) -> dict[int, int]:
    """{categorie_id: bedrag} (alleen de directe categorie, zonder opsplitsing)."""
    rows = db.session.execute(
        base_query(
            params,
            period,
            [Transaction.category_id, _sum_amount()],
            types=(kind,),
            person=person,
            user=user,
        ).group_by(Transaction.category_id)
    )
    return {cid: int(total) for cid, total in rows if cid is not None}


@dataclass
class CategoryNode:
    category: Category
    own: int
    total: int
    children: list["CategoryNode"] = field(default_factory=list)


def category_tree(totals_by_id: dict[int, int], kind: str) -> list[CategoryNode]:
    cats = list(
        db.session.execute(
            select(Category).where(Category.household_id == household_id(), Category.kind == kind)
        ).scalars()
    )
    by_parent: dict[int | None, list[Category]] = {}
    for c in sorted(cats, key=lambda c: (c.sort_order, c.name)):
        by_parent.setdefault(c.parent_id, []).append(c)
    nodes: list[CategoryNode] = []
    for parent in by_parent.get(None, []):
        children = [
            CategoryNode(c, totals_by_id.get(c.id, 0), totals_by_id.get(c.id, 0))
            for c in by_parent.get(parent.id, [])
            if totals_by_id.get(c.id, 0)
        ]
        own = totals_by_id.get(parent.id, 0)
        total = own + sum(c.total for c in children)
        if total:
            nodes.append(CategoryNode(parent, own, total, children))
    return sorted(nodes, key=lambda n: -n.total)


def category_month_matrix(
    params: ReportParams,
    ym_from: str,
    ym_to: str,
    kind: str,
    *,
    person: PersonFilter | None = None,
    user=None,
) -> dict[tuple[int, str], int]:
    start_day = params.start_day
    window = Period(
        financial_month(ym_from, start_day).start, financial_month(ym_to, start_day).end
    )
    month = fin_month_expr(Transaction.date, start_day)
    rows = db.session.execute(
        base_query(
            params,
            window,
            [Transaction.category_id, month.label("ym"), _sum_amount()],
            types=(kind,),
            person=person,
            user=user,
        ).group_by(Transaction.category_id, month)
    )
    return {(cid, ym): int(total) for cid, ym, total in rows if cid is not None}


def owner_totals(params: ReportParams, period: Period, user=None) -> dict[int | None, Totals]:
    """Inkomsten/uitgaven per eigenaar (lid-id of None = gezamenlijk), ongeacht het persoonsfilter."""
    rows = db.session.execute(
        base_query(
            params,
            period,
            [Account.owner_member_id, Transaction.type, _sum_amount()],
            person=HOUSEHOLD,
            user=user,
        ).group_by(Account.owner_member_id, Transaction.type)
    )
    result: dict[int | None, Totals] = {}
    for owner, tx_type, amount in rows:
        t = result.setdefault(owner, Totals())
        if tx_type == "income":
            t.income = int(amount)
        else:
            t.expense = int(amount)
    return result


def owner_category_totals(
    params: ReportParams, period: Period, kind: str, user=None
) -> dict[tuple[int | None, int], int]:
    rows = db.session.execute(
        base_query(
            params,
            period,
            [Account.owner_member_id, Transaction.category_id, _sum_amount()],
            types=(kind,),
            person=HOUSEHOLD,
            user=user,
        ).group_by(Account.owner_member_id, Transaction.category_id)
    )
    return {(owner, cid): int(total) for owner, cid, total in rows if cid is not None}


def owner_savings_deposits(
    params: ReportParams, period: Period, owners, user=None
) -> dict[int | None, int]:
    return {
        owner: savings_deposits(
            params,
            period,
            person=PersonFilter("joint", "joint")
            if owner is None
            else PersonFilter(f"member:{owner}", "member", owner),
            user=user,
        )
        for owner in owners
    }


def joint_contributions(params: ReportParams, period: Period, user=None) -> dict[int, int]:
    """Overboekingen van de rekeningen van een lid naar gezamenlijke rekeningen."""
    src, dest = aliased(Account), aliased(Account)
    rows = db.session.execute(
        select(src.owner_member_id, func.coalesce(func.sum(Transaction.amount_cents), 0))
        .select_from(Transaction)
        .join(src, src.id == Transaction.account_id)
        .join(dest, dest.id == Transaction.to_account_id)
        .where(
            access.transaction_visibility_clause(user),
            Transaction.household_id == household_id(),
            Transaction.type == "transfer",
            Transaction.date >= period.start,
            Transaction.date <= period.end,
            dest.owner_member_id.is_(None),
            src.owner_member_id.isnot(None),
        )
        .group_by(src.owner_member_id)
    )
    return {owner: int(total) for owner, total in rows}


# --- vermogen -----------------------------------------------------------------------------------


def account_month_end_balances(
    ym_from: str,
    ym_to: str,
    *,
    user=None,
    person: PersonFilter = HOUSEHOLD,
    start_day: int | None = None,
) -> tuple[list[str], list[Account], dict[int, list[int]]]:
    """Saldo per rekening aan het einde van elke financiële maand (toekomstige maanden = laatste stand)."""
    start_day = start_day or get_household().financial_month_start_day
    stmt = access.visible_accounts(user, include_archived=False)
    clause = owner_clause(person)
    if clause is not None:
        stmt = stmt.where(clause)
    accounts = list(db.session.execute(stmt).scalars())
    ids = [a.id for a in accounts]
    months = months_between(ym_from, ym_to)
    month = fin_month_expr(Transaction.date, start_day)
    out_delta = case(
        (Transaction.type == "income", Transaction.amount_cents), else_=-Transaction.amount_cents
    )
    deltas: dict[tuple[int, str], int] = {}
    for account_id, ym, total in db.session.execute(
        select(Transaction.account_id, month, func.sum(out_delta))
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.account_id.in_(ids), Transaction.date >= Account.opening_date)
        .group_by(Transaction.account_id, month)
    ):
        deltas[(account_id, ym)] = deltas.get((account_id, ym), 0) + int(total)
    for account_id, ym, total in db.session.execute(
        select(Transaction.to_account_id, month, func.sum(Transaction.amount_cents))
        .join(Account, Account.id == Transaction.to_account_id)
        .where(
            Transaction.type == "transfer",
            Transaction.to_account_id.in_(ids),
            Transaction.date >= Account.opening_date,
        )
        .group_by(Transaction.to_account_id, month)
    ):
        deltas[(account_id, ym)] = deltas.get((account_id, ym), 0) + int(total)

    series: dict[int, list[int]] = {}
    for account in accounts:
        opening_ym = ym_of(account.opening_date, start_day)
        history_start = min([opening_ym, *[ym for (aid, ym) in deltas if aid == account.id]])
        running, by_month = 0, {}
        for ym in months_between(history_start, max(ym_to, history_start)):
            if ym >= opening_ym:
                if ym == opening_ym:
                    running += account.opening_balance_cents
                running += deltas.get((account.id, ym), 0)
            by_month[ym] = running if ym >= opening_ym else 0
        series[account.id] = [by_month.get(ym, 0) for ym in months]
    return months, accounts, series


def window_for(period: Period, start_day: int, max_months: int = 12) -> tuple[str, str]:
    months = months_in(period, start_day)
    return (months[-max_months] if len(months) > max_months else months[0]), months[-1]
