# Changelog

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
