"""Notifications by email and Telegram.

Nothing in the ticket code sends messages. The notifier reads what already
happened (the audit trail and the comments) since the last run, decides who
should hear about it, queues the messages in the ``outbox`` table and sends
them. That keeps web requests fast, survives an SMTP outage (messages are
retried) and never sends the same thing twice (``dedup_key`` is unique).

Who hears what:

* new ticket -> the service desk's Telegram chat;
* ticket assigned -> the technician, by email;
* public answer from the desk / ticket waiting for the user / ticket resolved
  -> the requester, by email (the resolution mail links to the survey);
* reply from the requester -> the assigned technician, by email;
* SLA at risk or breached -> Telegram and the assigned technician, once per
  ticket, clock and state.

Settings in a JSON file (``notifications.example.json``, which also holds the
IMAP mailbox read by :mod:`deskscope.mailin`); secrets only in environment
variables: ``DESKSCOPE_SMTP_PASSWORD``, ``DESKSCOPE_TELEGRAM_TOKEN`` and
``DESKSCOPE_IMAP_PASSWORD``.
"""

from __future__ import annotations

import json
import os
import smtplib
import sqlite3
import ssl
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from . import tickets, users
from .db import utcnow
from .i18n import translate
from .sla import SlaPolicy

SMTP_PASSWORD_ENV = "DESKSCOPE_SMTP_PASSWORD"
TELEGRAM_TOKEN_ENV = "DESKSCOPE_TELEGRAM_TOKEN"
MAX_ATTEMPTS = 5

DEFAULT_CONFIG: dict = {
    "base_url": "http://127.0.0.1:8000",
    "lang": "en",
    "email": {"enabled": False, "server": "", "port": 587, "security": "starttls",
              "user": "", "from": ""},
    "telegram": {"enabled": False, "chat_id": ""},
    "imap": {"enabled": False, "server": "", "port": 993, "user": "", "folder": "INBOX"},
    "sla_alerts": True,
}


class NotifyConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Message:
    channel: str  # email | telegram
    recipient: str  # email address or chat id
    subject: str | None
    body: str
    dedup_key: str


# ---- configuration -------------------------------------------------------------------


def load_config(path: str | Path | None) -> dict:
    """Settings from JSON merged over the defaults (everything off without a file)."""
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    if path is None or not Path(path).exists():
        return config
    try:
        data = json.loads(Path(path).read_text("utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise NotifyConfigError(f"{path}: invalid JSON ({exc})") from exc
    for key, value in data.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    if config["lang"] not in ("en", "es"):
        raise NotifyConfigError("lang must be 'en' or 'es'")
    if config["email"]["security"] not in ("starttls", "ssl", "none"):
        raise NotifyConfigError("email.security must be starttls, ssl or none")
    if config["email"]["enabled"] and not (config["email"]["server"] and config["email"]["from"]):
        raise NotifyConfigError("email.server and email.from are required when email is enabled")
    if config["telegram"]["enabled"] and not str(config["telegram"]["chat_id"]).strip():
        raise NotifyConfigError("telegram.chat_id is required when Telegram is enabled")
    if config["imap"]["enabled"] and not (config["imap"]["server"] and config["imap"]["user"]):
        raise NotifyConfigError("imap.server and imap.user are required when IMAP is enabled")
    config["base_url"] = str(config["base_url"]).rstrip("/")
    return config


def _setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ---- deciding who hears what ---------------------------------------------------------


def _t(config: dict, key: str, **params) -> str:
    return translate(config["lang"], key, **params)


def _link(config: dict, ticket_id: int) -> str:
    return f"{config['base_url']}/tickets/{ticket_id}"


def _subject(ticket: dict) -> str:
    # "[DeskScope #12]" lets the mail importer thread replies into the ticket.
    return f"[DeskScope #{ticket['id']}] {ticket['title']}"


def _email(config, to, ticket, key, kind, **params) -> Message | None:
    if not config["email"]["enabled"] or not to or to.endswith(".invalid"):
        return None
    body = _t(config, f"mail.{kind}", id=ticket["id"], title=ticket["title"], **params)
    body += "\n\n" + _link(config, ticket["id"]) + "\n\n" + _t(config, "mail.footer")
    return Message("email", to, _subject(ticket), body, key)


def _telegram(config, key, text) -> Message | None:
    if not config["telegram"]["enabled"]:
        return None
    return Message("telegram", str(config["telegram"]["chat_id"]), None, text, key)


def collect(
    conn: sqlite3.Connection, config: dict, policy: SlaPolicy, now: datetime | None = None
) -> list[Message]:
    """Messages for everything that happened since the last run (cursors in ``settings``)."""
    now = now or datetime.now(timezone.utc)
    bot = users.system_user(conn)
    last_event = _setting(conn, "notify.event_id")
    last_comment = _setting(conn, "notify.comment_id")
    max_event = conn.execute("SELECT COALESCE(MAX(id), 0) FROM ticket_events").fetchone()[0]
    max_comment = conn.execute("SELECT COALESCE(MAX(id), 0) FROM comments").fetchone()[0]
    messages: list[Message | None] = []

    # First run: start from now instead of announcing the whole history.
    if last_event is None or last_comment is None:
        _set_setting(conn, "notify.event_id", str(max_event))
        _set_setting(conn, "notify.comment_id", str(max_comment))
        conn.commit()
        last_event, last_comment = max_event, max_comment

    cache: dict[int, dict] = {}

    def ticket(tid: int) -> dict:
        if tid not in cache:
            cache[tid] = tickets.get_ticket(conn, bot, tid)
        return cache[tid]

    def person(uid: int | None):
        return users.get_user(conn, uid) if uid else None

    events = conn.execute(
        "SELECT * FROM ticket_events WHERE id > ? AND id <= ? ORDER BY id",
        (int(last_event), max_event),
    ).fetchall()
    for event in events:
        t = ticket(event["ticket_id"])
        actor_id = event["actor_id"]
        requester, assignee = person(t["requester_id"]), person(t["assignee_id"])
        key = f"event:{event['id']}"
        if event["field"] == "created":
            who = person(actor_id)
            messages.append(_telegram(config, key, _t(
                config, "notify.new_ticket", id=t["id"],
                priority=_t(config, f"priority.{t['priority']}"), title=t["title"],
                who=who.full_name if who else "?", link=_link(config, t["id"]))))
        elif event["field"] == "assignee" and assignee and assignee.id != actor_id:
            messages.append(_email(config, assignee.email, t, key, "assigned"))
        elif event["field"] == "status" and requester and requester.id != actor_id:
            if event["new_value"] == "resolved":
                messages.append(_email(config, requester.email, t, key, "resolved"))
            elif event["new_value"] == "pending":
                messages.append(_email(config, requester.email, t, key, "pending"))

    comments = conn.execute(
        "SELECT c.*, u.role AS author_role, u.full_name AS author_name FROM comments c"
        " JOIN users u ON u.id = c.author_id WHERE c.id > ? AND c.id <= ? AND c.internal = 0"
        " ORDER BY c.id",
        (int(last_comment), max_comment),
    ).fetchall()
    for comment in comments:
        t = ticket(comment["ticket_id"])
        key = f"comment:{comment['id']}"
        excerpt = comment["body"][:1500]
        if comment["author_id"] == t["requester_id"]:
            assignee = person(t["assignee_id"])
            if assignee:
                messages.append(_email(config, assignee.email, t, key, "reply",
                                       name=comment["author_name"], text=excerpt))
        elif comment["author_role"] != "requester":
            requester = person(t["requester_id"])
            if requester:
                messages.append(_email(config, requester.email, t, key, "answer",
                                       name=comment["author_name"], text=excerpt))

    if config.get("sla_alerts", True):
        active = tickets.list_tickets(conn, bot, tickets.TicketFilter())
        for t in tickets.attach_sla(conn, policy, active, now):
            for clock in (t["sla"].response, t["sla"].resolution):
                if clock.finished or clock.state not in ("at_risk", "breached"):
                    continue
                key = f"sla:{t['id']}:{clock.name}:{clock.state}"
                text = _t(config, f"notify.sla_{clock.state}", id=t["id"], title=t["title"],
                          clock=_t(config, f"sla.{clock.name}"), link=_link(config, t["id"]))
                messages.append(_telegram(config, key + ":tg", text))
                assignee = person(t["assignee_id"])
                if assignee:
                    messages.append(_email(config, assignee.email, t, key + ":mail",
                                           f"sla_{clock.state}",
                                           clock=_t(config, f"sla.{clock.name}")))

    _set_setting(conn, "notify.event_id", str(max_event))
    _set_setting(conn, "notify.comment_id", str(max_comment))
    conn.commit()
    return [m for m in messages if m is not None]


def enqueue(conn: sqlite3.Connection, messages: list[Message]) -> int:
    """Queue messages; ones already queued (same dedup_key) are ignored."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO outbox (channel, recipient, subject, body, dedup_key, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(m.channel, m.recipient, m.subject, m.body, m.dedup_key, utcnow()) for m in messages],
    )
    conn.commit()
    return conn.total_changes - before


# ---- sending ---------------------------------------------------------------------------


Sender = Callable[[dict, sqlite3.Row], None]


def send_email(config: dict, row) -> None:
    cfg = config["email"]
    message = EmailMessage()
    message["From"] = cfg["from"]
    message["To"] = row["recipient"]
    message["Subject"] = row["subject"] or "DeskScope"
    message["Auto-Submitted"] = "auto-generated"  # stops auto-replies looping back
    message.set_content(row["body"])
    password = os.environ.get(SMTP_PASSWORD_ENV, "")
    context = ssl.create_default_context()
    if cfg["security"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["server"], int(cfg["port"]), context=context, timeout=20)
    else:
        server = smtplib.SMTP(cfg["server"], int(cfg["port"]), timeout=20)
    with server:
        if cfg["security"] == "starttls":
            server.starttls(context=context)
        if cfg.get("user"):
            server.login(cfg["user"], password)
        server.send_message(message)


def send_telegram(config: dict, row) -> None:
    token = os.environ.get(TELEGRAM_TOKEN_ENV)
    if not token:
        raise RuntimeError(f"{TELEGRAM_TOKEN_ENV} is not set")
    data = urllib.parse.urlencode({
        "chat_id": row["recipient"], "text": row["body"], "disable_web_page_preview": "true",
    }).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST"
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 (fixed host)
        if response.status != 200:
            raise RuntimeError(f"Telegram answered HTTP {response.status}")


SENDERS: dict[str, Sender] = {"email": send_email, "telegram": send_telegram}


def send_pending(
    conn: sqlite3.Connection, config: dict, senders: dict[str, Sender] | None = None
) -> dict[str, int]:
    """Send queued messages; failures are retried on later runs up to MAX_ATTEMPTS."""
    senders = senders or SENDERS
    counts = {"sent": 0, "failed": 0}
    rows = conn.execute(
        "SELECT * FROM outbox WHERE sent_at IS NULL AND attempts < ? ORDER BY id",
        (MAX_ATTEMPTS,),
    ).fetchall()
    for row in rows:
        try:
            senders[row["channel"]](config, row)
        except Exception as exc:  # noqa: BLE001 - any delivery error is recorded and retried
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE id = ?",
                (f"{type(exc).__name__}: {exc}"[:500], row["id"]),
            )
            counts["failed"] += 1
        else:
            conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, sent_at = ?, last_error = NULL"
                " WHERE id = ?",
                (utcnow(), row["id"]),
            )
            counts["sent"] += 1
        conn.commit()
    return counts


def run(
    conn: sqlite3.Connection,
    config: dict,
    policy: SlaPolicy,
    senders: dict[str, Sender] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One notifier pass: collect, queue and send."""
    queued = enqueue(conn, collect(conn, config, policy, now))
    return {"queued": queued, **send_pending(conn, config, senders)}
