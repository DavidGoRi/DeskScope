"""Knowledge base: articles written from solved tickets, with full-text search.

Published articles for "everyone" are self-service help that employees see
(and are suggested while they type a new ticket); "staff" articles are
internal procedures. Drafts are only visible to the service desk.
"""

from __future__ import annotations

import re
import sqlite3

from . import tickets
from .db import utcnow
from .models import (
    CATEGORIES,
    TEXT_MAX,
    TITLE_MAX,
    NotFound,
    PermissionDenied,
    User,
    ValidationError,
)
from .tickets import _clean_text

AUDIENCES = ("everyone", "staff")
ARTICLE_STATUSES = ("draft", "published")
BODY_MAX = 20_000

_WORD_RE = re.compile(r"\w+", re.UNICODE)
# Words too common to help a search ("the printer does not print" -> printer, print).
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "cannot", "de", "del",
    "does", "el", "en", "es", "for", "from", "has", "have", "how", "i", "in", "is", "it", "la",
    "las", "los", "me", "mi", "my", "no", "not", "of", "on", "or", "our", "para", "pero", "por",
    "que", "se", "since", "sin", "su", "the", "this", "to", "un", "una", "we", "what", "when",
    "why", "with", "y", "ya",
})

_SELECT = """
    SELECT a.*, u.full_name AS author_name FROM articles a JOIN users u ON u.id = a.author_id
"""


def _visible_sql(actor: User) -> tuple[str, list]:
    if actor.is_staff:
        return "1 = 1", []
    return "a.status = 'published' AND a.audience = 'everyone'", []


def fts_query(text: str, mode: str = "all") -> str | None:
    """Turn free text into a safe FTS5 query: quoted prefix terms, no operators."""
    words = [w for w in _WORD_RE.findall(text.lower()) if len(w) > 1 and w not in STOPWORDS]
    if not words:
        return None
    terms = [f'"{w}"*' for w in dict.fromkeys(words)][:12]
    return (" OR " if mode == "any" else " AND ").join(terms)


def _validate(title: str, body: str, category: str, audience: str) -> tuple[str, str]:
    title = _clean_text(title, TITLE_MAX, "error.title_invalid")
    body = _clean_text(body, BODY_MAX, "error.description_invalid")
    if category not in CATEGORIES:
        raise ValidationError("error.category_invalid")
    if audience not in AUDIENCES:
        raise ValidationError("error.audience_invalid")
    return title, body


def create_article(
    conn: sqlite3.Connection,
    actor: User,
    title: str,
    body: str,
    category: str,
    audience: str = "everyone",
    publish: bool = False,
    source_ticket_id: int | None = None,
) -> int:
    if not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    title, body = _validate(title, body, category, audience)
    if source_ticket_id is not None:
        tickets.get_ticket(conn, actor, source_ticket_id)  # must exist
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO articles (title, body, category, audience, status, source_ticket_id,"
        " author_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (title, body, category, audience, "published" if publish else "draft",
         source_ticket_id, actor.id, now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_article(
    conn: sqlite3.Connection,
    actor: User,
    article_id: int,
    title: str,
    body: str,
    category: str,
    audience: str,
    publish: bool,
) -> None:
    if not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    get_article(conn, actor, article_id)
    title, body = _validate(title, body, category, audience)
    conn.execute(
        "UPDATE articles SET title = ?, body = ?, category = ?, audience = ?, status = ?,"
        " updated_at = ? WHERE id = ?",
        (title, body, category, audience, "published" if publish else "draft", utcnow(),
         article_id),
    )
    conn.commit()


def delete_article(conn: sqlite3.Connection, actor: User, article_id: int) -> None:
    if not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    get_article(conn, actor, article_id)
    conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    conn.commit()


def get_article(
    conn: sqlite3.Connection, actor: User, article_id: int, count_view: bool = False
) -> dict:
    visible, params = _visible_sql(actor)
    row = conn.execute(f"{_SELECT} WHERE a.id = ? AND {visible}", (article_id, *params)).fetchone()
    if row is None:
        raise NotFound("error.article_not_found")
    article = dict(row)
    if count_view and article["status"] == "published":
        conn.execute("UPDATE articles SET views = views + 1 WHERE id = ?", (article_id,))
        conn.commit()
        article["views"] += 1
    return article


def search(
    conn: sqlite3.Connection,
    actor: User,
    text: str = "",
    category: str | None = None,
    limit: int = 50,
    mode: str = "all",
    published_only: bool = False,
) -> list[dict]:
    """Articles the actor may read, best match first (or most viewed without text)."""
    visible, params = _visible_sql(actor)
    where = [visible]
    if published_only:
        where.append("a.status = 'published'")
    if category in CATEGORIES:
        where.append("a.category = ?")
        params.append(category)
    query = fts_query(text, mode) if text else None
    if text and query is None:
        return []
    if query:
        sql = (
            "SELECT a.*, u.full_name AS author_name, bm25(articles_fts, 5.0, 1.0) AS rank,"
            " snippet(articles_fts, 1, '[', ']', '…', 18) AS snippet"
            " FROM articles_fts JOIN articles a ON a.id = articles_fts.rowid"
            " JOIN users u ON u.id = a.author_id"
            f" WHERE articles_fts MATCH ? AND {' AND '.join(where)} ORDER BY rank LIMIT ?"
        )
        rows = conn.execute(sql, (query, *params, limit))
    else:
        sql = (
            f"{_SELECT} WHERE {' AND '.join(where)}"
            " ORDER BY a.views DESC, a.updated_at DESC LIMIT ?"
        )
        rows = conn.execute(sql, (*params, limit))
    return [dict(r) for r in rows]


def suggest(conn: sqlite3.Connection, actor: User, text: str, limit: int = 5) -> list[dict]:
    """Published articles that may answer what someone is typing (any word may match)."""
    if len(text.strip()) < 4:
        return []
    return search(conn, actor, text, limit=limit, mode="any", published_only=True)


def draft_from_ticket(conn: sqlite3.Connection, actor: User, ticket_id: int) -> dict:
    """Pre-filled article from a solved ticket: the problem and the public answers."""
    if not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    ticket = tickets.get_ticket(conn, actor, ticket_id)
    answers = [
        c["body"] for c in tickets.list_comments(conn, actor, ticket_id)
        if not c["internal"] and c["author_role"] != "requester"
    ]
    body = ticket["description"].strip()
    if answers:
        body += "\n\n---\n\n" + "\n\n".join(a.strip() for a in answers)
    return {
        "title": ticket["title"],
        "body": body[:TEXT_MAX],
        "category": ticket["category"],
        "audience": "everyone",
        "source_ticket_id": ticket_id,
    }
