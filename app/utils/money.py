"""Geldbedragen: altijd integer cents, Nederlandse notatie."""

import re
from decimal import Decimal, InvalidOperation


class MoneyError(ValueError):
    """Ongeldig bedrag."""


_ALLOWED = re.compile(r"[0-9.,]+")


def parse_money(value: str) -> int:
    """Zet '1234,56', '1.234,56', '1234.56' of '€ 12' om naar cents (kan negatief zijn)."""
    if value is None:
        raise MoneyError("Vul een bedrag in.")
    s = str(value).strip().replace("€", "").replace(" ", "").replace(" ", "")
    if not s:
        raise MoneyError("Vul een bedrag in.")
    negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-+()")
    if not _ALLOWED.fullmatch(s):
        raise MoneyError("Ongeldig bedrag.")

    if "," in s and "." in s:
        decimal_sep = "," if s.rfind(",") > s.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        s = s.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in s:
        s = s.replace(",", "") if s.count(",") > 1 else s.replace(",", ".")
    elif "." in s:
        parts = s.split(".")
        if len(parts) > 2:
            s = "".join(parts)
        elif len(parts[1]) == 3 and parts[0] not in {"", "0"}:
            # Nederlandse duizendtalnotatie: 1.234 = 1234
            s = "".join(parts)

    try:
        amount = Decimal(s)
    except InvalidOperation as exc:
        raise MoneyError("Ongeldig bedrag.") from exc
    if amount != amount.quantize(Decimal("0.01")):
        raise MoneyError("Gebruik maximaal twee decimalen.")
    cents = int(amount * 100)
    return -cents if negative else cents


def format_eur(cents: int | None, *, sign: bool = False) -> str:
    """12345 -> '€ 123,45'; negatief -> '-€ 123,45'."""
    if cents is None:
        return "€ 0,00"
    negative = cents < 0
    euros, rest = divmod(abs(int(cents)), 100)
    body = f"€ {euros:,}".replace(",", ".") + f",{rest:02d}"
    if negative:
        return "-" + body
    return "+" + body if sign and cents > 0 else body


def format_number(cents: int) -> str:
    """Zonder valutateken, voor exports."""
    negative = cents < 0
    euros, rest = divmod(abs(int(cents)), 100)
    text = f"{euros},{rest:02d}"
    return "-" + text if negative else text
