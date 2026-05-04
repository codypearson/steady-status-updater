"""Load configuration from environment variables and optional .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for JIRA, calendar, and report defaults."""

    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_deploy_issue_type_name: str | None
    jira_deploy_issue_type_id: str | None
    jira_rt_summary_contains: str
    jira_rt_approved_comment_header: str
    jira_comment_author_account_id: str | None
    filter_today_id: str
    filter_tomorrow_dev_id: str
    filter_tomorrow_deploy_id: str
    # Saved filter whose issues are scanned for flags (**Blocked**); rows are used as-is (expect parents).
    filter_blocked_parents_id: str
    # Custom field id for Impediment/Flagged when ``flagged`` is unavailable (e.g. customfield_10021).
    jira_flagged_field_id: str | None
    ical_path: Path | None
    ical_url: str | None
    timezone_name: str
    cal_rt_event_substring: str
    include_all_day_meetings: bool
    new_ticket_message: str
    meetings_exclude_rt_events: bool
    calendar_user_email: str
    cal_filter_attendee_response: bool

    @staticmethod
    def from_env(dotenv_path: Path | None = None) -> Settings:
        """
        Build settings from the process environment.

        Loads `.env` from the current working directory when present (override
        with dotenv_path). Missing required keys raise ValueError with a clear message.
        """
        if dotenv_path is not None:
            load_dotenv(dotenv_path, override=False)
        else:
            load_dotenv(override=False)

        def req(key: str) -> str:
            v = os.environ.get(key)
            if not v or not str(v).strip():
                raise ValueError(
                    f"Missing or empty required environment variable: {key}"
                )
            return str(v).strip()

        deploy_name = os.environ.get("JIRA_DEPLOY_ISSUE_TYPE_NAME", "").strip()
        deploy_id = os.environ.get("JIRA_DEPLOY_ISSUE_TYPE_ID", "").strip()
        if not deploy_name and not deploy_id:
            raise ValueError(
                "Set either JIRA_DEPLOY_ISSUE_TYPE_NAME or JIRA_DEPLOY_ISSUE_TYPE_ID "
                "for Deploy subtask detection."
            )

        rt_contains = os.environ.get(
            "JIRA_RT_SUMMARY_CONTAINS", "Review & Test"
        ).strip()

        rt_header = os.environ.get(
            "JIRA_RT_APPROVED_COMMENT_HEADER", "Testing Completed"
        ).strip()
        if not rt_header:
            raise ValueError(
                "JIRA_RT_APPROVED_COMMENT_HEADER must be non-empty "
                '(default is "Testing Completed").'
            )

        author_aid = os.environ.get("JIRA_COMMENT_AUTHOR_ACCOUNT_ID", "").strip()

        ical_path_raw = os.environ.get("ICAL_PATH", "").strip()
        ical_url_raw = os.environ.get("ICAL_URL", "").strip()
        if bool(ical_path_raw) == bool(ical_url_raw):
            raise ValueError(
                "Set exactly one of ICAL_PATH (local .ics file) or ICAL_URL (secret HTTP feed)."
            )
        ical_path: Path | None = None
        ical_url: str | None = None
        if ical_path_raw:
            ical_path = Path(ical_path_raw).expanduser().resolve()
            if not ical_path.is_file():
                raise ValueError(f"ICAL_PATH is not a readable file: {ical_path}")
        else:
            ical_url = ical_url_raw

        tz = os.environ.get("STEADY_TIMEZONE") or os.environ.get("TZ") or "UTC"

        jira_email_val = req("JIRA_EMAIL")
        calendar_user = os.environ.get("CALENDAR_USER_EMAIL", "").strip()
        if not calendar_user:
            calendar_user = jira_email_val

        return Settings(
            jira_base_url=req("JIRA_BASE_URL").rstrip("/"),
            jira_email=jira_email_val,
            jira_api_token=req("JIRA_API_TOKEN"),
            jira_deploy_issue_type_name=deploy_name or None,
            jira_deploy_issue_type_id=deploy_id or None,
            jira_rt_summary_contains=rt_contains,
            jira_rt_approved_comment_header=rt_header,
            jira_comment_author_account_id=author_aid or None,
            filter_today_id=os.environ.get("FILTER_TODAY_ID", "12992").strip(),
            filter_tomorrow_dev_id=os.environ.get(
                "FILTER_TOMORROW_DEV_ID", "12990"
            ).strip(),
            filter_tomorrow_deploy_id=os.environ.get(
                "FILTER_TOMORROW_DEPLOY_ID", "12561"
            ).strip(),
            filter_blocked_parents_id=os.environ.get(
                "FILTER_BLOCKED_PARENTS_ID", "12991"
            ).strip(),
            jira_flagged_field_id=os.environ.get("JIRA_FLAGGED_FIELD_ID", "").strip()
            or None,
            ical_path=ical_path,
            ical_url=ical_url,
            timezone_name=tz,
            cal_rt_event_substring=os.environ.get(
                "CAL_RT_EVENT_SUBSTRING", "R&T"
            ).strip(),
            include_all_day_meetings=_bool_env("STEADY_INCLUDE_ALL_DAY_MEETINGS"),
            new_ticket_message=os.environ.get(
                "STEADY_NEW_TICKET_MESSAGE",
                "Picking up a new ticket.",
            ).strip(),
            meetings_exclude_rt_events=_bool_env("STEADY_MEETINGS_EXCLUDE_RT_EVENTS"),
            calendar_user_email=calendar_user,
            cal_filter_attendee_response=_bool_env(
                "CAL_FILTER_ATTENDEE_RESPONSE", default=True
            ),
        )
