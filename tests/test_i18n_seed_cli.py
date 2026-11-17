"""Translations, demo data and the command line."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from deskscope import cli, tickets, users
from deskscope.db import connect, init_db
from deskscope.i18n import LANGUAGES, MESSAGES, pick_language, translate
from deskscope.models import CATEGORIES, PRIORITIES, ROLES, STATUSES
from deskscope.seed import DEMO_PASSWORD, DEMO_USERS, SCENARIOS, seed_demo

SRC = Path(__file__).resolve().parents[1] / "src" / "deskscope"

# ---- i18n -------------------------------------------------------------------


def test_every_language_has_the_same_keys():
    keys = {lang: set(MESSAGES[lang]) for lang in LANGUAGES}
    assert keys["en"] == keys["es"]


def test_placeholders_match_between_languages():
    for key, text in MESSAGES["en"].items():
        assert set(re.findall(r"{(\w+)}", text)) == set(re.findall(r"{(\w+)}", MESSAGES["es"][key])), key


def test_every_literal_key_used_in_code_exists():
    used: set[str] = set()
    for path in [*SRC.glob("templates/*.html"), *SRC.glob("*.py")]:
        text = path.read_text("utf-8")
        used |= set(re.findall(r"""t\(['"]([a-z_]+\.[a-z_]+)['"]""", text))
        used |= set(re.findall(r"""["'](error\.[a-z_]+)["']""", text)) - {"error.html"}
    missing = sorted(k for k in used if k not in MESSAGES["en"])
    assert not missing, missing


@pytest.mark.parametrize(
    ("prefix", "values"),
    [("status", STATUSES), ("action", STATUSES[1:]), ("priority", PRIORITIES),
     ("category", CATEGORIES), ("role", ROLES)],
)
def test_vocabulary_is_translated(prefix, values):
    for value in values:
        for lang in LANGUAGES:
            assert f"{prefix}.{value}" in MESSAGES[lang]


def test_pick_language():
    assert pick_language("es", "en-US") == "es"
    assert pick_language(None, "es-ES,es;q=0.9,en;q=0.8") == "es"
    assert pick_language("fr", "de-DE,fr;q=0.5") == "en"
    assert pick_language(None, None) == "en"


def test_translate_formats_and_falls_back():
    assert translate("es", "new.created", id=7) == "Ticket n.º 7 abierto."
    assert translate("xx", "nav.tickets") == "Tickets"
    assert translate("en", "no.such_key") == "no.such_key"


# ---- demo data --------------------------------------------------------------

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("lang", ["en", "es"])
def test_demo_data_is_consistent(tmp_path, lang):
    path = tmp_path / "demo.db"
    init_db(path)
    conn = connect(path)
    try:
        ids = seed_demo(conn, lang=lang, now=NOW)
        admin = users.get_user(conn, ids["laura.sanchez"])
        all_tickets = tickets.list_tickets(conn, admin, tickets.TicketFilter(status="any"))
        assert len(all_tickets) == len(SCENARIOS)
        assert {t["status"] for t in all_tickets} == set(STATUSES)
        for t in all_tickets:
            assert t["created_at"] <= t["updated_at"] <= NOW.isoformat()
            assert (t["resolved_at"] is not None) == (t["status"] in ("resolved", "closed"))
            assert (t["closed_at"] is not None) == (t["status"] == "closed")
            assert (t["assignee_id"] is None) == (t["status"] == "new")
            events = tickets.list_events(conn, admin, t["id"])
            assert events[0]["field"] == "created"
            statuses = [e for e in events if e["field"] == "status"]
            if statuses:
                assert statuses[-1]["new_value"] == t["status"]
        # Ticket numbers grow with time.
        by_id = sorted(all_tickets, key=lambda t: t["id"])
        assert [t["created_at"] for t in by_id] == sorted(t["created_at"] for t in by_id)
        assert users.authenticate(conn, "ana.garcia", DEMO_PASSWORD)
        if lang == "es":
            assert any("impresora" in t["title"] for t in all_tickets)
    finally:
        conn.close()


def test_demo_needs_an_empty_database(conn, people):
    with pytest.raises(RuntimeError):
        seed_demo(conn)


def test_demo_uses_only_fictitious_addresses():
    for username, _, _ in DEMO_USERS:
        assert re.fullmatch(r"[a-z]+\.[a-z]+", username)


# ---- command line -----------------------------------------------------------


def test_cli_demo_and_reset(tmp_path, capsys):
    db = tmp_path / "data" / "demo.db"
    assert cli.main(["--db", str(db), "demo", "--lang", "es"]) == 0
    assert "deskscope-demo" in capsys.readouterr().out
    assert cli.main(["--db", str(db), "demo"]) == 1
    assert cli.main(["--db", str(db), "demo", "--reset"]) == 0


def test_cli_init_and_create_user(tmp_path, monkeypatch, capsys):
    db = tmp_path / "desk.db"
    assert cli.main(["--db", str(db), "init"]) == 0
    monkeypatch.setenv("DESKSCOPE_PASSWORD", "first-admin-password")
    assert cli.main(["--db", str(db), "create-user", "laura", "--name", "Laura",
                     "--email", "laura@example.com", "--role", "admin"]) == 0
    assert "Created admin 'laura'" in capsys.readouterr().out
    assert cli.main(["--db", str(db), "create-user", "laura", "--name", "Laura",
                     "--email", "laura@example.com"]) == 1
    conn = connect(db)
    try:
        assert users.authenticate(conn, "laura", "first-admin-password").is_admin
    finally:
        conn.close()


def test_cli_uses_the_environment_database(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKSCOPE_DB", str(tmp_path / "env.db"))
    assert cli.main(["init"]) == 0
    assert (tmp_path / "env.db").exists()


def test_secret_key_is_created_once_and_kept(tmp_path, monkeypatch):
    monkeypatch.delenv("DESKSCOPE_SECRET_KEY", raising=False)
    db = tmp_path / "desk.db"
    first = cli.load_secret_key(db)
    assert len(first) >= 40 and cli.load_secret_key(db) == first
    monkeypatch.setenv("DESKSCOPE_SECRET_KEY", "from-env")
    assert cli.load_secret_key(db) == "from-env"
