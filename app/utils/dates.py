"""Datums in dd-mm-jjjj; week begint op maandag."""

import re
from datetime import date, datetime

_NL = re.compile(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})$")
MONTHS_NL = [
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
]
MONTHS_SHORT_NL = [
    "jan",
    "feb",
    "mrt",
    "apr",
    "mei",
    "jun",
    "jul",
    "aug",
    "sep",
    "okt",
    "nov",
    "dec",
]


def parse_date(value: str) -> date:
    """Accepteert dd-mm-jjjj en jjjj-mm-dd."""
    text = (value or "").strip()
    if not text:
        raise ValueError("Vul een datum in.")
    m = _NL.match(text)
    try:
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("Ongeldige datum (gebruik dd-mm-jjjj).") from exc


def format_date(value: date | datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%d-%m-%Y")


def month_label(ym: str) -> str:
    year, month = ym.split("-")
    return f"{MONTHS_NL[int(month) - 1]} {year}"
