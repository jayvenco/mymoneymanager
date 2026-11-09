# syntax=docker/dockerfile:1

# ---- Stage 1: Tailwind CSS + vendored front-end bibliotheken ----
FROM node:22-alpine AS assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci
COPY tailwind.config.js ./
COPY scripts/vendor.mjs scripts/vendor.mjs
COPY app/static/src app/static/src
COPY app/templates app/templates
COPY app/static/js app/static/js
RUN npm run build

# ---- Stage 2: runtime (zonder Node) ----
FROM python:3.12-slim

ARG APP_VERSION=dev
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="MyMoneyManager" \
      org.opencontainers.image.description="Self-hosted huishoudboekje voor twee partners" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Amsterdam \
    PUID=99 PGID=100 UMASK=022 \
    MALLOC_ARENA_MAX=2 \
    DATA_DIR=/data BACKUP_DIR=/backups \
    APP_VERSION=${APP_VERSION} GIT_SHA=${GIT_SHA} BUILD_DATE=${BUILD_DATE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt \
    && find /usr/local/lib/python3.12/site-packages/babel/locale-data -name '*.dat' \
         ! -name 'root.dat' ! -name 'en*.dat' ! -name 'nl*.dat' -delete \
    && find /usr/local -depth \( -name '__pycache__' -o -name 'tests' -path '*/site-packages/*' \) -exec rm -rf {} + \
    && pip uninstall -y pip setuptools wheel >/dev/null 2>&1 || true \
    && rm -rf /root/.cache

COPY . .
COPY --from=assets /build/app/static/css/app.css app/static/css/app.css
COPY --from=assets /build/app/static/vendor app/static/vendor
RUN chmod +x /app/entrypoint.sh && mkdir -p /data /backups

VOLUME ["/data", "/backups"]
EXPOSE 8708

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8708/health', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
