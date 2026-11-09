"""Financiële maanden en rapportageperiodes.

Een financiële maand met startdag D (>1) loopt van D van de vorige kalendermaand
t/m D-1 van de benoemde maand. Voorbeeld met D=25: "november" = 25 okt t/m 24 nov.
Bij D=1 is dit gewoon de kalendermaand.
"""

import calendar
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Period:
    start: date
    end: date  # inclusief
    label: str = ""

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def ym_of(d: date, start_day: int) -> str:
    """Naam (YYYY-MM) van de financiële maand waarin `d` valt."""
    year, month = d.year, d.month
    if start_day > 1 and d.day >= start_day:
        year, month = _add_months(year, month, 1)
    return f"{year:04d}-{month:02d}"


def financial_month(ym: str, start_day: int) -> Period:
    year, month = (int(p) for p in ym.split("-"))
    if start_day <= 1:
        last = calendar.monthrange(year, month)[1]
        return Period(date(year, month, 1), date(year, month, last), ym)
    py, pm = _add_months(year, month, -1)
    return Period(date(py, pm, start_day), date(year, month, start_day) - timedelta(days=1), ym)


def financial_month_for(d: date, start_day: int) -> Period:
    return financial_month(ym_of(d, start_day), start_day)


def shift_ym(ym: str, delta: int) -> str:
    year, month = (int(p) for p in ym.split("-"))
    ny, nm = _add_months(year, month, delta)
    return f"{ny:04d}-{nm:02d}"


def months_between(first_ym: str, last_ym: str) -> list[str]:
    result, current = [], first_ym
    while current <= last_ym:
        result.append(current)
        current = shift_ym(current, 1)
    return result


def add_months_clamped(d: date, months: int) -> date:
    year, month = _add_months(d.year, d.month, months)
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))
