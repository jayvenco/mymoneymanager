import io
import json
import sqlite3
import zipfile
from datetime import datetime, timedelta

import pytest
from flask_migrate import upgrade

from app import create_app
from app.extensions import db
from app.models import Transaction, User
from app.services import backup as svc
from tests.conftest import SETUP_FORM, tx_form


@pytest.fixture()
def mapp(tmp_path):
    """App met echte migraties (zoals in productie) in plaats van create_all."""
    app = create_app(
        {
            "SECRET_KEY": "t",
            "DATA_DIR": tmp_path / "data",
            "BACKUP_DIR": tmp_path / "backups",
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "RATELIMIT_ENABLED": False,
        }
    )
    with app.app_context():
        upgrade()
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture()
def mclient(mapp):
    client = mapp.test_client()
    assert client.post("/setup", data=SETUP_FORM).status_code == 302
    return client


def seed_tx(client, n=3):
    with client.application.app_context():
        from app.models import Account, Category

        acc = db.session.query(Account).filter_by(name="Betaal Admin").one().id
        cat = db.session.query(Category).filter_by(name="Boodschappen").one().id
    ids = {"acc": {"Betaal Admin": acc}, "cat": {"Boodschappen": cat}}
    for i in range(n):
        assert (
            client.post(
                "/transacties/", data=tx_form(ids, description=f"tx{i}", amount=f"{i + 1},00")
            ).status_code
            == 204
        )


def test_backup_zip_contains_snapshot_and_manifest(mapp, mclient, tmp_path):
    seed_tx(mclient)
    with mapp.app_context():
        data = svc.build_backup_bytes()
    zf = zipfile.ZipFile(io.BytesIO(data))
    assert set(zf.namelist()) == {"manifest.json", "mymoneymanager.db"}
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["app"] == "MyMoneyManager" and manifest["schema_version"]
    assert manifest["record_counts"]["transaction"] == 3
    assert manifest["record_counts"]["account"] == 4 and manifest["record_counts"]["member"] == 2
    assert manifest["created_at"] and manifest["app_version"]
    (tmp_path / "snapshot.db").write_bytes(zf.read("mymoneymanager.db"))
    conn = sqlite3.connect(tmp_path / "snapshot.db")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute('SELECT COUNT(*) FROM "transaction"').fetchone()[0] == 3


def test_backup_while_writing_is_consistent(mapp, mclient):
    """Een back-up tijdens schrijfactiviteit moet een geldige, volledige database opleveren."""
    import threading

    seed_tx(mclient, 2)
    stop = threading.Event()

    def writer():
        client = mapp.test_client()
        client.post("/login", data={"username": "admin", "password": SETUP_FORM["password"]})
        with mapp.app_context():
            from app.models import Account, Category

            ids = {
                "acc": {"Betaal Admin": db.session.query(Account).first().id},
                "cat": {
                    "Boodschappen": db.session.query(Category)
                    .filter_by(name="Boodschappen")
                    .one()
                    .id
                },
            }
        while not stop.is_set():
            client.post("/transacties/", data=tx_form(ids))

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(5):
            with mapp.app_context():
                summary = svc.inspect_backup(svc.build_backup_bytes())
            assert summary.counts["transaction"] >= 2
    finally:
        stop.set()
        thread.join()


def test_restore_round_trip_on_fresh_container_has_zero_data_loss(tmp_path, mapp, mclient):
    seed_tx(mclient, 5)
    with mapp.app_context():
        data = svc.build_backup_bytes()
        before = {t.description: (t.amount_cents, t.date) for t in db.session.query(Transaction)}
    # 'verse container': nieuwe lege app + setup, daarna restore
    fresh = create_app(
        {
            "SECRET_KEY": "t",
            "DATA_DIR": tmp_path / "fresh-data",
            "BACKUP_DIR": tmp_path / "fresh-bak",
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "RATELIMIT_ENABLED": False,
        }
    )
    with fresh.app_context():
        upgrade()
        summary = svc.restore_backup(data)
        assert summary.counts["transaction"] == 5
        after = {t.description: (t.amount_cents, t.date) for t in db.session.query(Transaction)}
        assert after == before
        assert (
            db.session.query(User)
            .filter_by(username="admin")
            .one()
            .password_hash.startswith("$argon2")
        )
        assert [b.kind for b in svc.list_backups()] == ["safety"]
        db.session.remove()
        db.engine.dispose()


def test_restore_replaces_changes_made_after_backup(mapp, mclient):
    seed_tx(mclient, 2)
    with mapp.app_context():
        data = svc.build_backup_bytes()
    seed_tx(mclient, 4)
    with mapp.app_context():
        assert db.session.query(Transaction).count() == 6
        svc.restore_backup(data)
        assert db.session.query(Transaction).count() == 2
        safety = [b for b in svc.list_backups() if b.kind == "safety"]
        assert len(safety) == 1
        # veiligheidskopie bevat de 6 transacties van vóór het herstel
        summary = svc.inspect_backup(safety[0].path.read_bytes())
        assert summary.counts["transaction"] == 6


def test_inspect_rejects_bad_files(mapp, mclient):
    with mapp.app_context():
        with pytest.raises(svc.BackupError, match="geen geldig zip"):
            svc.inspect_backup(b"dit is geen zip")
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("iets.txt", "x")
        with pytest.raises(svc.BackupError, match="manifest"):
            svc.inspect_backup(bad.getvalue())
        good = zipfile.ZipFile(io.BytesIO(svc.build_backup_bytes()))
        newer = io.BytesIO()
        with zipfile.ZipFile(newer, "w") as zf:
            manifest = json.loads(good.read("manifest.json"))
            manifest["schema_version"] = "ffffffffffff"
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("mymoneymanager.db", good.read("mymoneymanager.db"))
        with pytest.raises(svc.BackupError, match="nieuwere versie"):
            svc.inspect_backup(newer.getvalue())
        corrupt = io.BytesIO()
        with zipfile.ZipFile(corrupt, "w") as zf:
            zf.writestr("manifest.json", good.read("manifest.json"))
            zf.writestr("mymoneymanager.db", b"SQLite format 3\x00" + b"\x00" * 200)
        with pytest.raises(svc.BackupError):
            svc.inspect_backup(corrupt.getvalue())


def test_retention_removes_only_old_auto_backups(mapp, mclient):
    from pathlib import Path

    with mapp.app_context():
        folder = mapp.config["BACKUP_DIR"]
        now = datetime(2026, 11, 9, 3, 0)
        for days in (0, 30, 359, 361, 500):
            stamp = (now - timedelta(days=days)).strftime("%Y%m%d-%H%M%S")
            (folder / f"mymoneymanager-{stamp}-auto.zip").write_bytes(b"x")
        (folder / "mymoneymanager-20240101-030000-manual.zip").write_bytes(b"x")
        (folder / "mymoneymanager-20240101-030000-safety.zip").write_bytes(b"x")
        (folder / "andere-bestand.zip").write_bytes(b"x")
        assert svc.get_settings().keep_days == 360  # standaard
        removed = svc.apply_retention(now=now)
        assert len(removed) == 2  # 361 en 500 dagen oud
        names = {p.name for p in Path(folder).iterdir()}
        assert (
            "mymoneymanager-20240101-030000-manual.zip" in names and "andere-bestand.zip" in names
        )
        assert sum(n.endswith("-auto.zip") for n in names) == 3
        svc.save_settings(True, "monthly", "03:00", 1)
        svc.apply_retention(now=now)  # de nieuwste auto-backup blijft altijd staan
        assert sum(n.endswith("-auto.zip") for n in {p.name for p in Path(folder).iterdir()}) == 1


def test_default_is_monthly_with_360_days(mapp, mclient):
    with mapp.app_context():
        s = svc.get_settings()
        assert (s.frequency, s.keep_days, s.enabled) == ("monthly", 360, True)


def test_auto_backup_frequencies(mapp, mclient):
    with mapp.app_context():
        svc.save_settings(True, "monthly", "03:00", 360)
        assert svc.run_auto_backup(mapp, datetime(2026, 5, 5, 2, 59)) is None  # nog te vroeg
        assert svc.run_auto_backup(mapp, datetime(2026, 5, 5, 3, 1)) is not None  # eerste keer
        assert svc.auto_backup_due(datetime(2026, 5, 31, 12, 0)) is False  # zelfde maand
        assert svc.auto_backup_due(datetime(2026, 6, 1, 3, 1)) is True  # nieuwe maand
        svc.save_settings(True, "weekly", "03:00", 360)
        assert svc.auto_backup_due(datetime(2026, 5, 11, 3, 1)) is False
        assert svc.auto_backup_due(datetime(2026, 5, 12, 3, 1)) is True
        svc.save_settings(True, "daily", "03:00", 360)
        assert svc.auto_backup_due(datetime(2026, 5, 5, 23, 0)) is False
        assert svc.auto_backup_due(datetime(2026, 5, 6, 3, 1)) is True
        svc.save_settings(False, "daily", "03:00", 360)
        assert svc.auto_backup_due(datetime(2030, 1, 1, 12, 0)) is False
        for bad in (("x", "03:00", 360), ("daily", "25:99", 360), ("daily", "03:00", 0)):
            with pytest.raises(svc.BackupError):
                svc.save_settings(True, *bad)


def test_settings_form_saves(mapp, mclient):
    resp = mclient.post(
        "/gegevens/backup/instellingen",
        data={"enabled": "1", "frequency": "weekly", "time": "04:30", "keep_days": "90"},
    )
    assert resp.status_code == 302
    with mapp.app_context():
        s = svc.get_settings()
        assert (s.frequency, s.time, s.keep_days) == ("weekly", "04:30", 90)
    page = mclient.get("/gegevens/").get_data(as_text=True)
    assert 'value="90"' in page and "Bewaren (dagen)" in page


def test_backup_routes_admin_only(mapp, mclient):
    seed_tx(mclient, 1)
    page = mclient.get("/gegevens/").get_data(as_text=True)
    assert "Download back-up" in page and "Automatische back-ups" in page
    resp = mclient.get("/gegevens/backup/download")
    assert (
        resp.mimetype == "application/zip" and "attachment" in resp.headers["Content-Disposition"]
    )
    mclient.post("/gegevens/backup/nu")
    with mapp.app_context():
        name = svc.list_backups()[0].name
    assert mclient.get(f"/gegevens/backup/{name}").status_code == 200
    assert mclient.get("/gegevens/backup/../../etc/passwd").status_code == 404
    assert mclient.get("/gegevens/backup/evil.zip").status_code == 404
    # restore-flow via upload
    summary = mclient.post(
        "/gegevens/restore/controle",
        data={"backup": (io.BytesIO(resp.data), "b.zip")},
        content_type="multipart/form-data",
    )
    html = summary.get_data(as_text=True)
    assert "vervangen door de inhoud" in html and "transaction" in html
    token = html.split('name="token" value="')[1].split('"')[0]
    refused = mclient.post("/gegevens/restore/uitvoeren", data={"token": token})
    assert refused.status_code == 302  # zonder bevestiging gebeurt er niets
    done = mclient.post("/gegevens/restore/uitvoeren", data={"token": token, "confirm": "1"})
    assert done.status_code == 302 and "/login" in done.headers["Location"]
    bad = mclient.post(
        "/gegevens/restore/controle",
        data={"backup": (io.BytesIO(b"nope"), "b.zip")},
        content_type="multipart/form-data",
    )
    assert bad.status_code == 302


def test_non_admin_cannot_touch_backups_or_export(mapp, mclient):
    from tests.conftest import PARTNER_PASSWORD

    with mapp.app_context():
        from app.models import Member

        partner = db.session.query(Member).filter_by(name="Partner Test").one().id
        user = User(username="partner", member_id=partner)
        user.set_password(PARTNER_PASSWORD)
        db.session.add(user)
        db.session.commit()
    client = mapp.test_client()
    client.post("/login", data={"username": "partner", "password": PARTNER_PASSWORD})
    for url in ("/gegevens/backup/download", "/gegevens/export/json", "/gegevens/export/csv"):
        assert client.get(url).status_code == 403
    for url in ("/gegevens/backup/nu", "/gegevens/restore/controle", "/gegevens/restore/uitvoeren"):
        assert client.post(url).status_code == 403
    assert client.get("/instellingen/huishouden/").status_code == 403
    assert "Download back-up" not in client.get("/gegevens/").get_data(as_text=True)


def test_full_export_json_and_csv_exclude_secrets(mapp, mclient):
    seed_tx(mclient, 2)
    data = mclient.get("/gegevens/export/json").get_json()
    assert set(data["tables"]) >= {"transaction", "account", "category", "member", "user", "budget"}
    assert len(data["tables"]["transaction"]) == 2
    assert "password_hash" not in json.dumps(data) and "$argon2" not in json.dumps(data)
    z = zipfile.ZipFile(io.BytesIO(mclient.get("/gegevens/export/csv").data))
    assert "transaction.csv" in z.namelist() and "account.csv" in z.namelist()
    assert "$argon2" not in "".join(z.read(n).decode("utf-8") for n in z.namelist())
    assert "tx0" in z.read("transaction.csv").decode("utf-8")
