"""Applicatieconfiguratie uit omgevingsvariabelen."""

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

APP_NAME = "MyMoneyManager"


class ConfigError(RuntimeError):
    """De configuratie is onvolledig of ongeldig."""


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    env = os.environ
    overrides = overrides or {}

    data_dir = Path(overrides.get("DATA_DIR") or env.get("DATA_DIR", "/data"))
    backup_dir = Path(overrides.get("BACKUP_DIR") or env.get("BACKUP_DIR", "/backups"))
    https = _bool(env.get("HTTPS"))
    lifetime_days = int(env.get("SESSION_LIFETIME_DAYS", "7"))

    secret = overrides.get("SECRET_KEY") or env.get("SECRET_KEY")
    if not secret:
        raise ConfigError(
            "SECRET_KEY ontbreekt. Stel de omgevingsvariabele SECRET_KEY in "
            "(bijv. `openssl rand -hex 32`); de applicatie start niet zonder."
        )

    config: dict[str, Any] = {
        "SECRET_KEY": secret,
        "DATA_DIR": data_dir,
        "BACKUP_DIR": backup_dir,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{data_dir / 'mymoneymanager.db'}",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"timeout": 30}},
        "HTTPS": https,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": https,
        "REMEMBER_COOKIE_HTTPONLY": True,
        "REMEMBER_COOKIE_SAMESITE": "Lax",
        "REMEMBER_COOKIE_SECURE": https,
        "REMEMBER_COOKIE_DURATION": timedelta(days=lifetime_days),
        "PERMANENT_SESSION_LIFETIME": timedelta(days=lifetime_days),
        "SESSION_LIFETIME_DAYS": lifetime_days,
        "TIMEZONE": env.get("TZ", "Europe/Amsterdam"),
        "BABEL_DEFAULT_LOCALE": "nl",
        "BABEL_DEFAULT_TIMEZONE": env.get("TZ", "Europe/Amsterdam"),
        "BACKUP_TIME": env.get("BACKUP_TIME", "03:00"),
        "BACKUP_FREQUENCY": env.get("BACKUP_FREQUENCY", "monthly").lower(),
        "BACKUP_RETENTION_DAYS": int(env.get("BACKUP_RETENTION_DAYS", "360")),
        "LOG_LEVEL": env.get("LOG_LEVEL", "INFO").upper(),
        "APP_VERSION": env.get("APP_VERSION", "dev"),
        "GIT_SHA": env.get("GIT_SHA", "unknown"),
        "BUILD_DATE": env.get("BUILD_DATE", "unknown"),
        "RATELIMIT_STORAGE_URI": "memory://",
        "RATELIMIT_HEADERS_ENABLED": True,
        "SCHEDULER_ENABLED": _bool(env.get("SCHEDULER_ENABLED"), False),
        "MAX_CONTENT_LENGTH": 256 * 1024 * 1024,
    }
    config.update({k: v for k, v in overrides.items() if k not in {"DATA_DIR", "BACKUP_DIR"}})
    return config
