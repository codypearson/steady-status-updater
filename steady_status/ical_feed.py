"""Fetch or load iCalendar data (HTTP secret URL or local .ics path)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar


@dataclass(frozen=True)
class CalendarEvent:
    """One occurrence suitable for listing in the report."""

    summary: str
    start_local: datetime | None
    end_local: datetime | None
    is_all_day: bool


def fetch_ical_bytes(url: str, timeout: int = 60) -> bytes:
    """Download raw iCalendar data from the secret URL."""
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def parse_calendar(data: bytes) -> Calendar:
    """Parse bytes into an icalendar Calendar root."""
    return Calendar.from_ical(data)


def read_ical_file(path: Path) -> bytes:
    """Read raw iCalendar bytes from a local file (e.g. cron-refreshed export)."""
    return path.read_bytes()


def _mailto_from_attendee(attendee: Any) -> str | None:
    """Return lowercase email from an ``ATTENDEE`` property value (``mailto:``)."""
    raw = str(attendee).strip()
    if not raw.lower().startswith("mailto:"):
        return None
    addr = raw.split(":", 1)[1].split("?")[0].strip().lower()
    return addr or None


def _partstat_from_attendee(attendee: Any) -> str | None:
    """Return uppercased ``PARTSTAT`` for an ``ATTENDEE`` property, if present."""
    params = getattr(attendee, "params", None)
    if not params:
        return None
    for key in ("PARTSTAT", "partstat"):
        if key in params:
            val = params[key]
            if isinstance(val, list):
                val = val[0] if val else None
            if val is None:
                return None
            return str(val).strip().upper()
    return None


def _skip_vevent_for_user_response(component: Any, user_email_lower: str) -> bool:
    """
    Return True if this ``VEVENT`` should be omitted for the calendar owner.

    Skips when the owner's ``ATTENDEE`` row has ``PARTSTAT=DECLINED`` or
    ``TENTATIVE`` (typical mapping for No / Maybe). Events with no matching
    ``ATTENDEE`` line are not skipped.
    """
    attendees = component.get("attendee")
    if attendees is None:
        return False
    items = attendees if isinstance(attendees, list) else [attendees]
    for att in items:
        em = _mailto_from_attendee(att)
        if em != user_email_lower:
            continue
        ps = _partstat_from_attendee(att)
        if ps in ("DECLINED", "TENTATIVE"):
            return True
        return False
    return False


def load_calendar_source(
    *,
    ical_path: Path | None,
    ical_url: str | None,
) -> Calendar:
    """
    Load calendar from ``ICAL_PATH`` or ``ICAL_URL``.

    Exactly one source must be provided (enforced in :func:`Settings.from_env`).
    """
    if ical_path is not None:
        data = read_ical_file(ical_path)
    elif ical_url:
        data = fetch_ical_bytes(ical_url)
    else:
        raise ValueError("No calendar source (set ICAL_PATH or ICAL_URL).")
    return parse_calendar(data)


def had_rt_event_today(
    cal: Calendar,
    today: date,
    tz: ZoneInfo,
    rt_substring: str,
    include_all_day: bool,
    *,
    attendee_email: str | None = None,
    filter_attendee_response: bool = False,
) -> bool:
    """
    Return True if any event on `today` matches the R&T calendar substring.

    Used for `#review` and for showing the **R&T** JIRA subsection.
    """
    needle = rt_substring.lower()
    if not needle:
        return False
    for ev in iter_day_events(
        cal,
        today,
        tz,
        include_all_day,
        attendee_email=attendee_email,
        filter_attendee_response=filter_attendee_response,
    ):
        if needle in ev.summary.lower():
            return True
    return False


def iter_day_events(
    cal: Calendar,
    day: date,
    tz: ZoneInfo,
    include_all_day: bool,
    *,
    attendee_email: str | None = None,
    filter_attendee_response: bool = False,
) -> list[CalendarEvent]:
    """
    Collect events that overlap `day` in `tz`, excluding all-day by default.

    Events are de-duplicated by (summary, start_local iso) when possible.

    When ``filter_attendee_response`` is True and ``attendee_email`` is set,
    events where that attendee's ``PARTSTAT`` is ``DECLINED`` or ``TENTATIVE``
    (calendar No / Maybe) are omitted. Events without ``ATTENDEE`` lines are kept.
    """
    seen: set[tuple[str, str]] = set()
    out: list[CalendarEvent] = []
    user_lower = (attendee_email or "").strip().lower()
    for component in cal.walk():
        if component.name != "VEVENT":
            continue
        if filter_attendee_response and user_lower:
            if _skip_vevent_for_user_response(component, user_lower):
                continue
        ev = _component_to_event(component, day, tz, include_all_day)
        if ev is None:
            continue
        key = (ev.summary, _event_sort_key(ev))
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    out.sort(key=lambda e: _event_sort_key(e))
    return out


def _event_sort_key(ev: CalendarEvent) -> str:
    if ev.start_local is not None:
        return ev.start_local.isoformat()
    return ev.summary.lower()


def _component_to_event(
    component: Any,
    day: date,
    tz: ZoneInfo,
    include_all_day: bool,
) -> CalendarEvent | None:
    summary_raw = component.get("summary")
    summary = str(summary_raw) if summary_raw is not None else "(no title)"
    summary = summary.strip() or "(no title)"

    dtstart_prop = component.get("dtstart")
    dtend_prop = component.get("dtend")

    if dtstart_prop is None:
        return None

    dtstart = dtstart_prop.dt
    dtend = None
    if dtend_prop is not None:
        dtend = dtend_prop.dt

    # datetime is a subclass of date — classify timed events before date-only.
    if isinstance(dtstart, datetime):
        start_local = _to_local(dtstart, tz)
        end_local = None
        if isinstance(dtend, datetime):
            end_local = _to_local(dtend, tz)
        if _local_range_overlaps_day(start_local, end_local, day):
            return CalendarEvent(
                summary=summary,
                start_local=start_local,
                end_local=end_local,
                is_all_day=False,
            )
        return None

    if isinstance(dtstart, date):
        if not include_all_day:
            return None
        start_d = dtstart
        end_exclusive = start_d + timedelta(days=1)
        if isinstance(dtend, date) and not isinstance(dtend, datetime):
            end_exclusive = dtend
        if start_d <= day < end_exclusive:
            return CalendarEvent(
                summary=summary,
                start_local=None,
                end_local=None,
                is_all_day=True,
            )
        return None

    return None


def _to_local(dt: datetime, tz: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(tz)


def _local_range_overlaps_day(
    start_local: datetime,
    end_local: datetime | None,
    day: date,
) -> bool:
    """True if the timed event intersects the calendar date `day` in local time."""
    start_date = start_local.date()
    if end_local is None:
        return start_date == day
    end_date = end_local.date()
    if start_date <= day <= end_date:
        return True
    if start_local.date() == day:
        return True
    if end_local.date() == day:
        return True
    return False


def filter_events_excluding_substring(
    events: list[CalendarEvent],
    substring: str | None,
) -> list[CalendarEvent]:
    """
    Remove events whose summary contains the given substring (case-insensitive).

    Used to optionally drop R&T blocks from the meeting list when they are
    listed under **R&T** in JIRA instead.
    """
    if not substring:
        return events
    needle = substring.lower()
    return [e for e in events if needle not in e.summary.lower()]


def format_meeting_line(ev: CalendarEvent) -> str:
    """Single bullet line for a meeting or calendar block."""
    if ev.start_local is not None and not ev.is_all_day:
        start_t = ev.start_local.strftime("%H:%M")
        if ev.end_local is not None:
            end_t = ev.end_local.strftime("%H:%M")
            return f"{ev.summary} ({start_t}–{end_t})"
        return f"{ev.summary} ({start_t})"
    return ev.summary
