from pathlib import Path

from app.extensions import db
from app.models import User
from app.themes import THEME_KEYS, THEMES

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "src" / "input.css"


def test_five_themes_including_light_white():
    assert len(THEMES) == 5
    assert {t.key for t in THEMES} == {"light", "dark", "ocean", "warm", "midnight"}
    assert THEMES[0].key == "light" and THEMES[0].label == "Wit" and THEMES[0].page == "#ffffff"
    assert {t.kind for t in THEMES} == {"light", "dark"}
    assert {t.key for t in THEMES} | {"system"} == THEME_KEYS


def test_every_non_default_theme_has_a_palette_in_css():
    css = CSS.read_text()
    for key in ("light", "ocean", "warm", "midnight"):
        assert f'html[data-theme="{key}"]' in css, key
    assert "--c-brand-600" in css and "--page" in css


def test_theme_route_accepts_all_and_rejects_unknown(app, configured):
    for key in sorted(THEME_KEYS):
        assert configured.post("/settings/theme", data={"theme": key}).status_code == 204
        with app.app_context():
            assert db.session.query(User).filter_by(username="admin").one().theme_preference == key
    assert configured.post("/settings/theme", data={"theme": "neon"}).status_code == 400
    assert configured.post("/instellingen/thema", data={"theme": "<script>"}).status_code == 400


def test_theme_is_rendered_per_user_and_listed(app, configured):
    configured.post("/settings/theme", data={"theme": "midnight"})
    html = configured.get("/transacties/").get_data(as_text=True)
    assert 'data-theme="midnight"' in html
    for label in ("Wit", "Donker", "Oceaan", "Warm", "Middernacht", "Systeem"):
        assert label in html  # themamenu in de header
    profile = configured.get("/instellingen/profiel").get_data(as_text=True)
    assert profile.count('name="theme"') == 6
    assert 'value="midnight" class="peer sr-only" checked' in profile
    # opslaan via profiel
    assert configured.post("/instellingen/profiel", data={"theme": "warm"}).status_code == 302
    assert 'data-theme="warm"' in configured.get("/").get_data(as_text=True)
