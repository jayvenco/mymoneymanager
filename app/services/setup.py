"""Eerste-keer-installatie: huishouden, leden, admin-login, rekeningen en categorieën."""

import json
import re
from dataclasses import dataclass, field
from datetime import date

from ..extensions import db
from ..models import ACCOUNT_TYPES, Account, Household, Member, User
from ..utils.dates import parse_date
from ..utils.money import MoneyError, parse_money
from .seed import seed_default_categories

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_USERNAME = re.compile(r"^[a-z0-9_.-]{3,40}$")
MIN_PASSWORD = 10


@dataclass
class SetupResult:
    errors: dict[str, str] = field(default_factory=dict)
    user: User | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


def setup_needed() -> bool:
    return db.session.execute(db.select(db.func.count(User.id))).scalar_one() == 0


def validate_password(password: str, confirm: str | None = None) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Het wachtwoord moet minimaal {MIN_PASSWORD} tekens bevatten."
    if confirm is not None and password != confirm:
        return "De wachtwoorden komen niet overeen."
    return None


def _color(value: str, default: str) -> str:
    value = (value or "").strip()
    return value if _COLOR.match(value) else default


def _parse_accounts(raw: str, errors: dict[str, str]) -> list[dict]:
    try:
        rows = json.loads(raw or "[]")
    except ValueError:
        errors["accounts"] = "De rekeninggegevens zijn ongeldig."
        return []
    if not isinstance(rows, list):
        errors["accounts"] = "De rekeninggegevens zijn ongeldig."
        return []
    parsed = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not str(row.get("name", "")).strip():
            continue  # lege rij overslaan
        name = str(row["name"]).strip()[:120]
        owner = row.get("owner")
        if owner not in {"admin", "partner", "joint"}:
            errors["accounts"] = f"Rekening {index}: kies een eigenaar."
            continue
        acc_type = row.get("type", "checking")
        if acc_type not in ACCOUNT_TYPES:
            errors["accounts"] = f"Rekening {index}: ongeldig type."
            continue
        try:
            balance = parse_money(str(row.get("opening_balance") or "0"))
            opening = parse_date(str(row.get("opening_date") or date.today().isoformat()))
        except (MoneyError, ValueError) as exc:
            errors["accounts"] = f"Rekening {index} ({name}): {exc}"
            continue
        visibility = "private" if row.get("visibility") == "private" else "shared"
        parsed.append(
            {
                "name": name,
                "bank": str(row.get("bank", "")).strip()[:80],
                "owner": owner,
                "type": acc_type,
                "balance": balance,
                "opening": opening,
                "visibility": visibility,
            }
        )
    return parsed


def run_setup(form) -> SetupResult:
    """Valideert het formulier en maakt alles in één transactie aan."""
    result = SetupResult()
    errors = result.errors

    username = (form.get("username") or "").strip().lower()
    if not _USERNAME.match(username):
        errors["username"] = "Gebruikersnaam: 3–40 tekens (letters, cijfers, . _ -)."
    pw_error = validate_password(form.get("password", ""), form.get("password2", ""))
    if pw_error:
        errors["password"] = pw_error

    admin_name = (form.get("admin_name") or "").strip()
    if not admin_name:
        errors["admin_name"] = "Vul je naam in."
    partner_name = (form.get("partner_name") or "").strip()
    if not partner_name:
        errors["partner_name"] = "Vul de naam van je partner in."
    household_name = (form.get("household_name") or "").strip() or "Ons huishouden"

    try:
        start_day = int(form.get("start_day") or 1)
        if not 1 <= start_day <= 28:
            raise ValueError
    except ValueError:
        errors["start_day"] = "Kies een dag tussen 1 en 28."
        start_day = 1

    accounts = _parse_accounts(form.get("accounts", "[]"), errors)
    if errors:
        return result

    household = Household(name=household_name[:120], financial_month_start_day=start_day)
    db.session.add(household)
    db.session.flush()

    admin_member = Member(
        household_id=household.id,
        name=admin_name[:80],
        color=_color(form.get("admin_color"), "#4f46e5"),
        avatar_initials=Member.initials_for(admin_name),
        sort_order=0,
    )
    partner = Member(
        household_id=household.id,
        name=partner_name[:80],
        color=_color(form.get("partner_color"), "#ec4899"),
        avatar_initials=Member.initials_for(partner_name),
        sort_order=1,
    )
    db.session.add_all([admin_member, partner])
    db.session.flush()

    user = User(username=username, member_id=admin_member.id, is_admin=True)
    user.set_password(form.get("password", ""))
    db.session.add(user)

    owners = {"admin": admin_member.id, "partner": partner.id, "joint": None}
    palette = {"admin": admin_member.color, "partner": partner.color, "joint": "#0ea5e9"}
    for order, row in enumerate(accounts):
        db.session.add(
            Account(
                household_id=household.id,
                owner_member_id=owners[row["owner"]],
                name=row["name"],
                bank=row["bank"],
                type=row["type"],
                visibility=row["visibility"],
                opening_balance_cents=row["balance"],
                opening_date=row["opening"],
                color=palette[row["owner"]],
                icon={"savings": "piggy-bank", "cash": "coins", "investment": "trending-up"}.get(
                    row["type"], "wallet"
                ),
                sort_order=order,
            )
        )

    seed_default_categories(household.id)
    db.session.commit()
    result.user = user
    return result
