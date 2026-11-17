"""Ticket rules: validation, visibility, workflow, comments and audit trail."""

from __future__ import annotations

import pytest

from deskscope import tickets, users
from deskscope.models import NotFound, PermissionDenied, ValidationError
from deskscope.tickets import TicketFilter


def new(conn, actor, title="Printer does not print", **kw):
    kw.setdefault("description", "Blank pages since this morning.")
    kw.setdefault("category", "hardware")
    return tickets.create_ticket(conn, actor, title, **kw)


def ids(items):
    return [t["id"] for t in items]


# ---- creation ---------------------------------------------------------------


def test_create_ticket_defaults(conn, people):
    tid = new(conn, people["req"], device=" fin-lt03 ")
    t = tickets.get_ticket(conn, people["req"], tid)
    assert t["status"] == "new" and t["priority"] == "medium"
    assert t["device"] == "FIN-LT03"
    assert t["requester_name"] == "Ana Example" and t["assignee_id"] is None
    events = tickets.list_events(conn, people["req"], tid)
    assert [(e["field"], e["new_value"]) for e in events] == [("created", "new")]


@pytest.mark.parametrize(
    ("impact", "urgency", "priority"),
    [("high", "high", "critical"), ("high", "medium", "high"), ("medium", "high", "high"),
     ("high", "low", "medium"), ("low", "high", "medium"), ("medium", "medium", "medium"),
     ("medium", "low", "low"), ("low", "medium", "low"), ("low", "low", "low")],
)
def test_priority_follows_the_itil_matrix(conn, people, impact, urgency, priority):
    tid = new(conn, people["req"], impact=impact, urgency=urgency)
    t = tickets.get_ticket(conn, people["req"], tid)
    assert (t["impact"], t["urgency"], t["priority"]) == (impact, urgency, priority)


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"title": "   "}, "error.title_invalid"),
        ({"title": "x" * 121}, "error.title_invalid"),
        ({"description": ""}, "error.description_invalid"),
        ({"description": "x" * 5001}, "error.description_invalid"),
        ({"category": "coffee"}, "error.category_invalid"),
        ({"impact": "huge"}, "error.classification_invalid"),
        ({"urgency": "yesterday"}, "error.classification_invalid"),
        ({"device": "PC 01"}, "error.device_invalid"),
        ({"device": "X" * 65}, "error.device_invalid"),
    ],
)
def test_create_validation(conn, people, kwargs, key):
    with pytest.raises(ValidationError) as exc:
        new(conn, people["req"], **kwargs)
    assert exc.value.key == key


# ---- visibility -------------------------------------------------------------


def test_requesters_only_see_their_own_tickets(conn, people):
    mine = new(conn, people["req"])
    theirs = new(conn, people["req2"])
    with pytest.raises(NotFound):
        tickets.get_ticket(conn, people["req"], theirs)
    with pytest.raises(NotFound):
        tickets.list_comments(conn, people["req"], theirs)
    # Asking for another scope does not widen what a requester sees.
    for scope in tickets.SCOPES:
        assert ids(tickets.list_tickets(conn, people["req"], TicketFilter(scope=scope))) == [mine]
    assert tickets.status_counts(conn, people["req"])["new"] == 1


def test_staff_see_everything_and_can_narrow_it(conn, people):
    a = new(conn, people["req"])
    b = new(conn, people["req2"])
    c = new(conn, people["agent"])
    tickets.assign(conn, people["agent"], b, people["agent"].id)
    staff = people["agent"]
    assert set(ids(tickets.list_tickets(conn, staff))) == {a, b, c}
    assert ids(tickets.list_tickets(conn, staff, TicketFilter(scope="mine"))) == [c]
    assert ids(tickets.list_tickets(conn, staff, TicketFilter(scope="assigned"))) == [b]
    assert set(ids(tickets.list_tickets(conn, staff, TicketFilter(scope="unassigned")))) == {a, c}
    assert tickets.status_counts(conn, staff) == {
        "new": 2, "open": 1, "pending": 0, "resolved": 0, "closed": 0
    }


def test_missing_ticket_is_not_found(conn, people):
    with pytest.raises(NotFound):
        tickets.get_ticket(conn, people["admin"], 12345)


def test_status_filter(conn, people):
    a = new(conn, people["req"])
    b = new(conn, people["req"])
    tickets.change_status(conn, people["agent"], b, "resolved")
    agent = people["agent"]
    assert ids(tickets.list_tickets(conn, agent, TicketFilter(status="active"))) == [a]
    assert ids(tickets.list_tickets(conn, agent, TicketFilter(status="resolved"))) == [b]
    assert set(ids(tickets.list_tickets(conn, agent, TicketFilter(status="any")))) == {a, b}


def test_search(conn, people):
    a = new(conn, people["req"], "VPN drops", description="Every 10 minutes", category="network")
    b = new(conn, people["req"], "Excel 100% CPU", device="FIN-PC02", category="software")
    agent = people["agent"]

    def find(q):
        return ids(tickets.list_tickets(conn, agent, TicketFilter(status="any", query=q)))

    assert find("vpn") == [a]
    assert find("minutes") == [a]
    assert find("fin-pc") == [b]
    assert find(f"#{b}") == [b]
    assert find(str(a)) == [a]
    assert find("100%") == [b]
    assert find("%") == [b]  # the wildcard is escaped, not "match everything"
    assert find("_") == []


def test_sort_by_priority(conn, people):
    low = new(conn, people["agent"], impact="low", urgency="low")
    crit = new(conn, people["agent"], impact="high", urgency="high")
    med = new(conn, people["agent"], impact="medium", urgency="medium")
    got = ids(tickets.list_tickets(conn, people["agent"], TicketFilter(sort="priority")))
    assert got == [crit, med, low]


# ---- workflow ---------------------------------------------------------------


def test_full_staff_workflow_sets_timestamps(conn, people):
    agent = people["agent"]
    tid = new(conn, people["req"])
    tickets.change_status(conn, agent, tid, "open")
    tickets.change_status(conn, agent, tid, "pending")
    tickets.change_status(conn, agent, tid, "resolved")
    t = tickets.get_ticket(conn, agent, tid)
    assert t["status"] == "resolved" and t["resolved_at"] and not t["closed_at"]
    tickets.change_status(conn, agent, tid, "open")  # the fix did not hold
    assert tickets.get_ticket(conn, agent, tid)["resolved_at"] is None
    tickets.change_status(conn, agent, tid, "closed")
    t = tickets.get_ticket(conn, agent, tid)
    assert t["closed_at"] and t["resolved_at"]
    history = [(e["old_value"], e["new_value"]) for e in tickets.list_events(conn, agent, tid)
               if e["field"] == "status"]
    assert history == [("new", "open"), ("open", "pending"), ("pending", "resolved"),
                       ("resolved", "open"), ("open", "closed")]


def test_closed_is_final(conn, people):
    tid = new(conn, people["req"])
    tickets.change_status(conn, people["agent"], tid, "closed")
    t = tickets.get_ticket(conn, people["agent"], tid)
    assert t["transitions"] == []
    with pytest.raises(PermissionDenied):
        tickets.change_status(conn, people["admin"], tid, "open")
    with pytest.raises(ValidationError):
        tickets.add_comment(conn, people["agent"], tid, "late note")
    with pytest.raises(ValidationError):
        tickets.assign(conn, people["agent"], tid, people["agent"].id)
    with pytest.raises(ValidationError):
        tickets.set_classification(conn, people["agent"], tid, "low", "low")


def test_requester_transitions(conn, people):
    req = people["req"]
    tid = new(conn, req)
    assert tickets.get_ticket(conn, req, tid)["transitions"] == ["closed"]
    with pytest.raises(PermissionDenied):
        tickets.change_status(conn, req, tid, "resolved")
    tickets.change_status(conn, people["agent"], tid, "resolved")
    assert tickets.get_ticket(conn, req, tid)["transitions"] == ["open", "closed"]
    tickets.change_status(conn, req, tid, "open")  # "it still fails"
    assert tickets.get_ticket(conn, req, tid)["transitions"] == []
    with pytest.raises(PermissionDenied):
        tickets.change_status(conn, req, tid, "closed")


def test_requester_can_withdraw_a_new_ticket(conn, people):
    tid = new(conn, people["req"])
    tickets.change_status(conn, people["req"], tid, "closed")
    assert tickets.get_ticket(conn, people["req"], tid)["status"] == "closed"


def test_unknown_status_and_no_op(conn, people):
    tid = new(conn, people["req"])
    with pytest.raises(ValidationError):
        tickets.change_status(conn, people["agent"], tid, "on-fire")
    tickets.change_status(conn, people["agent"], tid, "new")  # same status: nothing logged
    assert len(tickets.list_events(conn, people["agent"], tid)) == 1


def test_other_requesters_cannot_touch_a_ticket(conn, people):
    tid = new(conn, people["req"])
    with pytest.raises(NotFound):
        tickets.change_status(conn, people["req2"], tid, "closed")
    with pytest.raises(NotFound):
        tickets.add_comment(conn, people["req2"], tid, "hello")


# ---- assignment and priority ------------------------------------------------


def test_assigning_a_new_ticket_starts_work(conn, people):
    agent = people["agent"]
    tid = new(conn, people["req"])
    tickets.assign(conn, agent, tid, people["agent2"].id)
    t = tickets.get_ticket(conn, agent, tid)
    assert t["assignee_name"] == "Tina Tech" and t["status"] == "open"
    tickets.assign(conn, agent, tid, people["agent2"].id)  # no change, no event
    tickets.assign(conn, agent, tid, None)
    events = [(e["field"], e["old_value"], e["new_value"])
              for e in tickets.list_events(conn, agent, tid)]
    assert events == [("created", None, "new"), ("assignee", None, "Tina Tech"),
                      ("status", "new", "open"), ("assignee", "Tina Tech", None)]
    assert tickets.get_ticket(conn, agent, tid)["status"] == "open"


def test_assignee_must_be_active_staff(conn, people):
    tid = new(conn, people["req"])
    with pytest.raises(ValidationError):
        tickets.assign(conn, people["agent"], tid, people["req2"].id)
    with pytest.raises(ValidationError):
        tickets.assign(conn, people["agent"], tid, 999)
    users.set_active(conn, people["admin"], people["agent2"].id, False)
    with pytest.raises(ValidationError):
        tickets.assign(conn, people["agent"], tid, people["agent2"].id)


def test_only_staff_assign_and_triage(conn, people):
    tid = new(conn, people["req"])
    with pytest.raises(PermissionDenied):
        tickets.assign(conn, people["req"], tid, people["agent"].id)
    with pytest.raises(PermissionDenied):
        tickets.set_classification(conn, people["req"], tid, "high", "high")
    tickets.set_classification(conn, people["agent"], tid, "high", "medium")
    tickets.set_classification(conn, people["agent"], tid, "high", "medium")  # no change
    with pytest.raises(ValidationError):
        tickets.set_classification(conn, people["agent"], tid, "asap", "high")
    changes = [(e["field"], e["old_value"], e["new_value"])
               for e in tickets.list_events(conn, people["agent"], tid) if e["field"] != "created"]
    assert changes == [("impact", "medium", "high"), ("priority", "medium", "high")]
    assert tickets.get_ticket(conn, people["agent"], tid)["priority"] == "high"


# ---- comments ---------------------------------------------------------------


def test_internal_notes_are_hidden_from_requesters(conn, people):
    tid = new(conn, people["req"])
    tickets.add_comment(conn, people["agent"], tid, "Can you restart it?")
    tickets.add_comment(conn, people["agent"], tid, "Toner looks fake", internal=True)
    tickets.add_comment(conn, people["req"], tid, "Restarted, same thing")
    seen_by_req = [c["body"] for c in tickets.list_comments(conn, people["req"], tid)]
    seen_by_staff = [c["body"] for c in tickets.list_comments(conn, people["agent"], tid)]
    assert seen_by_req == ["Can you restart it?", "Restarted, same thing"]
    assert seen_by_staff == ["Can you restart it?", "Toner looks fake", "Restarted, same thing"]


def test_requesters_cannot_write_internal_notes(conn, people):
    tid = new(conn, people["req"])
    with pytest.raises(PermissionDenied):
        tickets.add_comment(conn, people["req"], tid, "secret", internal=True)


def test_requester_reply_puts_a_pending_ticket_back_in_the_queue(conn, people):
    tid = new(conn, people["req"])
    tickets.change_status(conn, people["agent"], tid, "pending")
    tickets.add_comment(conn, people["agent"], tid, "still waiting", internal=True)
    assert tickets.get_ticket(conn, people["agent"], tid)["status"] == "pending"
    tickets.add_comment(conn, people["req"], tid, "Swapped the cable, still flickers")
    assert tickets.get_ticket(conn, people["agent"], tid)["status"] == "open"


def test_empty_comment_is_rejected(conn, people):
    tid = new(conn, people["req"])
    with pytest.raises(ValidationError) as exc:
        tickets.add_comment(conn, people["req"], tid, "   ")
    assert exc.value.key == "error.comment_invalid"
