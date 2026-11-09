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
        '"opening_date":"2025-01-01","visibility":"shared"}]'
    ),
}


@pytest.fixture()
def configured(client):
    """Client waarop de setup-wizard al is doorlopen (ingelogd als admin)."""
    resp = client.post("/setup", data=SETUP_FORM)
    assert resp.status_code == 302
    return client
