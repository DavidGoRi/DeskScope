"""Web interface (FastAPI + server-rendered Jinja2 templates)."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import __version__, security, tickets, users
from .db import connect, init_db
from .i18n import LANGUAGES, pick_language, translate
from .models import (
    CATEGORIES,
    PRIORITIES,
    ROLES,
    STATUSES,
    DeskError,
    NotFound,
    User,
    ValidationError,
)

HERE = Path(__file__).parent
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 15 * 60
SESSION_MAX_AGE = 8 * 60 * 60  # one working day

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self';"
        " form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
}


class LoginRequired(Exception):
    """Raised by the auth dependency; turned into a redirect to /login."""


class CsrfError(DeskError):
    status_code = 400


def safe_next(value: str | None) -> str:
    """Only allow redirects to a local path (no open redirect)."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/tickets"


def create_app(
    db_path: str | Path,
    secret_key: str,
    *,
    secure_cookies: bool = False,
) -> FastAPI:
    """Build the application. ``secure_cookies`` should be on behind HTTPS."""
    db_path = Path(db_path)
    init_db(db_path)

    app = FastAPI(
        title="DeskScope", version=__version__, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret_key,
        session_cookie="deskscope_session",
        max_age=SESSION_MAX_AGE,
        same_site="lax",
        https_only=secure_cookies,
    )
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    failures: dict[str, deque[float]] = defaultdict(deque)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path not in ("/healthz",) and not request.url.path.startswith("/static"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    # ---- request helpers -------------------------------------------------

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def current_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)) -> User | None:
        uid = request.session.get("uid")
        if not uid:
            return None
        try:
            user = users.get_user(conn, int(uid))
        except (NotFound, ValueError):
            request.session.clear()
            return None
        if not user.active:
            request.session.clear()
            return None
        return user

    def require_user(user: User | None = Depends(current_user)) -> User:
        if user is None:
            raise LoginRequired
        return user

    def lang_of(request: Request) -> str:
        return pick_language(request.cookies.get("lang"), request.headers.get("accept-language"))

    def csrf_token(request: Request) -> str:
        token = request.session.get("csrf")
        if not token:
            token = security.new_token()
            request.session["csrf"] = token
        return token

    def check_csrf(request: Request, csrf_token: str = Form("")) -> None:
        if not security.tokens_match(request.session.get("csrf"), csrf_token):
            raise CsrfError("error.csrf")

    def flash(request: Request, key: str, kind: str = "ok", **params: object) -> None:
        request.session.setdefault("flash", []).append(
            {"kind": kind, "key": key, "params": {k: str(v) for k, v in params.items()}}
        )

    def fmt_time(value: str | None) -> str:
        if not value:
            return "—"
        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")

    def render(
        request: Request, name: str, user: User | None, status_code: int = 200, **ctx: object
    ) -> Response:
        lang = lang_of(request)

        def t(key: str, **params: object) -> str:
            return translate(lang, key, **params)

        def event_text(event: dict) -> str:
            field, old, new = event["field"], event["old_value"], event["new_value"]
            if field == "status":
                return t("event.status", old=t(f"status.{old}"), new=t(f"status.{new}"))
            if field == "priority":
                return t("event.priority", old=t(f"priority.{old}"), new=t(f"priority.{new}"))
            if field == "assignee":
                return t("event.assignee", new=new) if new else t("event.unassigned", old=old)
            return t(f"event.{field}")

        ctx.update(
            user=user,
            lang=lang,
            other_lang="es" if lang == "en" else "en",
            t=t,
            event_text=event_text,
            fmt_time=fmt_time,
            csrf=csrf_token(request),
            messages=request.session.pop("flash", []),
            version=__version__,
            STATUSES=STATUSES,
            PRIORITIES=PRIORITIES,
            CATEGORIES=CATEGORIES,
            ROLES=ROLES,
        )
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def back_to(ticket_id: int) -> RedirectResponse:
        return RedirectResponse(f"/tickets/{ticket_id}", status_code=303)

    def run_action(request: Request, ticket_id: int, action: Callable[[], object], ok: str):
        """Run a ticket change and come back to the ticket with a message."""
        try:
            action()
        except ValidationError as exc:
            flash(request, exc.key, "error", **exc.params)
        else:
            flash(request, ok)
        return back_to(ticket_id)

    # ---- error handling --------------------------------------------------

    @app.exception_handler(LoginRequired)
    async def on_login_required(request: Request, exc: LoginRequired):
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse("/login?next=" + quote(target, safe="/"), status_code=303)

    @app.exception_handler(DeskError)
    async def on_desk_error(request: Request, exc: DeskError):
        uid = request.session.get("uid")
        user = None
        if uid:
            conn = connect(db_path)
            try:
                user = users.get_user(conn, int(uid))
            except NotFound:
                user = None
            finally:
                conn.close()
        return render(
            request, "error.html", user, status_code=exc.status_code,
            error_key=exc.key, error_params=exc.params,
        )

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        key = "error.not_found" if exc.status_code == 404 else "error.title"
        return render(
            request, "error.html", None, status_code=exc.status_code,
            error_key=key, error_params={},
        )

    # ---- routes: session -------------------------------------------------

    @app.get("/healthz")
    def healthz():
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/")
    def home():
        return RedirectResponse("/tickets", status_code=303)

    @app.get("/lang/{code}")
    def set_language(code: str, next: str = "/tickets"):
        response = RedirectResponse(safe_next(next), status_code=303)
        if code in LANGUAGES:
            response.set_cookie(
                "lang", code, max_age=365 * 24 * 3600, samesite="lax", secure=secure_cookies
            )
        return response

    @app.get("/login")
    def login_form(request: Request, next: str = "/tickets", user=Depends(current_user)):
        if user:
            return RedirectResponse(safe_next(next), status_code=303)
        return render(request, "login.html", None, next=safe_next(next), username="")

    @app.post("/login", dependencies=[Depends(check_csrf)])
    def login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        next: str = Form("/tickets"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        client = request.client.host if request.client else "?"
        key = f"{client}|{username.strip().lower()}"
        now = time.monotonic()
        attempts = failures[key]
        while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if len(attempts) >= LOGIN_MAX_FAILURES:
            return render(
                request, "login.html", None, status_code=429,
                next=safe_next(next), username=username, error="login.locked",
            )
        user = users.authenticate(conn, username, password)
        if user is None:
            attempts.append(now)
            return render(
                request, "login.html", None, status_code=401,
                next=safe_next(next), username=username, error="login.failed",
            )
        failures.pop(key, None)
        # New session on login: drops anything set before (session fixation).
        request.session.clear()
        request.session["uid"] = user.id
        request.session["csrf"] = security.new_token()
        return RedirectResponse(safe_next(next), status_code=303)

    @app.post("/logout", dependencies=[Depends(check_csrf)])
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # ---- routes: tickets -------------------------------------------------

    @app.get("/tickets")
    def ticket_list(
        request: Request,
        status: str = "active",
        scope: str = "all",
        q: str = "",
        sort: str = "updated",
        user: User = Depends(require_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        if status not in ("active", "any", *STATUSES):
            status = "active"
        if scope not in tickets.SCOPES:
            scope = "all"
        if sort not in tickets.SORTS:
            sort = "updated"
        flt = tickets.TicketFilter(status=status, scope=scope, query=q[:100], sort=sort)
        return render(
            request, "tickets.html", user,
            items=tickets.list_tickets(conn, user, flt),
            counts=tickets.status_counts(conn, user),
            flt=flt, scopes=tickets.SCOPES, sorts=list(tickets.SORTS),
        )

    @app.get("/tickets/new")
    def ticket_new_form(request: Request, user: User = Depends(require_user)):
        return render(request, "ticket_new.html", user, form={"priority": "medium"})

    @app.post("/tickets/new", dependencies=[Depends(check_csrf)])
    def ticket_create(
        request: Request,
        title: str = Form(""),
        description: str = Form(""),
        category: str = Form("other"),
        priority: str = Form("medium"),
        device: str = Form(""),
        user: User = Depends(require_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            ticket_id = tickets.create_ticket(
                conn, user, title, description, category, priority, device
            )
        except ValidationError as exc:
            form = {
                "title": title, "description": description, "category": category,
                "priority": priority, "device": device,
            }
            return render(
                request, "ticket_new.html", user, status_code=400,
                form=form, error_key=exc.key, error_params=exc.params,
            )
        flash(request, "new.created", id=ticket_id)
        return back_to(ticket_id)

    @app.get("/tickets/{ticket_id:int}")
    def ticket_detail(
        request: Request,
        ticket_id: int,
        user: User = Depends(require_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        ticket = tickets.get_ticket(conn, user, ticket_id)
        return render(
            request, "ticket.html", user,
            ticket=ticket,
            comments=tickets.list_comments(conn, user, ticket_id),
            events=tickets.list_events(conn, user, ticket_id),
            staff=users.list_staff(conn) if user.is_staff else [],
        )

    @app.post("/tickets/{ticket_id:int}/comment", dependencies=[Depends(check_csrf)])
    def ticket_comment(
        request: Request,
        ticket_id: int,
        body: str = Form(""),
        internal: str = Form(""),
        user: User = Depends(require_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return run_action(
            request, ticket_id,
            lambda: tickets.add_comment(conn, user, ticket_id, body, internal == "on"),
            "ticket.comment_added",
        )

    @app.post("/tickets/{ticket_id:int}/status", dependencies=[Depends(check_csrf)])
    def ticket_status(
        request: Request,
        ticket_id: int,
        status: str = Form(""),
        user: User = Depends(require_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return run_action(
            request, ticket_id,
            lambda: tickets.change_status(conn, user, ticket_id, status),
            "ticket.updated",
        )

    @app.post("/tickets/{ticket_id:int}/assign", dependencies=[Depends(check_csrf)])
    def ticket_assign(
        request: Request,
        ticket_id: int,
        assignee: str = Form(""),
        user: User = Depends(require_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        assignee_id = int(assignee) if assignee.isdigit() else None
        return run_action(
            request, ticket_id,
            lambda: tickets.assign(conn, user, ticket_id, assignee_id),
            "ticket.updated",
        )

    @app.post("/tickets/{ticket_id:int}/priority", dependencies=[Depends(check_csrf)])
    def ticket_priority(
        request: Request,
        ticket_id: int,
        priority: str = Form(""),
        user: User = Depends(require_user),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return run_action(
            request, ticket_id,
            lambda: tickets.set_priority(conn, user, ticket_id, priority),
            "ticket.updated",
        )

    # ---- routes: user administration -------------------------------------

    def require_admin(user: User = Depends(require_user)) -> User:
        if not user.is_admin:
            raise NotFound("error.not_found")
        return user

    @app.get("/admin/users")
    def user_list(
        request: Request,
        user: User = Depends(require_admin),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        return render(
            request, "users.html", user, people=users.list_users(conn), form={"role": "requester"}
        )

    @app.post("/admin/users", dependencies=[Depends(check_csrf)])
    def user_create(
        request: Request,
        username: str = Form(""),
        full_name: str = Form(""),
        email: str = Form(""),
        role: str = Form("requester"),
        password: str = Form(""),
        user: User = Depends(require_admin),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            created = users.create_user(conn, username, full_name, email, role, password)
        except ValidationError as exc:
            form = {"username": username, "full_name": full_name, "email": email, "role": role}
            return render(
                request, "users.html", user, status_code=400,
                people=users.list_users(conn), form=form,
                error_key=exc.key, error_params=exc.params,
            )
        flash(request, "users.created", username=created.username)
        return RedirectResponse("/admin/users", status_code=303)

    @app.post("/admin/users/{user_id:int}/active", dependencies=[Depends(check_csrf)])
    def user_toggle(
        request: Request,
        user_id: int,
        active: str = Form("0"),
        user: User = Depends(require_admin),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            users.set_active(conn, user, user_id, active == "1")
        except ValidationError as exc:
            flash(request, exc.key, "error", **exc.params)
        return RedirectResponse("/admin/users", status_code=303)

    return app
