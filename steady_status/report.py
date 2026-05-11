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
    is_done_status,
    is_in_progress_status,
    partition_development_and_rt,
    partition_today_bundle,
    rt_comment_approval_emoji,
)


def _md_browse_link(client: JiraClient, key: str, summary: str) -> str:
    return f"[{key}]({client.issue_url(key)}) - {summary}"


def _display_issue_link(client: JiraClient, issue: JiraIssue) -> str:
    """Markdown link line using parent ticket key/title when the row is a subtask."""
    return _md_browse_link(client, issue.display_key(), issue.display_summary())


def _group_issues_by_parent(issues: list[JiraIssue]) -> list[list[JiraIssue]]:
    """
    Bucket issues by parent story key (or the issue's own key when not a subtask).

    Order follows the input list: the first time a parent appears, that group is
    opened; further subtasks with the same parent append to that group.
    """
    order: list[str] = []
    buckets: dict[str, list[JiraIssue]] = {}
    for issue in issues:
        group_key = issue.parent_key or issue.key
        if group_key not in buckets:
            order.append(group_key)
            buckets[group_key] = []
        buckets[group_key].append(issue)
    return [buckets[key] for key in order]


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

    Uses parent key/title on the top bullet when applicable; nested ``*`` lines
    list subtask titles. Multiple subtasks under the same parent share one top bullet.
    """
    lines: list[str] = []
    for group in _group_issues_by_parent(issues):
        first = group[0]
        lines.append(f"* {_display_issue_link(client, first)}")
        if first.parent_key:
            for issue in group:
                lines.append(f"  * {issue.summary}")
    return lines


def _lines_rt_today(
    client: JiraClient,
    issues: list[JiraIssue],
) -> list[str]:
    """
    Markdown lines under **Today** → **R&T** with per-issue approval emoji.

    ☑️ when your issue comment includes the configured approval header (see
    ``JIRA_RT_APPROVED_COMMENT_HEADER``); otherwise 🔙.

    For ``Review & Test`` subtasks grouped under a story, the emoji prefixes the
    parent ticket line (approval is checked on the subtask issue). Nested bullets
    that only repeat the configured R&T summary substring are omitted. Other
    subtasks under the same parent still appear as nested lines with emoji.
    Tomorrow’s **R&T** list uses a simpler layout—see :func:`_lines_rt_tomorrow`.
    """
    lines: list[str] = []
    for group in _group_issues_by_parent(issues):
        first = group[0]
        if first.parent_key:
            emoji = rt_comment_approval_emoji(client, first.key)
            lines.append(f"* {emoji} {_display_issue_link(client, first)}")
            for issue in group:
                if client.is_rt_summary(issue):
                    continue
                nested_emoji = rt_comment_approval_emoji(client, issue.key)
                lines.append(f"  * {nested_emoji} {issue.summary}")
        else:
            issue = group[0]
            emoji = rt_comment_approval_emoji(client, issue.key)
            lines.append(f"* {emoji} {_display_issue_link(client, issue)}")
    return lines


def _lines_deploy(client: JiraClient, issues: list[JiraIssue]) -> list[str]:
    """
    Non-Development bullets with rocket emoji.

    Subtasks that share a parent are collapsed to one ``🚀`` line (parent key /
    title only). Nested bullets are omitted—Deploy subtask summaries are often
    generic (e.g. \"Deploy\") and add noise next to the emoji.
    """
    lines: list[str] = []
    for group in _group_issues_by_parent(issues):
        first = group[0]
        lines.append(f"* 🚀 {_display_issue_link(client, first)}")
    return lines


def _lines_meetings(events: list[CalendarEvent]) -> list[str]:
    return [f"* 📅 {format_meeting_line(ev)}" for ev in events]


def _partition_deploy_by_status(
    deploy_today: list[JiraIssue],
    deploy_tomorrow: list[JiraIssue],
) -> tuple[list[JiraIssue], list[JiraIssue]]:
    """
    Split deploy issues so they appear under **Today** vs **Tomorrow** by Jira status.

    * **Done** → listed only under **Today** (from the today filter).
    * **In Progress** → listed only under **Tomorrow**, even when the same issue
      also matches the today filter (deduped by parent/story or issue key).

    Other statuses are omitted from both sections.
    """
    today_deploy = [issue for issue in deploy_today if is_done_status(issue)]

    seen_keys: set[str] = set()
    tomorrow_deploy: list[JiraIssue] = []

    def _append_in_progress(issue: JiraIssue) -> None:
        if not is_in_progress_status(issue):
            return
        group_key = issue.rollup_group_key()
        if group_key in seen_keys:
            return
        seen_keys.add(group_key)
        tomorrow_deploy.append(issue)

    for issue in deploy_tomorrow:
        _append_in_progress(issue)
    for issue in deploy_today:
        _append_in_progress(issue)

    return today_deploy, tomorrow_deploy


def _lines_development_tomorrow(client: JiraClient, issues: list[JiraIssue]) -> list[str]:
    """Next business day **Development**: issue links; subtasks nested under the parent line."""
    if not issues:
        return []
    lines: list[str] = []
    for group in _group_issues_by_parent(issues):
        first = group[0]
        lines.append(f"* {_display_issue_link(client, first)}")
        if first.parent_key:
            for issue in group:
                lines.append(f"  * {issue.summary}")
    return lines


def _lines_rt_tomorrow(client: JiraClient, issues: list[JiraIssue]) -> list[str]:
    """
    **Tomorrow** → **R&T**: one bullet per story (parent link when issues are subtasks).

    Nested subtask titles are omitted—they are usually the literal \"Review & Test\" and
    repeat the section heading without adding detail (same idea as :func:`_lines_deploy`).
    """
    if not issues:
        return []
    lines: list[str] = []
    for group in _group_issues_by_parent(issues):
        first = group[0]
        lines.append(f"* {_display_issue_link(client, first)}")
    return lines


BLOCKED_COMMENT_TRIGGER = "Blocked"


def _collect_flagged_blocked_entries(
    client: JiraClient,
    issues: list[JiraIssue],
) -> list[tuple[JiraIssue, str | None]]:
    """
    Emit one **Blocked** row per flagged issue returned directly by the blocked-work filter.

    Only ``issue.key`` for each filter row is considered (no parent lookup). The saved
    filter (default ``FILTER_BLOCKED_PARENTS_ID`` / 12991) is expected to list those parent
    tickets already. Dedupes repeated keys. Loads your ``Blocked`` comment from that issue.
    """
    rows: list[tuple[JiraIssue, str | None]] = []
    seen_keys: set[str] = set()

    for issue in issues:
        if issue.key in seen_keys:
            continue
        if not client.issue_is_flagged(issue.key):
            continue
        seen_keys.add(issue.key)

        blocked_note = client.get_my_most_recent_comment_containing(
            issue.key,
            BLOCKED_COMMENT_TRIGGER,
            whole_word=True,
        )
        rows.append((issue, blocked_note))

    return rows


def _lines_blocked_section(
    client: JiraClient,
    rows: list[tuple[JiraIssue, str | None]],
) -> list[str]:
    """
    One Markdown bullet per blocked issue using that row's **own** key, URL, and summary.

    Unlike other sections, parent rollup is not applied—subtasks appear under their own key.
    Optional ``Blocked`` comment text is nested as an indented sub-bullet under the issue line.
    """
    lines: list[str] = []
    for issue, blocked_note in rows:
        link_line = _md_browse_link(client, issue.key, issue.summary)
        if blocked_note:
            flattened_comment = " ".join(blocked_note.split())
            lines.append(f"* {link_line}")
            lines.append(f"  * {flattened_comment}")
        else:
            lines.append(f"* {link_line}")
    return lines


def build_markdown(
    settings: Settings,
    client: JiraClient,
    cal: Calendar,
    anchor_date: date,
) -> str:
    """
    Produce full Markdown for **Today**, **Tomorrow**, and **Blocked** relative to anchor_date.

    anchor_date is the logical \"today\" for the report (usually the current day
    in the configured timezone). The **Tomorrow** block uses the next **weekday**
    (Mon–Fri) after ``anchor_date``, so e.g. on Friday it plans for Monday.
    The **R&T** subsection under **Today** appears when the calendar has an R&T event
    on that day *or* when the today filter returns at least one R&T-classified task.
    Under **Tomorrow**, the same idea applies using the planning weekday and the
    tomorrow dev filter (same JIRA summary substring rule for both).

    **Blocked** lists issues returned by ``FILTER_BLOCKED_PARENTS_ID`` that are flagged,
    using each row's own key only (the filter is expected to already return the parent
    tickets). Lines show that row's key and title (not parent rollup). Your latest
    \"Blocked\" comment on that issue is included when present.

    Deploy-type issues: **Done** appears only under **Today** → **Non-Development**;
    **In Progress** only under **Tomorrow**, so issues matched by both saved filters
    are not duplicated across days.
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
    deploy_today_raw, development_today, rt_today = partition_today_bundle(
        issues_today, client
    )

    issues_tomorrow_dev = client.search_filter(settings.filter_tomorrow_dev_id)
    tomorrow_dev_non_deploy = [
        issue for issue in issues_tomorrow_dev if not client.is_deploy_issue(issue)
    ]
    development_tomorrow, rt_tomorrow = partition_development_and_rt(
        tomorrow_dev_non_deploy, client
    )
    issues_tomorrow_deploy_raw = client.search_filter(settings.filter_tomorrow_deploy_id)
    deploy_tomorrow_raw = [i for i in issues_tomorrow_deploy_raw if client.is_deploy_issue(i)]
    deploy_today, deploy_tomorrow = _partition_deploy_by_status(
        deploy_today_raw,
        deploy_tomorrow_raw,
    )

    issues_blocked = client.search_filter(settings.filter_blocked_parents_id)
    blocked_entries = _collect_flagged_blocked_entries(client, issues_blocked)

    show_rt_today = had_rt_cal or bool(rt_today)

    sections_today: list[str] = []
    sections_today.append("## Today")
    sections_today.append("")
    if show_rt_today:
        sections_today.append("#review")
        sections_today.append("")

    sections_today.append("**Development**")
    dev_lines = _lines_development_today(client, development_today)
    if dev_lines:
        sections_today.extend(dev_lines)
    sections_today.append("")

    if show_rt_today:
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
    dev_t_lines = _lines_development_tomorrow(client, development_tomorrow)
    if dev_t_lines:
        sections_tomorrow.extend(dev_t_lines)
    else:
        sections_tomorrow.append(f"* {settings.new_ticket_message}")
    sections_tomorrow.append("")

    show_rt_tomorrow = had_rt_planning or bool(rt_tomorrow)
    if show_rt_tomorrow:
        sections_tomorrow.append("**R&T**")
        if had_rt_planning:
            sections_tomorrow.append("* On R&T")
        rt_tomorrow_lines = _lines_rt_tomorrow(client, rt_tomorrow)
        if rt_tomorrow_lines:
            sections_tomorrow.extend(rt_tomorrow_lines)
        sections_tomorrow.append("")

    nd_t: list[str] = []
    nd_t.extend(_lines_meetings(meetings_tomorrow))
    nd_t.extend(_lines_deploy(client, deploy_tomorrow))
    if nd_t:
        sections_tomorrow.append("**Non-Development**")
        sections_tomorrow.extend(nd_t)
        sections_tomorrow.append("")

    sections_blocked: list[str] = []
    if blocked_entries:
        sections_blocked.append("## Blocked")
        sections_blocked.append("")
        sections_blocked.extend(_lines_blocked_section(client, blocked_entries))
        sections_blocked.append("")

    body = "\n".join(sections_today + sections_tomorrow + sections_blocked).strip() + "\n"
    return body
