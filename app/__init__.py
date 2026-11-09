"""Flask app-factory voor MyMoneyManager."""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request, url_for
from flask_babel import gettext as _
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import APP_NAME, load_config
from .extensions import babel, csrf, db, limiter, login_manager, migrate
from .utils.dates import format_date, month_label
from .utils.money import format_eur


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """WAL + foreign keys voor elke SQLite-verbinding."""
    import sqlite3

    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


NAV_ITEMS = [
    ("reports.overview", "Rapportages", "bar-chart-3"),
    ("transactions.list_view", "Transacties", "arrow-left-right"),
    ("accounts.list_view", "Rekeningen", "wallet"),
    ("budgets.overview", "Budgetten", "gauge"),
    ("goals.list_view", "Spaardoelen", "piggy-bank"),
    ("settings.index", "Instellingen", "settings"),
]


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    config = load_config(test_config)
    app.config.update(config)
    app.config["TESTING"] = bool((test_config or {}).get("TESTING", False))
    _configure_logging(app.config["LOG_LEVEL"])

    app.config["DATA_DIR"].mkdir(parents=True, exist_ok=True)
    app.config["BACKUP_DIR"].mkdir(parents=True, exist_ok=True)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)  # type: ignore[method-assign]

    db.init_app(app)
    migrate.init_app(
        app, db, directory=str(Path(app.root_path).parent / "migrations"), render_as_batch=True
    )
    csrf.init_app(app)
    limiter.init_app(app)
    babel.init_app(
        app, locale_selector=lambda: "nl", timezone_selector=lambda: app.config["TIMEZONE"]
    )

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Log in om verder te gaan."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "basic"

    from .models import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    _register_blueprints(app)
    _register_hooks(app)
    _register_errors(app)

    from .cli import register_cli

    register_cli(app)

    app.jinja_env.filters["eur"] = format_eur
    app.jinja_env.filters["datum"] = format_date
    app.jinja_env.filters["maand"] = month_label

    return app


def _register_blueprints(app: Flask) -> None:
    from .blueprints.auth import bp as auth_bp
    from .blueprints.reports import bp as reports_bp
    from .blueprints.setup import bp as setup_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(setup_bp)
    app.register_blueprint(reports_bp)


def _register_hooks(app: Flask) -> None:
    from .services.setup import setup_needed

    @app.route("/health")
    @limiter.exempt
    def health():
        try:
            db.session.execute(text("SELECT 1"))
            return jsonify(status="ok", db=True, version=app.config["APP_VERSION"])
        except Exception:  # noqa: BLE001 - health moet nooit crashen
            return jsonify(status="error", db=False), 503

    @app.before_request
    def force_setup():
        if app.extensions.get("mmm_setup_done"):
            return None
        endpoint = request.endpoint or ""
        if endpoint in {"health", "static", "setup.index"} or endpoint.startswith("static"):
            return None
        if setup_needed():
            return redirect(url_for("setup.index"))
        app.extensions["mmm_setup_done"] = True
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-eval'; frame-ancestors 'none'; form-action 'self'",
        )
        if app.config["HTTPS"]:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if (
            request.endpoint
            and request.endpoint != "static"
            and "Cache-Control" not in response.headers
        ):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def inject_globals():
        nav = [
            {"endpoint": ep, "label": label, "icon": icon}
            for ep, label, icon in NAV_ITEMS
            if ep in app.view_functions
        ]
        return {
            "app_name": APP_NAME,
            "app_version": app.config["APP_VERSION"],
            "nav_items": nav,
            "_": _,
        }


def _register_errors(app: Flask) -> None:
    @app.errorhandler(403)
    def forbidden(_e):
        return render_template(
            "error.html", code=403, message=_("Je hebt hier geen toegang toe.")
        ), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message=_("Deze pagina bestaat niet.")), 404

    @app.errorhandler(429)
    def too_many(_e):
        message = _("Te veel inlogpogingen. Probeer het over een minuut opnieuw.")
        if request.endpoint == "auth.login":
            return render_template("auth/login.html", error=message), 429
        return render_template("error.html", code=429, message=message), 429

    @app.errorhandler(500)
    def server_error(_e):
        return render_template(
            "error.html", code=500, message=_("Er ging iets mis aan onze kant.")
        ), 500
