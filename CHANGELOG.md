# Changelog

## [1.0.0] - 2026-11-18

Phase 5: email, Telegram, Docker and the live demo. First stable release.

### Added
- Tickets by email (`deskscope fetch-mail`, IMAP): known senders open tickets, replies with `[DeskScope #n]` in the subject become comments without the quoted text; unknown senders, auto-replies and bulk mail are ignored and every Message-ID is remembered.
- Notifications by email and Telegram, built on an outbox: the notifier reads the audit trail and comments since its last run, queues messages with a unique key and sends them with retries (`deskscope notify`, `--dry-run`). New tickets, assignments, answers, waiting-for-you, resolution with the survey link, requester replies and SLA at risk / breached (once per ticket, clock and state).
- `deskscope worker --every 60` and `serve --worker-every 60` to run mail and notifications continuously.
- `notifications.example.json`; secrets only in `DESKSCOPE_SMTP_PASSWORD`, `DESKSCOPE_TELEGRAM_TOKEN` and `DESKSCOPE_IMAP_PASSWORD`.
- Dockerfile (non-root user, health check, `/data` volume) and `docker-compose.yml`; CI builds and runs the image.
- Read-only live demo on GitHub Pages rendered by the real application (`tools/build_demo.py`) for three people in two languages, rebuilt weekly; knowledge base suggestions work in the static pages.
- `tools/Update-Screenshots.ps1`: screenshots, `docs/demo.gif` and `docs/social-preview.png`.
- Release workflow: wheel, sdist, SHA-256 checksums and notes from this changelog.

### Changed
- Migration 5 rebuilds the tickets table (new "email" source) following SQLite's procedure: foreign keys off during migrations and `foreign_key_check` before turning them back on.

## [0.4.0] - 2026-11-17

Phase 4: knowledge base, satisfaction and metrics.

### Added
- Knowledge base: articles for everyone (self-service) or staff only, drafts, read counter, written from scratch or pre-filled from a solved ticket (public answers only) and linked to it.
- Full-text search with SQLite FTS5 (BM25 ranking, prefixes, accent-insensitive), kept in sync by triggers; free text is turned into quoted terms so FTS operators cannot be injected.
- Suggestions while typing a new ticket (`/kb/suggest`, inserted with `textContent`) and related articles on each ticket for technicians.
- Satisfaction survey (1-5 and a comment) for the requester of a resolved ticket.
- Metrics page for 7, 30 or 90 days: volumes, backlog at risk, mean and median time to resolve and first response in business time, SLA compliance, satisfaction, daily SVG chart and breakdowns by priority, category, technician, source and device.
- CSV export with business durations and SLA states, UTF-8 with BOM and protected against formula injection.
- API: `/api/v1/articles`, `/api/v1/articles/{id}`, `/api/v1/tickets/{id}/rate` and `/api/v1/reports/summary`.
- Demo: 60 days of closed tickets (`--history`), six articles and ratings.

## [0.3.0] - 2026-11-17

Phase 3: integrations and REST API.

### Added
- Importers for the rest of the toolkit (`deskscope import pcscope|netscope|adflow`):
  - PCScope: device sheets from `devices.csv` and one ticket per device and rule from `issues.csv` (critical by default, `--levels` to widen); handles PowerShell's BOM and culture-specific delimiters.
  - NetScope: tickets for critical findings and for new devices (`--severities`, `--no-new-devices`).
  - ADFlow: onboarding and offboarding tickets with a checklist from a live run's `summary.json` (`--include-dry-runs`).
- Reconciliation: stable keys with a partial unique index (one active ticket per key), internal notes when details change, automatic resolution when a problem disappears, new ticket when it comes back; events needing a person are never auto-closed.
- `integrations` system account that opens those tickets and cannot sign in.
- Device sheet (PCScope) and "other tickets for this device" on the ticket page, and a source badge on imported tickets.
- REST API at `/api/v1` (tickets, comments, status, assignment, triage, devices, imports) with OpenAPI at `/api/openapi.json` and Swagger UI at `/api/docs`.
- API tokens: created under *API tokens* or with `deskscope token create|list|revoke`, shown once, stored as SHA-256, revocable, with last use; bearer only (never the session cookie).
- Fictitious example exports in `src/deskscope/examples`, imported by `deskscope demo`.
- CI smoke test calls the real API with a real token.

## [0.2.0] - 2026-11-17

Phase 2: service level agreements.

### Added
- ITIL classification: impact and urgency on every ticket, with the priority derived from the impact × urgency matrix and a live preview in the form.
- Triage for technicians (impact and urgency), logged in the history.
- SLA engine (`sla.py`): business calendar with working days, hours, time zone and holidays; first-response and resolution clocks; the resolution clock pauses while the ticket waits for the user.
- SLA states (on time, at risk, breached, paused, met) in the ticket list and a service level card with progress bars on each ticket (staff only).
- "At risk" and "breached" counters, an SLA filter and a "due first" sort order.
- `sla.json` configuration (`--sla`, `DESKSCOPE_SLA`), `sla.example.json` with Spanish national holidays 2026–2027, and the `deskscope sla` command to validate it.
- Numbered SQL migrations applied in one transaction each; v0.1 databases are upgraded keeping their priorities and backfilling first-response times.

### Changed
- Requesters no longer pick a priority directly; they answer the impact and urgency questions.
- `tzdata` is installed on Windows, which ships without the IANA time zone database.

## [0.1.0] - 2026-11-17

Phase 1: the core help desk.

### Added
- Tickets with category, priority, device, status, comments and internal notes.
- Roles: requester, technician (agent) and administrator, with permissions enforced in the domain layer.
- Workflow `new → open → pending → resolved → closed` with per-role transitions; a requester's reply reopens a pending ticket; assigning a new ticket starts work on it.
- Audit trail of status, priority and assignee changes.
- Ticket list with search, status/scope filters and sorting.
- User administration (create, enable, disable).
- English and Spanish interface, light and dark mode, responsive layout.
- Security: PBKDF2-SHA256 passwords, CSRF tokens, login throttling, strict CSP and security headers, 404 for tickets you cannot see, no open redirects.
- CLI: `init`, `create-user`, `demo` (fictitious company in English or Spanish) and `serve`.
- 103 tests and CI on Windows and Linux (Python 3.10 and 3.13) with a smoke test of the real server.
