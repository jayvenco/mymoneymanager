"""Databasemodellen. Alle bedragen zijn integer cents (nooit floats)."""

import contextlib
import json
from datetime import date as Date
from datetime import datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from flask_login import UserMixin
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Table,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .extensions import db

_hasher = PasswordHasher()

ACCOUNT_TYPES = ("checking", "savings", "investment", "cash")
VISIBILITIES = ("shared", "private")
TX_TYPES = ("income", "expense", "transfer")
FREQUENCIES = ("weekly", "monthly", "quarterly", "yearly")


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


class Household(db.Model):
    __tablename__ = "household"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(db.String(120))
    financial_month_start_day: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    __table_args__ = (
        CheckConstraint("financial_month_start_day BETWEEN 1 AND 28", name="ck_hh_startday"),
    )


class Member(db.Model):
    __tablename__ = "member"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"), index=True)
    name: Mapped[str] = mapped_column(db.String(80))
    color: Mapped[str] = mapped_column(db.String(9), default="#4f46e5")
    avatar_initials: Mapped[str] = mapped_column(db.String(3), default="")
    sort_order: Mapped[int] = mapped_column(default=0)
    is_archived: Mapped[bool] = mapped_column(default=False)

    # De koppeling loopt via User.member_id (voorkomt een circulaire FK).
    user: Mapped["User | None"] = relationship(back_populates="member", uselist=False)

    @property
    def user_id(self) -> int | None:
        return self.user.id if self.user else None

    @staticmethod
    def initials_for(name: str) -> str:
        parts = [p for p in name.split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class User(UserMixin, db.Model):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(db.String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(db.String(255))
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("member.id"), unique=True, nullable=True
    )
    is_admin: Mapped[bool] = mapped_column(default=False)
    totp_secret: Mapped[str | None] = mapped_column(db.String(64), nullable=True)
    theme_preference: Mapped[str] = mapped_column(db.String(10), default="system")
    default_person_filter: Mapped[str] = mapped_column(db.String(20), default="household")
    # Bewaart per gebruiker o.a. periode/vergelijking/persoonsfilter.
    preferences_json: Mapped[str] = mapped_column(db.Text, default="{}")
    last_login: Mapped[datetime | None] = mapped_column(nullable=True)

    member: Mapped[Member | None] = relationship(back_populates="user")

    def set_password(self, password: str) -> None:
        self.password_hash = _hasher.hash(password)

    def check_password(self, password: str) -> bool:
        try:
            ok = _hasher.verify(self.password_hash, password)
        except (VerificationError, InvalidHashError):
            return False
        if ok and _hasher.check_needs_rehash(self.password_hash):
            self.password_hash = _hasher.hash(password)
        return ok

    @staticmethod
    def burn_password_check(password: str) -> None:
        """Gelijke rekentijd bij onbekende gebruikersnaam (tegen user enumeration)."""
        with contextlib.suppress(VerificationError, InvalidHashError):
            _hasher.verify(_DUMMY_HASH, password)

    @property
    def preferences(self) -> dict:
        try:
            return json.loads(self.preferences_json or "{}")
        except ValueError:
            return {}

    def set_preference(self, key: str, value) -> None:
        prefs = self.preferences
        prefs[key] = value
        self.preferences_json = json.dumps(prefs)


_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


class Account(db.Model):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"), index=True)
    # NULL = gezamenlijk
    owner_member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"), nullable=True)
    name: Mapped[str] = mapped_column(db.String(120))
    bank: Mapped[str] = mapped_column(db.String(80), default="")
    iban_last4: Mapped[str] = mapped_column(db.String(4), default="")
    type: Mapped[str] = mapped_column(db.String(12), default="checking")
    visibility: Mapped[str] = mapped_column(db.String(8), default="shared")
    opening_balance_cents: Mapped[int] = mapped_column(default=0)
    opening_date: Mapped[Date] = mapped_column(default=Date.today)
    color: Mapped[str] = mapped_column(db.String(9), default="#64748b")
    icon: Mapped[str] = mapped_column(db.String(40), default="wallet")
    is_archived: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(default=0)

    owner: Mapped[Member | None] = relationship()

    __table_args__ = (
        CheckConstraint("type IN ('checking','savings','investment','cash')", name="ck_acc_type"),
        CheckConstraint("visibility IN ('shared','private')", name="ck_acc_vis"),
    )


class Category(db.Model):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"), index=True)
    name: Mapped[str] = mapped_column(db.String(80))
    kind: Mapped[str] = mapped_column(db.String(8))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)
    color: Mapped[str] = mapped_column(db.String(9), default="#64748b")
    icon: Mapped[str] = mapped_column(db.String(40), default="tag")
    is_archived: Mapped[bool] = mapped_column(default=False)
    sort_order: Mapped[int] = mapped_column(default=0)
    # Systeemcategorie (bijv. Correctie): niet verwijderbaar.
    is_system: Mapped[bool] = mapped_column(default=False)
    # Standaard uitgesloten van rapportages (Correctie).
    exclude_from_reports: Mapped[bool] = mapped_column(default=False)
    # "Losse grote uitgaven": apart zichtbaar en uit te sluiten in andere rapporten.
    is_one_off: Mapped[bool] = mapped_column(default=False)

    parent: Mapped["Category | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Category"]] = relationship(back_populates="parent")

    __table_args__ = (CheckConstraint("kind IN ('income','expense')", name="ck_cat_kind"),)


class Tag(db.Model):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"), index=True)
    name: Mapped[str] = mapped_column(db.String(60))
    color: Mapped[str] = mapped_column(db.String(9), default="#64748b")

    __table_args__ = (UniqueConstraint("household_id", "name", name="uq_tag_household_name"),)


TransactionTag = Table(
    "transaction_tag",
    db.metadata,
    Column("transaction_id", ForeignKey("transaction.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_txtag_tag", "tag_id"),
)


class Transaction(db.Model):
    __tablename__ = "transaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"))
    date: Mapped[Date] = mapped_column()
    amount_cents: Mapped[int] = mapped_column()
    type: Mapped[str] = mapped_column(db.String(8))
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    to_account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)
    description: Mapped[str] = mapped_column(db.String(255), default="")
    payee: Mapped[str] = mapped_column(db.String(120), default="")
    notes: Mapped[str] = mapped_column(db.Text, default="")
    # Herkomst (CSV-import / terugkerende regel); gebruikt voor duplicaatdetectie.
    external_ref: Mapped[str | None] = mapped_column(db.String(64), nullable=True)
    # Terugkerende uitgave die elke 6 of 12 maanden herzien moet worden (None = niet van toepassing).
    review_months: Mapped[int | None] = mapped_column(nullable=True)
    review_checked_on: Mapped[Date | None] = mapped_column(nullable=True)
    recurring_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("recurring_rule.id", ondelete="SET NULL"), nullable=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    updated_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    account: Mapped[Account] = relationship(foreign_keys=[account_id])
    to_account: Mapped[Account | None] = relationship(foreign_keys=[to_account_id])
    category: Mapped[Category | None] = relationship()
    tags: Mapped[list[Tag]] = relationship(secondary=TransactionTag, lazy="selectin")

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_tx_amount_positive"),
        CheckConstraint("type IN ('income','expense','transfer')", name="ck_tx_type"),
        CheckConstraint(
            "(type = 'transfer' AND to_account_id IS NOT NULL AND to_account_id != account_id)"
            " OR (type != 'transfer' AND to_account_id IS NULL)",
            name="ck_tx_transfer_target",
        ),
        Index("ix_tx_household_date", "household_id", "date"),
        Index("ix_tx_account_date", "account_id", "date"),
        Index("ix_tx_category_date", "category_id", "date"),
        Index("ix_tx_to_account_date", "to_account_id", "date"),
    )


class Budget(db.Model):
    __tablename__ = "budget"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("category.id"))
    # NULL = hele huishouden
    member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"), nullable=True)
    # 'YYYY-MM'; NULL = standaard voor elke maand
    month: Mapped[str | None] = mapped_column(db.String(7), nullable=True)
    amount_cents: Mapped[int] = mapped_column()
    rollover: Mapped[bool] = mapped_column(default=False)

    category: Mapped[Category] = relationship()

    __table_args__ = (
        UniqueConstraint("household_id", "category_id", "member_id", "month", name="uq_budget"),
    )


class RecurringRule(db.Model):
    __tablename__ = "recurring_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"), index=True)
    name: Mapped[str] = mapped_column(db.String(120))
    type: Mapped[str] = mapped_column(db.String(8))
    amount_cents: Mapped[int] = mapped_column()
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    to_account_id: Mapped[int | None] = mapped_column(ForeignKey("account.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("category.id"), nullable=True)
    description: Mapped[str] = mapped_column(db.String(255), default="")
    payee: Mapped[str] = mapped_column(db.String(120), default="")
    notes: Mapped[str] = mapped_column(db.Text, default="")
    tag_ids_json: Mapped[str] = mapped_column(db.Text, default="[]")
    frequency: Mapped[str] = mapped_column(db.String(10), default="monthly")
    interval: Mapped[int] = mapped_column(default=1)
    # Dag van de maand waarop de regel oorspronkelijk viel (voorkomt drift bij 29-31).
    anchor_day: Mapped[int | None] = mapped_column(nullable=True)
    next_date: Mapped[Date] = mapped_column()
    end_date: Mapped[Date | None] = mapped_column(nullable=True)
    auto_create: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)

    account: Mapped[Account] = relationship(foreign_keys=[account_id])
    to_account: Mapped[Account | None] = relationship(foreign_keys=[to_account_id])
    category: Mapped[Category | None] = relationship()

    __table_args__ = (
        CheckConstraint(
            "frequency IN ('weekly','monthly','quarterly','yearly')", name="ck_rr_freq"
        ),
        CheckConstraint("interval >= 1", name="ck_rr_interval"),
    )


class SavingsGoal(db.Model):
    __tablename__ = "savings_goal"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"), index=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("account.id"))
    # NULL = gezamenlijk doel
    member_id: Mapped[int | None] = mapped_column(ForeignKey("member.id"), nullable=True)
    name: Mapped[str] = mapped_column(db.String(120))
    target_cents: Mapped[int] = mapped_column()
    target_date: Mapped[Date | None] = mapped_column(nullable=True)
    color: Mapped[str] = mapped_column(db.String(9), default="#10b981")

    account: Mapped[Account] = relationship()


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    household_id: Mapped[int] = mapped_column(ForeignKey("household.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)
    entity: Mapped[str] = mapped_column(db.String(40))
    entity_id: Mapped[int] = mapped_column()
    action: Mapped[str] = mapped_column(db.String(8))
    before_json: Mapped[str | None] = mapped_column(db.Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(db.Text, nullable=True)
    timestamp: Mapped[datetime] = mapped_column(default=_now, server_default=func.now())

    user: Mapped[User | None] = relationship()

    __table_args__ = (
        CheckConstraint("action IN ('create','update','delete')", name="ck_audit_action"),
        Index("ix_audit_entity", "entity", "entity_id"),
    )


class AppSetting(db.Model):
    """Sleutel/waarde-instellingen die in de UI wijzigbaar zijn (o.a. back-ups)."""

    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(db.String(60), primary_key=True)
    value: Mapped[str] = mapped_column(db.Text, default="")

    @staticmethod
    def get(key: str, default: str | None = None) -> str | None:
        row = db.session.get(AppSetting, key)
        return row.value if row else default

    @staticmethod
    def put(key: str, value: str) -> None:
        row = db.session.get(AppSetting, key)
        if row:
            row.value = value
        else:
            db.session.add(AppSetting(key=key, value=value))
