"""Centrale toegangscontrole.

ELKE query op rekeningen of transacties loopt via deze module. Regels:
- Admin ziet alles.
- Overige logins zien `shared` rekeningen en `private` rekeningen waarvan ze zelf eigenaar zijn.
- Een transactie is zichtbaar als de rekening zichtbaar is én (bij een overboeking) ook de
  tegenrekening, zodat een privé-rekening nooit via de gezamenlijke kant lekt.
"""

from flask_login import current_user
from sqlalchemy import Select, and_, false, or_, select

from ..extensions import db
from ..models import Account, Transaction


def _user(user=None):
    return user if user is not None else current_user


def account_visibility_clause(user=None):
    user = _user(user)
    if not getattr(user, "is_authenticated", False):
        return false()
    if user.is_admin:
        return Account.household_id.isnot(None)
    shared = Account.visibility == "shared"
    if user.member_id is None:
        return shared
    return or_(
        shared, and_(Account.visibility == "private", Account.owner_member_id == user.member_id)
    )


def visible_accounts(user=None, *, include_archived: bool = True) -> Select:
    stmt = select(Account).where(account_visibility_clause(user))
    if not include_archived:
        stmt = stmt.where(Account.is_archived.is_(False))
    return stmt.order_by(Account.sort_order, Account.id)


def visible_account_ids(user=None) -> Select:
    return select(Account.id).where(account_visibility_clause(user))


def can_see_account(account_id: int | None, user=None) -> bool:
    if account_id is None:
        return False
    stmt = select(Account.id).where(Account.id == account_id, account_visibility_clause(user))
    return db.session.execute(stmt).first() is not None


def transaction_visibility_clause(user=None):
    ids = visible_account_ids(user)
    return and_(
        Transaction.account_id.in_(ids),
        or_(Transaction.to_account_id.is_(None), Transaction.to_account_id.in_(ids)),
    )


def visible_transactions(user=None) -> Select:
    return select(Transaction).where(transaction_visibility_clause(user))


def get_visible_transaction(tx_id: int, user=None) -> Transaction | None:
    stmt = visible_transactions(user).where(Transaction.id == tx_id)
    return db.session.execute(stmt).scalar_one_or_none()


def get_visible_account(account_id: int, user=None) -> Account | None:
    stmt = visible_accounts(user).where(Account.id == account_id)
    return db.session.execute(stmt).scalar_one_or_none()
