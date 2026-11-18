"""Command line: ``deskscope init | create-user | demo | sla | import | token | notify |
fetch-mail | worker | serve``."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
import threading
from pathlib import Path

from . import __version__, integrations, mailin, notify, security, tokens, users
from .db import connect, init_db
from .models import ROLES, DeskError
from .seed import DEMO_PASSWORD, DEMO_USERS, seed_demo, seed_integrations
from .sla import WEEKDAY_NAMES, SlaConfigError, SlaPolicy, load_policy

DEFAULT_DB = Path("data") / "deskscope.db"


def default_db() -> Path:
    return Path(os.environ.get("DESKSCOPE_DB") or DEFAULT_DB)


def sla_path(args: argparse.Namespace) -> Path:
    """--sla, else $DESKSCOPE_SLA, else sla.json next to the database."""
    if args.sla:
        return args.sla
    env = os.environ.get("DESKSCOPE_SLA")
    return Path(env) if env else args.db.parent / "sla.json"


def load_sla(args: argparse.Namespace) -> SlaPolicy:
    path = sla_path(args)
    if args.sla and not path.exists():
        raise SystemExit(f"SLA file not found: {path}")
    try:
        return load_policy(path)
    except SlaConfigError as exc:
        raise SystemExit(f"Invalid SLA configuration in {path}: {exc}") from exc


def _cmd_sla(args: argparse.Namespace) -> int:
    policy = load_sla(args)
    path = sla_path(args)
    cal = policy.calendar
    source = path if path.exists() else "built-in defaults"
    print(f"SLA policy from {source}")
    days = ", ".join(WEEKDAY_NAMES[d] for d in sorted(cal.weekdays))
    print(f"  Business hours: {days} {cal.start:%H:%M}-{cal.end:%H:%M} ({cal.tz.key})")
    print(f"  Holidays: {len(cal.holidays)}   At risk from: {policy.warn_ratio:.0%} of the target")
    print(f"  {'priority':<10}{'response':>10}{'resolution':>12}  (business minutes)")
    for priority, values in policy.targets.items():
        print(f"  {priority:<10}{values['response']:>10}{values['resolution']:>12}")
    return 0


def notify_path(args: argparse.Namespace) -> Path:
    """--notify-config, else $DESKSCOPE_NOTIFY, else notifications.json next to the database."""
    if args.notify_config:
        return args.notify_config
    env = os.environ.get("DESKSCOPE_NOTIFY")
    return Path(env) if env else args.db.parent / "notifications.json"


def load_notify(args: argparse.Namespace) -> dict:
    path = notify_path(args)
    if args.notify_config and not path.exists():
        raise SystemExit(f"Notification settings not found: {path}")
    try:
        return notify.load_config(path)
    except notify.NotifyConfigError as exc:
        raise SystemExit(f"Invalid notification settings in {path}: {exc}") from exc


def worker_pass(db_path: Path, config: dict, policy: SlaPolicy) -> dict[str, int]:
    """Read the mailbox (if configured), then queue and send notifications."""
    result: dict[str, int] = {}
    conn = connect(db_path)
    try:
        if config["imap"]["enabled"]:
            result.update({f"mail_{k}": v for k, v in mailin.fetch(conn, config).items()})
        result.update(notify.run(conn, config, policy))
    finally:
        conn.close()
    return result


def _cmd_notify(args: argparse.Namespace) -> int:
    init_db(args.db)
    config, policy = load_notify(args), load_sla(args)
    conn = connect(args.db)
    try:
        if args.dry_run:
            for m in notify.collect(conn, config, policy):
                print(f"[{m.channel}] {m.recipient}: {m.subject or m.body.splitlines()[0]}")
            conn.rollback()
            return 0
        result = notify.run(conn, config, policy)
    finally:
        conn.close()
    print(", ".join(f"{k}: {v}" for k, v in result.items()))
    return 0


def _cmd_fetch_mail(args: argparse.Namespace) -> int:
    init_db(args.db)
    config = load_notify(args)
    if not config["imap"]["enabled"]:
        print("IMAP is not enabled in the notification settings.", file=sys.stderr)
        return 1
    conn = connect(args.db)
    try:
        counts = mailin.fetch(conn, config)
    finally:
        conn.close()
    print(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "no new messages")
    return 0


def _run_worker(db_path: Path, config: dict, policy: SlaPolicy, every: int,
                stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            result = worker_pass(db_path, config, policy)
            if any(result.values()):
                print("worker: " + ", ".join(f"{k} {v}" for k, v in result.items() if v),
                      flush=True)
        except Exception as exc:  # noqa: BLE001 - keep the worker alive, report and retry
            print(f"worker error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        stop.wait(every)


def _cmd_worker(args: argparse.Namespace) -> int:
    init_db(args.db)
    stop = threading.Event()
    try:
        _run_worker(args.db, load_notify(args), load_sla(args), args.every, stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


def load_secret_key(db_path: Path) -> str:
    """Session signing key: $DESKSCOPE_SECRET_KEY, else a file kept next to the database."""
    env = os.environ.get("DESKSCOPE_SECRET_KEY")
    if env:
        return env
    key_file = db_path.parent / "secret.key"
    if key_file.exists():
        return key_file.read_text("utf-8").strip()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = security.new_token()
    key_file.write_text(key, "utf-8")
    with contextlib.suppress(OSError):  # not supported everywhere
        key_file.chmod(0o600)
    return key


def _cmd_init(args: argparse.Namespace) -> int:
    init_db(args.db)
    print(f"Database ready: {args.db}")
    return 0


def _read_password(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    if not sys.stdin.isatty():
        raise SystemExit(f"No terminal to ask for the password: set ${env_name}.")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("The passwords do not match.")
    return first


def _cmd_create_user(args: argparse.Namespace) -> int:
    init_db(args.db)
    password = _read_password(args.password_env)
    conn = connect(args.db)
    try:
        user = users.create_user(
            conn, args.username, args.name, args.email, args.role, password
        )
    except DeskError as exc:
        print(f"Error: {exc.key}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Created {user.role} '{user.username}' (id {user.id}).")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    db_path: Path = args.db
    if db_path.exists():
        if not args.reset:
            print(f"{db_path} already exists. Use --reset to replace it.", file=sys.stderr)
            return 1
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    init_db(db_path)
    conn = connect(db_path)
    try:
        seed_demo(conn, lang=args.lang, history=args.history)
        imported = seed_integrations(conn, lang=args.lang)
        count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    finally:
        conn.close()
    print(f"Demo database with {len(DEMO_USERS)} users and {count} tickets: {db_path}")
    for result in imported:
        print(f"  imported from {result.summary()}")
    print(f"Every demo account uses the password '{DEMO_PASSWORD}':")
    for username, full_name, role in DEMO_USERS:
        print(f"  {username:<15} {role:<10} {full_name}")
    return 0


def _split(value: str) -> list[str]:
    return [v.strip().lower() for v in value.split(",") if v.strip()]


def _cmd_import(args: argparse.Namespace) -> int:
    init_db(args.db)
    conn = connect(args.db)
    try:
        if args.tool == "pcscope":
            devices, issues = integrations.load_pcscope_export(args.path)
            result = integrations.import_pcscope(
                conn, devices, issues, levels=_split(args.levels), lang=args.lang
            )
        elif args.tool == "netscope":
            result = integrations.import_netscope(
                conn, integrations.load_netscope_scan(args.path),
                severities=_split(args.severities),
                include_new_devices=not args.no_new_devices, lang=args.lang,
            )
        elif args.tool == "hardenscope":
            result = integrations.import_hardenscope(
                conn, integrations.load_hardenscope_fleet(args.path),
                severities=_split(args.severities), lang=args.lang,
            )
        else:
            result = integrations.import_adflow(
                conn, integrations.load_adflow_run(args.path), lang=args.lang,
                include_dry_runs=args.include_dry_runs,
            )
    except integrations.ImportFormatError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(result.summary())
    for label, ids in (("opened", result.created), ("updated", result.updated),
                       ("resolved", result.resolved)):
        if ids:
            print(f"  {label}: " + ", ".join(f"#{i}" for i in ids))
    return 0


def _cmd_token(args: argparse.Namespace) -> int:
    init_db(args.db)
    conn = connect(args.db)
    try:
        if args.action in ("create", "list"):
            owner = users.get_by_username(conn, args.username)
            if owner is None:
                print(f"Error: no user '{args.username}'", file=sys.stderr)
                return 1
            if args.action == "create":
                # The CLI runs with the server's own rights: it acts as the owner.
                print(tokens.create_token(conn, owner, owner.id, args.name))
                print("Store it now: it is not shown again.", file=sys.stderr)
            else:
                for row in tokens.list_tokens(conn, owner.id):
                    state = "revoked" if row["revoked_at"] else "active"
                    print(f"  {row['id']:>4}  {row['prefix']}…  {state:<8} {row['name']}")
        else:
            row = conn.execute(
                "SELECT user_id FROM api_tokens WHERE id = ?", (args.token_id,)
            ).fetchone()
            if row is None:
                print(f"Error: no token {args.token_id}", file=sys.stderr)
                return 1
            tokens.revoke_token(conn, users.get_user(conn, row["user_id"]), args.token_id)
            print(f"Token {args.token_id} revoked.")
    except DeskError as exc:
        print(f"Error: {exc.key}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    policy = load_sla(args)
    app = create_app(
        args.db, load_secret_key(args.db),
        secure_cookies=args.secure_cookies, sla_policy=policy,
    )
    if args.worker_every:
        # Mail and notifications in a background thread of the same process.
        config = load_notify(args)
        threading.Thread(
            target=_run_worker, args=(args.db, config, policy, args.worker_every,
                                      threading.Event()),
            daemon=True, name="deskscope-worker",
        ).start()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.secure_cookies:
        print(
            "Warning: listening on the network without --secure-cookies. "
            "Put DeskScope behind HTTPS before real use.",
            file=sys.stderr,
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deskscope", description="Lightweight IT help desk.")
    parser.add_argument("--version", action="version", version=f"DeskScope {__version__}")
    parser.add_argument(
        "--db", type=Path, default=None,
        help=f"SQLite database (default: $DESKSCOPE_DB or {DEFAULT_DB})",
    )
    parser.add_argument(
        "--sla", type=Path, default=None,
        help="SLA policy JSON (default: $DESKSCOPE_SLA or sla.json next to the database)",
    )
    parser.add_argument(
        "--notify-config", type=Path, default=None,
        help="notification and mailbox settings (default: $DESKSCOPE_NOTIFY or"
        " notifications.json next to the database)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sla", help="check and show the SLA policy")
    p.set_defaults(func=_cmd_sla)

    p = sub.add_parser("init", help="create or upgrade the database")
    p.set_defaults(func=_cmd_init)

    p = sub.add_parser("create-user", help="add an account (e.g. the first administrator)")
    p.add_argument("username")
    p.add_argument("--name", required=True, help="full name")
    p.add_argument("--email", required=True)
    p.add_argument("--role", choices=ROLES, default="requester")
    p.add_argument(
        "--password-env", default="DESKSCOPE_PASSWORD",
        help="environment variable holding the password (else it is asked for)",
    )
    p.set_defaults(func=_cmd_create_user)

    p = sub.add_parser("demo", help="create a database with a fictitious company")
    p.add_argument("--lang", choices=("en", "es"), default="en", help="language of the tickets")
    p.add_argument("--reset", action="store_true", help="replace the database if it exists")
    p.add_argument("--history", type=int, default=60,
                   help="older closed tickets to generate for the metrics (default 60)")
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser(
        "import", help="open or resolve tickets from PCScope, NetScope, ADFlow or HardenScope"
    )
    tools = p.add_subparsers(dest="tool", required=True)
    t = tools.add_parser("pcscope", help="folder written by PCScope.ps1 -ExportCsv")
    t.add_argument("path", type=Path)
    t.add_argument("--levels", default="critical",
                   help="issue levels that open tickets, e.g. critical,warning")
    t = tools.add_parser("netscope", help="a scan-*.json file or a NetScope output folder")
    t.add_argument("path", type=Path)
    t.add_argument("--severities", default="crit", help="finding severities, e.g. crit,warn")
    t.add_argument("--no-new-devices", action="store_true",
                   help="do not open tickets for devices seen for the first time")
    t = tools.add_parser("adflow", help="an ADFlow run folder (or its summary.json)")
    t.add_argument("path", type=Path)
    t.add_argument("--include-dry-runs", action="store_true",
                   help="also import planned changes from dry runs")
    t = tools.add_parser("hardenscope", help="a HardenScope fleet-*.json or its output folder")
    t.add_argument("path", type=Path)
    t.add_argument("--severities", default="critical",
                   help="severities of the failed checks that open tickets, e.g. critical,high")
    for t in tools.choices.values():
        t.add_argument("--lang", choices=("en", "es"), default="en",
                       help="language of the tickets")
    p.set_defaults(func=_cmd_import)

    p = sub.add_parser("token", help="API tokens")
    actions = p.add_subparsers(dest="action", required=True)
    t = actions.add_parser("create", help="create a token and print it once")
    t.add_argument("username", help="owner, e.g. 'integrations' for scheduled imports")
    t.add_argument("--name", required=True, help="what the token is for")
    t = actions.add_parser("list", help="list a user's tokens")
    t.add_argument("username")
    t = actions.add_parser("revoke", help="revoke a token by id")
    t.add_argument("token_id", type=int)
    p.set_defaults(func=_cmd_token)

    p = sub.add_parser("notify", help="send pending notifications (email, Telegram)")
    p.add_argument("--dry-run", action="store_true", help="show what would be sent")
    p.set_defaults(func=_cmd_notify)

    p = sub.add_parser("fetch-mail", help="turn unread emails into tickets and comments")
    p.set_defaults(func=_cmd_fetch_mail)

    p = sub.add_parser("worker", help="fetch mail and send notifications in a loop")
    p.add_argument("--every", type=int, default=60, help="seconds between passes")
    p.set_defaults(func=_cmd_worker)

    p = sub.add_parser("serve", help="run the web interface")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--secure-cookies", action="store_true",
        help="send cookies over HTTPS only (use behind a TLS reverse proxy)",
    )
    p.add_argument("--worker-every", type=int, default=0, metavar="SECONDS",
                   help="also run the mail/notification worker in the background")
    p.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db is None:
        args.db = default_db()
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
