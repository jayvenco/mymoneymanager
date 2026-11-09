#!/bin/bash
# MyMoneyManager: voorbereiding + installatie/update op Unraid.
# Veilig om opnieuw te draaien: mappen en SECRET_KEY blijven behouden, de container
# wordt opnieuw aangemaakt met het nieuwste image (data blijft staan).
#
# Gebruik (op Unraid, als root):
#   bash install-unraid.sh
# Optioneel, via omgevingsvariabelen:
#   PORT=8080  IMAGE_TAG=latest  HTTPS=false  TZ=Europe/Amsterdam
#   APPDATA=/mnt/user/appdata/mymoneymanager   (of /mnt/cache/appdata/... voor een cache-pool)
#   GHCR_USER=<github-naam> GHCR_TOKEN=<PAT met read:packages>   (alleen nodig als het package privé is)
set -euo pipefail

NAME="${NAME:-mymoneymanager}"
IMAGE="ghcr.io/jayvenco/mymoneymanager:${IMAGE_TAG:-latest}"
APPDATA="${APPDATA:-/mnt/user/appdata/mymoneymanager}"
PORT="${PORT:-8080}"
PUID="${PUID:-99}"
PGID="${PGID:-100}"
TZ="${TZ:-Europe/Amsterdam}"
HTTPS="${HTTPS:-false}"
ICON="https://raw.githubusercontent.com/jayvenco/mymoneymanager/main/unraid/icon.png"

say() { printf '\n==> %s\n' "$*"; }

command -v docker >/dev/null || { echo "Docker niet gevonden. Zet Docker aan in Unraid (Settings -> Docker)." >&2; exit 1; }

say "Mappen aanmaken (indien niet aanwezig)"
mkdir -p "$APPDATA/data" "$APPDATA/backups"
chown -R "$PUID:$PGID" "$APPDATA/data" "$APPDATA/backups"
chmod 755 "$APPDATA"
echo "  $APPDATA/data     -> /data"
echo "  $APPDATA/backups  -> /backups"
case "$APPDATA" in
  /mnt/user/*) echo "  Tip: zet de appdata-share op de cache/pool (liefst exclusive share) of gebruik APPDATA=/mnt/cache/appdata/mymoneymanager." ;;
esac

say "SECRET_KEY"
KEYFILE="$APPDATA/secret_key"
if [ -n "${SECRET_KEY:-}" ]; then
  echo "  Gebruik SECRET_KEY uit de omgeving."
elif [ -s "$KEYFILE" ]; then
  SECRET_KEY="$(cat "$KEYFILE")"
  echo "  Bestaande sleutel gebruikt ($KEYFILE)."
else
  SECRET_KEY="$( (openssl rand -hex 32 2>/dev/null) || head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' )"
  umask 077; printf '%s' "$SECRET_KEY" > "$KEYFILE"
  echo "  Nieuwe sleutel aangemaakt en bewaard in $KEYFILE (alleen root kan lezen)."
  echo "  Bewaar een kopie in je wachtwoordmanager; zonder deze sleutel worden sessies ongeldig."
fi
[ -n "$SECRET_KEY" ] && { umask 077; [ -s "$KEYFILE" ] || printf '%s' "$SECRET_KEY" > "$KEYFILE"; }

if [ -n "${GHCR_TOKEN:-}" ]; then
  say "Inloggen bij ghcr.io"
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "${GHCR_USER:?Zet ook GHCR_USER}" --password-stdin
fi

say "Image ophalen: $IMAGE"
if ! docker pull "$IMAGE"; then
  echo "Pullen mislukt. Is het package privé? Draai opnieuw met GHCR_USER en GHCR_TOKEN (PAT met read:packages)," >&2
  echo "of maak het package publiek op GitHub (Packages -> mymoneymanager -> Package settings -> Change visibility)." >&2
  exit 1
fi

say "Container (opnieuw) aanmaken"
if docker inspect "$NAME" >/dev/null 2>&1; then
  docker stop -t 30 "$NAME" >/dev/null 2>&1 || true
  docker rm "$NAME" >/dev/null
  echo "  Oude container verwijderd (data blijft staan)."
fi

docker run -d --name "$NAME" \
  --restart unless-stopped \
  -p "$PORT:8080" \
  -v "$APPDATA/data:/data" \
  -v "$APPDATA/backups:/backups" \
  -e SECRET_KEY="$SECRET_KEY" \
  -e PUID="$PUID" -e PGID="$PGID" -e UMASK=022 \
  -e TZ="$TZ" -e HTTPS="$HTTPS" \
  -l net.unraid.docker.managed=dockerman \
  -l "net.unraid.docker.webui=http://[IP]:[PORT:8080]/" \
  -l "net.unraid.docker.icon=$ICON" \
  "$IMAGE" >/dev/null

say "Wachten tot de app klaar is"
for _ in $(seq 1 60); do
  state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$NAME" 2>/dev/null || echo onbekend)"
  if [ "$state" = "healthy" ] || [ "$state" = "running" ]; then
    IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo
    echo "Klaar! Open http://${IP:-<unraid-ip>}:$PORT  (eerste keer: setup-wizard)"
    exit 0
  fi
  [ "$state" = "exited" ] && break
  sleep 2
done
echo "De app is niet gezond gestart. Bekijk de log: docker logs $NAME" >&2
exit 1
