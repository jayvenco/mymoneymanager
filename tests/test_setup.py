from app.extensions import db
from app.models import Account, Category, Household, Member, User
from tests.conftest import SETUP_FORM


def test_setup_creates_household(app, configured):
    with app.app_context():
        household = db.session.execute(db.select(Household)).scalar_one()
        assert household.name == "Test Huishouden"
        assert household.financial_month_start_day == 25
        members = db.session.execute(db.select(Member).order_by(Member.sort_order)).scalars().all()
        assert [m.name for m in members] == ["Jayvenco Admin", "Partner Test"]
        assert members[0].avatar_initials == "JA"
        admin = db.session.execute(db.select(User)).scalar_one()
        assert admin.is_admin and admin.member_id == members[0].id
        assert admin.password_hash.startswith("$argon2")

        accounts = {a.name: a for a in db.session.execute(db.select(Account)).scalars()}
        assert accounts["Betaal Admin"].opening_balance_cents == 100050
        assert accounts["Betaal Admin"].owner_member_id == members[0].id
        assert accounts["Spaar Partner"].visibility == "private"
        assert accounts["Spaar Partner"].owner_member_id == members[1].id
        assert accounts["Gezamenlijk"].owner_member_id is None


def test_default_categories_seeded(app, configured):
    with app.app_context():
        top = {
            c.name
            for c in db.session.execute(
                db.select(Category).where(Category.parent_id.is_(None))
            ).scalars()
        }
        assert {"Vaste lasten", "Eten", "Kleding", "Entertainment", "Investeren"} <= top
        assert {"Losse grote uitgaven", "Salaris", "Overige inkomsten", "Correctie"} <= top
        vaste = db.session.execute(
            db.select(Category).where(Category.name == "Vaste lasten")
        ).scalar_one()
        assert {c.name for c in vaste.children} == {
            "Huur/Hypotheek",
            "Energie",
            "Water",
            "Verzekeringen",
            "Internet & telefoon",
            "Abonnementen",
        }


def test_setup_is_permanently_disabled(configured):
    assert configured.get("/setup").status_code == 404
    assert configured.post("/setup", data=SETUP_FORM).status_code == 404


def test_setup_validation_errors(client):
    bad = dict(SETUP_FORM, password="kort", password2="kort", partner_name="")
    resp = client.post("/setup", data=bad)
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "minimaal 10 tekens" in html
    assert "Vul de naam van je partner in" in html


def test_setup_accounts_optional(client):
    resp = client.post("/setup", data=dict(SETUP_FORM, accounts="[]"))
    assert resp.status_code == 302
