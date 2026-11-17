"""Command line: ``deskscope init | create-user | demo | serve``."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import os
import sys
from pathlib import Path

from . import __version__, security, users
from .db import connect, init_db
from .models import ROLES, DeskError
from .seed import DEMO_PASSWORD, DEMO_USERS, seed_demo

DEFAULT_DB = Path("data") / "deskscope.db"


def default_db() -> Path:
    return Path(os.environ.get("DESKSCOPE_DB") or DEFAULT_DB)


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
        seed_demo(conn, lang=args.lang)
        count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    finally:
        conn.close()
    print(f"Demo database with {len(DEMO_USERS)} users and {count} tickets: {db_path}")
    print(f"Every demo account uses the password '{DEMO_PASSWORD}':")
    for username, full_name, role in DEMO_USERS:
        print(f"  {username:<15} {role:<10} {full_name}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    app = create_app(args.db, load_secret_key(args.db), secure_cookies=args.secure_cookies)
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
    sub = parser.add_subparsers(dest="command", required=True)

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
    p.set_defaults(func=_cmd_demo)

    p = sub.add_parser("serve", help="run the web interface")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument(
        "--secure-cookies", action="store_true",
        help="send cookies over HTTPS only (use behind a TLS reverse proxy)",
    )
    p.set_defaults(func=_cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.db is None:
        args.db = default_db()
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
