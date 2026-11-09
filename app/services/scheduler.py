"""Achtergrondtaken met APScheduler. Eén instantie tegelijk dankzij een bestandslock."""

import fcntl
import logging
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask

log = logging.getLogger("mmm.scheduler")


def acquire_lock(app: Flask):
    """Geeft een open bestand terug als dit proces de lock heeft, anders None."""
    path = app.config["DATA_DIR"] / ".scheduler.lock"
    handle = open(path, "w")  # noqa: SIM115 - moet open blijven zolang het proces leeft
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def run_recurring(app: Flask) -> None:
    from .household import get_household
    from .recurring import generate_due

    with app.app_context():
        if get_household() is None:
            return  # nog niet ingesteld: er valt niets te genereren
        try:
            created = generate_due(date.today())
            if created:
                log.info("terugkerende transacties aangemaakt: %s", created)
        except Exception:
            log.exception("taak 'terugkerende transacties' mislukt")


def start_scheduler(app: Flask) -> BackgroundScheduler | None:
    handle = acquire_lock(app)
    if handle is None:
        return None
    scheduler = BackgroundScheduler(timezone=app.config["TIMEZONE"], daemon=True)
    scheduler.add_job(
        run_recurring, "cron", hour=0, minute=10, args=[app], id="recurring", replace_existing=True
    )
    scheduler.add_job(
        run_recurring, "date", args=[app], id="recurring-startup"
    )  # ingehaald na herstart
    try:
        from .backup import register_backup_job

        register_backup_job(app, scheduler)
    except ImportError:
        pass
    scheduler.start()
    app.extensions["mmm_scheduler"] = scheduler
    app.extensions["mmm_scheduler_lock"] = handle
    log.info("scheduler gestart (pid-lock verkregen)")
    return scheduler
