"""Service desk metrics and the CSV export.

Durations are business time, measured by the same SLA engine that runs the
clocks (so "time to resolve" excludes nights, weekends, holidays and the
time a ticket waited for the user).
"""

from __future__ import annotations

import csv
import io
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import tickets
from .models import CATEGORIES, PRIORITIES, PermissionDenied, User
from .sla import SlaPolicy, parse_ts

PERIODS = (7, 30, 90)

EXPORT_COLUMNS = (
    "id", "created_at", "status", "priority", "impact", "urgency", "category", "source", "device",
    "requester", "assignee", "title", "first_response_at", "resolved_at", "closed_at",
    "response_business_minutes", "resolution_business_minutes", "sla_response", "sla_resolution",
    "satisfaction",
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _pct(part: int, total: int) -> float | None:
    return round(100.0 * part / total, 1) if total else None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _tickets_where(conn: sqlite3.Connection, where: str, params: tuple) -> list[dict]:
    rows = conn.execute(tickets._TICKET_SELECT + f" WHERE {where}", params)
    return [dict(r) for r in rows]


def build_report(
    conn: sqlite3.Connection,
    actor: User,
    policy: SlaPolicy,
    days: int = 30,
    now: datetime | None = None,
) -> dict:
    """Everything the metrics page and the API show for the last ``days`` days."""
    if not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    since_iso, now_iso = _iso(since), _iso(now)

    opened = _tickets_where(conn, "t.created_at >= ? AND t.created_at <= ?", (since_iso, now_iso))
    resolved = _tickets_where(
        conn, "t.resolved_at IS NOT NULL AND t.resolved_at >= ? AND t.resolved_at <= ?",
        (since_iso, now_iso),
    )
    backlog = _tickets_where(conn, "t.status IN ('new', 'open', 'pending')", ())
    tickets.attach_sla(conn, policy, opened, now)
    tickets.attach_sla(conn, policy, resolved, now)
    tickets.attach_sla(conn, policy, backlog, now)

    resolution_times = [t["sla"].resolution.used_seconds for t in resolved]
    responded = [t for t in opened if t["sla"].response.finished]
    response_times = [t["sla"].response.used_seconds for t in responded]
    ratings = [t["satisfaction"] for t in resolved if t["satisfaction"]]

    def sla_met(items: list[dict], clock: str) -> float | None:
        done = [getattr(t["sla"], clock) for t in items]
        done = [c for c in done if c.finished]
        return _pct(sum(c.state == "met" for c in done), len(done))

    # Per category (opened in the period) and per priority (resolved in the period).
    by_category = []
    for category in CATEGORIES:
        mine = [t for t in opened if t["category"] == category]
        solved = [t for t in resolved if t["category"] == category]
        by_category.append({
            "category": category,
            "opened": len(mine),
            "resolved": len(solved),
            "mean_resolution": _mean([t["sla"].resolution.used_seconds for t in solved]),
        })
    by_priority = []
    for priority in PRIORITIES:
        solved = [t for t in resolved if t["priority"] == priority]
        by_priority.append({
            "priority": priority,
            "opened": sum(t["priority"] == priority for t in opened),
            "resolved": len(solved),
            "sla_resolution": sla_met(solved, "resolution"),
            "mean_resolution": _mean([t["sla"].resolution.used_seconds for t in solved]),
        })

    agents: dict[str, list[dict]] = defaultdict(list)
    for t in resolved:
        agents[t["assignee_name"] or "—"].append(t)
    by_agent = sorted(
        (
            {
                "name": name,
                "resolved": len(items),
                "mean_resolution": _mean([t["sla"].resolution.used_seconds for t in items]),
                "sla_resolution": sla_met(items, "resolution"),
                "satisfaction": _mean([t["satisfaction"] for t in items if t["satisfaction"]]),
            }
            for name, items in agents.items()
        ),
        key=lambda a: -a["resolved"],
    )

    devices = Counter(t["device"] for t in opened if t["device"])
    sources = Counter(t["source"] for t in opened)

    # Opened and resolved per day, in the desk's time zone.
    tz = policy.calendar.tz
    first_day = since.astimezone(tz).date() + timedelta(days=1)
    day_list = [first_day + timedelta(days=i) for i in range(days)]
    opened_per_day = Counter(parse_ts(t["created_at"]).astimezone(tz).date() for t in opened)
    resolved_per_day = Counter(parse_ts(t["resolved_at"]).astimezone(tz).date() for t in resolved)
    daily = [
        {"day": d.isoformat(), "opened": opened_per_day.get(d, 0),
         "resolved": resolved_per_day.get(d, 0)}
        for d in day_list
    ]

    return {
        "days": days,
        "since": since_iso,
        "until": now_iso,
        "opened": len(opened),
        "resolved": len(resolved),
        "backlog": len(backlog),
        "backlog_at_risk": sum(t["sla"].worst.state == "at_risk" for t in backlog),
        "backlog_breached": sum(t["sla"].worst.state == "breached" for t in backlog),
        "mean_resolution": _mean(resolution_times),
        "median_resolution": _median(resolution_times),
        "mean_response": _mean(response_times),
        "sla_response": sla_met(responded, "response"),
        "sla_resolution": sla_met(resolved, "resolution"),
        "satisfaction": round(_mean(ratings), 2) if ratings else None,
        "satisfaction_count": len(ratings),
        "satisfaction_distribution": {n: ratings.count(n) for n in range(1, 6)},
        "by_category": by_category,
        "by_priority": by_priority,
        "by_agent": by_agent,
        "by_source": dict(sources.most_common()),
        "top_devices": devices.most_common(5),
        "daily": daily,
    }


# ---- CSV export ------------------------------------------------------------------


def safe_cell(value: object) -> str:
    """Neutralise spreadsheet formulas (CSV injection): =, +, -, @, tab and CR."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def export_rows(
    conn: sqlite3.Connection,
    actor: User,
    policy: SlaPolicy,
    days: int = 30,
    now: datetime | None = None,
) -> list[dict]:
    if not actor.is_staff:
        raise PermissionDenied("error.forbidden")
    now = now or datetime.now(timezone.utc)
    items = _tickets_where(
        conn, "t.created_at >= ? ORDER BY t.id", (_iso(now - timedelta(days=days)),)
    )
    tickets.attach_sla(conn, policy, items, now)
    rows = []
    for t in items:
        response, resolution = t["sla"].response, t["sla"].resolution
        rows.append({
            "id": t["id"],
            "created_at": t["created_at"],
            "status": t["status"],
            "priority": t["priority"],
            "impact": t["impact"],
            "urgency": t["urgency"],
            "category": t["category"],
            "source": t["source"],
            "device": t["device"],
            "requester": t["requester_name"],
            "assignee": t["assignee_name"],
            "title": t["title"],
            "first_response_at": t["first_response_at"],
            "resolved_at": t["resolved_at"],
            "closed_at": t["closed_at"],
            "response_business_minutes": round(response.used_seconds / 60)
            if response.finished else None,
            "resolution_business_minutes": round(resolution.used_seconds / 60)
            if resolution.finished else None,
            "sla_response": response.state,
            "sla_resolution": resolution.state,
            "satisfaction": t["satisfaction"],
        })
    return rows


def to_csv(rows: list[dict]) -> str:
    """UTF-8 CSV with BOM so Excel opens accents correctly; every cell made safe."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([safe_cell(row.get(col)) for col in EXPORT_COLUMNS])
    return "﻿" + out.getvalue()
