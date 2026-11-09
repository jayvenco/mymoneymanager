#!/bin/sh
# Volgorde: rechten herstellen -> database-migraties -> Gunicorn. De app draait nooit als root.
set -e

PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-022}"
DATA_DIR="${DATA_DIR:-/data}"
BACKUP_DIR="${BACKUP_DIR:-/backups}"

if [ -z "${SECRET_KEY:-}" ]; then
  echo "FOUT: SECRET_KEY ontbreekt. Genereer er een met: openssl rand -hex 32" >&2
  exit 1
fi

mkdir -p "$DATA_DIR" "$BACKUP_DIR"

if [ "$(id -u)" = "0" ]; then
  chown -R "$PUID:$PGID" "$DATA_DIR" "$BACKUP_DIR"
  chmod u+rwX "$DATA_DIR" "$BACKUP_DIR"
  umask "$UMASK"
  RUN="gosu $PUID:$PGID"
else
  echo "Waarschuwing: container draait niet als root; PUID/PGID worden genegeerd." >&2
  umask "$UMASK"
  RUN=""
fi

export HOME=/tmp
echo "MyMoneyManager ${APP_VERSION:-dev}: database-migraties uitvoeren..."
$RUN flask --app wsgi:app db upgrade

# Alleen de webserver draait de achtergrondtaken (herhaaltransacties, back-ups).
export SCHEDULER_ENABLED=true
echo "MyMoneyManager: Gunicorn starten op poort 8080 (uid=$PUID gid=$PGID)"
exec $RUN gunicorn wsgi:app \
  --bind 0.0.0.0:8080 \
  --workers 2 --threads 4 --worker-class gthread \
  --access-logfile - --error-logfile - \
  --timeout 60
