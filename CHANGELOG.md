# Changelog

Alle noemenswaardige wijzigingen staan hier. Formaat: [Keep a Changelog](https://keepachangelog.com/nl/1.1.0/).

## [1.0.0] — 2026-11-09

Eerste release.

### Toegevoegd
- Huishouden met twee leden, eigen/gezamenlijke rekeningen en privé-rekeningen met centrale toegangscontrole.
- Transacties (snel invoeren, inline bewerken, bulkacties, undo, audit-log), rekeningen (saldo controleren), categorieën en tags.
- Budgetten met overrides en rollover, vaste lasten (APScheduler met bestandslock) en spaardoelen.
- Rapportages als startscherm: 11 rapporten met financiële maanden, vergelijking, persoonsfilter en export naar CSV, XLSX en PDF.
- Back-up/restore via de SQLite online backup API, automatische back-ups met retentie, volledige export en CSV-import (ING, Rabobank, ABN AMRO).
- Inloggen met Argon2, rate limit, CSRF en optionele TOTP-2FA; instellingen en huishoudbeheer.
- Docker-image met PUID/PGID-entrypoint, Unraid-template, Docker Compose, CI en release-workflow naar GHCR.
- PWA, licht/donker thema, mobiele bottom-navigatie, `flask seed-demo`.
