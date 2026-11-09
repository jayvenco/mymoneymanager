"""Back-up en restore met de SQLite online backup API (veilig tijdens gebruik)."""

import io
import json
import logging
import re
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, current_app
from sqlalchemy import inspect, text

from ..extensions import db
from ..models import AppSetting

log = logging.getLogger("mmm.backup")

DB_NAME = "mymoneymanager.db"
MANIFEST = "manifest.json"
FORMAT_VERSION = 1
FILE_RE = re.compile(r"^mymoneymanager-(\d{8})-(\d{6})-(auto|manual|safety)\.zip$")


class BackupError(Exception):
    """Back-up of restore is mislukt; het bericht is voor gebruikers bedoeld."""


# --- instellingen -------------------------------------------------------------------------------


FREQUENCIES = {"daily": "Dagelijks", "weekly": "Wekelijks", "monthly": "Maandelijks"}


@dataclass
class BackupSettings:
    enabled: bool
    frequency: str  # daily | weekly | monthly
    time: str
    keep_days: int


def get_settings(app: Flask | None = None) -> BackupSettings:
    cfg = (app or current_app).config
    try:
        keep = int(AppSetting.get("backup_keep_days", str(cfg["BACKUP_RETENTION_DAYS"])) or 0)
    except ValueError:
        keep = cfg["BACKUP_RETENTION_DAYS"]
    raw_time = AppSetting.get("backup_time", cfg["BACKUP_TIME"]) or "03:00"
    frequency = AppSetting.get("backup_frequency", cfg["BACKUP_FREQUENCY"])
    return BackupSettings(
        enabled=(AppSetting.get("backup_enabled", "1") == "1"),
        frequency=frequency if frequency in FREQUENCIES else "monthly",
        time=raw_time if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw_time) else "03:00",
        keep_days=max(1, keep),
    )


def save_settings(enabled: bool, frequency: str, time: str, keep_days: int) -> None:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time):
        raise BackupError("Gebruik een tijd als 03:00.")
    if frequency not in FREQUENCIES:
        raise BackupError("Kies dagelijks, wekelijks of maandelijks.")
    if not 1 <= keep_days <= 3650:
        raise BackupError("Bewaartermijn moet tussen 1 en 3650 dagen liggen.")
    AppSetting.put("backup_enabled", "1" if enabled else "0")
    AppSetting.put("backup_frequency", frequency)
    AppSetting.put("backup_time", time)
    AppSetting.put("backup_keep_days", str(keep_days))
    db.session.commit()


# --- back-up maken ------------------------------------------------------------------------------


def db_path(app: Flask | None = None) -> Path:
    return (app or current_app).config["DATA_DIR"] / DB_NAME


def schema_version() -> str:
    try:
        return db.session.execute(text("SELECT version_num FROM alembic_version")).scalar() or ""
    except Exception:  # noqa: BLE001 - tabel ontbreekt bij tests zonder migraties
        db.session.rollback()
        return "unversioned"


def record_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name != 'alembic_version' ORDER BY name"
        )
    ]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def _snapshot(app: Flask, target: Path) -> None:
    """Consistente kopie van de live database via de online backup API."""
    src = sqlite3.connect(db_path(app), timeout=30)
    try:
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def build_backup_bytes(app: Flask | None = None) -> bytes:
    """Zip met database-snapshot en manifest.json, volledig in het geheugen opgebouwd."""
    app = app or current_app
    version = schema_version()
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / DB_NAME
        _snapshot(app, snapshot)
        conn = sqlite3.connect(snapshot)
        try:
            counts = record_counts(conn)
        finally:
            conn.close()
        manifest = {
            "app": "MyMoneyManager",
            "format": FORMAT_VERSION,
            "app_version": app.config["APP_VERSION"],
            "schema_version": version,
            "created_at": datetime.now().replace(microsecond=0).isoformat(),
            "record_counts": counts,
        }
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(MANIFEST, json.dumps(manifest, indent=2, ensure_ascii=False))
            zf.write(snapshot, DB_NAME)
        return out.getvalue()


def create_backup(
    kind: str = "manual", app: Flask | None = None, now: datetime | None = None
) -> Path:
    app = app or current_app
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    path = app.config["BACKUP_DIR"] / f"mymoneymanager-{stamp}-{kind}.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(build_backup_bytes(app))
    tmp.replace(path)
    log.info("back-up gemaakt: %s", path.name)
    return path


# --- overzicht en retentie ----------------------------------------------------------------------


@dataclass
class BackupFile:
    name: str
    path: Path
    kind: str
    created: datetime
    size: int


def list_backups(app: Flask | None = None) -> list[BackupFile]:
    app = app or current_app
    result = []
    for path in app.config["BACKUP_DIR"].glob("mymoneymanager-*.zip"):
        m = FILE_RE.match(path.name)
        if not m:
            continue
        created = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        result.append(BackupFile(path.name, path, m.group(3), created, path.stat().st_size))
    return sorted(result, key=lambda b: b.created, reverse=True)


def backup_by_name(name: str, app: Flask | None = None) -> BackupFile | None:
    if not FILE_RE.match(name):
        return None
    return next((b for b in list_backups(app) if b.name == name), None)


def apply_retention(app: Flask | None = None, now: datetime | None = None) -> list[str]:
    """Verwijdert automatische back-ups ouder dan de bewaartermijn (de nieuwste blijft altijd staan).

    Handmatige en veiligheidskopieën worden nooit automatisch verwijderd.
    """
    app = app or current_app
    cutoff = (now or datetime.now()) - timedelta(days=get_settings(app).keep_days)
    auto = [b for b in list_backups(app) if b.kind == "auto"]  # nieuwste eerst
    removed = []
    for b in auto[1:]:
        if b.created < cutoff:
            b.path.unlink(missing_ok=True)
            removed.append(b.name)
    return removed


def auto_backup_due(now: datetime, app: Flask | None = None) -> bool:
    s = get_settings(app)
    if not s.enabled:
        return False
    hour, minute = (int(x) for x in s.time.split(":"))
    if (now.hour, now.minute) < (hour, minute):
        return False
    autos = [b for b in list_backups(app) if b.kind == "auto"]
    if not autos:
        return True
    last = autos[0].created
    if s.frequency == "daily":
        return last.date() < now.date()
    if s.frequency == "weekly":
        return now - last >= timedelta(days=7) - timedelta(hours=1)
    return (last.year, last.month) < (
        now.year,
        now.month,
    )  # maandelijks: eerste run in een nieuwe maand


def run_auto_backup(app: Flask, now: datetime | None = None) -> Path | None:
    with app.app_context():
        now = now or datetime.now()
        if not auto_backup_due(now, app):
            return None
        path = create_backup("auto", app, now)
        apply_retention(app)
        return path


def register_backup_job(app: Flask, scheduler) -> None:
    """Elke 5 minuten controleren of de dagelijkse back-up (configureerbaar tijdstip) aan de beurt is."""

    def job() -> None:
        try:
            run_auto_backup(app)
        except Exception:
            log.exception("automatische back-up mislukt")

    scheduler.add_job(
        job,
        "interval",
        minutes=5,
        id="backup",
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(seconds=45),
    )


# --- restore ------------------------------------------------------------------------------------


@dataclass
class BackupSummary:
    manifest: dict
    schema_known: bool
    newer_than_app: bool
    counts: dict[str, int]


def _known_revisions(app: Flask) -> tuple[list[str], str]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(app.root_path).parent / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    revisions = [r.revision for r in script.walk_revisions()]
    return revisions, script.get_current_head()


def inspect_backup(data: bytes, app: Flask | None = None) -> BackupSummary:
    """Valideert een back-upzip (manifest, database, integriteit, schemaversie)."""
    app = app or current_app
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BackupError("Dit is geen geldig zip-bestand.") from exc
    names = set(zf.namelist())
    if MANIFEST not in names or DB_NAME not in names:
        raise BackupError(
            "Dit lijkt geen MyMoneyManager-back-up: manifest.json of database ontbreekt."
        )
    try:
        manifest = json.loads(zf.read(MANIFEST))
    except ValueError as exc:
        raise BackupError("manifest.json is onleesbaar.") from exc
    if manifest.get("app") != "MyMoneyManager" or manifest.get("format") != FORMAT_VERSION:
        raise BackupError("Onbekend back-upformaat.")
    revisions, head = _known_revisions(app)
    version = manifest.get("schema_version")
    known = version in revisions or version == "unversioned"
    if not known:
        raise BackupError(
            f"Deze back-up komt van een nieuwere versie van de app (schema {version}). "
            "Werk eerst de app bij en probeer het opnieuw."
        )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / DB_NAME
        path.write_bytes(zf.read(DB_NAME))
        try:
            conn = sqlite3.connect(path)
            try:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("De database in de back-up is beschadigd.")
                counts = record_counts(conn)
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise BackupError("De database in de back-up is onleesbaar.") from exc
    return BackupSummary(manifest, known, version != head, counts)


def restore_backup(data: bytes, app: Flask | None = None) -> BackupSummary:
    """Herstelt een back-up in de live database (na een veiligheidskopie) en migreert indien nodig."""
    from flask_migrate import upgrade

    app = app or current_app
    summary = inspect_backup(data, app)
    safety = create_backup("safety", app)  # altijd eerst een veiligheidskopie van de huidige stand
    log.warning("restore gestart; veiligheidskopie: %s", safety.name)
    db.session.remove()
    with tempfile.TemporaryDirectory() as tmp:
        src_path = Path(tmp) / DB_NAME
        src_path.write_bytes(zipfile.ZipFile(io.BytesIO(data)).read(DB_NAME))
        src = sqlite3.connect(src_path)
        try:
            dst = sqlite3.connect(db_path(app), timeout=60)
            try:
                src.backup(dst)  # overschrijft de live database pagina voor pagina
            finally:
                dst.close()
        finally:
            src.close()
    db.engine.dispose()
    _revisions, head = _known_revisions(app)
    if schema_version() not in {"unversioned", head}:
        upgrade()  # alleen nodig wanneer de back-up van een ouder schema komt
    app.extensions.pop("mmm_setup_done", None)
    return summary


def table_names() -> list[str]:
    return sorted(inspect(db.engine).get_table_names())
