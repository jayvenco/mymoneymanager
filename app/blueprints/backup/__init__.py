"""Gegevensbeheer: back-up, restore, volledige export en CSV-import."""

import csv
import io
import json
import re
import secrets
import zipfile
from datetime import date, datetime

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required, logout_user
from sqlalchemy import text

from ...extensions import db
from ...models import Account, Category
from ...services import access, importer
from ...services import backup as svc
from ...services.household import household_id
from ...services.transactions import _int
from .. import admin_required
from ..accounts import can_manage

bp = Blueprint("backup", __name__, url_prefix="/gegevens")

SENSITIVE_COLUMNS = {"password_hash", "totp_secret"}


def _fmt_size(size: int) -> str:
    return f"{size / 1024:.0f} kB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


# --- hub ----------------------------------------------------------------------------------------


@bp.get("/")
@login_required
def index():
    ctx = {"can_import": True}
    if current_user.is_admin:
        ctx.update(settings=svc.get_settings(), backups=svc.list_backups(), fmt_size=_fmt_size)
    return render_template("backup/index.html", **ctx)


# --- back-up ------------------------------------------------------------------------------------


@bp.get("/backup/download")
@admin_required
def download_now():
    data = svc.build_backup_bytes()
    name = f"mymoneymanager-{datetime.now():%Y%m%d-%H%M%S}-manual.zip"
    return Response(
        data,
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@bp.post("/backup/nu")
@admin_required
def create_now():
    path = svc.create_backup("manual")
    flash(f"Back-up gemaakt: {path.name}", "success")
    return redirect(url_for("backup.index"))


@bp.post("/backup/instellingen")
@admin_required
def save_settings():
    try:
        svc.save_settings(
            enabled=request.form.get("enabled") == "1",
            time=request.form.get("time", ""),
            daily=int(request.form.get("daily", "0") or 0),
            weekly=int(request.form.get("weekly", "0") or 0),
            monthly=int(request.form.get("monthly", "0") or 0),
        )
        svc.apply_retention()
        flash("Back-upinstellingen opgeslagen.", "success")
    except (svc.BackupError, ValueError) as exc:
        flash(str(exc) if isinstance(exc, svc.BackupError) else "Vul geldige getallen in.", "error")
    return redirect(url_for("backup.index"))


@bp.get("/backup/<name>")
@admin_required
def download_stored(name: str):
    backup = svc.backup_by_name(name)
    if backup is None:
        abort(404)
    return send_file(
        backup.path, as_attachment=True, download_name=backup.name, mimetype="application/zip"
    )


@bp.post("/backup/<name>/verwijder")
@admin_required
def delete_stored(name: str):
    backup = svc.backup_by_name(name)
    if backup is None:
        abort(404)
    backup.path.unlink(missing_ok=True)
    flash("Back-up verwijderd.", "success")
    return redirect(url_for("backup.index"))


# --- restore ------------------------------------------------------------------------------------


def _restore_path(token: str):
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        abort(400)
    return importer.import_dir() / f"restore-{token}.zip"


@bp.post("/restore/controle")
@admin_required
def restore_check():
    upload = request.files.get("backup")
    stored = request.form.get("stored")
    if stored:
        backup = svc.backup_by_name(stored)
        if backup is None:
            abort(404)
        data = backup.path.read_bytes()
    elif upload and upload.filename:
        data = upload.read()
    else:
        flash("Kies een back-upbestand.", "error")
        return redirect(url_for("backup.index"))
    try:
        summary = svc.inspect_backup(data)
    except svc.BackupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("backup.index"))
    token = secrets.token_hex(16)
    _restore_path(token).write_bytes(data)
    return render_template("backup/restore.html", summary=summary, token=token)


@bp.post("/restore/uitvoeren")
@admin_required
def restore_run():
    path = _restore_path(request.form.get("token", ""))
    if not path.exists():
        flash("De upload is verlopen; begin opnieuw.", "error")
        return redirect(url_for("backup.index"))
    if request.form.get("confirm") != "1":
        flash("Bevestig dat je de huidige gegevens wilt vervangen.", "error")
        return redirect(url_for("backup.index"))
    data = path.read_bytes()
    path.unlink(missing_ok=True)
    try:
        svc.restore_backup(data)
    except svc.BackupError as exc:
        flash(str(exc), "error")
        return redirect(url_for("backup.index"))
    logout_user()
    flash("Back-up hersteld. Log opnieuw in met de gegevens uit de back-up.", "success")
    return redirect(url_for("auth.login"))


# --- volledige export ---------------------------------------------------------------------------


def _dump_tables() -> dict[str, list[dict]]:
    data: dict[str, list[dict]] = {}
    for table in svc.table_names():
        if table == "alembic_version":
            continue
        result = db.session.execute(text(f'SELECT * FROM "{table}"'))
        cols = [c for c in result.keys() if c not in SENSITIVE_COLUMNS]  # noqa: SIM118
        data[table] = [{c: row._mapping[c] for c in cols} for row in result]
    return data


def _json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


@bp.get("/export/json")
@admin_required
def export_json():
    payload = {
        "app": "MyMoneyManager",
        "exported_at": datetime.now().replace(microsecond=0).isoformat(),
        "app_version": current_app.config["APP_VERSION"],
        "note": "Wachtwoordhashes en 2FA-geheimen zijn bewust niet opgenomen.",
        "tables": _dump_tables(),
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    return Response(
        body,
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="mymoneymanager-export-{date.today():%Y%m%d}.json"'
        },
    )


@bp.get("/export/csv")
@admin_required
def export_csv_zip():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for table, rows in _dump_tables().items():
            buf = io.StringIO()
            writer = csv.writer(buf, delimiter=";")
            if rows:
                writer.writerow(rows[0].keys())
                for row in rows:
                    writer.writerow(
                        [
                            ""
                            if v is None
                            else _json_default(v)
                            if isinstance(v, (date, datetime))
                            else v
                            for v in row.values()
                        ]
                    )
            zf.writestr(f"{table}.csv", "﻿" + buf.getvalue())
    return Response(
        out.getvalue(),
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="mymoneymanager-csv-{date.today():%Y%m%d}.zip"'
        },
    )


# --- CSV-import ---------------------------------------------------------------------------------


def _importable_accounts() -> list[Account]:
    return [
        a
        for a in db.session.execute(access.visible_accounts(include_archived=False)).scalars()
        if can_manage(a)
    ]


def _category_choices(kind: str):
    return list(
        db.session.execute(
            db.select(Category)
            .where(
                Category.household_id == household_id(),
                Category.kind == kind,
                Category.is_archived.is_(False),
                Category.exclude_from_reports.is_(False),
            )
            .order_by(Category.sort_order, Category.name)
        ).scalars()
    )


def _category_tree(kind: str):
    cats = _category_choices(kind)
    return [(p, [c for c in cats if c.parent_id == p.id]) for p in cats if p.parent_id is None]


@bp.get("/import")
@login_required
def import_start():
    return render_template(
        "backup/import_start.html",
        accounts=_importable_accounts(),
        presets=importer.PRESETS,
    )


@bp.post("/import/koppelen")
@login_required
def import_map():
    account = next(
        (a for a in _importable_accounts() if a.id == _int(request.form.get("account_id"))), None
    )
    upload = request.files.get("file")
    if account is None or upload is None or not upload.filename:
        flash("Kies een rekening en een bestand.", "error")
        return redirect(url_for("backup.import_start"))
    data = upload.read()
    try:
        parsed = importer.parse_file(data)
    except importer.ImportError_ as exc:
        flash(str(exc), "error")
        return redirect(url_for("backup.import_start"))
    token = importer.save_upload(data)
    chosen = request.form.get("preset")
    preset_key = (
        chosen
        if chosen in importer.PRESETS and chosen != "auto"
        else importer.detect_preset(parsed.headers)
    )
    preset = importer.PRESETS[preset_key]
    return render_template(
        "backup/import_map.html",
        token=token,
        account=account,
        parsed=parsed,
        sample=parsed.rows[:5],
        fields=importer.FIELDS,
        mapping=preset["map"],
        preset_key=preset_key,
        preset_label=preset["label"],
        date_format=preset["date_format"],
        date_formats=importer.DATE_FORMATS,
        expense_tree=_category_tree("expense"),
        income_tree=_category_tree("income"),
    )


def _mapping_from_form() -> dict[str, str]:
    return {key: request.form.get(f"map_{key}", "") for key, _label, _req in importer.FIELDS}


@bp.post("/import/voorbeeld")
@login_required
def import_preview():
    account = next(
        (a for a in _importable_accounts() if a.id == _int(request.form.get("account_id"))), None
    )
    if account is None:
        abort(404)
    token = request.form.get("token", "")
    mapping = _mapping_from_form()
    if not mapping["date"] or not mapping["amount"]:
        flash("Koppel minimaal de kolommen Datum en Bedrag.", "error")
        return redirect(url_for("backup.import_start"))
    try:
        data = importer.load_upload(token)
        default_expense = _int(request.form.get("default_expense"))
        default_income = _int(request.form.get("default_income"))
        parsed, preview = importer.build_preview(
            data,
            mapping,
            request.form.get("date_format", "auto"),
            account.id,
            default_expense,
            default_income,
        )
    except importer.ImportError_ as exc:
        flash(str(exc), "error")
        return redirect(url_for("backup.import_start"))
    return render_template(
        "backup/import_preview.html",
        preview=preview,
        account=account,
        token=token,
        mapping=mapping,
        date_format=request.form.get("date_format", "auto"),
        default_expense=default_expense,
        default_income=default_income,
        limit=importer.PREVIEW_LIMIT,
    )


@bp.post("/import/uitvoeren")
@login_required
def import_commit():
    account = next(
        (a for a in _importable_accounts() if a.id == _int(request.form.get("account_id"))), None
    )
    if account is None:
        abort(404)
    token = request.form.get("token", "")
    mapping = _mapping_from_form()
    try:
        data = importer.load_upload(token)
        _parsed, preview = importer.build_preview(
            data,
            mapping,
            request.form.get("date_format", "auto"),
            account.id,
            _int(request.form.get("default_expense")),
            _int(request.form.get("default_income")),
        )
    except importer.ImportError_ as exc:
        flash(str(exc), "error")
        return redirect(url_for("backup.import_start"))
    selected = {i for i in (_int(v) for v in request.form.getlist("rows")) if i is not None}
    # Rijen die niet in het voorbeeld stonden, worden met dezelfde regels (zonder fouten) meegenomen.
    unseen = {r.index for r in preview.rows[importer.PREVIEW_LIMIT :]}
    valid_categories = {c.id for kind in ("expense", "income") for c in _category_choices(kind)}
    rows = [
        r
        for r in preview.rows
        if (r.index in selected or r.index in unseen) and r.category_id in valid_categories
    ]
    if request.form.get("skip_dupes", "1") == "1":
        rows = [r for r in rows if not r.duplicate]
    count = importer.commit_rows(rows, account, current_user)
    importer.discard_upload(token)
    flash(f"{count} transacties geïmporteerd in {account.name}.", "success")
    return redirect(url_for("transactions.list_view", account=account.id))
