"""Web interface, end to end through the FastAPI test client."""

from __future__ import annotations

import pytest
from conftest import PASSWORD, csrf_from, login

from deskscope import tickets, users
from deskscope.web import safe_next


def open_ticket(client, title="Monitor flickers", **extra):
    data = {"title": title, "description": "Right screen", "category": "hardware",
            "impact": "low", "urgency": "medium", "device": "", "csrf_token": csrf_from(client, "/tickets/new")}
    data.update(extra)
    return client.post("/tickets/new", data=data, follow_redirects=False)


def test_pages_need_a_session(client):
    r = client.get("/tickets?status=any", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/tickets%3Fstatus%3Dany"


def test_login_and_logout(client):
    r = login(client, "ana")
    assert r.status_code == 303 and r.headers["location"] == "/tickets"
    page = client.get("/tickets")
    assert "Ana Example" in page.text
    r = client.post("/logout", data={"csrf_token": csrf_from(client)}, follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert client.get("/tickets", follow_redirects=False).status_code == 303


def test_wrong_password(client):
    r = login(client, "ana", "not-the-password")
    assert r.status_code == 401
    assert "Wrong username or password" in r.text


def test_login_is_throttled(client):
    for _ in range(5):
        assert login(client, "ana", "bad-password-x").status_code == 401
    r = login(client, "ana", PASSWORD)  # even the right password waits now
    assert r.status_code == 429


def test_login_rejects_offsite_next(client):
    token = csrf_from(client, "/login")
    r = client.post("/login", data={"username": "ana", "password": PASSWORD, "csrf_token": token,
                                    "next": "//evil.example.com"}, follow_redirects=False)
    assert r.headers["location"] == "/tickets"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("/tickets/3", "/tickets/3"), ("//evil.example.com", "/tickets"),
     ("https://evil.example.com", "/tickets"), ("/\\evil.example.com", "/tickets"),
     (None, "/tickets"), ("", "/tickets")],
)
def test_safe_next(value, expected):
    assert safe_next(value) == expected


def test_forms_without_csrf_token_are_rejected(client):
    login(client, "ana")
    r = client.post("/tickets/new", data={"title": "x", "description": "y", "category": "other"})
    assert r.status_code == 400
    assert "form expired" in r.text
    r = client.post("/tickets/new", data={"title": "x", "description": "y", "category": "other",
                                          "csrf_token": "forged"})
    assert r.status_code == 400


def test_open_a_ticket_and_talk_about_it(client, conn, people):
    login(client, "ana")
    r = open_ticket(client)
    assert r.status_code == 303
    url = r.headers["location"]
    page = client.get(url)
    assert "Monitor flickers" in page.text and "Ticket #1 opened." in page.text
    # The flash message is shown once.
    assert "Ticket #1 opened." not in client.get(url).text

    token = csrf_from(client, url)
    client.post(f"{url}/comment", data={"body": "It's worse in the afternoon", "csrf_token": token})
    assert tickets.list_comments(conn, people["req"], 1)[0]["body"] == "It's worse in the afternoon"


def test_invalid_ticket_form_keeps_what_was_typed(client):
    login(client, "ana")
    r = open_ticket(client, title="", description="Long explanation I don't want to retype")
    assert r.status_code == 400
    assert "Long explanation I don&#39;t want to retype" in r.text
    assert "The summary is required" in r.text


def test_html_is_escaped(client):
    login(client, "ana")
    url = open_ticket(client, title="<script>alert(1)</script>").headers["location"]
    page = client.get(url).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_requesters_get_404_for_other_peoples_tickets(client, conn, people):
    tid = tickets.create_ticket(conn, people["req2"], "Bob's laptop", "Broken", "hardware")
    login(client, "ana")
    assert client.get(f"/tickets/{tid}").status_code == 404
    token = csrf_from(client)
    r = client.post(f"/tickets/{tid}/comment", data={"body": "hi", "csrf_token": token})
    assert r.status_code == 404
    assert "Bob's laptop" not in client.get("/tickets?status=any").text


def test_requesters_do_not_see_staff_controls(client, conn, people):
    tid = tickets.create_ticket(conn, people["req"], "Mine", "Text", "other")
    login(client, "ana")
    page = client.get(f"/tickets/{tid}").text
    assert "/assign" not in page and "Internal note" not in page
    token = csrf_from(client)
    r = client.post(f"/tickets/{tid}/assign", data={"assignee": str(people["agent"].id),
                                                    "csrf_token": token})
    assert r.status_code == 403


def test_technician_works_a_ticket(client, conn, people):
    tid = tickets.create_ticket(conn, people["req"], "VPN drops", "Every 10 minutes", "network")
    login(client, "tech1")
    url = f"/tickets/{tid}"
    token = csrf_from(client, url)
    client.post(f"{url}/assign", data={"assignee": str(people["agent"].id), "csrf_token": token})
    client.post(f"{url}/classify", data={"impact": "high", "urgency": "high",
                                         "csrf_token": token})
    client.post(f"{url}/comment", data={"body": "UDP 4500 blocked", "internal": "on",
                                        "csrf_token": token})
    r = client.post(f"{url}/status", data={"status": "resolved", "csrf_token": token})
    assert "Ticket updated." in r.text
    t = tickets.get_ticket(conn, people["agent"], tid)
    assert (t["status"], t["priority"], t["assignee_id"]) == ("resolved", "critical",
                                                              people["agent"].id)
    assert "UDP 4500 blocked" in r.text and "Internal" in r.text
    assert "changed the status from In progress to Resolved" in r.text


def test_invalid_transition_shows_a_message(client, conn, people):
    tid = tickets.create_ticket(conn, people["req"], "x title", "y", "other")
    tickets.change_status(conn, people["agent"], tid, "closed")
    login(client, "tech1")
    token = csrf_from(client)
    r = client.post(f"/tickets/{tid}/comment", data={"body": "late", "csrf_token": token})
    assert "The ticket is closed." in r.text
    r = client.post(f"/tickets/{tid}/status", data={"status": "open", "csrf_token": token})
    assert r.status_code == 403


def test_ticket_list_filters(client, conn, people):
    tickets.create_ticket(conn, people["req"], "Printer jam", "Floor 2", "hardware")
    tickets.create_ticket(conn, people["req2"], "Outlook crash", "Calendar", "software")
    login(client, "tech1")
    page = client.get("/tickets?q=printer").text
    assert "Printer jam" in page and "Outlook crash" not in page
    page = client.get("/tickets?scope=bogus&status=bogus&sort=bogus").text
    assert "Printer jam" in page and "Outlook crash" in page
    assert "No tickets match" in client.get("/tickets?status=closed").text


def test_user_administration(client, conn, people):
    login(client, "tech1")
    assert client.get("/admin/users").status_code == 404  # hidden from non-admins
    client.post("/logout", data={"csrf_token": csrf_from(client)})

    login(client, "admin")
    page = client.get("/admin/users").text
    assert "Tina Tech" in page
    token = csrf_from(client, "/admin/users")
    r = client.post("/admin/users", data={"username": "new.hire", "full_name": "New Hire",
                                          "email": "new.hire@example.com", "role": "requester",
                                          "password": "welcome-aboard-1", "csrf_token": token})
    assert "User new.hire created." in r.text
    r = client.post("/admin/users", data={"username": "x", "full_name": "X", "email": "x@x.io",
                                          "role": "requester", "password": "welcome-aboard-1",
                                          "csrf_token": token})
    assert r.status_code == 400 and "Usernames have 3 to 32" in r.text

    client.post(f"/admin/users/{people['req'].id}/active", data={"active": "0", "csrf_token": token})
    r = client.post(f"/admin/users/{people['admin'].id}/active",
                    data={"active": "0", "csrf_token": token})
    assert "You cannot disable your own account." in r.text


def test_disabled_user_loses_the_session(client, conn, people):
    login(client, "ana")
    assert client.get("/tickets", follow_redirects=False).status_code == 200
    users.set_active(conn, people["admin"], people["req"].id, False)
    assert client.get("/tickets", follow_redirects=False).status_code == 303


def test_spanish_interface(client):
    r = client.get("/lang/es?next=/login", follow_redirects=False)
    assert r.headers["location"] == "/login"
    login(client, "ana")
    page = client.get("/tickets").text
    assert '<html lang="es">' in page and "Nuevo ticket" in page
    assert client.get("/lang/es?next=//evil.example.com",
                      follow_redirects=False).headers["location"] == "/tickets"


def test_browser_language_is_the_default(client):
    page = client.get("/login", headers={"accept-language": "es-ES,es;q=0.9"}).text
    assert "Iniciar sesión" in page
    page = client.get("/login", headers={"accept-language": "de-DE"}).text
    assert "Sign in" in page


def test_security_headers(client):
    r = client.get("/login")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_healthz_and_unknown_pages(client):
    assert client.get("/healthz").json()["status"] == "ok"
    r = client.get("/nope")
    assert r.status_code == 404 and "Page not found." in r.text
    assert client.get("/static/style.css").status_code == 200
