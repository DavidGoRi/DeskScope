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

## What works today (v0.4, phases 1 to 4)

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

### Integrations: the toolkit opens its own tickets

| From | What it reads | What happens in DeskScope |
|---|---|---|
| **PCScope** | `devices.csv` + `issues.csv` from `PCScope.ps1 -ExportCsv` | A **device sheet** per computer (model, serial, Windows build, disk, BitLocker, antivirus, last user…) shown inside every ticket about that device, with its other tickets. One ticket per device and rule for critical issues (or the levels you choose). |
| **NetScope** | a `scan-*.json` (or the newest in its folder) | Tickets for critical findings (Telnet open, SMBv1…) and for **devices seen for the first time**. |
| **ADFlow** | the `summary.json` of a live run | An **onboarding** ticket per account created (groups, mailbox, home folder and a checklist) and an **offboarding** ticket per account disabled. |

Imports are **idempotent and self-healing**:

- each finding has a stable key (`pcscope:OPS-PC04:diskSpace`), and the database refuses two *active* tickets with the same key (partial unique index), so re-running an import never duplicates anything;
- if the details change (*93 % full → 97 % full*) an internal note is added instead of a new ticket;
- when a later import no longer reports a problem on a device it inventoried, the ticket is **resolved automatically** with a note; a problem that comes back opens a new ticket, so the history stays honest;
- events that need a person (a new device on the network, an onboarding) are never closed automatically.

Tickets from tools are opened by a built-in `integrations` system account that can never sign in, and their priority comes from the same impact × urgency matrix (a disabled antivirus is more urgent than a full disk).

```bash
deskscope import pcscope  \\fileserver\pcscope\export --lang es
deskscope import netscope C:\NetScope\output
deskscope import adflow   C:\ADFlow\output\runs\20261117-080000-onboarding
```

### REST API

Every operation is also available at `/api/v1` (OpenAPI spec at `/api/openapi.json`, interactive docs at `/api/docs`), authenticated with **bearer tokens** that each person creates under *API tokens*:

- tokens are 256-bit random secrets shown **once**; only their SHA-256 is stored, with a short prefix to recognise them, the last time they were used and a revoke button;
- the API only accepts the `Authorization` header, never the session cookie, so it is not exposed to CSRF;
- it goes through the same domain functions as the web pages, with the token owner's permissions (an employee's token still only sees that employee's tickets);
- errors are JSON with a stable code (`{"error": "error.ticket_not_found", "message": "..."}`).

So PCScope's scheduled task can push its results straight to DeskScope:

```powershell
$headers = @{ Authorization = "Bearer $env:DESKSCOPE_TOKEN" }
$body = @{
    devices = @(Import-Csv .\output\export\devices.csv -Delimiter ';')
    issues  = @(Import-Csv .\output\export\issues.csv -Delimiter ';')
} | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post -Uri 'https://deskscope.example.com/api/v1/import/pcscope' `
    -Headers $headers -ContentType 'application/json; charset=utf-8' `
    -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

(`deskscope token create integrations --name "PCScope task"` prints a token for the system account.)

### Knowledge base and self-service

- **Articles written from solved tickets**: one click pre-fills the article with the problem and the desk's public answers (never the internal notes) and links it back to the ticket.
- **Self-service before the ticket**: while an employee types the summary of a new ticket, matching articles appear under it, so many problems never become tickets.
- **Full-text search** with SQLite FTS5: ranked with BM25, prefix matching and accent-insensitive (*impresion* finds *impresión*). Free text is turned into quoted terms, so nobody can inject FTS operators.
- **Audiences and drafts**: articles for everyone (self-service) or for the service desk only (internal procedures); drafts are only visible to technicians. Each published article counts its reads.
- Technicians see **related articles** on every ticket.

### Metrics, satisfaction and export

- **Satisfaction survey**: once a ticket is resolved its requester rates it from 1 to 5, with an optional comment; the rating shows on the ticket and in the history.
- **Metrics page** for the last 7, 30 or 90 days:
  - tickets opened, resolved and still open (with how many are at risk or breached);
  - mean and median **time to resolve** and mean **first response**, in business time, measured by the SLA engine (pauses excluded);
  - **% resolved on time** and **% answered on time**;
  - **satisfaction** average;
  - opened vs resolved per day (server-rendered SVG, no JavaScript chart library);
  - breakdowns by priority, category, technician, source and the devices with the most tickets.
- **CSV export** of the period's tickets with business durations and SLA states. It is UTF-8 with BOM so Excel shows accents, and every cell is protected against **CSV/formula injection** (`=`, `+`, `-`, `@`… are neutralised): ticket titles are typed by users.
- The same numbers at `GET /api/v1/reports/summary?days=30`.

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

deskscope demo --lang en        # fictitious company: 60 days of history, articles, imports
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
3. ✅ **Integrations**: PCScope device sheet inside the ticket, self-healing tickets from PCScope issues and NetScope findings, onboarding and offboarding tickets from ADFlow, REST API with tokens.
4. ✅ **Knowledge base and metrics**: articles from solved tickets, suggestions while typing, full-text search, satisfaction survey, MTTR and SLA compliance in business time, breakdowns, safe CSV export.
5. **Release**: tickets by email (IMAP), notifications by email and Telegram, Docker image, live demo and v1.0.0.

## Development

```bash
pip install -e ".[dev]"
ruff check .
python -m pytest
```

224 tests (domain rules, permissions, the business-hours calendar and SLA clocks, schema migrations, the three importers, tokens, the REST API, full-text search, metrics computed on a fixed calendar, CSV injection, web flows, security headers, translations, demo data and CLI) run on Windows and Linux with Python 3.10 and 3.13. CI also starts the real server and calls the API with a real token.

The fictitious exports used by the demo and the tests are in [`src/deskscope/examples`](src/deskscope/examples): a PCScope CSV export (culture-specific `;` delimiter and BOM, as PowerShell writes it), a NetScope scan and an ADFlow run.

The schema evolves through numbered SQL migrations (`src/deskscope/migrations/`), each applied in one transaction; upgrading a v0.1 database keeps every priority and backfills the first-response times.

```
src/deskscope/
  models.py     vocabulary, workflow rules, errors
  tickets.py    ticket rules (every function takes the acting user)
  sla.py        business calendar, SLA clocks, sla.json parsing
  integrations.py  PCScope / NetScope / ADFlow importers and reconciliation
  tokens.py     API tokens (hashed)
  api.py        REST API mounted at /api
  knowledge.py  knowledge base and FTS5 search
  reports.py    metrics and CSV export
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
