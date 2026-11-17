"""Service level agreements measured in business hours.

Two clocks run for every ticket, as in ITIL:

* **response**: from opening until the service desk first answers in public
  (or fixes it);
* **resolution**: from opening until the ticket is resolved, *paused* while
  the ticket waits for the user (status ``pending``).

Both count only working time: the configured weekdays and opening hours, in
the desk's time zone, minus holidays. Targets depend on the priority.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import PRIORITIES

WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Minutes of business time per priority.
DEFAULT_TARGETS: dict[str, dict[str, int]] = {
    "critical": {"response": 30, "resolution": 4 * 60},
    "high": {"response": 60, "resolution": 8 * 60},
    "medium": {"response": 4 * 60, "resolution": 3 * 8 * 60},
    "low": {"response": 8 * 60, "resolution": 5 * 8 * 60},
}


class SlaConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BusinessCalendar:
    tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Madrid"))
    weekdays: frozenset[int] = frozenset(range(5))  # Monday = 0
    start: time = time(9, 0)
    end: time = time(18, 0)
    holidays: frozenset[date] = frozenset()

    def is_working_day(self, day: date) -> bool:
        return day.weekday() in self.weekdays and day not in self.holidays

    def _window(self, day: date) -> tuple[datetime, datetime]:
        return (
            datetime.combine(day, self.start, self.tz),
            datetime.combine(day, self.end, self.tz),
        )

    def business_seconds(self, start: datetime, end: datetime) -> float:
        """Working seconds between two instants (0 if end <= start)."""
        if end <= start:
            return 0.0
        start, end = start.astimezone(self.tz), end.astimezone(self.tz)
        total = 0.0
        day = start.date()
        while day <= end.date():
            if self.is_working_day(day):
                opens, closes = self._window(day)
                lo, hi = max(start, opens), min(end, closes)
                if hi > lo:
                    total += (hi - lo).total_seconds()
            day += timedelta(days=1)
        return total

    def add_business_seconds(self, start: datetime, seconds: float) -> datetime:
        """The instant when ``seconds`` of working time have passed since ``start``."""
        current = start.astimezone(self.tz)
        remaining = max(0.0, seconds)
        for _ in range(3700):  # ten years of days: guards against an empty calendar
            day = current.date()
            if self.is_working_day(day):
                opens, closes = self._window(day)
                lo = max(current, opens)
                if lo < closes:
                    available = (closes - lo).total_seconds()
                    if remaining <= available:
                        return lo + timedelta(seconds=remaining)
                    remaining -= available
            current = datetime.combine(day + timedelta(days=1), time(0), self.tz)
        raise SlaConfigError("the business calendar has no working time")


@dataclass(frozen=True)
class SlaPolicy:
    calendar: BusinessCalendar = field(default_factory=BusinessCalendar)
    targets: dict[str, dict[str, int]] = field(default_factory=lambda: dict(DEFAULT_TARGETS))
    warn_ratio: float = 0.75  # "at risk" once 75 % of the target is used

    def target_seconds(self, priority: str, clock: str) -> float:
        return self.targets[priority][clock] * 60.0


@dataclass(frozen=True)
class Clock:
    """One SLA clock of one ticket."""

    name: str  # "response" | "resolution"
    state: str  # met | breached | at_risk | running | paused
    due: datetime
    done_at: datetime | None
    target_seconds: float
    used_seconds: float  # business seconds consumed so far (or until done)

    @property
    def remaining_seconds(self) -> float:
        return self.target_seconds - self.used_seconds

    @property
    def finished(self) -> bool:
        return self.done_at is not None

    @property
    def is_problem(self) -> bool:
        return self.state in ("breached", "at_risk")


@dataclass(frozen=True)
class TicketSla:
    response: Clock
    resolution: Clock

    @property
    def worst(self) -> Clock:
        """The clock to show in a list: an open problem first, then the next due."""
        open_clocks = [c for c in (self.response, self.resolution) if not c.finished]
        if open_clocks:
            order = {"breached": 0, "at_risk": 1, "running": 2, "paused": 3}
            return min(open_clocks, key=lambda c: (order[c.state], c.due))
        breached = [c for c in (self.resolution, self.response) if c.state == "breached"]
        return breached[0] if breached else self.resolution

    @property
    def breached(self) -> bool:
        return "breached" in (self.response.state, self.resolution.state)


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def pending_intervals(
    status_events: Iterable[tuple[str, str | None, str | None]], now: datetime
) -> list[tuple[datetime, datetime]]:
    """Intervals spent in ``pending`` from (created_at, old, new) status events."""
    intervals = []
    started: datetime | None = None
    for at, _old, new in status_events:
        moment = parse_ts(at)
        if new == "pending" and started is None:
            started = moment
        elif new != "pending" and started is not None:
            intervals.append((started, moment))
            started = None
    if started is not None:
        intervals.append((started, now))
    return intervals


def _state(used: float, target: float, done: bool, warn_ratio: float) -> str:
    if done:
        return "met" if used <= target else "breached"
    if used > target:
        return "breached"
    if used >= target * warn_ratio:
        return "at_risk"
    return "running"


def evaluate(
    policy: SlaPolicy,
    ticket: dict,
    status_events: Iterable[tuple[str, str | None, str | None]],
    now: datetime,
) -> TicketSla:
    """Both clocks of a ticket. ``status_events`` are (at, old, new), oldest first."""
    cal = policy.calendar
    created = parse_ts(ticket["created_at"])
    priority = ticket["priority"]

    # Response clock.
    responded = parse_ts(ticket.get("first_response_at"))
    target = policy.target_seconds(priority, "response")
    used = cal.business_seconds(created, responded or now)
    response = Clock(
        "response", _state(used, target, responded is not None, policy.warn_ratio),
        cal.add_business_seconds(created, target), responded, target, used,
    )

    # Resolution clock, paused while waiting for the user.
    resolved = parse_ts(ticket.get("resolved_at"))
    stop = resolved or now
    pauses = pending_intervals(status_events, stop)
    paused = sum(cal.business_seconds(a, min(b, stop)) for a, b in pauses)
    target = policy.target_seconds(priority, "resolution")
    used = cal.business_seconds(created, stop) - paused
    state = _state(used, target, resolved is not None, policy.warn_ratio)
    if resolved is None and ticket["status"] == "pending" and state != "breached":
        state = "paused"
    # While paused the due date keeps moving; show it as if the clock restarted now.
    due = cal.add_business_seconds(created, target + paused)
    if state == "paused":
        due = cal.add_business_seconds(now, target - used)
    resolution = Clock("resolution", state, due, resolved, target, used)
    return TicketSla(response, resolution)


# ---- configuration ----------------------------------------------------------


def _parse_time(value: str, key: str) -> time:
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise SlaConfigError(f"{key}: expected HH:MM, got {value!r}") from exc


def policy_from_dict(data: dict) -> SlaPolicy:
    """Build a policy from the JSON structure documented in sla.example.json."""
    hours = data.get("business_hours", {})
    try:
        tz = ZoneInfo(hours.get("timezone", "Europe/Madrid"))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SlaConfigError(f"unknown time zone {hours.get('timezone')!r}") from exc
    names = hours.get("days", ["mon", "tue", "wed", "thu", "fri"])
    unknown = [d for d in names if d not in WEEKDAY_NAMES]
    if unknown or not names:
        raise SlaConfigError(f"business_hours.days: unknown or empty {unknown or names}")
    start = _parse_time(hours.get("start", "09:00"), "business_hours.start")
    end = _parse_time(hours.get("end", "18:00"), "business_hours.end")
    if end <= start:
        raise SlaConfigError("business_hours.end must be later than start")
    try:
        holidays = frozenset(date.fromisoformat(d) for d in data.get("holidays", []))
    except (TypeError, ValueError) as exc:
        raise SlaConfigError("holidays: expected YYYY-MM-DD dates") from exc

    targets = {p: dict(v) for p, v in DEFAULT_TARGETS.items()}
    for priority, values in data.get("targets_minutes", {}).items():
        if priority not in PRIORITIES:
            raise SlaConfigError(f"targets_minutes: unknown priority {priority!r}")
        for clock in ("response", "resolution"):
            if clock in values:
                minutes = values[clock]
                if not isinstance(minutes, int) or minutes <= 0:
                    raise SlaConfigError(f"targets_minutes.{priority}.{clock}: positive integer")
                targets[priority][clock] = minutes
    for priority, values in targets.items():
        if values["response"] > values["resolution"]:
            raise SlaConfigError(f"targets_minutes.{priority}: response longer than resolution")

    warn = data.get("warn_ratio", 0.75)
    if not isinstance(warn, (int, float)) or not 0 < warn < 1:
        raise SlaConfigError("warn_ratio must be between 0 and 1")

    calendar = BusinessCalendar(
        tz=tz,
        weekdays=frozenset(WEEKDAY_NAMES.index(d) for d in names),
        start=start,
        end=end,
        holidays=holidays,
    )
    return SlaPolicy(calendar=calendar, targets=targets, warn_ratio=float(warn))


def load_policy(path: str | Path | None) -> SlaPolicy:
    """Policy from a JSON file, or the defaults when there is no file."""
    if path is None or not Path(path).exists():
        return SlaPolicy()
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except json.JSONDecodeError as exc:
        raise SlaConfigError(f"{path}: invalid JSON ({exc})") from exc
    return policy_from_dict(data)
