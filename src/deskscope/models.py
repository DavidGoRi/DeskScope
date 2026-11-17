"""Domain vocabulary, workflow rules and errors."""

from __future__ import annotations

from dataclasses import dataclass

ROLES = ("requester", "agent", "admin")
STAFF_ROLES = frozenset({"agent", "admin"})

STATUSES = ("new", "open", "pending", "resolved", "closed")
OPEN_STATUSES = frozenset({"new", "open", "pending"})
PRIORITIES = ("critical", "high", "medium", "low")
CATEGORIES = ("hardware", "software", "network", "access", "other")
LEVELS = ("high", "medium", "low")

# ITIL priority matrix: impact (how many are affected) x urgency (how fast it hurts).
PRIORITY_MATRIX: dict[tuple[str, str], str] = {
    ("high", "high"): "critical",
    ("high", "medium"): "high",
    ("high", "low"): "medium",
    ("medium", "high"): "high",
    ("medium", "medium"): "medium",
    ("medium", "low"): "low",
    ("low", "high"): "medium",
    ("low", "medium"): "low",
    ("low", "low"): "low",
}


def priority_for(impact: str, urgency: str) -> str:
    return PRIORITY_MATRIX[(impact, urgency)]

# Workflow for staff. "closed" is final: a closed ticket is reopened by
# opening a new one, which keeps the metrics of the old one honest.
STAFF_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"open", "pending", "resolved", "closed"}),
    "open": frozenset({"pending", "resolved", "closed"}),
    "pending": frozenset({"open", "resolved", "closed"}),
    "resolved": frozenset({"open", "closed"}),
    "closed": frozenset(),
}

# What the person who opened the ticket may do: confirm or reject a fix,
# or withdraw a ticket nobody has picked up yet.
REQUESTER_TRANSITIONS: dict[str, frozenset[str]] = {
    "new": frozenset({"closed"}),
    "resolved": frozenset({"open", "closed"}),
}

TITLE_MAX = 120
TEXT_MAX = 5000
DEVICE_MAX = 64


class DeskError(Exception):
    """Base error. ``key`` is an i18n message key shown to the user."""

    status_code = 400

    def __init__(self, key: str, **params: object) -> None:
        super().__init__(key)
        self.key = key
        self.params = params


class ValidationError(DeskError):
    status_code = 400


class PermissionDenied(DeskError):
    status_code = 403


class NotFound(DeskError):
    status_code = 404


@dataclass(frozen=True)
class User:
    id: int
    username: str
    full_name: str
    email: str
    role: str
    active: bool

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def allowed_transitions(user: User, status: str, is_requester: bool) -> list[str]:
    """Statuses this user may move a ticket to, in display order."""
    allowed: set[str] = set()
    if user.is_staff:
        allowed |= STAFF_TRANSITIONS.get(status, frozenset())
    if is_requester:
        allowed |= REQUESTER_TRANSITIONS.get(status, frozenset())
    return [s for s in STATUSES if s in allowed]
