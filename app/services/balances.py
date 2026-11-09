"""Saldoberekening. Alles in SQL; bedragen in cents.

Het saldo van een rekening = beginsaldo + alle transacties vanaf de begindatum (t/m `as_of`):
inkomsten en inkomende overboekingen tellen mee, uitgaven en uitgaande overboekingen gaan eraf.
"""

from collections.abc import Iterable
from datetime import date, timedelta

from sqlalchemy import case, func, select

from ..extensions import db
from ..models import Account, Transaction


def account_balances(
    account_ids: Iterable[int] | None = None, as_of: date | None = None
) -> dict[int, int]:
    acc_stmt = select(Account.id, Account.opening_balance_cents)
    if account_ids is not None:
        acc_stmt = acc_stmt.where(Account.id.in_(list(account_ids)))
    balances = {row.id: row.opening_balance_cents for row in db.session.execute(acc_stmt)}
    if not balances:
        return balances

    out_amount = case(
        (Transaction.type == "income", Transaction.amount_cents), else_=-Transaction.amount_cents
    )
    out_stmt = (
        select(Transaction.account_id, func.sum(out_amount))
        .join(Account, Account.id == Transaction.account_id)
        .where(Transaction.account_id.in_(list(balances)), Transaction.date >= Account.opening_date)
        .group_by(Transaction.account_id)
    )
    in_stmt = (
        select(Transaction.to_account_id, func.sum(Transaction.amount_cents))
        .join(Account, Account.id == Transaction.to_account_id)
        .where(
            Transaction.type == "transfer",
            Transaction.to_account_id.in_(list(balances)),
            Transaction.date >= Account.opening_date,
        )
        .group_by(Transaction.to_account_id)
    )
    if as_of is not None:
        out_stmt = out_stmt.where(Transaction.date <= as_of)
        in_stmt = in_stmt.where(Transaction.date <= as_of)
    for account_id, total in db.session.execute(out_stmt):
        balances[account_id] += int(total or 0)
    for account_id, total in db.session.execute(in_stmt):
        balances[account_id] += int(total or 0)
    return balances


def account_balance(account_id: int, as_of: date | None = None) -> int:
    return account_balances([account_id], as_of).get(account_id, 0)


def balance_series(account_id: int, start: date, end: date) -> list[tuple[date, int]]:
    """Dagelijks eindsaldo van `start` t/m `end` (0 vóór de begindatum van de rekening)."""
    account = db.session.get(Account, account_id)
    if account is None or end < start:
        return []
    running = (
        account_balance(account_id, start - timedelta(days=1))
        if start > account.opening_date
        else 0
    )

    first = max(start, account.opening_date)
    delta = case(
        (Transaction.type == "income", Transaction.amount_cents), else_=-Transaction.amount_cents
    )
    out_rows = db.session.execute(
        select(Transaction.date, func.sum(delta))
        .where(
            Transaction.account_id == account_id,
            Transaction.date >= first,
            Transaction.date <= end,
        )
        .group_by(Transaction.date)
    )
    in_rows = db.session.execute(
        select(Transaction.date, func.sum(Transaction.amount_cents))
        .where(
            Transaction.to_account_id == account_id,
            Transaction.type == "transfer",
            Transaction.date >= first,
            Transaction.date <= end,
        )
        .group_by(Transaction.date)
    )
    deltas: dict[date, int] = {}
    for day, total in list(out_rows) + list(in_rows):
        deltas[day] = deltas.get(day, 0) + int(total or 0)

    series: list[tuple[date, int]] = []
    day = start
    while day <= end:
        if day == account.opening_date:
            running += account.opening_balance_cents
        if day >= account.opening_date:
            running += deltas.get(day, 0)
        series.append((day, running))
        day += timedelta(days=1)
    return series
