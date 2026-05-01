"""Assemble Steady Markdown from JIRA and calendar data."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar

from steady_status.config import Settings
from steady_status.ical_feed import (
    CalendarEvent,
    filter_events_excluding_substring,
    format_meeting_line,
    had_rt_event_today,
    iter_day_events,
)
from steady_status.jira_client import (
    JiraClient,
    JiraIssue,
    partition_today_bundle,
    rt_comment_approval_emoji,
)


def _md_browse_link(client: JiraClient, key: str, summary: str) -> str:
    return f"[{key}]({client.issue_url(key)}) - {summary}"


def _display_issue_link(client: JiraClient, issue: JiraIssue) -> str:
    """Markdown link line using parent ticket key/title when the row is a subtask."""
    return _md_browse_link(client, issue.display_key(), issue.display_summary())


def next_weekday_after(anchor: date) -> date:
    """
    First calendar day after ``anchor`` that falls on Monday–Friday.

    Used so “tomorrow” planning skips weekends: e.g. after Friday the next
    planning day is Monday, not Saturday.
    """
    d = anchor + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _lines_development_today(
    client: JiraClient,
    issues: list[JiraIssue],
) -> list[str]:
    """
    Markdown lines under **Development** for today's completed work.

    Uses parent key/title on the top bullet when applicable; a nested list
    item (indented ``*``) holds the subtask title only.
    """
    lines: list[str] = []
    for issue in issues:
        link = _display_issue_link(client, issue)
        lines.append(f"* {link}")
        if issue.parent_key:
            lines.append(f"  * {issue.summary}")
    return lines


def _lines_rt_today(
    client: JiraClient,
    issues: list[JiraIssue],
) -> list[str]:
    """
    Markdown lines under **R&T** with approval emoji.

    ☑️ when your issue comment includes the configured approval header (see
    ``JIRA_RT_APPROVED_COMMENT_HEADER``); otherwise 🔙.
    """
    lines: list[str] = []
    for issue in issues:
        emoji = rt_comment_approval_emoji(client, issue.key)
        link = _display_issue_link(client, issue)
        lines.append(f"* {emoji} {link}")
    return lines


def _lines_deploy(client: JiraClient, issues: list[JiraIssue]) -> list[str]:
    """Non-Development bullets with rocket emoji."""
    return [
        f"* 🚀 {_display_issue_link(client, issue)}" for issue in issues
    ]


def _lines_meetings(events: list[CalendarEvent]) -> list[str]:
    return [f"* 📅 {format_meeting_line(ev)}" for ev in events]


def _lines_development_tomorrow(client: JiraClient, issues: list[JiraIssue]) -> list[str]:
    """Next business day planning: issue links only."""
    if not issues:
        return []
    return [
        f"* {_display_issue_link(client, issue)}" for issue in issues
    ]


def build_markdown(
    settings: Settings,
    client: JiraClient,
    cal: Calendar,
    anchor_date: date,
) -> str:
    """
    Produce full Markdown for **Today** and **Tomorrow** relative to anchor_date.

    anchor_date is the logical \"today\" for the report (usually the current day
    in the configured timezone). The **Tomorrow** block uses the next **weekday**
    (Mon–Fri) after ``anchor_date``, so e.g. on Friday it plans for Monday. The **R&T** line
    under Tomorrow is included only when the calendar shows an R&T event on
    that planning day (same substring rule as ``#review`` for today).
    """
    tz = ZoneInfo(settings.timezone_name)
    today = anchor_date
    planning_day = next_weekday_after(anchor_date)

    include_all_day = settings.include_all_day_meetings
    cal_owner = settings.calendar_user_email
    cal_rsp = settings.cal_filter_attendee_response
    had_rt_cal = had_rt_event_today(
        cal,
        today,
        tz,
        settings.cal_rt_event_substring,
        include_all_day,
        attendee_email=cal_owner,
        filter_attendee_response=cal_rsp,
    )
    had_rt_planning = had_rt_event_today(
        cal,
        planning_day,
        tz,
        settings.cal_rt_event_substring,
        include_all_day,
        attendee_email=cal_owner,
        filter_attendee_response=cal_rsp,
    )

    meetings_today_raw = iter_day_events(
        cal,
        today,
        tz,
        include_all_day,
        attendee_email=cal_owner,
        filter_attendee_response=cal_rsp,
    )
    meetings_tomorrow_raw = iter_day_events(
        cal,
        planning_day,
        tz,
        include_all_day,
        attendee_email=cal_owner,
        filter_attendee_response=cal_rsp,
    )

    if settings.meetings_exclude_rt_events and settings.cal_rt_event_substring:
        meetings_today = filter_events_excluding_substring(
            meetings_today_raw, settings.cal_rt_event_substring
        )
        meetings_tomorrow = filter_events_excluding_substring(
            meetings_tomorrow_raw, settings.cal_rt_event_substring
        )
    else:
        meetings_today = meetings_today_raw
        meetings_tomorrow = meetings_tomorrow_raw

    issues_today = client.search_filter(settings.filter_today_id)
    deploy_today, development_today, rt_today = partition_today_bundle(
        issues_today, client
    )

    issues_tomorrow_dev = client.search_filter(settings.filter_tomorrow_dev_id)
    issues_tomorrow_deploy_raw = client.search_filter(settings.filter_tomorrow_deploy_id)
    deploy_tomorrow = [i for i in issues_tomorrow_deploy_raw if client.is_deploy_issue(i)]

    sections_today: list[str] = []
    sections_today.append("## Today")
    sections_today.append("")
    if had_rt_cal:
        sections_today.append("#review")
        sections_today.append("")

    sections_today.append("**Development**")
    dev_lines = _lines_development_today(client, development_today)
    if dev_lines:
        sections_today.extend(dev_lines)
    sections_today.append("")

    if had_rt_cal:
        sections_today.append("**R&T**")
        rt_lines = _lines_rt_today(client, rt_today)
        if rt_lines:
            sections_today.extend(rt_lines)
        sections_today.append("")

    nd_lines: list[str] = []
    nd_lines.extend(_lines_meetings(meetings_today))
    nd_lines.extend(_lines_deploy(client, deploy_today))
    if nd_lines:
        sections_today.append("**Non-Development**")
        sections_today.extend(nd_lines)
        sections_today.append("")

    sections_tomorrow: list[str] = []
    sections_tomorrow.append("## Tomorrow")
    sections_tomorrow.append("")

    sections_tomorrow.append("**Development**")
    dev_t_lines = _lines_development_tomorrow(client, issues_tomorrow_dev)
    if dev_t_lines:
        sections_tomorrow.extend(dev_t_lines)
    else:
        sections_tomorrow.append(f"* {settings.new_ticket_message}")
    sections_tomorrow.append("")

    if had_rt_planning:
        sections_tomorrow.append("**R&T**")
        sections_tomorrow.append("* On R&T")
        sections_tomorrow.append("")

    nd_t: list[str] = []
    nd_t.extend(_lines_meetings(meetings_tomorrow))
    nd_t.extend(_lines_deploy(client, deploy_tomorrow))
    if nd_t:
        sections_tomorrow.append("**Non-Development**")
        sections_tomorrow.extend(nd_t)
        sections_tomorrow.append("")

    body = "\n".join(sections_today + sections_tomorrow).strip() + "\n"
    return body
