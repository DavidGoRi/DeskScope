"""Tickets: creation, visibility, workflow, comments and audit trail.

Every function takes the acting user and enforces permissions itself, so the
web layer (and the API and integrations that come later) cannot skip a check.
A ticket the user may not see is reported as *not found*, not *forbidden*,
so requesters cannot probe which ticket numbers exist.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from . import sla
from .db import utcnow
from .models import (
    CATEGORIES,
    DEVICE_MAX,
    LEVELS,
    STATUSES,
    TEXT_MAX,
    TITLE_MAX,
    NotFound,
    PermissionDenied,
    User,
    ValidationError,
    allowed_transitions,
    priority_for,
)

_TICKET_SELECT = """
    SELECT t.*, r.full_name AS requester_name, a.full_name AS assignee_name
    FROM tickets t
    JOIN users r ON r.id = t.requester_id
    LEFT JOIN users a ON a.id = t.assignee_id
"""

SCOPES = ("all", "mine", "assigned", "unassigned")
SORTS = {
    "updated": "t.updated_at DESC, t.id DESC",
    "created": "t.created_at DESC, t.id DESC",
    "priority": "CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
    " WHEN 'medium' THEN 2 ELSE 3 END, t.updated_at DESC",
    "sla": "t.created_at",  # re-sorted by due date once the SLA is known
}


@dataclass(frozen=True)
class TicketFilter:
    status: str = "active"  # "active" = new/open/pending, "any", or one status
    scope: str = "all"
    query: str = ""
    sort: str = "updated"
    sla_problems: bool = False  # only tickets whose SLA is at risk or breached


def _clean_text(value: str, maximum: int, key: str) -> str:
    value = (value or "").strip()
    if not value or len(value) > maximum:
        raise ValidationError(key, max=maximum)
    return value


def _clean_device(device: str | None) -> str | None:
    device = (device or "").strip()
    if not device:
        return None
    if len(device) > DEVICE_MAX or any(c.isspace() for c in device):
        raise ValidationError("error.device_invalid", max=DEVICE_MAX)
    return device.upper()


def _log(conn, ticket_id, actor_id, field, old, new, at) -> None:
    conn.execute(
        "INSERT INTO ticket_events (ticket_id, actor_id, field, old_value, new_value, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ticket_id, actor_id, field, old, new, at),
    )


def _row(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row:
    row = conn.execute(_TICKET_SELECT + " WHERE t.id = ?", (ticket_id,)).fetchone()
    if row is None:
        raise NotFound("error.ticket_not_found")
    return row


def can_view(actor: User, ticket: sqlite3.Row | dict) -> bool:
    return actor.is_staff or ticket["requester_id"] == actor.id


def get_ticket(conn: sqlite3.Connection, actor: User, ticket_id: int) -> dict:
    row = _row(conn, ticket_id)
    if not can_view(actor, row):
        raise NotFound("error.ticket_not_found")
    ticket = dict(row)
    ticket["transitions"] = allowed_transitions(
        actor, ticket["status"], ticket["requester_id"] == actor.id
    )
    return ticket


def create_ticket(
    conn: sqlite3.Connection,
    actor: User,
    title: str,
    description: str,
    category: str,
    impact: str = "medium",
    urgency: str = "medium",
    device: str | None = None,
) -> int:
    """Open a ticket. The priority comes from the ITIL impact x urgency matrix."""
    title = _clean_text(title, TITLE_MAX, "error.title_invalid")
    description = _clean_text(description, TEXT_MAX, "error.description_invalid")
    if category not in CATEGORIES:
        raise ValidationError("error.category_invalid")
    if impact not in LEVELS or urgency not in LEVELS:
        raise ValidationError("error.classification_invalid")
    priority = priority_for(impact, urgency)
    device = _clean_device(device)
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO tickets (title, description, category, impact, urgency, priority, status,"
        " requester_id, device, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)",
        (title, description, category, impact, urgency, priority, actor.id, device, now, now),
    )
    _log(conn, cur.lastrowid, actor.id, "created", None, "new", now)
    conn.commit()
    return cur.lastrowid


def list_tickets(
    conn: sqlite3.Connection, actor: User, flt: TicketFilter | None = None
) -> list[dict]:
    flt = flt or TicketFilter()
    where: list[str] = []
    params: list[object] = []

    # Requesters only ever see their own tickets, whatever scope they ask for.
    if not actor.is_staff or flt.scope == "mine":
        where.append("t.requester_id = ?")
        params.append(actor.id)
    elif flt.scope == "assigned":
        where.append("t.assignee_id = ?")
        params.append(actor.id)
    elif flt.scope == "unassigned":
        where.append("t.assignee_id IS NULL")

    if flt.status == "active":
        where.append("t.status IN ('new', 'open', 'pending')")
    elif flt.status in STATUSES:
        where.append("t.status = ?")
        params.append(flt.status)

    query = flt.query.strip()
    if query:
        if query.lstrip("#").isdigit():
            where.append("t.id = ?")
            params.append(int(query.lstrip("#")))
        else:
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(t.title LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\'"
                " OR t.device LIKE ? ESCAPE '\\')"
            )
            params += [like, like, like]

    sql = _TICKET_SELECT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + SORTS.get(flt.sort, SORTS["updated"])
    return [dict(r) for r in conn.execute(sql, params)]


def status_counts(conn: sqlite3.Connection, actor: User) -> dict[str, int]:
    sql = "SELECT status, COUNT(*) FROM tickets"
    params: tuple = ()
    if not actor.is_staff:
        sql += " WHERE requester_id = ?"
        params = (actor.id,)
    counts = dict.fromkeys(STATUSES, 0)
    counts.update({s: n for s, n in conn.execute(sql + " GROUP BY status", params)})
    return counts


def change_status(conn: sqlite3.Connection, actor: User, ticket_id: int, new_status: str) -> None:
    ticket = get_ticket(conn, actor, ticket_id)
    old = ticket["status"]
    if new_status == old:
        return
    if new_status not in ticket["transitions"]:
        if new_status not in STATUSES:
            raise ValidationError("error.status_invalid")
        raise PermissionDenied("error.transition_not_allowed", old=old, new=new_status)
    now = utcnow()
    resolved_at = ticket["resolved_at"]
    closed_at = ticket["closed_at"]
    if new_status == "resolved":
        resolved_at = now
    elif new_status == "closed":
        closed_at = now
        resolved_at = resolved_at or now
    else:  # back to work: the previous resolution did not hold
        resolved_at = None
    # A fix (or a withdrawn ticket) also answers the user: it stops the response clock.
    first_response = ticket["first_response_at"]
    if new_status in ("resolved", "closed") and not first_response:
        first_response = now
    conn.execute(
        "UPDATE tickets SET status = ?, resolved_at = ?, closed_at = ?, first_response_at = ?,"
        " updated_at = ? WHERE id = ?",
        (new_status, resolved_at, closed_at, first_response, now, ticket_id),
    )
    _log(conn, ticket_id, actor.id, "status", old, new_status, now)
    conn.commit()


def assign(
    conn: sqlite3.Connection, actor: User, ticket_id: int, assignee_id: int | None
) -> None:
    if not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    ticket = get_ticket(conn, actor, ticket_id)
    if ticket["status"] == "closed":
        raise ValidationError("error.ticket_closed")
    new_name = None
    if assignee_id is not None:
        row = conn.execute(
            "SELECT full_name FROM users"
            " WHERE id = ? AND role IN ('agent', 'admin') AND active = 1",
            (assignee_id,),
        ).fetchone()
        if row is None:
            raise ValidationError("error.assignee_invalid")
        new_name = row["full_name"]
    if assignee_id == ticket["assignee_id"]:
        return
    now = utcnow()
    conn.execute(
        "UPDATE tickets SET assignee_id = ?, updated_at = ? WHERE id = ?",
        (assignee_id, now, ticket_id),
    )
    _log(conn, ticket_id, actor.id, "assignee", ticket["assignee_name"], new_name, now)
    # Picking up a new ticket starts work on it.
    if assignee_id is not None and ticket["status"] == "new":
        conn.execute("UPDATE tickets SET status = 'open' WHERE id = ?", (ticket_id,))
        _log(conn, ticket_id, actor.id, "status", "new", "open", now)
    conn.commit()


def set_classification(
    conn: sqlite3.Connection, actor: User, ticket_id: int, impact: str, urgency: str
) -> None:
    """Triage: the service desk corrects impact and urgency; the priority follows."""
    if not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    if impact not in LEVELS or urgency not in LEVELS:
        raise ValidationError("error.classification_invalid")
    ticket = get_ticket(conn, actor, ticket_id)
    if ticket["status"] == "closed":
        raise ValidationError("error.ticket_closed")
    priority = priority_for(impact, urgency)
    changes = [
        (field, ticket[field], value)
        for field, value in (("impact", impact), ("urgency", urgency), ("priority", priority))
        if ticket[field] != value
    ]
    if not changes:
        return
    now = utcnow()
    conn.execute(
        "UPDATE tickets SET impact = ?, urgency = ?, priority = ?, updated_at = ? WHERE id = ?",
        (impact, urgency, priority, now, ticket_id),
    )
    for field, old, new in changes:
        _log(conn, ticket_id, actor.id, field, old, new, now)
    conn.commit()


def add_comment(
    conn: sqlite3.Connection, actor: User, ticket_id: int, body: str, internal: bool = False
) -> int:
    ticket = get_ticket(conn, actor, ticket_id)
    body = _clean_text(body, TEXT_MAX, "error.comment_invalid")
    if internal and not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    if ticket["status"] == "closed":
        raise ValidationError("error.ticket_closed")
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO comments (ticket_id, author_id, body, internal, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (ticket_id, actor.id, body, int(internal), now),
    )
    conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
    if actor.is_staff and not internal and not ticket["first_response_at"]:
        conn.execute("UPDATE tickets SET first_response_at = ? WHERE id = ?", (now, ticket_id))
    # The requester answering a ticket that waits on them puts it back in the queue.
    if ticket["status"] == "pending" and ticket["requester_id"] == actor.id and not internal:
        conn.execute("UPDATE tickets SET status = 'open' WHERE id = ?", (ticket_id,))
        _log(conn, ticket_id, actor.id, "status", "pending", "open", now)
    conn.commit()
    return cur.lastrowid


def list_comments(conn: sqlite3.Connection, actor: User, ticket_id: int) -> list[dict]:
    get_ticket(conn, actor, ticket_id)
    sql = (
        "SELECT c.*, u.full_name AS author_name, u.role AS author_role FROM comments c"
        " JOIN users u ON u.id = c.author_id WHERE c.ticket_id = ?"
    )
    if not actor.is_staff:
        sql += " AND c.internal = 0"
    return [dict(r) for r in conn.execute(sql + " ORDER BY c.created_at, c.id", (ticket_id,))]


def list_events(conn: sqlite3.Connection, actor: User, ticket_id: int) -> list[dict]:
    get_ticket(conn, actor, ticket_id)
    rows = conn.execute(
        "SELECT e.*, u.full_name AS actor_name FROM ticket_events e"
        " LEFT JOIN users u ON u.id = e.actor_id WHERE e.ticket_id = ?"
        " ORDER BY e.created_at, e.id",
        (ticket_id,),
    )
    return [dict(r) for r in rows]


# ---- SLA ----------------------------------------------------------------------


def status_history(conn: sqlite3.Connection, ticket_ids: list[int]) -> dict[int, list[tuple]]:
    """(at, old, new) status changes per ticket, oldest first, in one query."""
    history: dict[int, list[tuple]] = defaultdict(list)
    if not ticket_ids:
        return history
    marks = ",".join("?" * len(ticket_ids))
    rows = conn.execute(
        "SELECT ticket_id, created_at, old_value, new_value FROM ticket_events"
        f" WHERE field = 'status' AND ticket_id IN ({marks}) ORDER BY created_at, id",
        ticket_ids,
    )
    for row in rows:
        history[row["ticket_id"]].append((row["created_at"], row["old_value"], row["new_value"]))
    return history


def attach_sla(
    conn: sqlite3.Connection,
    policy: sla.SlaPolicy,
    items: list[dict],
    now: datetime | None = None,
) -> list[dict]:
    """Add ``item["sla"]`` (a :class:`sla.TicketSla`) to each ticket."""
    now = now or datetime.now(timezone.utc)
    history = status_history(conn, [t["id"] for t in items])
    for item in items:
        item["sla"] = sla.evaluate(policy, item, history.get(item["id"], []), now)
    return items


def list_with_sla(
    conn: sqlite3.Connection,
    actor: User,
    flt: TicketFilter,
    policy: sla.SlaPolicy,
    now: datetime | None = None,
) -> list[dict]:
    """Ticket list with SLA clocks, the SLA filter and the "due first" order applied."""
    items = attach_sla(conn, policy, list_tickets(conn, actor, flt), now)
    if flt.sla_problems:
        items = [
            t for t in items
            if t["status"] in ("new", "open", "pending") and t["sla"].worst.is_problem
        ]
    if flt.sort == "sla":
        order = {"breached": 0, "at_risk": 1, "running": 2, "paused": 3, "met": 4}
        items.sort(
            key=lambda t: (t["sla"].worst.finished, order[t["sla"].worst.state],
                           t["sla"].worst.due)
        )
    return items


def sla_summary(items: list[dict]) -> dict[str, int]:
    """How many active tickets have an SLA clock at risk or breached."""
    summary = {"at_risk": 0, "breached": 0}
    for t in items:
        if t["status"] in ("new", "open", "pending"):
            state = t["sla"].worst.state
            if state in summary:
                summary[state] += 1
    return summary
