import pyotp

from app.extensions import db
from app.models import User
from tests.conftest import SETUP_FORM, make_app


def test_root_redirects_to_setup_when_empty(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/setup")


def test_health_is_open_and_minimal(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["db"] is True


def test_login_flow(app, configured):
    configured.post("/logout")
    resp = configured.post("/login", data={"username": "admin", "password": "fout"})
    assert resp.status_code == 401
    assert "Onjuiste gebruikersnaam of wachtwoord" in resp.get_data(as_text=True)
    resp = configured.post("/login", data={"username": "ADMIN", "password": SETUP_FORM["password"]})
    assert resp.status_code == 302
    assert configured.get("/").status_code == 200


def test_requires_login(app, configured):
    configured.post("/logout")
    resp = configured.get("/")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


def test_open_redirect_is_blocked(app, configured):
    configured.post("/logout")
    resp = configured.post(
        "/login?next=https://evil.example/",
        data={"username": "admin", "password": SETUP_FORM["password"]},
    )
    assert resp.headers["Location"] == "/"


def test_csrf_enforced(tmp_path):
    app = make_app(tmp_path, WTF_CSRF_ENABLED=True)
    client = app.test_client()
    resp = client.post("/setup", data=SETUP_FORM)
    assert resp.status_code == 400


def test_login_rate_limit(tmp_path):
    app = make_app(tmp_path, RATELIMIT_ENABLED=True)
    client = app.test_client()
    client.post("/setup", data=SETUP_FORM)
    client.post("/logout")
    codes = [
        client.post("/login", data={"username": "admin", "password": "x"}).status_code
        for _ in range(7)
    ]
    assert codes[:5] == [401] * 5
    assert codes[5] == 429
    assert "Te veel inlogpogingen" in client.post("/login", data={}).get_data(as_text=True)


def test_totp_second_factor(app, configured):
    secret = pyotp.random_base32()
    with app.app_context():
        user = db.session.execute(db.select(User)).scalar_one()
        user.totp_secret = secret
        db.session.commit()
    configured.post("/logout")
    resp = configured.post("/login", data={"username": "admin", "password": SETUP_FORM["password"]})
    assert resp.headers["Location"].endswith("/login/2fa")
    assert configured.get("/").status_code == 302  # nog niet ingelogd
    bad = configured.post("/login/2fa", data={"code": "000000"})
    assert bad.status_code == 401
    ok = configured.post("/login/2fa", data={"code": pyotp.TOTP(secret).now()})
    assert ok.status_code == 302
    assert configured.get("/").status_code == 200


def test_secret_key_required(tmp_path, monkeypatch):
    import pytest

    from app import create_app
    from app.config import ConfigError

    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ConfigError):
        create_app({"DATA_DIR": tmp_path / "d", "BACKUP_DIR": tmp_path / "b"})


def test_session_cookie_flags(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS", "true")
    app = make_app(tmp_path)
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_gzip_and_static_caching(app, configured):
    page = configured.get("/transacties/", headers={"Accept-Encoding": "gzip"})
    assert (
        page.headers.get("Content-Encoding") == "gzip" and "Accept-Encoding" in page.headers["Vary"]
    )
    import gzip

    assert b"Transacties" in gzip.decompress(page.data)
    plain = configured.get("/transacties/")
    assert "Content-Encoding" not in plain.headers and b"Transacties" in plain.data
    css = configured.get("/static/js/app.js?v=1.0.0", headers={"Accept-Encoding": "gzip"})
    assert css.headers["Content-Encoding"] == "gzip"
    assert css.headers["Cache-Control"] == "no-cache"  # 'dev'-versie cachet niet onbeperkt
    again = configured.get("/static/js/app.js?v=1.0.0", headers={"Accept-Encoding": "gzip"})
    assert again.data == css.data
    app.config["APP_VERSION"] = "1.0.0"
    cached = configured.get("/static/js/app.js?v=1.0.0")
    assert "immutable" in cached.headers["Cache-Control"]
    sw = configured.get("/sw.js")
    assert sw.status_code == 200 and sw.headers["Service-Worker-Allowed"] == "/"
    assert "javascript" in sw.mimetype
    assert configured.get("/health", headers={"Accept-Encoding": "gzip"}).status_code == 200
    assert (
        configured.get("/gegevens/backup/download", headers={"Accept-Encoding": "gzip"}).mimetype
        == "application/zip"
    )
