#!/usr/bin/env bash
# Controleert de PUID/PGID-afhandeling van de container.
# Gebruik: tests/entrypoint_test.sh <image>
set -euo pipefail
IMAGE="${1:?image ontbreekt}"
# Named volumes (geen bind mounts): zo klopt de eigenaar ook op Docker Desktop (macOS).
SUFFIX="$$"
trap 'docker rm -f mmm-entry-test >/dev/null 2>&1 || true; docker volume rm -f "mmm-data-$SUFFIX" "mmm-bak-$SUFFIX" >/dev/null 2>&1 || true' EXIT
docker rm -f mmm-entry-test >/dev/null 2>&1 || true

fail() { echo "FOUT: $*" >&2; exit 1; }

# 1. Zonder SECRET_KEY weigert de container te starten.
if docker run --rm "$IMAGE" >/dev/null 2>&1; then fail "container startte zonder SECRET_KEY"; fi
echo "ok: weigert te starten zonder SECRET_KEY"

# 2. Met PUID/PGID 1234/2345 eindigen bestanden met die eigenaar en draait de app niet als root.
docker run -d --name mmm-entry-test -e SECRET_KEY=test -e PUID=1234 -e PGID=2345 -e UMASK=027 \
  -v "mmm-data-$SUFFIX:/data" -v "mmm-bak-$SUFFIX:/backups" "$IMAGE" >/dev/null

for _ in $(seq 1 60); do
  if docker exec mmm-entry-test python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8708/health',timeout=2)" 2>/dev/null; then break; fi
  sleep 1
done
docker exec mmm-entry-test python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8708/health',timeout=2).read().decode())" \
  || { docker logs mmm-entry-test; fail "health-check mislukt"; }

owner="$(docker exec mmm-entry-test stat -c '%u:%g' /data/mymoneymanager.db)"
[ "$owner" = "1234:2345" ] || fail "database heeft eigenaar $owner, verwacht 1234:2345"
echo "ok: database is eigendom van 1234:2345"

owner_dir="$(docker exec mmm-entry-test stat -c '%u:%g' /data /backups | sort -u)"
[ "$owner_dir" = "1234:2345" ] || fail "mappen hebben eigenaar $owner_dir"
echo "ok: /data en /backups zijn eigendom van 1234:2345"

procs="$(docker exec mmm-entry-test sh -c 'for p in /proc/[0-9]*; do if tr "\0" " " < $p/cmdline 2>/dev/null | grep -q "[g]unicorn"; then awk "/^Uid:/{print \$2}" $p/status; fi; done' | sort -u | tr '\n' ' ')"
[ "$procs" = "1234 " ] || fail "gunicorn-processen draaien met uid(s): '$procs' (verwacht 1234)"
echo "ok: alle gunicorn-processen draaien als uid 1234, niet als root"
echo "Alle entrypoint-tests geslaagd."
