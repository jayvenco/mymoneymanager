"""Generiek rapportmodel: dezelfde data voor HTML, CSV, XLSX en PDF."""

from dataclasses import dataclass, field
from typing import Any

from ..utils.money import format_eur, format_number


@dataclass
class Cell:
    """Eén tabelcel. `kind`: text | money | pct | int | delta."""

    v: Any = ""
    kind: str = "text"
    color: str | None = None  # hex-kleur voor tekst (bijv. lidkleur)
    bold: bool = False
    indent: int = 0
    href: str | None = None
    heat: str | None = None  # ok | warn | over (budget-heatmap)
    note: str | None = None  # extra regel onder de waarde (bijv. "van € 100,00")

    def text(self) -> str:
        """Weergave voor CSV/PDF (Nederlandse notatie)."""
        if self.v is None or self.v == "":
            return ""
        if self.kind == "money":
            return format_eur(int(self.v))
        if self.kind == "delta":
            return format_eur(int(self.v), sign=True)
        if self.kind == "pct":
            return f"{float(self.v):.1f}%".replace(".", ",")
        if self.kind == "int":
            return str(int(self.v))
        return str(self.v)

    def csv(self) -> str:
        if self.kind in {"money", "delta"} and self.v not in (None, ""):
            return format_number(int(self.v))
        return self.text()


def T(v: Any = "", **kw) -> Cell:  # noqa: N802 - korte constructors maken de builders leesbaar
    return Cell(v, "text", **kw)


def M(cents: int | None, **kw) -> Cell:  # noqa: N802
    return Cell(cents if cents is not None else "", "money", **kw)


def D(cents: int | None, **kw) -> Cell:  # noqa: N802
    return Cell(cents if cents is not None else "", "delta", **kw)


def P(pct: float | None, **kw) -> Cell:  # noqa: N802
    return Cell(pct if pct is not None else "", "pct", **kw)


def N(n: int | None, **kw) -> Cell:  # noqa: N802
    return Cell(n if n is not None else "", "int", **kw)


@dataclass
class Section:
    key: str
    title: str
    columns: list[str] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)
    total: list[Cell] | None = None
    chart: dict | None = None  # {"type","labels","datasets","stacked","currency","height"}
    note: str | None = None
    numeric_from: int = 1  # kolommen vanaf deze index worden rechts uitgelijnd
    heatmap: bool = False


@dataclass
class Report:
    key: str
    title: str
    sections: list[Section] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
    insights: list[str] = field(default_factory=list)


def series(label: str, values_cents: list[int], color: str, **kw) -> dict:
    """Chart.js-dataset in euro's (afgerond op centen)."""
    return {
        "label": label,
        "data": [round(v / 100, 2) for v in values_cents],
        "backgroundColor": color,
        "borderColor": color,
        **kw,
    }
