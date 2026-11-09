import pytest

from app import create_app
from app.extensions import db


def make_app(tmp_path, **overrides):
    config = {
        "SECRET_KEY": "test-secret",
        "DATA_DIR": tmp_path / "data",
        "BACKUP_DIR": tmp_path / "backups",
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "RATELIMIT_ENABLED": False,
        "SCHEDULER_ENABLED": False,
    }
    config.update(overrides)
    app = create_app(config)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture()
def app(tmp_path):
    app = make_app(tmp_path)
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture()
def client(app):
    return app.test_client()


SETUP_FORM = {
    "admin_name": "Jayvenco Admin",
    "admin_color": "#4f46e5",
    "username": "admin",
    "password": "correct-horse-battery",
    "password2": "correct-horse-battery",
    "household_name": "Test Huishouden",
    "partner_name": "Partner Test",
    "partner_color": "#ec4899",
    "start_day": "25",
    "accounts": (
        '[{"name":"Betaal Admin","owner":"admin","type":"checking","opening_balance":"1.000,50",'
        '"opening_date":"2025-01-01","visibility":"shared"},'
        '{"name":"Spaar Partner","owner":"partner","type":"savings","opening_balance":"250",'
        '"opening_date":"2025-01-01","visibility":"private"},'
        '{"name":"Gezamenlijk","owner":"joint","type":"checking","opening_balance":"0",'
        '"opening_date":"2025-01-01","visibility":"shared"},'
        '{"name":"Prive Admin","owner":"admin","type":"checking","opening_balance":"500",'
        '"opening_date":"2025-01-01","visibility":"private"}]'
    ),
}


@pytest.fixture()
def configured(client):
    """Client waarop de setup-wizard al is doorlopen (ingelogd als admin)."""
    resp = client.post("/setup", data=SETUP_FORM)
    assert resp.status_code == 302
    return client


PARTNER_PASSWORD = "partner-password-123"


@pytest.fixture()
def ids(app, configured):
    """Id's van de aangemaakte leden, rekeningen en categorieën."""
    from app.models import Account, Category, Member

    with app.app_context():
        members = {m.name: m.id for m in db.session.execute(db.select(Member)).scalars()}
        accounts = {a.name: a.id for a in db.session.execute(db.select(Account)).scalars()}
        cats = {c.name: c.id for c in db.session.execute(db.select(Category)).scalars()}
    return {
        "admin_member": members["Jayvenco Admin"],
        "partner_member": members["Partner Test"],
        "acc": accounts,
        "cat": cats,
    }


@pytest.fixture()
def partner_client(app, ids):
    """Tweede login (geen beheerder) gekoppeld aan het partner-lid."""
    from app.models import User

    with app.app_context():
        user = User(username="partner", member_id=ids["partner_member"], is_admin=False)
        user.set_password(PARTNER_PASSWORD)
        db.session.add(user)
        db.session.commit()
    client = app.test_client()
    resp = client.post("/login", data={"username": "partner", "password": PARTNER_PASSWORD})
    assert resp.status_code == 302
    return client


def tx_form(ids, **over):
    data = {
        "type": "expense",
        "amount": "12,50",
        "date": "15-03-2025",
        "account_id": ids["acc"]["Betaal Admin"],
        "category_id": ids["cat"]["Boodschappen"],
        "description": "Albert Heijn",
        "payee": "AH",
        "tags": "",
    }
    data.update(over)
    return data
