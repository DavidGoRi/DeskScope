"""SLA engine: business calendar, clocks, pauses, configuration and migration."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import PASSWORD, csrf_from, login

from deskscope import cli, sla, tickets, users
from deskscope.db import SCHEMA_VERSION, connect, migrate
from deskscope.sla import BusinessCalendar, SlaConfigError, SlaPolicy, evaluate, policy_from_dict
from deskscope.tickets import TicketFilter

MADRID = ZoneInfo("Europe/Madrid")
ROOT = Path(__file__).resolve().parents[1]
# Monday 16 Nov 2026 is a working day; Tuesday 8 Dec 2026 is a holiday in the tests.
CAL = BusinessCalendar(tz=MADRID, holidays=frozenset({date(2026, 12, 8)}))
POLICY = SlaPolicy(calendar=CAL)


def at(day: int, hour: float, month: int = 11) -> datetime:
    h, m = int(hour), int(round((hour % 1) * 60))
    return datetime(2026, month, day, h, m, tzinfo=MADRID)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


# ---- business calendar ------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end", "hours"),
    [
        (at(16, 10), at(16, 12), 2),          # same morning
        (at(16, 7), at(16, 10), 1),           # before opening counts from 09:00
        (at(16, 17), at(17, 10), 2),          # overnight
        (at(20, 17), at(23, 10), 2),          # Friday -> Monday skips the weekend
        (at(21, 10), at(22, 18), 0),          # a whole weekend
        (at(7, 17, 12), at(9, 10, 12), 2),    # Monday -> Wednesday skips the holiday
        (at(16, 12), at(16, 10), 0),          # end before start
        (at(16, 9), at(23, 9), 45),           # one working week
    ],
)
def test_business_seconds(start, end, hours):
    assert CAL.business_seconds(start, end) == hours * 3600


@pytest.mark.parametrize(
    ("start", "hours", "expected"),
    [
        (at(16, 10), 2, at(16, 12)),
        (at(16, 17), 2, at(17, 10)),
        (at(20, 17), 2, at(23, 10)),          # Friday afternoon -> Monday
        (at(21, 12), 1, at(23, 10)),          # opened on Saturday
        (at(20, 20), 0, at(23, 9)),           # zero from outside hours -> next opening
        (at(7, 17, 12), 2, at(9, 10, 12)),    # skips the holiday
        (at(16, 9), 18, at(17, 18)),          # exactly two days
    ],
)
def test_add_business_seconds(start, hours, expected):
    assert CAL.add_business_seconds(start, hours * 3600) == expected


def test_add_and_measure_are_inverse():
    start = at(18, 15.25)
    for minutes in (1, 59, 600, 2400, 9999):
        due = CAL.add_business_seconds(start, minutes * 60)
        assert CAL.business_seconds(start, due) == minutes * 60


def test_works_with_utc_inputs():
    start = at(16, 10).astimezone(timezone.utc)
    assert CAL.add_business_seconds(start, 3600) == at(16, 11)


def test_calendar_without_working_time_is_an_error():
    empty = BusinessCalendar(tz=MADRID, weekdays=frozenset())
    with pytest.raises(SlaConfigError):
        empty.add_business_seconds(at(16, 10), 60)


# ---- clocks -----------------------------------------------------------------


def ticket(priority="high", created=None, status="open", **extra):
    data = {"priority": priority, "status": status, "created_at": iso(created or at(16, 9)),
            "first_response_at": None, "resolved_at": None}
    data.update({k: iso(v) if isinstance(v, datetime) else v for k, v in extra.items()})
    return data


def test_response_met_and_breached():
    # high: respond within 60 business minutes
    ok = evaluate(POLICY, ticket(first_response_at=at(16, 9.5)), [], at(16, 12))
    assert ok.response.state == "met" and ok.response.due == at(16, 10)
    late = evaluate(POLICY, ticket(first_response_at=at(16, 11)), [], at(16, 12))
    assert late.response.state == "breached"
    assert late.breached


@pytest.mark.parametrize(
    ("now", "state"),
    [(at(16, 9.5), "running"), (at(16, 9.8), "at_risk"), (at(16, 10.5), "breached")],
)
def test_open_response_clock(now, state):
    clock = evaluate(POLICY, ticket(), [], now).response
    assert clock.state == state and not clock.finished


def test_nights_and_weekends_do_not_count():
    # Opened Friday 17:30 with 60 min to answer: due Monday 09:30, fine on Saturday.
    t = ticket(created=at(20, 17.5))
    result = evaluate(POLICY, t, [], at(21, 12))
    assert result.response.due == at(23, 9.5)
    assert result.response.used_seconds == 30 * 60  # only 17:30-18:00 on Friday
    assert result.response.state == "running"


def test_waiting_for_the_user_pauses_the_resolution_clock():
    # high: resolve within 8 business hours. Pending from 10:00 to 14:00 on Monday.
    events = [(iso(at(16, 9.5)), "new", "open"), (iso(at(16, 10)), "open", "pending"),
              (iso(at(16, 14)), "pending", "open"), (iso(at(16, 15)), "open", "resolved")]
    t = ticket(status="resolved", resolved_at=at(16, 15), first_response_at=at(16, 9.5))
    clock = evaluate(POLICY, t, events, at(17, 12)).resolution
    assert clock.used_seconds == 2 * 3600  # 6 h elapsed - 4 h paused
    assert clock.state == "met"
    assert clock.due == at(17, 12)  # 8 h + 4 h of pause from Monday 09:00


def test_paused_clock_while_still_pending():
    events = [(iso(at(16, 9.5)), "new", "pending")]
    t = ticket(status="pending", first_response_at=at(16, 9.5))
    result = evaluate(POLICY, t, events, at(18, 12))
    assert result.resolution.state == "paused"
    assert result.resolution.used_seconds == 30 * 60
    # 7.5 h of the target remain, counted from now: Wednesday 12:00 + 6 h -> Thursday 10:30.
    assert result.resolution.due == at(19, 10.5)


def test_breached_before_the_pause_stays_breached():
    events = [(iso(at(17, 10)), "open", "pending")]
    t = ticket(status="pending", first_response_at=at(16, 9.5))
    assert evaluate(POLICY, t, events, at(18, 12)).resolution.state == "breached"


def test_worst_clock_shown_in_lists():
    t = ticket(first_response_at=at(16, 9.2))
    result = evaluate(POLICY, t, [], at(16, 16))
    assert result.response.state == "met"
    assert result.worst.name == "resolution"  # the clock that still runs
    done = ticket(status="resolved", first_response_at=at(16, 11), resolved_at=at(16, 12))
    result = evaluate(POLICY, done, [], at(16, 13))
    assert result.worst.name == "response" and result.worst.state == "breached"


def test_targets_depend_on_priority():
    low = evaluate(POLICY, ticket("low"), [], at(16, 10)).response
    crit = evaluate(POLICY, ticket("critical"), [], at(16, 10)).response
    assert (low.state, crit.state) == ("running", "breached")
    assert low.target_seconds == 8 * 3600 and crit.target_seconds == 30 * 60


# ---- configuration ----------------------------------------------------------


def test_example_file_is_valid():
    policy = sla.load_policy(ROOT / "sla.example.json")
    assert policy.calendar.tz.key == "Europe/Madrid"
    assert date(2026, 12, 25) in policy.calendar.holidays
    assert policy.targets["critical"] == {"response": 30, "resolution": 240}


def test_missing_file_means_defaults(tmp_path):
    policy = sla.load_policy(tmp_path / "nope.json")
    assert policy.targets == sla.DEFAULT_TARGETS
    assert policy.calendar.start == time(9) and policy.calendar.end == time(18)


def test_partial_config_keeps_the_other_defaults():
    policy = policy_from_dict({"targets_minutes": {"low": {"resolution": 9000}},
                               "business_hours": {"days": ["mon", "sat"], "timezone": "UTC"}})
    assert policy.targets["low"] == {"response": 480, "resolution": 9000}
    assert policy.calendar.weekdays == frozenset({0, 5})


@pytest.mark.parametrize(
    "data",
    [
        {"business_hours": {"timezone": "Mars/Olympus"}},
        {"business_hours": {"days": ["monday"]}},
        {"business_hours": {"days": []}},
        {"business_hours": {"start": "18:00", "end": "09:00"}},
        {"business_hours": {"start": "9am"}},
        {"holidays": ["25/12/2026"]},
        {"targets_minutes": {"urgent": {"response": 5}}},
        {"targets_minutes": {"high": {"response": 0}}},
        {"targets_minutes": {"high": {"response": "1h"}}},
        {"targets_minutes": {"high": {"response": 600, "resolution": 60}}},
        {"warn_ratio": 1.5},
    ],
)
def test_invalid_config(data):
    with pytest.raises(SlaConfigError):
        policy_from_dict(data)


def test_invalid_json(tmp_path):
    bad = tmp_path / "sla.json"
    bad.write_text("{ not json", "utf-8")
    with pytest.raises(SlaConfigError, match="invalid JSON"):
        sla.load_policy(bad)


def test_cli_shows_and_validates_the_policy(tmp_path, capsys):
    db = tmp_path / "desk.db"
    assert cli.main(["--db", str(db), "sla"]) == 0
    assert "built-in defaults" in capsys.readouterr().out
    assert cli.main(["--db", str(db), "--sla", str(ROOT / "sla.example.json"), "sla"]) == 0
    assert "Holidays: 17" in capsys.readouterr().out
    (tmp_path / "sla.json").write_text(json.dumps({"warn_ratio": 7}), "utf-8")
    with pytest.raises(SystemExit, match="Invalid SLA configuration"):
        cli.main(["--db", str(db), "sla"])
    with pytest.raises(SystemExit, match="not found"):
        cli.main(["--db", str(db), "--sla", str(tmp_path / "missing.json"), "sla"])


# ---- migration from schema v1 -------------------------------------------------


def test_migration_from_v1_keeps_priorities_and_backfills_responses(tmp_path):
    path = tmp_path / "old.db"
    conn = connect(path)
    first = resources.files("deskscope").joinpath("migrations/001_initial.sql").read_text("utf-8")
    conn.executescript(first + "\nPRAGMA user_version = 1;")
    conn.executescript("""
        INSERT INTO users VALUES (1, 'emp', 'Emp', 'e@example.com', 'requester', 'h', 1, 't');
        INSERT INTO users VALUES (2, 'tech', 'Tech', 't@example.com', 'agent', 'h', 1, 't');
        INSERT INTO tickets (id, title, description, category, priority, status, requester_id,
            created_at, updated_at, resolved_at) VALUES
            (1, 'a', 'd', 'other', 'critical', 'open', 1, '2026-01-01T09:00:00+00:00', 'x', NULL),
            (2, 'b', 'd', 'other', 'high', 'resolved', 1, '2026-01-01T09:00:00+00:00', 'x',
             '2026-01-01T12:00:00+00:00'),
            (3, 'c', 'd', 'other', 'medium', 'open', 1, '2026-01-01T09:00:00+00:00', 'x', NULL),
            (4, 'd', 'd', 'other', 'low', 'open', 1, '2026-01-01T09:00:00+00:00', 'x', NULL);
        INSERT INTO comments (ticket_id, author_id, body, internal, created_at) VALUES
            (1, 1, 'requester first', 0, '2026-01-01T09:05:00+00:00'),
            (1, 2, 'internal note', 1, '2026-01-01T09:10:00+00:00'),
            (1, 2, 'public answer', 0, '2026-01-01T09:20:00+00:00'),
            (2, 2, 'answer after fix', 0, '2026-01-01T13:00:00+00:00');
    """)
    assert migrate(conn) == SCHEMA_VERSION
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM tickets")}
    assert [(rows[i]["impact"], rows[i]["urgency"]) for i in (1, 2, 3, 4)] == [
        ("high", "high"), ("medium", "high"), ("medium", "medium"), ("low", "low")]
    assert rows[1]["first_response_at"] == "2026-01-01T09:20:00+00:00"
    assert rows[2]["first_response_at"] == "2026-01-01T12:00:00+00:00"  # the fix came first
    assert rows[3]["first_response_at"] is None
    conn.close()


# ---- tickets with SLA ---------------------------------------------------------


def test_first_response_is_the_first_public_staff_answer(conn, people):
    tid = tickets.create_ticket(conn, people["req"], "VPN", "drops", "network")
    tickets.add_comment(conn, people["req"], tid, "more details")
    tickets.add_comment(conn, people["agent"], tid, "looking", internal=True)
    tickets.assign(conn, people["agent"], tid, people["agent"].id)
    assert tickets.get_ticket(conn, people["agent"], tid)["first_response_at"] is None
    tickets.add_comment(conn, people["agent"], tid, "Try the TCP profile")
    first = tickets.get_ticket(conn, people["agent"], tid)["first_response_at"]
    assert first
    tickets.add_comment(conn, people["agent2"], tid, "second answer")
    tickets.change_status(conn, people["agent"], tid, "resolved")
    assert tickets.get_ticket(conn, people["agent"], tid)["first_response_at"] == first


def test_resolving_counts_as_a_response(conn, people):
    tid = tickets.create_ticket(conn, people["req"], "Mouse", "broken", "hardware")
    tickets.change_status(conn, people["agent"], tid, "resolved")
    t = tickets.get_ticket(conn, people["agent"], tid)
    assert t["first_response_at"] == t["resolved_at"]


def _backdate(conn, tid, created):
    conn.execute("UPDATE tickets SET created_at = ? WHERE id = ?", (iso(created), tid))
    conn.execute("UPDATE ticket_events SET created_at = ? WHERE ticket_id = ?", (iso(created), tid))
    conn.commit()


def test_list_filter_and_sort_by_sla(conn, people):
    agent = people["agent"]
    late = tickets.create_ticket(conn, people["req"], "Late", "x", "other", "medium", "high")
    risky = tickets.create_ticket(conn, people["req"], "Risky", "x", "other", "medium", "high")
    fine = tickets.create_ticket(conn, people["req"], "Fine", "x", "other", "low", "low")
    now = at(16, 12)
    _backdate(conn, late, at(16, 9))      # high: 60 min to answer -> breached at 12:00
    _backdate(conn, risky, at(16, 11.2))  # 48 of 60 minutes used -> at risk
    _backdate(conn, fine, at(16, 11.5))   # low: 8 h to answer
    flt = TicketFilter(sort="sla")
    got = [t["title"] for t in tickets.list_with_sla(conn, agent, flt, POLICY, now)]
    assert got == ["Late", "Risky", "Fine"]
    flt = TicketFilter(sla_problems=True)
    got = {t["title"] for t in tickets.list_with_sla(conn, agent, flt, POLICY, now)}
    assert got == {"Late", "Risky"}
    items = tickets.list_with_sla(conn, agent, TicketFilter(), POLICY, now)
    assert tickets.sla_summary(items) == {"at_risk": 1, "breached": 1}


def test_sla_is_shown_to_staff_only(client, conn, people):
    tid = tickets.create_ticket(conn, people["req"], "VPN drops", "x", "network", "medium", "high")
    login(client, "ana")
    assert "First response" not in client.get(f"/tickets/{tid}").text
    assert "SLA" not in client.get("/tickets").text
    client.post("/logout", data={"csrf_token": csrf_from(client)})
    login(client, "tech1")
    page = client.get(f"/tickets/{tid}").text
    assert "First response" in page and "Resolution" in page and "Business hours" in page
    page = client.get("/tickets?sla=problems&sort=sla").text
    assert "SLA at risk or breached" in page


def test_triage_from_the_ticket_page(client, conn, people):
    tid = tickets.create_ticket(conn, people["req"], "Label printer", "x", "hardware")
    login(client, "tech1")
    token = csrf_from(client, f"/tickets/{tid}")
    r = client.post(f"/tickets/{tid}/classify",
                    data={"impact": "high", "urgency": "high", "csrf_token": token})
    assert "changed the impact from medium impact to high impact" in r.text
    assert tickets.get_ticket(conn, people["agent"], tid)["priority"] == "critical"
    r = client.post(f"/tickets/{tid}/classify",
                    data={"impact": "x", "urgency": "high", "csrf_token": token})
    assert "Choose a valid impact and urgency." in r.text


def test_new_ticket_form_explains_impact_and_urgency(client):
    login(client, "ana")
    page = client.get("/tickets/new").text
    assert "Who is affected?" in page and "Can you keep working?" in page
    assert 'data-matrix="{&#34;high|high&#34;: &#34;critical&#34;' in page


def test_app_uses_the_given_policy(db_path, people):
    from fastapi.testclient import TestClient

    from deskscope.web import create_app

    policy = policy_from_dict({"business_hours": {"timezone": "America/New_York",
                                                  "start": "08:00", "end": "16:00"}})
    with TestClient(create_app(db_path, "k", sla_policy=policy)) as c:
        conn = connect(db_path)
        tid = tickets.create_ticket(conn, users.get_by_username(conn, "ana"), "x", "y", "other")
        conn.close()
        login(c, "tech1", PASSWORD)
        assert "08:00–16:00, America/New_York" in c.get(f"/tickets/{tid}").text
