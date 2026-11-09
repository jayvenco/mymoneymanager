#!/bin/sh
# Lokale ontwikkelserver (database in .devdata/; FRESH=1 begint met een lege database)
cd "$(dirname "$0")/.." || exit 1
[ "${FRESH:-0}" = "1" ] && rm -rf .devdata
export SECRET_KEY=dev DATA_DIR="$PWD/.devdata" BACKUP_DIR="$PWD/.devdata/backups" SCHEDULER_ENABLED=false
.venv/bin/flask --app wsgi:app db upgrade >/dev/null 2>&1
exec .venv/bin/flask --app wsgi:app run --debug --port "${PORT:-5055}"
