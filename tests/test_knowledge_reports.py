"""Phase 4: knowledge base, satisfaction survey, metrics and the CSV export."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from conftest import csrf_from, login

from deskscope import cli, knowledge, reports, tickets, tokens
from deskscope.models import NotFound, PermissionDenied, ValidationError
from deskscope.sla import BusinessCalendar, SlaPolicy

MADRID = ZoneInfo("Europe/Madrid")
POLICY = SlaPolicy(calendar=BusinessCalendar(tz=MADRID))


def article(conn, actor, title="VPN drops every few minutes", body="Use the TCP profile.",
            category="network", audience="everyone", publish=True, **kw):
    return knowledge.create_article(conn, actor, title, body, category, audience, publish, **kw)


# ---- full-text search -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("printer prints blank", '"printer"* AND "prints"* AND "blank"*'),
        ('"quoted" OR NEAR(x) -minus', '"quoted"* AND "near"* AND "minus"*'),
        ("the printer does not print", '"printer"* AND "print"*'),
        ("the a of", None),
        ("", None),
        ("impresión", '"impresión"*'),
    ],
)
def test_fts_query_is_always_safe(text, expected):
    assert knowledge.fts_query(text) == expected


def test_search_ignores_accents_and_matches_prefixes(conn, people):
    a = article(conn, people["agent"], "La impresora saca hojas en blanco",
                "Tras cambiar el tóner la impresión sale vacía: quita la cinta.", category="hardware")
    article(conn, people["agent"], "VPN se desconecta", "Perfil TCP.")
    found = lambda q: [x["id"] for x in knowledge.search(conn, people["req"], q)]  # noqa: E731
    assert found("impresion") == [a]
    assert found("toner") == [a]
    assert found("impres blanco") == [a]
    assert found("impresora vpn") == []  # "all words" mode
    assert found("") != []  # no text: most viewed first
    assert knowledge.search(conn, people["req"], "toner")[0]["snippet"]


def test_index_follows_updates_and_deletes(conn, people):
    a = article(conn, people["agent"])
    knowledge.update_article(conn, people["agent"], a, "Outlook crashes", "Disable the CRM add-in.",
                             "software", "everyone", True)
    assert knowledge.search(conn, people["agent"], "vpn") == []
    assert [x["id"] for x in knowledge.search(conn, people["agent"], "outlook")] == [a]
    knowledge.delete_article(conn, people["agent"], a)
    assert knowledge.search(conn, people["agent"], "outlook") == []
    with pytest.raises(NotFound):
        knowledge.get_article(conn, people["agent"], a)


# ---- permissions -----------------------------------------------------------------------


def test_employees_only_read_published_self_service_articles(conn, people):
    public = article(conn, people["agent"], "Public VPN guide")
    draft = article(conn, people["agent"], "Draft VPN guide", publish=False)
    internal = article(conn, people["agent"], "Internal VPN procedure", audience="staff")
    req, agent = people["req"], people["agent"]
    assert {x["id"] for x in knowledge.search(conn, req, "vpn")} == {public}
    assert {x["id"] for x in knowledge.search(conn, agent, "vpn")} == {public, draft, internal}
    for hidden in (draft, internal):
        with pytest.raises(NotFound):
            knowledge.get_article(conn, req, hidden)
    with pytest.raises(PermissionDenied):
        article(conn, req, "My own article")
    with pytest.raises(PermissionDenied):
        knowledge.delete_article(conn, req, public)


def test_suggestions_are_published_and_match_any_word(conn, people):
    vpn = article(conn, people["agent"], "VPN disconnects outside the office")
    article(conn, people["agent"], "VPN draft", publish=False)
    article(conn, people["agent"], "VPN for staff", audience="staff")
    printer = article(conn, people["agent"], "Printer prints blank pages", category="hardware")
    got = [x["id"] for x in knowledge.suggest(conn, people["agent"], "my vpn and the printer")]
    assert set(got) == {vpn, printer, *got} and len(got) == 3  # staff article included for staff
    got = {x["id"] for x in knowledge.suggest(conn, people["req"], "my vpn and the printer")}
    assert got == {vpn, printer}
    assert knowledge.suggest(conn, people["req"], "vp") == []


def test_validation_and_views(conn, people):
    with pytest.raises(ValidationError):
        article(conn, people["agent"], title="")
    with pytest.raises(ValidationError):
        article(conn, people["agent"], audience="partners")
    with pytest.raises(ValidationError):
        article(conn, people["agent"], category="coffee")
    a = article(conn, people["agent"])
    d = article(conn, people["agent"], publish=False)
    knowledge.get_article(conn, people["req"], a, count_view=True)
    knowledge.get_article(conn, people["agent"], d, count_view=True)
    assert knowledge.get_article(conn, people["agent"], a)["views"] == 1
    assert knowledge.get_article(conn, people["agent"], d)["views"] == 0  # drafts are not counted


def test_draft_from_ticket_uses_public_answers_only(conn, people):
    tid = tickets.create_ticket(conn, people["req"], "Teams has no sound", "Nothing in calls.", "software")
    tickets.add_comment(conn, people["agent"], tid, "Toner looks fake", internal=True)
    tickets.add_comment(conn, people["req"], tid, "Still nothing")
    tickets.add_comment(conn, people["agent"], tid, "Select the headset in Teams > Settings > Devices.")
    draft = knowledge.draft_from_ticket(conn, people["agent"], tid)
    assert draft["title"] == "Teams has no sound" and draft["category"] == "software"
    assert "Select the headset" in draft["body"] and "Nothing in calls." in draft["body"]
    assert "fake" not in draft["body"] and "Still nothing" not in draft["body"]
    a = article(conn, people["agent"], draft["title"], draft["body"], "software", source_ticket_id=tid)
    assert knowledge.get_article(conn, people["agent"], a)["source_ticket_id"] == tid
    with pytest.raises(PermissionDenied):
        knowledge.draft_from_ticket(conn, people["req"], tid)


# ---- satisfaction ----------------------------------------------------------------------


def test_rating_rules(conn, people):
    tid = tickets.create_ticket(conn, people["req"], "Mouse", "broken", "hardware")
    with pytest.raises(ValidationError) as exc:
        tickets.rate(conn, people["req"], tid, 5)
    assert exc.value.key == "error.rate_not_resolved"
    tickets.change_status(conn, people["agent"], tid, "resolved")
    with pytest.raises(PermissionDenied):
        tickets.rate(conn, people["agent"], tid, 5)  # only the requester
    with pytest.raises(NotFound):
        tickets.rate(conn, people["req2"], tid, 5)
    with pytest.raises(ValidationError):
        tickets.rate(conn, people["req"], tid, 6)
    tickets.rate(conn, people["req"], tid, 4, "  quick  ")
    t = tickets.get_ticket(conn, people["req"], tid)
    assert (t["satisfaction"], t["satisfaction_comment"]) == (4, "quick")
    with pytest.raises(ValidationError) as exc:
        tickets.rate(conn, people["req"], tid, 5)
    assert exc.value.key == "error.already_rated"
    assert tickets.list_events(conn, people["req"], tid)[-1]["field"] == "satisfaction"


# ---- metrics ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=MADRID)  # a Friday


def backdated(conn, people, title, created, *, responded=None, resolved=None, priority=("medium", "medium"),
              device=None, category="hardware", score=None, assignee="agent"):
    tid = tickets.create_ticket(conn, people["req"], title, "x", category, *priority, device)
    iso = lambda m: m.astimezone(timezone.utc).isoformat()  # noqa: E731
    status = "closed" if resolved else "open"
    conn.execute(
        "UPDATE tickets SET created_at = ?, first_response_at = ?, resolved_at = ?, status = ?,"
        " assignee_id = ?, satisfaction = ? WHERE id = ?",
        (iso(created), iso(responded) if responded else None, iso(resolved) if resolved else None,
         status, people[assignee].id, score, tid),
    )
    conn.execute("UPDATE ticket_events SET created_at = ? WHERE ticket_id = ?", (iso(created), tid))
    conn.commit()
    return tid


@pytest.fixture
def history(conn, people):
    # medium: 4 h to respond, 24 h to resolve (business time, Mon-Fri 9-18 Madrid)
    backdated(conn, people, "a", NOW.replace(day=16, hour=9), responded=NOW.replace(day=16, hour=10),
              resolved=NOW.replace(day=16, hour=13), device="PC-1", score=5)
    backdated(conn, people, "b", NOW.replace(day=17, hour=9), responded=NOW.replace(day=17, hour=15),
              resolved=NOW.replace(day=17, hour=17), device="PC-1", score=3, assignee="agent2")
    backdated(conn, people, "c", NOW.replace(day=19, hour=9), priority=("high", "high"),
              category="network", device="PC-2")
    backdated(conn, people, "old", NOW - timedelta(days=60), responded=NOW - timedelta(days=60),
              resolved=NOW - timedelta(days=59))


def test_report(conn, people, history):
    r = reports.build_report(conn, people["agent"], POLICY, days=30, now=NOW)
    assert (r["opened"], r["resolved"], r["backlog"]) == (3, 2, 1)
    assert r["mean_resolution"] == (4 + 8) / 2 * 3600  # 4 h and 8 h of business time
    assert r["median_resolution"] == 6 * 3600
    assert r["mean_response"] == (1 + 6) / 2 * 3600
    assert r["sla_response"] == 50.0  # 1 h met, 6 h > 4 h breached
    assert r["sla_resolution"] == 100.0
    assert r["satisfaction"] == 4.0 and r["satisfaction_count"] == 2
    assert r["satisfaction_distribution"][5] == 1 and r["satisfaction_distribution"][3] == 1
    assert r["backlog_breached"] == 1  # critical opened Thursday, never answered
    assert r["top_devices"] == [("PC-1", 2), ("PC-2", 1)]
    assert r["by_source"] == {"web": 3}
    assert len(r["daily"]) == 30 and r["daily"][-1]["day"] == "2026-11-20"
    assert sum(d["opened"] for d in r["daily"]) == 3
    by_priority = {p["priority"]: p for p in r["by_priority"]}
    assert by_priority["critical"]["opened"] == 1 and by_priority["medium"]["resolved"] == 2
    agents = {a["name"]: a for a in r["by_agent"]}
    assert agents["Tom Tech"]["resolved"] == 1 and agents["Tina Tech"]["satisfaction"] == 3
    assert reports.build_report(conn, people["agent"], POLICY, days=90, now=NOW)["resolved"] == 3


def test_report_is_for_staff_and_handles_empty_periods(conn, people):
    with pytest.raises(PermissionDenied):
        reports.build_report(conn, people["req"], POLICY)
    r = reports.build_report(conn, people["agent"], POLICY, days=7, now=NOW)
    assert r["opened"] == 0 and r["mean_resolution"] is None and r["sla_resolution"] is None


# ---- CSV export ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"), ("+1", "'+1"), ("-1", "'-1"),
     ("@SUM(A1)", "'@SUM(A1)"), ("\tx", "'\tx"), ("Normal", "Normal"), (None, ""), (42, "42")],
)
def test_safe_cell(value, expected):
    assert reports.safe_cell(value) == expected


def test_export(conn, people, history):
    tid = backdated(conn, people, "=cmd|' /C calc'!A0", NOW.replace(day=18, hour=9))
    rows = reports.export_rows(conn, people["agent"], POLICY, days=30, now=NOW)
    assert [r["id"] for r in rows][-1] == tid and len(rows) == 4
    first = rows[0]
    assert first["resolution_business_minutes"] == 240 and first["sla_resolution"] == "met"
    text = reports.to_csv(rows)
    assert text.startswith("﻿")
    parsed = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    assert parsed[-1]["title"] == "'=cmd|' /C calc'!A0"
    assert list(parsed[0]) == list(reports.EXPORT_COLUMNS)
    with pytest.raises(PermissionDenied):
        reports.export_rows(conn, people["req"], POLICY)


# ---- web -------------------------------------------------------------------------------


def test_kb_pages(client, conn, people):
    public = article(conn, people["agent"], "VPN disconnects outside the office")
    draft = article(conn, people["agent"], "Unfinished draft", publish=False)
    login(client, "ana")
    page = client.get("/kb").text
    assert "VPN disconnects" in page and "Unfinished draft" not in page
    assert client.get(f"/kb/{draft}").status_code == 404
    assert client.get("/kb/new").status_code == 404
    assert client.get(f"/kb/{public}").status_code == 200
    assert client.get("/kb/suggest?q=vpn%20problem").json() == [
        {"id": public, "title": "VPN disconnects outside the office"}]
    assert client.get("/kb?q=nothing-like-this").text.count("No articles match.") == 1


def test_staff_write_edit_and_delete_articles(client, conn, people):
    tid = tickets.create_ticket(conn, people["req"], "Teams has no sound", "In calls", "software")
    tickets.add_comment(conn, people["agent"], tid, "Pick the headset in Teams settings.")
    tickets.change_status(conn, people["agent"], tid, "resolved")
    login(client, "tech1")
    assert "Write an article from this ticket" in client.get(f"/tickets/{tid}").text
    form = client.get(f"/kb/new?from_ticket={tid}").text
    assert "Pick the headset" in form and f'action="/kb/new?from_ticket={tid}"' in form
    token = csrf_from(client, "/kb")
    r = client.post(f"/kb/new?from_ticket={tid}", data={
        "title": "Teams has no sound", "body": "Pick the headset in Teams settings.",
        "category": "software", "audience": "everyone", "publish": "on", "csrf_token": token})
    assert "Article saved." in r.text and f"Written from ticket #{tid}" in r.text
    article_id = int(str(r.url).rstrip("/").split("/")[-1])
    r = client.post(f"/kb/{article_id}/edit", data={
        "title": "", "body": "x", "category": "software", "audience": "everyone",
        "csrf_token": token})
    assert r.status_code == 400 and "The summary is required" in r.text
    client.post(f"/kb/{article_id}/edit", data={
        "title": "Teams: no sound in calls", "body": "Choose the headset.", "category": "software",
        "audience": "staff", "publish": "on", "csrf_token": token})
    saved = knowledge.get_article(conn, people["agent"], article_id)
    assert (saved["title"], saved["status"], saved["audience"]) == (
        "Teams: no sound in calls", "published", "staff")
    assert "Teams: no sound in calls" in client.get(f"/tickets/{tid}").text  # related article
    r = client.post(f"/kb/{article_id}/delete", data={"csrf_token": token})
    assert "Article deleted." in r.text


def test_survey_on_the_ticket_page(client, conn, people):
    tid = tickets.create_ticket(conn, people["req"], "Mouse", "broken", "hardware")
    tickets.change_status(conn, people["agent"], tid, "resolved")
    login(client, "ana")
    page = client.get(f"/tickets/{tid}").text
    assert "How did we do?" in page and 'name="score"' in page
    token = csrf_from(client, f"/tickets/{tid}")
    r = client.post(f"/tickets/{tid}/rate", data={"score": "5", "comment": "Great",
                                                  "csrf_token": token})
    assert "Thanks for your rating." in r.text and "Rated 5/5" in r.text
    r = client.post(f"/tickets/{tid}/rate", data={"score": "1", "csrf_token": token})
    assert "already been rated" in r.text
    client.post("/logout", data={"csrf_token": csrf_from(client)})
    login(client, "tech1")
    page = client.get(f"/tickets/{tid}").text
    assert "Rated 5/5" in page and "rated the service 5/5" in page


def test_reports_pages(client, conn, people, history):
    login(client, "ana")
    assert client.get("/reports").status_code == 404
    assert client.get("/reports/export.csv").status_code == 404
    client.post("/logout", data={"csrf_token": csrf_from(client)})
    login(client, "tech1")
    page = client.get("/reports?days=7").text
    assert "Service desk metrics" in page and "<svg" in page and "Last 7 days" in page
    assert client.get("/reports?days=13").status_code == 200  # unknown period -> 30
    r = client.get("/reports/export.csv?days=90")
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="deskscope-tickets-90d-' in r.headers["content-disposition"]
    assert r.content.startswith(b"\xef\xbb\xbfid,created_at")


def test_api_articles_reports_and_rating(client, conn, people, history):
    public = article(conn, people["agent"], "VPN guide")
    req = {"Authorization": "Bearer " + tokens.create_token(conn, people["req"], people["req"].id, "t")}
    agent = {"Authorization": "Bearer " + tokens.create_token(conn, people["agent"], people["agent"].id, "t")}
    assert client.get("/api/v1/articles?q=vpn", headers=req).json()["articles"][0]["id"] == public
    assert client.get(f"/api/v1/articles/{public}", headers=req).json()["views"] == 1
    assert client.get("/api/v1/reports/summary?days=90", headers=req).status_code == 403
    summary = client.get("/api/v1/reports/summary?days=90", headers=agent).json()
    assert summary["days"] == 90 and "sla_resolution" in summary
    bad = client.get("/api/v1/reports/summary?days=12", headers=agent)
    assert bad.status_code == 400 and bad.json()["error"] == "error.period_invalid"
    tid = tickets.create_ticket(conn, people["req"], "Mouse", "broken", "hardware")
    tickets.change_status(conn, people["agent"], tid, "resolved")
    assert client.post(f"/api/v1/tickets/{tid}/rate", headers=req, json={"score": 9}).status_code == 422
    r = client.post(f"/api/v1/tickets/{tid}/rate", headers=req, json={"score": 4})
    assert r.json() == {"ticket_id": tid, "satisfaction": 4}


def test_demo_has_history_articles_and_ratings(tmp_path, capsys):
    db = str(tmp_path / "demo.db")
    assert cli.main(["--db", db, "demo", "--lang", "es", "--history", "40"]) == 0
    from deskscope.db import connect

    conn = connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM articles WHERE status = 'draft'").fetchone()[0] == 1
        rated = conn.execute("SELECT COUNT(*) FROM tickets WHERE satisfaction IS NOT NULL").fetchone()[0]
        assert rated >= 3
        closed = conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'closed'").fetchone()[0]
        assert closed > 30
        bad = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE resolved_at < created_at OR closed_at < resolved_at"
            " OR first_response_at > resolved_at").fetchone()[0]
        assert bad == 0
    finally:
        conn.close()
