"""Tickets by email (IMAP).

* A message from a known, active user opens a ticket (source "email").
* A reply whose subject contains "[DeskScope #12]" (as in every notification)
  becomes a comment on ticket 12, with the quoted text removed. The usual
  permissions apply: an employee can only answer their own tickets.
* Messages from unknown senders, automatic replies and bulk mail are ignored,
  which also prevents mail loops with out-of-office replies.
* Every processed Message-ID is remembered, so a message is never imported
  twice even if it is not marked as read.

The IMAP password comes from ``DESKSCOPE_IMAP_PASSWORD``.
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import html
import imaplib
import os
import re
import sqlite3
from email.message import EmailMessage
from email.utils import parseaddr

from . import tickets
from .db import utcnow
from .models import DeskError, User
from .users import _to_user

IMAP_PASSWORD_ENV = "DESKSCOPE_IMAP_PASSWORD"
TICKET_REF_RE = re.compile(r"\[(?:DeskScope\s*)?#(\d+)\]", re.IGNORECASE)
REPLY_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd|rv|reenv)\s*:\s*)+", re.IGNORECASE)
# Lines where a quoted earlier message starts, in English and Spanish clients.
QUOTE_START_RE = re.compile(
    r"^(on .+ wrote:|el .+ escribi[oó]:|-{2,}\s*original message\s*-{2,}|"
    r"-{2,}\s*mensaje original\s*-{2,}|from:\s|de:\s|_{10,})",
    re.IGNORECASE,
)


def _sender(conn: sqlite3.Connection, address: str) -> User | None:
    row = conn.execute(
        "SELECT * FROM users WHERE lower(email) = ? AND active = 1 AND is_system = 0"
        " ORDER BY id LIMIT 1",
        (address.lower(),),
    ).fetchone()
    return _to_user(row) if row else None


def _is_automatic(msg: EmailMessage) -> bool:
    auto = (msg.get("Auto-Submitted") or "no").strip().lower()
    precedence = (msg.get("Precedence") or "").strip().lower()
    return (
        auto != "no"
        or precedence in ("bulk", "junk", "list", "auto_reply")
        or msg.get("X-Autoreply") is not None
        or msg.get("X-Autorespond") is not None
    )


def body_text(msg: EmailMessage) -> str:
    """The plain-text body (or the HTML one converted to text)."""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return text.replace("\r\n", "\n")


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply."""
    kept = []
    for line in text.split("\n"):
        if line.lstrip().startswith(">") or QUOTE_START_RE.match(line.strip()):
            break
        kept.append(line.rstrip())
    return "\n".join(kept).strip()


def clean_subject(subject: str) -> str:
    subject = TICKET_REF_RE.sub("", subject or "")
    subject = REPLY_PREFIX_RE.sub("", subject)
    return " ".join(subject.split())


def process_message(conn: sqlite3.Connection, raw: bytes) -> tuple[str, int | None]:
    """Turn one email into a ticket or a comment. Returns (outcome, ticket id)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = (msg.get("Message-ID") or "").strip()
    if message_id and conn.execute(
        "SELECT 1 FROM mail_seen WHERE message_id = ?", (message_id,)
    ).fetchone():
        return "duplicate", None

    def remember(outcome: str, ticket_id: int | None = None) -> tuple[str, int | None]:
        if message_id:
            conn.execute(
                "INSERT OR IGNORE INTO mail_seen (message_id, ticket_id, outcome, received_at)"
                " VALUES (?, ?, ?, ?)",
                (message_id, ticket_id, outcome, utcnow()),
            )
            conn.commit()
        return outcome, ticket_id

    if _is_automatic(msg):
        return remember("ignored_automatic")
    user = _sender(conn, parseaddr(msg.get("From", ""))[1])
    if user is None:
        return remember("ignored_unknown_sender")

    subject = msg.get("Subject", "")
    text = body_text(msg)
    reference = TICKET_REF_RE.search(subject)
    try:
        if reference:
            ticket_id = int(reference.group(1))
            reply = strip_quoted(text) or "(empty reply)"
            tickets.add_comment(conn, user, ticket_id, reply[:5000])
            return remember("commented", ticket_id)
        title = clean_subject(subject)[:120] or text.strip().split("\n")[0][:120] or "(no subject)"
        description = text.strip()[:5000] or title
        ticket_id = tickets.create_ticket(
            conn, user, title, description, "other", "low", "medium", source="email"
        )
        return remember("created", ticket_id)
    except DeskError as exc:
        # Not their ticket, closed ticket, empty text...: never fail the whole batch.
        return remember(f"rejected:{exc.key}")


def fetch(conn: sqlite3.Connection, config: dict, imap_factory=None) -> dict[str, int]:
    """Read unseen messages from the configured IMAP folder and process them."""
    cfg = config["imap"]
    factory = imap_factory or (lambda: imaplib.IMAP4_SSL(cfg["server"], int(cfg["port"])))
    counts: dict[str, int] = {}
    client = factory()
    try:
        client.login(cfg["user"], os.environ.get(IMAP_PASSWORD_ENV, ""))
        client.select(cfg.get("folder", "INBOX"))
        status, data = client.search(None, "UNSEEN")
        if status != "OK":
            raise RuntimeError(f"IMAP search failed: {status}")
        for number in (data[0] or b"").split():
            status, parts = client.fetch(number, "(RFC822)")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                counts["errors"] = counts.get("errors", 0) + 1
                continue
            outcome, _ = process_message(conn, parts[0][1])
            outcome = outcome.split(":")[0]
            counts[outcome] = counts.get(outcome, 0) + 1
            client.store(number, "+FLAGS", "\\Seen")
    finally:
        with contextlib.suppress(Exception):  # closing a broken connection
            client.logout()
    return counts
