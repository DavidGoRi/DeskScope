# DeskScope

[![CI](https://github.com/DavidGoRi/DeskScope/actions/workflows/ci.yml/badge.svg)](https://github.com/DavidGoRi/DeskScope/actions/workflows/ci.yml)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-SQLite-009688?logo=fastapi&logoColor=white)
![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)

**A lightweight IT help desk for small offices: tickets, roles, an audit trail and, soon, SLAs, a knowledge base and integrations with the rest of the toolkit.**

DeskScope is the fourth piece of a small sysadmin toolkit:

| Project | What it does |
|---|---|
| [PCScope](https://github.com/DavidGoRi/PCScope) | Inventory and health of Windows PCs |
| [NetScope](https://github.com/DavidGoRi/NetScope) | Finds and identifies every device on the LAN |
| [ADFlow](https://github.com/DavidGoRi/ADFlow) | Automated Active Directory onboarding and offboarding |
| **DeskScope** | The help desk where all of that becomes tickets |

The goal is the loop a real service desk lives in: *the inventory spots a problem → a ticket is opened → a technician fixes it → the fix is documented*.

*Leer en [español](README.es.md).*

## What works today (v0.2, phases 1 and 2)

- **Tickets** with category, priority, device, status and a conversation.
- **Three roles**: employees open and follow *their own* tickets; technicians see and work every ticket; administrators also manage accounts.
- **Workflow** `New → In progress → Waiting for user → Resolved → Closed`, with rules per role: an employee can confirm or reject a fix, a technician cannot reopen a closed ticket, and a reply from the employee puts a waiting ticket back in the queue.
- **Internal notes** that the employee never sees.
- **Audit trail**: every change of status, priority or assignee is recorded with who and when.
- **Search and filters** by number, text, device, status, "assigned to me", "unassigned" and priority.

### SLAs the ITIL way

- **Priority is not a free choice.** The employee answers two plain questions, *who is affected?* (impact) and *can you keep working?* (urgency), and the priority comes from the ITIL matrix, previewed live while typing. Technicians re-triage impact and urgency, and every change is logged.

  | Impact ↓ / Urgency → | High | Medium | Low |
  |---|---|---|---|
  | **High** (office or site) | Critical | High | Medium |
  | **Medium** (team) | High | Medium | Low |
  | **Low** (one person) | Medium | Low | Low |

- **Two clocks per ticket**: *first response* (the first public answer from the desk, or the fix) and *resolution*.
- **Business hours only**: working days, opening hours, time zone and holidays from `sla.json`; a ticket opened on Friday at 17:30 is not late on Saturday.
- **Paused while waiting for the user**, as ITIL expects: time in *Waiting for user* does not count against the desk.
- **States**: on time, at risk (75 % of the target used, configurable), breached, paused, met. The list shows the clock that matters, counts *at risk* and *breached* tickets, filters them and sorts by due date; each ticket shows both clocks with progress bars.

Default targets (business time, all editable):

| Priority | First response | Resolution |
|---|---|---|
| Critical | 30 min | 4 h |
| High | 1 h | 8 h |
| Medium | 4 h | 3 days |
| Low | 8 h | 5 days |
- **English and Spanish** interface (browser language or a switch), light and dark mode, usable on a phone.

### Security, because a help desk holds sensitive data

- Passwords hashed with **PBKDF2-SHA256, 600,000 rounds** (standard library, no external crypto).
- **CSRF token** on every form, `SameSite=Lax` and `HttpOnly` session cookies, a new session on every login.
- **Brute-force throttling**: 5 failed logins in 15 minutes lock that user from that address.
- Strict **Content-Security-Policy** (no inline scripts or styles), `X-Frame-Options: DENY`, `nosniff`, `no-store`.
- Tickets you may not see answer **404, not 403**, so ticket numbers cannot be probed.
- Permissions are enforced in the domain layer, not only in the pages, so the future API cannot skip them.
- Only local redirects after login (no open redirect). Output is escaped by Jinja2.

## Try it

```bash
git clone https://github.com/DavidGoRi/DeskScope.git
cd DeskScope
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e .

deskscope demo --lang en        # fictitious company with 14 tickets
deskscope serve                 # http://127.0.0.1:8000
```

Every demo account uses the password `deskscope-demo`: `carlos.ruiz` (technician), `laura.sanchez` (administrator) or `ana.garcia` (employee). Sign in as different roles to see how the same ticket changes.

### Real use

```bash
deskscope init
set DESKSCOPE_PASSWORD=...      # or let it ask
deskscope create-user laura --name "Laura Sánchez" --email laura@example.com --role admin
deskscope serve --host 0.0.0.0 --secure-cookies   # behind an HTTPS reverse proxy
```

| Setting | Default |
|---|---|
| `--db` / `DESKSCOPE_DB` | `data/deskscope.db` |
| `DESKSCOPE_SECRET_KEY` | generated once in `data/secret.key` |
| `--sla` / `DESKSCOPE_SLA` | `sla.json` next to the database, else built-in defaults |

Copy [`sla.example.json`](sla.example.json) (Madrid business hours and Spanish national holidays for 2026–2027) to `data/sla.json` and adapt it. `deskscope sla` validates the file and prints the policy in use; `serve` refuses to start with an invalid one.

## Roadmap

1. ✅ **Core**: tickets, roles, workflow, comments, audit trail, bilingual UI.
2. ✅ **SLAs (ITIL)**: priority from an impact × urgency matrix, response and resolution targets in business hours with holidays, pause while waiting for the user, "at risk" warnings.
3. **Integrations**: PCScope device sheet inside the ticket, tickets opened automatically from PCScope critical findings and NetScope new devices, onboarding tickets from ADFlow, REST API with tokens.
4. **Knowledge base and metrics**: turn a solved ticket into an article, suggest articles while typing, MTTR, SLA compliance, top categories and devices, CSV export.
5. **Release**: tickets by email (IMAP), notifications by email and Telegram, Docker image, live demo and v1.0.0.

## Development

```bash
pip install -e ".[dev]"
ruff check .
python -m pytest
```

164 tests (domain rules, permissions, the business-hours calendar and SLA clocks, schema migrations, web flows, security headers, translations, demo data and CLI) run on Windows and Linux with Python 3.10 and 3.13. CI also starts the real server and checks it answers.

The schema evolves through numbered SQL migrations (`src/deskscope/migrations/`), each applied in one transaction; upgrading a v0.1 database keeps every priority and backfills the first-response times.

```
src/deskscope/
  models.py     vocabulary, workflow rules, errors
  tickets.py    ticket rules (every function takes the acting user)
  sla.py        business calendar, SLA clocks, sla.json parsing
  migrations/   numbered SQL schema migrations
  users.py      accounts and authentication
  security.py   password hashing, tokens
  web.py        FastAPI routes, sessions, CSRF, security headers
  i18n.py       English and Spanish text
  seed.py       fictitious demo company
  cli.py        init, create-user, demo, serve
```

## License

[MIT](LICENSE). All names in the demo are fictitious and use `example.com`.
