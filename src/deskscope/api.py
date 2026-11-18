"""REST API (``/api/v1``), authenticated with bearer tokens.

The API only accepts ``Authorization: Bearer dsk_...`` (never the session
cookie), so it needs no CSRF protection. Every call goes through the same
domain functions as the web pages, with the token owner's permissions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, integrations, knowledge, reports, tickets, tokens
from .db import connect
from .i18n import translate
from .models import DeskError, NotFound, PermissionDenied, User, ValidationError
from .sla import Clock, SlaPolicy

Level = Literal["high", "medium", "low"]


class TicketIn(BaseModel):
    title: str = Field(examples=["Printer on floor 2 prints blank pages"])
    description: str
    category: Literal["hardware", "software", "network", "access", "other"] = "other"
    impact: Level = "low"
    urgency: Level = "medium"
    device: str | None = Field(default=None, examples=["FIN-LT03"])


class CommentIn(BaseModel):
    body: str
    internal: bool = False


class StatusIn(BaseModel):
    status: Literal["new", "open", "pending", "resolved", "closed"]


class AssignIn(BaseModel):
    assignee_id: int | None


class ClassifyIn(BaseModel):
    impact: Level
    urgency: Level


class RatingIn(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str = ""


class PCScopeImport(BaseModel):
    devices: list[dict] = Field(description="Rows of PCScope's devices.csv")
    issues: list[dict] = Field(default=[], description="Rows of PCScope's issues.csv")
    levels: list[Literal["critical", "warning", "info"]] = ["critical"]


class _Unauthorized(Exception):
    pass


def _clock(clock: Clock) -> dict:
    return {
        "state": clock.state,
        "due": clock.due.astimezone(timezone.utc).isoformat(),
        "done_at": clock.done_at.astimezone(timezone.utc).isoformat() if clock.done_at else None,
        "remaining_seconds": round(clock.remaining_seconds),
    }


TICKET_FIELDS = (
    "id", "title", "description", "category", "impact", "urgency", "priority", "status",
    "device", "source", "requester_id", "requester_name", "assignee_id", "assignee_name",
    "created_at", "updated_at", "first_response_at", "resolved_at", "closed_at",
)


def _ticket(item: dict, staff: bool) -> dict:
    data = {k: item.get(k) for k in TICKET_FIELDS}
    if staff and "sla" in item:
        data["sla"] = {"response": _clock(item["sla"].response),
                       "resolution": _clock(item["sla"].resolution)}
    return data


def build_api(db_path: Path, policy: SlaPolicy) -> FastAPI:
    api = FastAPI(
        title="DeskScope API",
        version=__version__,
        description="Tickets, SLAs and integrations. Authenticate with `Authorization: "
        "Bearer <token>`; create tokens in DeskScope under *API tokens*.",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    bearer = HTTPBearer(auto_error=False)

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def token_user(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> User:
        user = tokens.authenticate_token(conn, credentials.credentials if credentials else None)
        if user is None:
            raise _Unauthorized
        return user

    def staff_user(user: User = Depends(token_user)) -> User:
        if not user.is_staff:
            raise PermissionDenied("error.forbidden")
        return user

    @api.exception_handler(_Unauthorized)
    async def on_unauthorized(request: Request, exc: _Unauthorized):
        return JSONResponse(
            {"error": "unauthorized", "message": "Missing, invalid or revoked API token."},
            status_code=401, headers={"WWW-Authenticate": "Bearer"},
        )

    @api.exception_handler(DeskError)
    async def on_desk_error(request: Request, exc: DeskError):
        return JSONResponse(
            {"error": exc.key, "message": translate("en", exc.key, **exc.params)},
            status_code=exc.status_code,
        )

    @api.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse({"error": "http", "message": exc.detail}, status_code=exc.status_code)

    v1 = "/v1"

    @api.get(f"{v1}/me", tags=["account"])
    def me(user: User = Depends(token_user)):
        """Who the token belongs to."""
        return {"id": user.id, "username": user.username, "full_name": user.full_name,
                "role": user.role}

    @api.get(f"{v1}/tickets", tags=["tickets"])
    def list_tickets(
        status: str = "active",
        scope: Literal["all", "mine", "assigned", "unassigned"] = "all",
        q: str = "",
        sla: Literal["", "problems"] = "",
        user: User = Depends(token_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """Tickets the token owner can see. `status`: active, any or one status."""
        flt = tickets.TicketFilter(status=status, scope=scope, query=q[:100],
                                   sla_problems=sla == "problems" and user.is_staff)
        items = tickets.list_with_sla(conn, user, flt, policy, datetime.now(timezone.utc))
        return {"count": len(items), "tickets": [_ticket(i, user.is_staff) for i in items]}

    @api.post(f"{v1}/tickets", status_code=201, tags=["tickets"])
    def create_ticket(
        body: TicketIn,
        user: User = Depends(token_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        ticket_id = tickets.create_ticket(
            conn, user, body.title, body.description, body.category, body.impact, body.urgency,
            body.device, source="api",
        )
        item = tickets.get_ticket(conn, user, ticket_id)
        tickets.attach_sla(conn, policy, [item])
        return _ticket(item, user.is_staff)

    @api.get(f"{v1}/tickets/{{ticket_id}}", tags=["tickets"])
    def get_ticket(
        ticket_id: int,
        user: User = Depends(token_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """One ticket with its conversation (internal notes only for staff) and history."""
        item = tickets.get_ticket(conn, user, ticket_id)
        tickets.attach_sla(conn, policy, [item])
        data = _ticket(item, user.is_staff)
        data["comments"] = [
            {k: c[k] for k in ("id", "author_name", "body", "internal", "created_at")}
            for c in tickets.list_comments(conn, user, ticket_id)
        ]
        data["events"] = [
            {k: e[k] for k in ("actor_name", "field", "old_value", "new_value", "created_at")}
            for e in tickets.list_events(conn, user, ticket_id)
        ]
        return data

    @api.post(f"{v1}/tickets/{{ticket_id}}/comments", status_code=201, tags=["tickets"])
    def add_comment(
        ticket_id: int,
        body: CommentIn,
        user: User = Depends(token_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        comment_id = tickets.add_comment(conn, user, ticket_id, body.body, body.internal)
        return {"id": comment_id}

    @api.post(f"{v1}/tickets/{{ticket_id}}/status", tags=["tickets"])
    def change_status(
        ticket_id: int,
        body: StatusIn,
        user: User = Depends(token_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        tickets.change_status(conn, user, ticket_id, body.status)
        return _ticket(tickets.get_ticket(conn, user, ticket_id), False)

    @api.post(f"{v1}/tickets/{{ticket_id}}/assign", tags=["tickets"])
    def assign(
        ticket_id: int,
        body: AssignIn,
        user: User = Depends(staff_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        tickets.assign(conn, user, ticket_id, body.assignee_id)
        return _ticket(tickets.get_ticket(conn, user, ticket_id), False)

    @api.post(f"{v1}/tickets/{{ticket_id}}/classify", tags=["tickets"])
    def classify(
        ticket_id: int,
        body: ClassifyIn,
        user: User = Depends(staff_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        tickets.set_classification(conn, user, ticket_id, body.impact, body.urgency)
        return _ticket(tickets.get_ticket(conn, user, ticket_id), False)

    @api.post(f"{v1}/tickets/{{ticket_id}}/rate", tags=["tickets"])
    def rate(
        ticket_id: int,
        body: RatingIn,
        user: User = Depends(token_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """Satisfaction survey (requester only, once the ticket is resolved)."""
        tickets.rate(conn, user, ticket_id, body.score, body.comment)
        return {"ticket_id": ticket_id, "satisfaction": body.score}

    @api.get(f"{v1}/articles", tags=["knowledge base"])
    def list_articles(
        q: str = "",
        category: str | None = None,
        user: User = Depends(token_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """Knowledge base search (published self-service articles for employees)."""
        items = knowledge.search(conn, user, q[:200], category)
        fields = ("id", "title", "category", "audience", "status", "views", "updated_at")
        return {"count": len(items), "articles": [{k: a[k] for k in fields} for a in items]}

    @api.get(f"{v1}/articles/{{article_id}}", tags=["knowledge base"])
    def get_article(
        article_id: int,
        user: User = Depends(token_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        article = knowledge.get_article(conn, user, article_id, count_view=True)
        fields = ("id", "title", "body", "category", "audience", "status", "views",
                  "author_name", "source_ticket_id", "created_at", "updated_at")
        return {k: article[k] for k in fields}

    @api.get(f"{v1}/reports/summary", tags=["metrics"])
    def report_summary(
        days: int = 30,
        user: User = Depends(staff_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """Service desk metrics for the last 7, 30 or 90 days; durations are business seconds."""
        if days not in reports.PERIODS:
            raise ValidationError("error.period_invalid")
        return reports.build_report(conn, user, policy, days)

    @api.get(f"{v1}/devices/{{name}}", tags=["devices"])
    def get_device(
        name: str,
        user: User = Depends(staff_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """Device sheet imported from PCScope."""
        device = integrations.get_device(conn, name)
        if device is None:
            raise NotFound("error.device_not_found")
        return device

    @api.post(f"{v1}/import/pcscope", tags=["integrations"])
    def import_pcscope(
        body: PCScopeImport,
        lang: Literal["en", "es"] = "en",
        user: User = Depends(staff_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """Rows of PCScope's devices.csv and issues.csv (e.g. from `Import-Csv`)."""
        result = integrations.import_pcscope(conn, body.devices, body.issues, body.levels, lang)
        return result.as_dict()

    @api.post(f"{v1}/import/netscope", tags=["integrations"])
    def import_netscope(
        scan: dict,
        lang: Literal["en", "es"] = "en",
        include_new_devices: bool = True,
        user: User = Depends(staff_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """A NetScope scan (the content of a scan-*.json file)."""
        if scan.get("tool") != "NetScope" or not isinstance(scan.get("devices"), list):
            raise DeskError("error.import_format")
        result = integrations.import_netscope(
            conn, scan, include_new_devices=include_new_devices, lang=lang
        )
        return result.as_dict()

    @api.post(f"{v1}/import/adflow", tags=["integrations"])
    def import_adflow(
        summary: dict,
        lang: Literal["en", "es"] = "en",
        user: User = Depends(staff_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """The summary.json of an ADFlow onboarding or offboarding run."""
        if summary.get("operation") not in ("Onboarding", "Offboarding"):
            raise DeskError("error.import_format")
        return integrations.import_adflow(conn, summary, lang=lang).as_dict()

    @api.post(f"{v1}/import/hardenscope", tags=["integrations"])
    def import_hardenscope(
        fleet: dict,
        lang: Literal["en", "es"] = "en",
        severities: str = "critical",
        user: User = Depends(staff_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        """A HardenScope fleet report (`HardenScope.ps1 -DeskScopeUrl` sends it).

        `severities` is a comma-separated list: critical, high, medium, low.
        """
        if not integrations._is_hardenscope_fleet(fleet):
            raise DeskError("error.import_format")
        wanted = [s.strip().lower() for s in severities.split(",") if s.strip()]
        if not wanted or any(s not in integrations.HARDENSCOPE_PRIORITY for s in wanted):
            raise DeskError("error.import_format")
        return integrations.import_hardenscope(conn, fleet, severities=wanted, lang=lang).as_dict()

    return api
