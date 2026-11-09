"""CSV-import van banktransacties: presets (ING, Rabobank, ABN AMRO), mapping, voorbeeld en duplicaten."""

import csv
import hashlib
import io
import re
import secrets
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime
from pathlib import Path

from flask import current_app
from sqlalchemy import func, select

from ..extensions import db
from ..models import Account, Transaction
from ..utils.money import MoneyError, parse_money
from . import transactions as tx_svc
from .household import household_id

FIELDS = [
    ("date", "Datum", True),
    ("amount", "Bedrag", True),
    ("direction", "Af/Bij-kolom (optioneel)", False),
    ("description", "Omschrijving", False),
    ("payee", "Naam / begunstigde", False),
    ("notes", "Notities / mededelingen", False),
]
DATE_FORMATS = [
    ("auto", "Automatisch herkennen"),
    ("%Y%m%d", "JJJJMMDD (20240131)"),
    ("%Y-%m-%d", "JJJJ-MM-DD (2024-01-31)"),
    ("%d-%m-%Y", "DD-MM-JJJJ (31-01-2024)"),
    ("%d/%m/%Y", "DD/MM/JJJJ (31/01/2024)"),
]

PRESETS: dict[str, dict] = {
    "ing": {
        "label": "ING (CSV)",
        "map": {
            "date": "Datum",
            "amount": "Bedrag (EUR)",
            "direction": "Af Bij",
            "description": "Naam / Omschrijving",
            "payee": "Naam / Omschrijving",
            "notes": "Mededelingen",
        },
        "date_format": "%Y%m%d",
    },
    "rabobank": {
        "label": "Rabobank (CSV)",
        "map": {
            "date": "Datum",
            "amount": "Bedrag",
            "description": "Omschrijving-1",
            "payee": "Naam tegenpartij",
            "notes": "Omschrijving-2",
        },
        "date_format": "%Y-%m-%d",
    },
    "abn": {
        "label": "ABN AMRO (TXT/CSV)",
        "map": {
            "date": "Transactiedatum",
            "amount": "Transactiebedrag",
            "description": "Omschrijving",
        },
        "date_format": "%Y%m%d",
    },
    "generic": {"label": "Anders (zelf koppelen)", "map": {}, "date_format": "auto"},
}
ABN_HEADERS = [
    "Rekeningnummer", "Muntsoort", "Transactiedatum", "Beginsaldo", "Eindsaldo",
    "Rentedatum", "Transactiebedrag", "Omschrijving",
]  # fmt: skip

MAX_ROWS = 20000
PREVIEW_LIMIT = 300  # aantal rijen dat in het voorbeeld wordt getoond


class ImportError_(Exception):  # noqa: N818 - voorkomt botsing met de ingebouwde ImportError
    pass


@dataclass
class ParsedFile:
    headers: list[str]
    rows: list[list[str]]


@dataclass
class Row:
    index: int
    date: Date | None = None
    cents: int = 0  # met teken: positief = inkomst
    description: str = ""
    payee: str = ""
    notes: str = ""
    ref: str = ""
    error: str | None = None
    duplicate: bool = False
    category_id: int | None = None


@dataclass
class Preview:
    rows: list[Row] = field(default_factory=list)

    @property
    def new(self) -> int:
        return sum(1 for r in self.rows if not r.error and not r.duplicate)

    @property
    def duplicates(self) -> int:
        return sum(1 for r in self.rows if r.duplicate and not r.error)

    @property
    def errors(self) -> int:
        return sum(1 for r in self.rows if r.error)


# --- bestand --------------------------------------------------------------------------------


def import_dir() -> Path:
    path = current_app.config["DATA_DIR"] / "imports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_upload(data: bytes) -> str:
    cleanup_old()
    token = secrets.token_hex(16)
    (import_dir() / f"{token}.csv").write_bytes(data)
    return token


def load_upload(token: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise ImportError_("Ongeldig importbestand; begin opnieuw.")
    path = import_dir() / f"{token}.csv"
    if not path.exists():
        raise ImportError_("Het geüploade bestand is verlopen; upload het opnieuw.")
    return path.read_bytes()


def discard_upload(token: str) -> None:
    if re.fullmatch(r"[0-9a-f]{32}", token or ""):
        (import_dir() / f"{token}.csv").unlink(missing_ok=True)


def cleanup_old(max_age_seconds: int = 86400) -> None:
    now = datetime.now().timestamp()
    for path in import_dir().glob("*.csv"):
        if now - path.stat().st_mtime > max_age_seconds:
            path.unlink(missing_ok=True)


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def parse_file(data: bytes) -> ParsedFile:
    text = decode(data)
    sample = text[:4096]
    first = sample.splitlines()[0] if sample.strip() else ""
    delimiter = max([";", ",", "\t"], key=first.count) if first else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise ImportError_("Het bestand bevat geen rijen.")
    if len(rows) > MAX_ROWS:
        raise ImportError_(f"Te veel rijen (max. {MAX_ROWS}).")
    # ABN AMRO TXT heeft geen kopregel
    if len(rows[0]) >= 8 and re.fullmatch(r"\d{8}", rows[0][2].strip() or "") and delimiter == "\t":
        return ParsedFile(ABN_HEADERS, rows)
    return ParsedFile([h.strip() for h in rows[0]], rows[1:])


def detect_preset(headers: list[str]) -> str:
    lower = {h.strip().lower() for h in headers}
    if {"naam / omschrijving", "af bij", "bedrag (eur)"} <= lower:
        return "ing"
    if {"naam tegenpartij", "omschrijving-1", "bedrag"} <= lower:
        return "rabobank"
    if {"transactiedatum", "transactiebedrag"} <= lower:
        return "abn"
    return "generic"


# --- parseren -------------------------------------------------------------------------------


def _parse_date(value: str, fmt: str) -> Date:
    value = value.strip()
    if fmt != "auto":
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError as exc:
            raise ValueError(f"Ongeldige datum: {value!r}") from exc
    for candidate in ("%Y%m%d", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(value, candidate).date()
        except ValueError:
            continue
    raise ValueError(f"Ongeldige datum: {value!r}")


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _ref(d: Date, cents: int, description: str, payee: str) -> str:
    # Omschrijving óf begunstigde: handmatige en geïmporteerde transacties blijven vergelijkbaar.
    raw = f"{d.isoformat()}|{cents}|{_norm(description or payee)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:24]  # noqa: S324 - geen beveiligingsdoel


def parse_rows(parsed: ParsedFile, mapping: dict[str, str], date_format: str) -> list[Row]:
    index = {h: i for i, h in enumerate(parsed.headers)}

    def col(row: list[str], name: str) -> str:
        i = index.get(mapping.get(name, ""))
        return row[i].strip() if i is not None and i < len(row) else ""

    result: list[Row] = []
    for n, raw in enumerate(parsed.rows):
        r = Row(index=n)
        try:
            r.date = _parse_date(col(raw, "date"), date_format)
            amount_text = col(raw, "amount")
            cents = parse_money(amount_text)
            direction = col(raw, "direction").lower()
            if direction in {"af", "d", "debet", "debit"}:
                cents = -abs(cents)
            elif direction in {"bij", "c", "credit"}:
                cents = abs(cents)
            if cents == 0:
                raise MoneyError("Bedrag is nul.")
            r.cents = cents
        except (ValueError, MoneyError) as exc:
            r.error = str(exc)
        description = col(raw, "description")
        payee = col(raw, "payee")
        notes = col(raw, "notes")
        if mapping.get("description") == mapping.get("payee"):
            description = payee  # ING: zelfde kolom voor beide
        if (
            mapping.get("notes")
            and mapping.get("notes", "").startswith("Omschrijving-")
            and mapping.get("description") == "Omschrijving-1"
        ):
            extra = [col(raw, "notes")]
            for extra_col in ("Omschrijving-3",):
                i = index.get(extra_col)
                if i is not None and i < len(raw) and raw[i].strip():
                    extra.append(raw[i].strip())
            notes = " ".join(x for x in extra if x)
        r.description, r.payee, r.notes = description[:255], payee[:120], notes
        if r.date and not r.error:
            r.ref = _ref(r.date, r.cents, r.description, r.payee)
        result.append(r)
    return result


def mark_duplicates(rows: list[Row], account_id: int) -> None:
    """Een rij is een duplicaat als het aantal bestaande gelijke transacties (zelfde rekening) ≥ het
    aantal keer dat die rij tot nu toe in het bestand voorkwam."""
    refs = {r.ref for r in rows if r.ref}
    if not refs:
        return
    existing: dict[str, int] = {}
    for ref, n in db.session.execute(
        select(Transaction.external_ref, func.count())
        .where(Transaction.account_id == account_id, Transaction.external_ref.in_(refs))
        .group_by(Transaction.external_ref)
    ):
        existing[ref] = n
    # Ook handmatig ingevoerde transacties (zonder ref) met dezelfde datum/bedrag/omschrijving tellen mee.
    manual = db.session.execute(
        select(
            Transaction.date,
            Transaction.type,
            Transaction.amount_cents,
            Transaction.description,
            Transaction.payee,
        ).where(Transaction.account_id == account_id, Transaction.external_ref.is_(None))
    ).all()
    for d, tx_type, cents, description, payee in manual:
        signed = cents if tx_type == "income" else -cents
        key = _ref(d, signed, description or "", payee or "")
        if key in refs:
            existing[key] = existing.get(key, 0) + 1
    seen: dict[str, int] = {}
    for r in rows:
        if not r.ref:
            continue
        seen[r.ref] = seen.get(r.ref, 0) + 1
        r.duplicate = existing.get(r.ref, 0) >= seen[r.ref]


def suggest_categories(
    rows: list[Row], default_expense: int | None, default_income: int | None
) -> None:
    """Gebruik de meest voorkomende categorie van eerdere transacties met dezelfde begunstigde."""
    payees = {_norm(r.payee) for r in rows if r.payee}
    known: dict[str, int] = {}
    if payees:
        for payee, cat, _n in db.session.execute(
            select(
                func.lower(func.trim(Transaction.payee)),
                Transaction.category_id,
                func.count().label("n"),
            )
            .where(
                Transaction.household_id == household_id(),
                Transaction.category_id.isnot(None),
                Transaction.payee != "",
            )
            .group_by(func.lower(func.trim(Transaction.payee)), Transaction.category_id)
            .order_by(func.count().asc())  # laatste (meest voorkomende) wint in de dict
        ):
            known[_norm(payee)] = cat
    for r in rows:
        r.category_id = known.get(_norm(r.payee)) or (
            default_income if r.cents > 0 else default_expense
        )


def build_preview(
    data: bytes, mapping: dict[str, str], date_format: str, account_id: int,
    default_expense: int | None, default_income: int | None,
) -> tuple[ParsedFile, Preview]:  # fmt: skip
    parsed = parse_file(data)
    rows = parse_rows(parsed, mapping, date_format)
    mark_duplicates(rows, account_id)
    suggest_categories(rows, default_expense, default_income)
    return parsed, Preview(rows)


def commit_rows(rows: list[Row], account: Account, user) -> int:
    """Maakt de transacties aan (één transactie voor alle rijen) met audit-log per transactie."""
    count = 0
    for r in rows:
        if r.error or not r.date or r.category_id is None:
            continue
        tx = Transaction(
            household_id=household_id(),
            date=r.date,
            amount_cents=abs(r.cents),
            type="income" if r.cents > 0 else "expense",
            account_id=account.id,
            category_id=r.category_id,
            description=r.description,
            payee=r.payee,
            notes=r.notes,
            external_ref=r.ref,
            created_by_user_id=getattr(user, "id", None),
            updated_by_user_id=getattr(user, "id", None),
        )
        db.session.add(tx)
        db.session.flush()
        tx_svc.write_audit("transaction", tx.id, "create", None, tx_svc.serialize(tx), user)
        count += 1
    db.session.commit()
    return count
