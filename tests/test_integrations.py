"""Phase 3: PCScope / NetScope / ADFlow imports, device sheets, API tokens and the REST API."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest
from conftest import csrf_from, login

from deskscope import cli, integrations, tickets, tokens, users
from deskscope.integrations import (
    ImportFormatError,
    import_adflow,
    import_netscope,
    import_pcscope,
    load_adflow_run,
    load_netscope_scan,
    load_pcscope_export,
    read_csv,
)
from deskscope.models import NotFound, PermissionDenied, ValidationError
from deskscope.seed import examples_dir

EXAMPLES = examples_dir()


@pytest.fixture
def pcscope():
    return load_pcscope_export(EXAMPLES / "pcscope-export")


@pytest.fixture
def scan():
    return load_netscope_scan(EXAMPLES / "netscope")


@pytest.fixture
def run():
    return load_adflow_run(EXAMPLES / "adflow-run")


def active(conn, source):
    return conn.execute(
        "SELECT id, external_key, title, priority, device, requester_id FROM tickets"
        " WHERE source = ? AND status IN ('new', 'open', 'pending') ORDER BY external_key",
        (source,),
    ).fetchall()


# ---- system account -------------------------------------------------------------


def test_integrations_account_cannot_sign_in_or_be_assigned(conn, people):
    bot = users.system_user(conn)
    assert bot.is_system and bot.is_staff
    assert users.authenticate(conn, "integrations", "!") is None
    assert users.authenticate(conn, "integrations", "") is None
    assert "integrations" not in [u.username for u in users.list_users(conn)]
    assert "integrations" not in [u.username for u in users.list_staff(conn)]
    with pytest.raises(PermissionDenied):
        users.set_active(conn, people["admin"], bot.id, False)


# ---- CSV reading ------------------------------------------------------------------


@pytest.mark.parametrize("delimiter", [",", ";"])
def test_read_csv_handles_bom_and_culture_delimiters(delimiter):
    text = f'﻿"Device"{delimiter}"Issue"\r\n"PC-01"{delimiter}"Drive C: is 90%, full"\r\n'
    assert read_csv(text) == [{"Device": "PC-01", "Issue": "Drive C: is 90%, full"}]
    assert read_csv("") == []


def test_pcscope_export_must_have_devices_csv(tmp_path):
    with pytest.raises(ImportFormatError, match="ExportCsv"):
        load_pcscope_export(tmp_path)


# ---- PCScope ------------------------------------------------------------------------


def test_pcscope_opens_tickets_for_critical_issues_and_saves_device_sheets(conn, pcscope):
    result = import_pcscope(conn, *pcscope)
    assert result.devices == 9 and len(result.created) == 3
    rows = active(conn, "pcscope")
    assert [r["external_key"] for r in rows] == [
        "pcscope:HR-PC02:antivirus", "pcscope:OPS-PC04:diskSpace", "pcscope:SAL-LT05:windowsUpdates"]
    by_key = {r["external_key"]: r for r in rows}
    # Security problems affect more people than a full disk does.
    assert by_key["pcscope:HR-PC02:antivirus"]["priority"] == "high"
    assert by_key["pcscope:OPS-PC04:diskSpace"]["priority"] == "medium"
    assert by_key["pcscope:OPS-PC04:diskSpace"]["title"] == "OPS-PC04: Drive C: is 93% full (16.4 GB free)"
    assert all(r["requester_id"] == users.system_user(conn).id for r in rows)
    sheet = integrations.get_device(conn, "ops-pc04")
    assert sheet["data"]["Model"] == "OptiDesk 3000" and sheet["data"]["SystemDriveUsedPercent"] == "93"


def test_pcscope_import_is_idempotent(conn, pcscope):
    import_pcscope(conn, *pcscope)
    again = import_pcscope(conn, *pcscope)
    assert (again.created, again.updated, again.resolved) == ([], [], [])
    assert len(again.unchanged) == 3


def test_pcscope_notes_changes_and_resolves_what_disappears(conn, pcscope):
    devices, issues = pcscope
    first = import_pcscope(conn, devices, issues)
    disk = active(conn, "pcscope")[1]["id"]
    later = [dict(i) for i in issues if i["Rule"] != "antivirus"]
    for row in later:
        if row["Rule"] == "diskSpace":
            row["Issue"] = "Drive C: is 97% full (5.1 GB free)"
    result = import_pcscope(conn, devices, later)
    assert result.updated == [disk]
    assert len(result.resolved) == 1
    bot = users.system_user(conn)
    notes = tickets.list_comments(conn, bot, disk)
    assert notes[-1]["internal"] and "97% full" in notes[-1]["body"]
    resolved = tickets.get_ticket(conn, bot, result.resolved[0])
    assert resolved["status"] == "resolved" and resolved["external_key"].endswith(":antivirus")
    assert "no longer reports" in tickets.list_comments(conn, bot, resolved["id"])[-1]["body"]
    # Same disk value again: nothing new to say.
    assert import_pcscope(conn, devices, later).updated == []
    assert first.created  # sanity


def test_devices_missing_from_the_export_keep_their_tickets(conn, pcscope):
    devices, issues = pcscope
    import_pcscope(conn, devices, issues)
    only_some = [d for d in devices if d["Device"] != "HR-PC02"]
    result = import_pcscope(conn, only_some, [i for i in issues if i["Device"] != "HR-PC02"])
    assert result.resolved == []  # HR-PC02 was not inventoried this time: no conclusion


def test_a_problem_that_comes_back_gets_a_new_ticket(conn, pcscope):
    devices, issues = pcscope
    import_pcscope(conn, devices, issues)
    import_pcscope(conn, devices, [])
    result = import_pcscope(conn, devices, issues)
    assert len(result.created) == 3
    keys = conn.execute("SELECT external_key FROM tickets WHERE source = 'pcscope'").fetchall()
    assert len(keys) == 6


def test_database_refuses_two_active_tickets_with_the_same_key(conn, pcscope):
    import_pcscope(conn, *pcscope)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tickets (title, description, category, priority, status, requester_id,"
            " created_at, updated_at, external_key) VALUES ('x', 'y', 'other', 'low', 'new', 1,"
            " 'x', 'x', 'pcscope:OPS-PC04:diskSpace')"
        )


def test_levels_grouping_and_unknown_rules(conn, pcscope):
    devices, _ = pcscope
    issues = [
        {"Device": "OPS-PC04", "Level": "warning", "Rule": "diskSpace", "Issue": "Drive D: is 85% full"},
        {"Device": "OPS-PC04", "Level": "critical", "Rule": "diskSpace", "Issue": "Drive C: is 93% full"},
        {"Device": "FIN-LT03", "Level": "warning", "Rule": "brandNewRule", "Issue": "Something new"},
        {"Device": "bad name", "Level": "critical", "Rule": "uac", "Issue": "UAC off"},
    ]
    result = import_pcscope(conn, devices, issues, levels=["critical", "warning"], lang="es")
    rows = {r["external_key"]: r for r in active(conn, "pcscope")}
    assert set(rows) == {"pcscope:OPS-PC04:diskSpace", "pcscope:FIN-LT03:brandNewRule"}
    disk = tickets.get_ticket(conn, users.system_user(conn), rows["pcscope:OPS-PC04:diskSpace"]["id"])
    assert "Drive C: is 93% full / Drive D: is 85% full" in disk["title"]
    assert disk["urgency"] == "high"  # the worst level of the group
    assert "PCScope ha encontrado" in disk["description"]
    other = tickets.get_ticket(conn, users.system_user(conn), rows["pcscope:FIN-LT03:brandNewRule"]["id"])
    assert other["category"] == "other"
    assert result.skipped == 1


def test_long_titles_are_shortened(conn, pcscope):
    devices, _ = pcscope
    issues = [{"Device": "OPS-PC04", "Level": "critical", "Rule": "blockedSoftware", "Issue": "x" * 300}]
    import_pcscope(conn, devices, issues)
    title = active(conn, "pcscope")[0]["title"]
    assert len(title) == 120 and title.endswith("…")


# ---- NetScope -----------------------------------------------------------------------


def test_netscope_opens_critical_findings_and_new_devices(conn, scan):
    result = import_netscope(conn, scan)
    keys = [r["external_key"] for r in active(conn, "netscope")]
    assert keys == ["netscope:02:00:5E:10:00:20:smb1_enabled",
                    "netscope:02:00:5E:10:20:77:new_device",
                    "netscope:02:00:5E:10:60:40:telnet_open"]
    assert len(result.created) == 3
    devices = {r["external_key"]: r["device"] for r in active(conn, "netscope")}
    assert devices["netscope:02:00:5E:10:20:77:new_device"] == "10.0.20.77"  # no name: the IP
    assert devices["netscope:02:00:5E:10:60:40:telnet_open"] == "WH-LABEL01"


def test_netscope_resolves_fixed_findings_but_not_new_devices(conn, scan):
    import_netscope(conn, scan)
    fixed = copy.deepcopy(scan)
    for device in fixed["devices"]:
        device["findings"] = [f for f in device["findings"] if f["code"] != "telnet_open"]
        device.pop("new", None)
        device["findings"] = [f for f in device["findings"] if f["code"] != "new_device"]
    result = import_netscope(conn, fixed)
    assert len(result.resolved) == 1
    keys = [r["external_key"] for r in active(conn, "netscope")]
    assert "netscope:02:00:5E:10:20:77:new_device" in keys  # a person must check it


def test_netscope_options(conn, scan):
    result = import_netscope(conn, scan, severities=["crit", "warn"], include_new_devices=False)
    keys = {r["external_key"].rsplit(":", 1)[1] for r in active(conn, "netscope")}
    assert keys == {"telnet_open", "http_only_admin", "smb1_enabled"}
    assert len(result.created) == 3


def test_load_netscope_scan(tmp_path, scan):
    (tmp_path / "scans").mkdir()
    old, new = dict(scan, finished="old"), dict(scan, finished="new")
    (tmp_path / "scans" / "scan-20261101-000000.json").write_text(json.dumps(old), "utf-8")
    (tmp_path / "scans" / "scan-20261117-000000.json").write_text(json.dumps(new), "utf-8")
    assert load_netscope_scan(tmp_path)["finished"] == "new"
    (tmp_path / "other.json").write_text('{"tool": "Other", "devices": []}', "utf-8")
    with pytest.raises(ImportFormatError, match="not a NetScope scan"):
        load_netscope_scan(tmp_path / "other.json")
    (tmp_path / "broken.json").write_text("{", "utf-8")
    with pytest.raises(ImportFormatError, match="invalid JSON"):
        load_netscope_scan(tmp_path / "broken.json")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ImportFormatError, match="no scan"):
        load_netscope_scan(tmp_path / "empty")


# ---- ADFlow -------------------------------------------------------------------------


def test_adflow_onboarding_tickets(conn, run):
    result = import_adflow(conn, run, lang="es")
    assert len(result.created) == 2 and result.skipped == 1
    rows = active(conn, "adflow")
    assert [r["title"] for r in rows] == ["Alta: Paula Costa (paula.costa)", "Alta: Hugo Meyer (hugo.meyer)"]
    paula = tickets.get_ticket(conn, users.system_user(conn), rows[0]["id"])
    assert "GG-HR, GG-HR-Payroll-RW" in paula["description"]
    assert "[ ] Entregar la contraseña inicial en persona" in paula["description"]
    hugo = tickets.get_ticket(conn, users.system_user(conn), rows[1]["id"])
    assert "Grupos: GG-Sales" in hugo["description"]  # a single group arrives as a string
    again = import_adflow(conn, run, lang="es")
    assert again.created == [] and again.updated == [] and len(again.unchanged) == 2


def test_adflow_offboarding_and_dry_runs(conn, run):
    off = dict(run, operation="Offboarding", results=[
        {"EmployeeId": "E0903", "SamAccountName": "pablo.ortiz", "DisplayName": "Pablo Ortiz",
         "Status": "Offboarded", "Groups": ["GG-IT"], "PreviousOu": "OU=IT,DC=example,DC=com",
         "ArchivedTo": "\\\\files\\archive$\\pablo.ortiz"}])
    import_adflow(conn, off)
    row = active(conn, "adflow")[0]
    assert row["title"] == "Offboarding: Pablo Ortiz (pablo.ortiz)" and row["priority"] == "high"
    dry = dict(run, mode="DryRun", results=[dict(r, Status="Planned") for r in run["results"]])
    assert import_adflow(conn, dry).created == []
    assert len(import_adflow(conn, dry, include_dry_runs=True).created) == 3


def test_load_adflow_run(tmp_path):
    with pytest.raises(ImportFormatError, match="not found"):
        load_adflow_run(tmp_path)
    (tmp_path / "summary.json").write_text('{"operation": "Backup"}', "utf-8")
    with pytest.raises(ImportFormatError, match="not an ADFlow"):
        load_adflow_run(tmp_path)


# ---- tokens -------------------------------------------------------------------------


def test_tokens_are_stored_hashed_and_can_be_revoked(conn, people):
    agent = people["agent"]
    secret = tokens.create_token(conn, agent, agent.id, "  nightly   import ")
    assert secret.startswith("dsk_") and len(secret) > 40
    stored = conn.execute("SELECT name, token_hash, prefix FROM api_tokens").fetchone()
    assert stored["name"] == "nightly import"
    assert secret not in stored["token_hash"] and stored["prefix"] == secret[:12]
    assert tokens.authenticate_token(conn, secret).id == agent.id
    assert tokens.list_tokens(conn, agent.id)[0]["last_used_at"]
    assert tokens.authenticate_token(conn, secret + "x") is None
    assert tokens.authenticate_token(conn, "Bearer " + secret) is None
    assert tokens.authenticate_token(conn, None) is None
    token_id = tokens.list_tokens(conn, agent.id)[0]["id"]
    with pytest.raises(NotFound):
        tokens.revoke_token(conn, people["req"], token_id)  # not theirs
    tokens.revoke_token(conn, agent, token_id)
    assert tokens.authenticate_token(conn, secret) is None


def test_token_rules(conn, people):
    with pytest.raises(ValidationError):
        tokens.create_token(conn, people["req"], people["req"].id, "   ")
    with pytest.raises(PermissionDenied):
        tokens.create_token(conn, people["req"], people["agent"].id, "not mine")
    secret = tokens.create_token(conn, people["admin"], people["req"].id, "for Ana")
    users.set_active(conn, people["admin"], people["req"].id, False)
    assert tokens.authenticate_token(conn, secret) is None


# ---- REST API -----------------------------------------------------------------------


def bearer(conn, user):
    return {"Authorization": "Bearer " + tokens.create_token(conn, user, user.id, "test")}


def test_api_needs_a_valid_token(client, conn, people):
    assert client.get("/api/v1/me").status_code == 401
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer dsk_nope"})
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    # A browser session is not enough: the API never reads cookies (no CSRF risk).
    login(client, "tech1")
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers=bearer(conn, people["agent"])).json()["username"] == "tech1"


def test_api_ticket_lifecycle(client, conn, people):
    req, agent = bearer(conn, people["req"]), bearer(conn, people["agent"])
    r = client.post("/api/v1/tickets", headers=req, json={
        "title": "Teams crashes", "description": "Since the update", "category": "software",
        "impact": "low", "urgency": "high", "device": "fin-lt03"})
    assert r.status_code == 201
    created = r.json()
    assert (created["priority"], created["source"], created["device"]) == ("medium", "api", "FIN-LT03")
    assert "sla" not in created  # requesters do not see SLA clocks
    tid = created["id"]
    assert client.post(f"/api/v1/tickets/{tid}/comments", headers=req,
                       json={"body": "secret", "internal": True}).status_code == 403
    assert client.post(f"/api/v1/tickets/{tid}/assign", headers=req,
                       json={"assignee_id": people["agent"].id}).status_code == 403
    client.post(f"/api/v1/tickets/{tid}/assign", headers=agent, json={"assignee_id": people["agent"].id})
    client.post(f"/api/v1/tickets/{tid}/classify", headers=agent, json={"impact": "high", "urgency": "high"})
    client.post(f"/api/v1/tickets/{tid}/comments", headers=agent, json={"body": "Reinstalling", "internal": False})
    r = client.post(f"/api/v1/tickets/{tid}/status", headers=agent, json={"status": "resolved"})
    assert r.json()["status"] == "resolved"
    full = client.get(f"/api/v1/tickets/{tid}", headers=agent).json()
    assert full["priority"] == "critical" and full["sla"]["response"]["state"] in ("met", "breached")
    assert [c["body"] for c in full["comments"]] == ["Reinstalling"]
    assert [e["field"] for e in full["events"]][:2] == ["created", "assignee"]
    listing = client.get("/api/v1/tickets?status=any", headers=req).json()
    assert listing["count"] == 1


def test_api_errors_are_json(client, conn, people):
    other = tickets.create_ticket(conn, people["req2"], "Bob's", "x", "other")
    req = bearer(conn, people["req"])
    r = client.get(f"/api/v1/tickets/{other}", headers=req)
    assert r.status_code == 404 and r.json()["error"] == "error.ticket_not_found"
    r = client.post("/api/v1/tickets", headers=req, json={"title": "", "description": "x"})
    assert r.status_code == 400 and "summary is required" in r.json()["message"]
    r = client.post("/api/v1/tickets", headers=req, json={"title": "x", "description": "y", "impact": "huge"})
    assert r.status_code == 422


def test_api_imports_are_for_staff(client, conn, people, pcscope, scan, run):
    agent, req = bearer(conn, people["agent"]), bearer(conn, people["req"])
    devices, issues = pcscope
    assert client.post("/api/v1/import/pcscope", headers=req,
                       json={"devices": devices, "issues": issues}).status_code == 403
    r = client.post("/api/v1/import/pcscope?lang=es", headers=agent,
                    json={"devices": devices, "issues": issues, "levels": ["critical"]})
    assert r.status_code == 200 and len(r.json()["created"]) == 3 and r.json()["devices"] == 9
    assert len(client.post("/api/v1/import/netscope", headers=agent, json=scan).json()["created"]) == 3
    assert len(client.post("/api/v1/import/adflow", headers=agent, json=run).json()["created"]) == 2
    bad = client.post("/api/v1/import/netscope", headers=agent, json={"tool": "x"})
    assert bad.status_code == 400 and bad.json()["error"] == "error.import_format"
    assert client.get("/api/v1/devices/ops-pc04", headers=agent).json()["data"]["Model"] == "OptiDesk 3000"
    assert client.get("/api/v1/devices/NOPE", headers=agent).status_code == 404
    assert client.get("/api/v1/devices/OPS-PC04", headers=req).status_code == 403


def test_openapi_and_docs(client):
    spec = client.get("/api/openapi.json").json()
    assert spec["info"]["title"] == "DeskScope API"
    assert "/v1/tickets" in spec["paths"] and "/v1/import/pcscope" in spec["paths"]
    docs = client.get("/api/docs")
    assert docs.status_code == 200 and "cdn.jsdelivr.net" in docs.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in client.get("/login").headers["content-security-policy"]


# ---- web pages ----------------------------------------------------------------------


def test_token_page_shows_the_secret_once(client, conn, people):
    login(client, "ana")
    token = csrf_from(client, "/account/tokens")
    r = client.post("/account/tokens", data={"name": "My script", "csrf_token": token})
    secret = r.text.split('<code class="secret">')[1].split("<")[0]
    assert secret.startswith("dsk_")
    assert tokens.authenticate_token(conn, secret).username == "ana"
    page = client.get("/account/tokens").text
    assert "My script" in page and secret not in page and secret[:12] in page
    assert secret not in str(client.cookies.get("deskscope_session"))
    token_id = tokens.list_tokens(conn, people["req"].id)[0]["id"]
    r = client.post(f"/account/tokens/{token_id}/revoke", data={"csrf_token": token})
    assert "Token revoked." in r.text
    r = client.post("/account/tokens", data={"name": " ", "csrf_token": token})
    assert r.status_code == 400 and "Give the token a name" in r.text


def test_ticket_page_shows_the_device_sheet_to_staff(client, conn, people, pcscope):
    import_pcscope(conn, *pcscope)
    disk = [r for r in active(conn, "pcscope") if r["device"] == "OPS-PC04"][0]["id"]
    older = tickets.create_ticket(conn, people["req"], "Slow PC", "x", "hardware", device="OPS-PC04")
    login(client, "tech1")
    page = client.get(f"/tickets/{disk}").text
    assert "Device (PCScope)" in page and "OptiDesk 3000" in page and "93% used, 16.4 GB free" in page
    assert f"#{older} Slow PC" in page  # other tickets for the same device
    assert 'class="source source-pcscope"' in page
    client.post("/logout", data={"csrf_token": csrf_from(client)})
    login(client, "ana")
    assert "Device (PCScope)" not in client.get(f"/tickets/{older}").text


# ---- command line -------------------------------------------------------------------


def test_cli_imports_and_tokens(tmp_path, capsys):
    db = str(tmp_path / "desk.db")
    assert cli.main(["--db", db, "import", "pcscope", str(EXAMPLES / "pcscope-export")]) == 0
    assert "pcscope: 3 opened" in capsys.readouterr().out
    assert cli.main(["--db", db, "import", "netscope", str(EXAMPLES / "netscope"), "--lang", "es"]) == 0
    assert cli.main(["--db", db, "import", "adflow", str(EXAMPLES / "adflow-run")]) == 0
    assert "adflow: 2 opened" in capsys.readouterr().out
    assert cli.main(["--db", db, "import", "adflow", str(tmp_path)]) == 1
    assert cli.main(["--db", db, "token", "create", "integrations", "--name", "PCScope task"]) == 0
    secret = capsys.readouterr().out.strip()
    assert secret.startswith("dsk_")
    assert cli.main(["--db", db, "token", "list", "integrations"]) == 0
    assert "PCScope task" in capsys.readouterr().out
    assert cli.main(["--db", db, "token", "revoke", "1"]) == 0
    assert cli.main(["--db", db, "token", "revoke", "99"]) == 1
    assert cli.main(["--db", db, "token", "list", "nobody"]) == 1


def test_demo_includes_the_integrations(tmp_path, capsys):
    db = str(tmp_path / "demo.db")
    assert cli.main(["--db", db, "demo", "--lang", "es"]) == 0
    out = capsys.readouterr().out
    assert "with 9 users and 22 tickets" in out and "netscope: 3 opened" in out
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 9
    conn.close()
