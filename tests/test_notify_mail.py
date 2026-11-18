"""Phase 5: notifications (outbox), tickets by email and the table-rebuilding migration."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from importlib import resources

import pytest

from deskscope import cli, mailin, notify, tickets
from deskscope.db import SCHEMA_VERSION, connect, migrate
from deskscope.sla import SlaPolicy

POLICY = SlaPolicy()


def config(**overrides):
    cfg = notify.load_config(None)
    cfg["email"].update(enabled=True, server="smtp.example.com", **{"from": "desk@example.com"})
    cfg["telegram"].update(enabled=True, chat_id="-100")
    cfg["sla_alerts"] = False
    cfg.update(overrides)
    return cfg


def by_channel(messages):
    return sorted((m.channel, m.recipient, (m.subject or m.body).split("\n")[0]) for m in messages)


# ---- configuration ----------------------------------------------------------------------


def test_config_defaults_and_validation(tmp_path):
    cfg = notify.load_config(tmp_path / "missing.json")
    assert not cfg["email"]["enabled"] and not cfg["telegram"]["enabled"]
    path = tmp_path / "n.json"
    path.write_text(json.dumps({"lang": "es", "email": {"enabled": True, "server": "s",
                                                        "from": "a@example.com"},
                                "base_url": "https://desk.example.com/"}), "utf-8")
    cfg = notify.load_config(path)
    assert cfg["email"]["port"] == 587 and cfg["base_url"] == "https://desk.example.com"
    for bad in ({"lang": "fr"}, {"email": {"enabled": True}}, {"telegram": {"enabled": True}},
                {"email": {"security": "tls13"}}, {"imap": {"enabled": True}}):
        path.write_text(json.dumps(bad), "utf-8")
        with pytest.raises(notify.NotifyConfigError):
            notify.load_config(path)
    path.write_text("{", "utf-8")
    with pytest.raises(notify.NotifyConfigError, match="invalid JSON"):
        notify.load_config(path)


# ---- who hears what ---------------------------------------------------------------------------


def test_first_run_does_not_announce_the_past(conn, people):
    tickets.create_ticket(conn, people["req"], "Old ticket", "x", "other")
    assert notify.collect(conn, config(), POLICY) == []
    tickets.create_ticket(conn, people["req"], "New ticket", "x", "other")
    messages = notify.collect(conn, config(), POLICY)
    assert by_channel(messages) == [("telegram", "-100", "🆕 #2 [Medium] New ticket")]
    assert notify.collect(conn, config(), POLICY) == []  # cursor moved


def test_ticket_lifecycle_notifications(conn, people):
    cfg = config()
    notify.collect(conn, cfg, POLICY)
    tid = tickets.create_ticket(conn, people["req"], "VPN drops", "x", "network")
    tickets.assign(conn, people["agent"], tid, people["agent2"].id)  # Tina hears about it
    tickets.assign(conn, people["agent2"], tid, people["agent2"].id)  # no-op
    tickets.add_comment(conn, people["agent2"], tid, "Checking the logs", internal=True)
    tickets.add_comment(conn, people["agent2"], tid, "Try the TCP profile")
    tickets.change_status(conn, people["agent2"], tid, "pending")
    tickets.add_comment(conn, people["req"], tid, "Still dropping")
    tickets.change_status(conn, people["agent2"], tid, "resolved")
    got = by_channel(notify.collect(conn, cfg, POLICY))
    subject = f"[DeskScope #{tid}] VPN drops"
    assert got == sorted([
        ("telegram", "-100", "🆕 #1 [Medium] VPN drops"),
        ("email", "tech2@example.com", subject),  # assigned
        ("email", "ana@example.com", subject),    # public answer
        ("email", "ana@example.com", subject),    # waiting for you
        ("email", "tech2@example.com", subject),  # Ana replied
        ("email", "ana@example.com", subject),    # resolved
    ])


def test_nobody_is_told_about_their_own_actions(conn, people):
    notify.collect(conn, config(), POLICY)
    tid = tickets.create_ticket(conn, people["agent"], "Own ticket", "x", "other")
    tickets.assign(conn, people["agent"], tid, people["agent"].id)
    tickets.change_status(conn, people["agent"], tid, "resolved")
    assert [m.channel for m in notify.collect(conn, config(), POLICY)] == ["telegram"]


def test_disabled_channels_send_nothing(conn, people):
    cfg = notify.load_config(None)
    notify.collect(conn, cfg, POLICY)
    tid = tickets.create_ticket(conn, people["req"], "x title", "y", "other")
    tickets.assign(conn, people["agent"], tid, people["agent2"].id)
    assert notify.collect(conn, cfg, POLICY) == []


def test_sla_alerts_once_per_state(conn, people):
    cfg = config(sla_alerts=True)
    notify.collect(conn, cfg, POLICY)
    tid = tickets.create_ticket(conn, people["req"], "Server down", "x", "other", "high", "high")
    tickets.assign(conn, people["agent"], tid, people["agent"].id)
    old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    conn.execute("UPDATE tickets SET created_at = ? WHERE id = ?", (old, tid))
    conn.commit()
    messages = notify.collect(conn, cfg, POLICY)
    keys = sorted(m.dedup_key for m in messages if m.dedup_key.startswith("sla:"))
    assert keys == [f"sla:{tid}:resolution:breached:mail", f"sla:{tid}:resolution:breached:tg",
                    f"sla:{tid}:response:breached:mail", f"sla:{tid}:response:breached:tg"]
    assert notify.enqueue(conn, messages) == len(messages)
    assert notify.enqueue(conn, notify.collect(conn, cfg, POLICY)) == 0  # same alerts again


def test_spanish_messages(conn, people):
    cfg = config(lang="es", base_url="https://desk.example.com")
    notify.collect(conn, cfg, POLICY)
    tid = tickets.create_ticket(conn, people["req"], "Impresora", "x", "hardware")
    tickets.assign(conn, people["agent"], tid, people["agent2"].id)
    mail = [m for m in notify.collect(conn, cfg, POLICY) if m.channel == "email"][0]
    assert "Se te ha asignado el ticket n.º 1 «Impresora»." in mail.body
    assert "https://desk.example.com/tickets/1" in mail.body


# ---- outbox -------------------------------------------------------------------------------


def test_outbox_retries_and_gives_up(conn, people):
    msgs = [notify.Message("email", "a@example.com", "s", "b", "k1"),
            notify.Message("telegram", "-100", None, "t", "k2")]
    assert notify.enqueue(conn, msgs) == 2
    assert notify.enqueue(conn, msgs) == 0
    sent = []

    def ok(cfg, row):
        sent.append(row["dedup_key"])

    def broken(cfg, row):
        raise OSError("SMTP down")

    assert notify.send_pending(conn, {}, {"email": broken, "telegram": ok}) == {"sent": 1, "failed": 1}
    row = conn.execute("SELECT * FROM outbox WHERE dedup_key = 'k1'").fetchone()
    assert row["attempts"] == 1 and "SMTP down" in row["last_error"] and row["sent_at"] is None
    for _ in range(10):
        notify.send_pending(conn, {}, {"email": broken, "telegram": ok})
    assert conn.execute("SELECT attempts FROM outbox WHERE dedup_key = 'k1'").fetchone()[0] == notify.MAX_ATTEMPTS
    assert notify.send_pending(conn, {}, {"email": ok, "telegram": ok}) == {"sent": 0, "failed": 0}
    assert sent == ["k2"]


def test_run_end_to_end(conn, people):
    cfg = config()
    notify.collect(conn, cfg, POLICY)
    tickets.create_ticket(conn, people["req"], "Mouse", "x", "hardware")
    delivered = []
    result = notify.run(conn, cfg, POLICY, {"email": lambda c, r: delivered.append(r),
                                            "telegram": lambda c, r: delivered.append(r)})
    assert result == {"queued": 1, "sent": 1, "failed": 0}
    assert delivered[0]["body"].startswith("🆕 #1")


def test_send_email_uses_starttls_and_marks_it_automatic(monkeypatch):
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls["server"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context):
            calls["tls"] = True

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, message):
            calls["message"] = message

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setenv(notify.SMTP_PASSWORD_ENV, "app-password")
    cfg = config()
    cfg["email"]["user"] = "desk@example.com"
    notify.send_email(cfg, {"recipient": "ana@example.com", "subject": "[DeskScope #1] x", "body": "hi"})
    assert calls["server"] == ("smtp.example.com", 587) and calls["tls"]
    assert calls["login"] == ("desk@example.com", "app-password")
    assert calls["message"]["Auto-Submitted"] == "auto-generated"


def test_send_telegram_needs_the_token(monkeypatch):
    monkeypatch.delenv(notify.TELEGRAM_TOKEN_ENV, raising=False)
    with pytest.raises(RuntimeError, match="DESKSCOPE_TELEGRAM_TOKEN"):
        notify.send_telegram(config(), {"recipient": "-100", "body": "x"})
    seen = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout):
        seen["url"], seen["data"] = request.full_url, request.data
        return Response()

    monkeypatch.setenv(notify.TELEGRAM_TOKEN_ENV, "123:abc")
    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    notify.send_telegram(config(), {"recipient": "-100", "body": "hello"})
    assert seen["url"] == "https://api.telegram.org/bot123:abc/sendMessage"
    assert b"chat_id=-100" in seen["data"] and b"text=hello" in seen["data"]


# ---- tickets by email ---------------------------------------------------------------------------


def mail(sender="ana@example.com", subject="Printer does not print", body="Blank pages.",
         message_id="<1@example.com>", html=False, **headers):
    msg = EmailMessage()
    msg["From"] = f"Ana Example <{sender}>"
    msg["To"] = "support@example.com"
    msg["Subject"] = subject
    if message_id:
        msg["Message-ID"] = message_id
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    if html:
        msg.set_content("fallback")
        msg.add_alternative(body, subtype="html")
        msg.make_mixed()
        return EmailMessage.as_bytes(_only_html(body, msg))
    msg.set_content(body)
    return msg.as_bytes()


def _only_html(body, original):
    msg = EmailMessage()
    for name in ("From", "To", "Subject", "Message-ID"):
        if original[name]:
            msg[name] = original[name]
    msg.set_content(body, subtype="html")
    return msg


def test_known_sender_opens_a_ticket(conn, people):
    outcome, tid = mailin.process_message(conn, mail(subject="Fwd: RE: Printer does not print"))
    assert outcome == "created"
    t = tickets.get_ticket(conn, people["req"], tid)
    assert (t["title"], t["description"], t["source"], t["priority"]) == (
        "Printer does not print", "Blank pages.", "email", "low")
    assert mailin.process_message(conn, mail()) == ("duplicate", None)


@pytest.mark.parametrize(
    ("kwargs", "outcome"),
    [
        ({"sender": "stranger@example.org"}, "ignored_unknown_sender"),
        ({"Auto_Submitted": "auto-replied"}, "ignored_automatic"),
        ({"Precedence": "bulk"}, "ignored_automatic"),
        ({"X_Autoreply": "yes"}, "ignored_automatic"),
    ],
)
def test_unwanted_mail_is_ignored(conn, people, kwargs, outcome):
    assert mailin.process_message(conn, mail(**kwargs)) == (outcome, None)
    assert conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_replies_become_comments_without_the_quote(conn, people):
    tid = tickets.create_ticket(conn, people["req"], "VPN", "drops", "network")
    body = ("It works now, thanks!\n\nOn Tue, 17 Nov 2026 Tom Tech wrote:\n"
            "> Try the TCP profile\n")
    outcome, got = mailin.process_message(
        conn, mail(subject=f"Re: [DeskScope #{tid}] VPN", body=body, message_id="<2@x>"))
    assert (outcome, got) == ("commented", tid)
    assert tickets.list_comments(conn, people["req"], tid)[-1]["body"] == "It works now, thanks!"


def test_employees_cannot_comment_on_other_peoples_tickets_by_mail(conn, people):
    tid = tickets.create_ticket(conn, people["req2"], "Bob's", "x", "other")
    outcome, _ = mailin.process_message(conn, mail(subject=f"[DeskScope #{tid}] hi", message_id="<3@x>"))
    assert outcome == "rejected:error.ticket_not_found"
    assert tickets.list_comments(conn, people["req2"], tid) == []


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("Hola\n\nEl mar, 17 nov 2026 escribió:\n> antes", "Hola"),
        ("Done.\n-----Original Message-----\nFrom: x", "Done."),
        ("Fine\nDe: Soporte\nEnviado: ayer", "Fine"),
        ("> only quote", ""),
        ("Line one\nLine two", "Line one\nLine two"),
    ],
)
def test_strip_quoted(text, kept):
    assert mailin.strip_quoted(text) == kept


def test_html_only_mail(conn, people):
    raw = mail(body="<p>Screen <b>flickers</b></p><script>alert(1)</script><p>since &amp; today</p>",
               html=True, message_id="<4@x>")
    outcome, tid = mailin.process_message(conn, raw)
    assert outcome == "created"
    text = tickets.get_ticket(conn, people["req"], tid)["description"]
    assert "Screen flickers" in text and "since & today" in text and "alert" not in text


def test_fetch_reads_unseen_messages_and_marks_them(conn, people):
    messages = {b"1": mail(message_id="<a@x>"), b"2": mail(sender="nobody@x.org", message_id="<b@x>")}

    class FakeIMAP:
        def __init__(self):
            self.stored, self.logged_in = [], None

        def login(self, user, password):
            self.logged_in = (user, password)

        def select(self, folder):
            return "OK", [b"2"]

        def search(self, charset, criteria):
            assert criteria == "UNSEEN"
            return "OK", [b"1 2"]

        def fetch(self, number, parts):
            return "OK", [(b"header", messages[number])]

        def store(self, number, flags, value):
            self.stored.append(number)

        def logout(self):
            pass

    fake = FakeIMAP()
    cfg = config()
    cfg["imap"].update(enabled=True, server="imap.example.com", user="support@example.com")
    counts = mailin.fetch(conn, cfg, imap_factory=lambda: fake)
    assert counts == {"created": 1, "ignored_unknown_sender": 1}
    assert fake.stored == [b"1", b"2"] and fake.logged_in[0] == "support@example.com"


def test_email_tickets_show_their_source(client, conn, people):
    from conftest import login

    _, tid = mailin.process_message(conn, mail(message_id="<web@x>"))
    login(client, "tech1")
    assert 'class="source source-email">Email<' in client.get(f"/tickets/{tid}").text


# ---- command line ----------------------------------------------------------------------------


def test_cli_notify_and_fetch_mail(tmp_path, capsys):
    db = tmp_path / "desk.db"
    settings = tmp_path / "notifications.json"
    settings.write_text(json.dumps({"telegram": {"enabled": True, "chat_id": "-1"}}), "utf-8")
    assert cli.main(["--db", str(db), "notify", "--dry-run"]) == 0
    assert cli.main(["--db", str(db), "--notify-config", str(settings), "notify", "--dry-run"]) == 0
    assert cli.main(["--db", str(db), "fetch-mail"]) == 1
    assert "IMAP is not enabled" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="not found"):
        cli.main(["--db", str(db), "--notify-config", str(tmp_path / "x.json"), "notify"])
    assert cli.worker_pass(db, notify.load_config(None), POLICY) == {"queued": 0, "sent": 0, "failed": 0}


# ---- migration 5 ------------------------------------------------------------------------------


def test_migration_5_rebuilds_tickets_without_losing_anything(tmp_path):
    conn = connect(tmp_path / "v4.db")
    files = resources.files("deskscope").joinpath("migrations")
    conn.execute("PRAGMA foreign_keys = OFF")
    for name in ("001_initial.sql", "002_sla.sql", "003_integrations.sql", "004_knowledge.sql"):
        conn.executescript(files.joinpath(name).read_text("utf-8"))
    conn.execute("PRAGMA user_version = 4")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        INSERT INTO users (id, username, full_name, email, role, password_hash, created_at)
        VALUES (10, 'emp', 'Emp', 'e@example.com', 'requester', 'h', 't');
        INSERT INTO tickets (id, title, description, category, priority, status, requester_id,
            created_at, updated_at, source, external_key, satisfaction)
        VALUES (7, 'Disk', 'full', 'hardware', 'medium', 'open', 10, 't', 't', 'pcscope', 'k', 4);
        INSERT INTO comments (ticket_id, author_id, body, created_at) VALUES (7, 10, 'hi', 't');
        INSERT INTO articles (title, body, category, author_id, source_ticket_id, created_at, updated_at)
        VALUES ('a', 'b', 'other', 10, 7, 't', 't');
    """)
    assert migrate(conn) == SCHEMA_VERSION
    row = conn.execute("SELECT * FROM tickets WHERE id = 7").fetchone()
    assert (row["source"], row["external_key"], row["satisfaction"]) == ("pcscope", "k", 4)
    assert conn.execute("SELECT COUNT(*) FROM comments WHERE ticket_id = 7").fetchone()[0] == 1
    assert conn.execute("SELECT source_ticket_id FROM articles").fetchone()[0] == 7
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.execute("UPDATE tickets SET source = 'email' WHERE id = 7")  # now allowed
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tickets SET source = 'fax' WHERE id = 7")
    conn.close()
