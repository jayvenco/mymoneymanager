"""Standaardcategorieën."""

from ..extensions import db
from ..models import Category

# (naam, kleur, icoon, subcategorieën)
EXPENSE_TREE = [
    (
        "Vaste lasten",
        "#6366f1",
        "home",
        [
            "Huur/Hypotheek",
            "Energie",
            "Water",
            "Verzekeringen",
            "Internet & telefoon",
            "Abonnementen",
        ],
    ),
    ("Eten", "#f59e0b", "utensils", ["Boodschappen", "Uit eten", "Afhalen"]),
    ("Kleding", "#ec4899", "shirt", []),
    ("Entertainment", "#8b5cf6", "ticket", ["Uitjes", "Hobby's", "Streaming", "Games"]),
    ("Investeren", "#14b8a6", "trending-up", []),
    ("Losse grote uitgaven", "#ef4444", "shopping-bag", []),
]
INCOME_TREE = [
    ("Salaris", "#10b981", "banknote", []),
    ("Overige inkomsten", "#22c55e", "plus-circle", []),
]
_SHADES = ["#818cf8", "#a5b4fc", "#fbbf24", "#fcd34d", "#c4b5fd", "#5eead4", "#fda4af", "#93c5fd"]


def seed_default_categories(household_id: int) -> None:
    order = 0
    for kind, tree in (("expense", EXPENSE_TREE), ("income", INCOME_TREE)):
        for name, color, icon, subs in tree:
            parent = Category(
                household_id=household_id,
                name=name,
                kind=kind,
                color=color,
                icon=icon,
                sort_order=order,
                is_one_off=(name == "Losse grote uitgaven"),
            )
            db.session.add(parent)
            db.session.flush()
            order += 1
            for i, sub in enumerate(subs):
                db.session.add(
                    Category(
                        household_id=household_id,
                        name=sub,
                        kind=kind,
                        parent_id=parent.id,
                        color=color,
                        icon=icon,
                        sort_order=i,
                    )
                )
    # Systeemcategorieën voor saldocorrecties (standaard uitgesloten van rapportages).
    for kind in ("expense", "income"):
        db.session.add(
            Category(
                household_id=household_id,
                name="Correctie",
                kind=kind,
                color="#94a3b8",
                icon="scale",
                sort_order=900,
                is_system=True,
                exclude_from_reports=True,
            )
        )
    db.session.flush()
