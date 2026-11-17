# Changelog

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
