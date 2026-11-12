"""Beschikbare thema's. `kind` bepaalt of de `dark`-klasse aan staat; kleuren staan in input.css."""

from typing import NamedTuple


class Theme(NamedTuple):
    key: str
    label: str
    kind: str  # light | dark
    page: str  # voorbeeldkleuren voor de keuzekaart
    card: str
    accent: str


THEMES: list[Theme] = [
    Theme("light", "Wit", "light", "#ffffff", "#f4f5f7", "#0f766e"),
    Theme("dark", "Donker", "dark", "#020617", "#0f172a", "#14b8a6"),
    Theme("ocean", "Oceaan", "light", "#f1f7fd", "#ffffff", "#2563eb"),
    Theme("warm", "Warm", "light", "#fbf7f1", "#fffefc", "#ea580c"),
    Theme("midnight", "Middernacht", "dark", "#090a1c", "#11132d", "#8b5cf6"),
]
THEME_KEYS = {t.key for t in THEMES} | {"system"}
