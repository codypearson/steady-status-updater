"""Command-line entry for the Steady daily update generator."""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import click

from steady_status.config import Settings
from steady_status.ical_feed import load_calendar_source
from steady_status.jira_client import JiraClient
from steady_status.report import build_markdown


EPILOG = """
Environment variables (see .env.example for defaults and commentary):

  Required: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN,
            exactly one of ICAL_PATH or ICAL_URL,
            and either JIRA_DEPLOY_ISSUE_TYPE_NAME or JIRA_DEPLOY_ISSUE_TYPE_ID.

  Optional: STEADY_TIMEZONE (or TZ), FILTER_TODAY_ID, FILTER_TOMORROW_DEV_ID,
  FILTER_TOMORROW_DEPLOY_ID, JIRA_RT_SUMMARY_CONTAINS,
  JIRA_RT_APPROVED_COMMENT_HEADER, JIRA_COMMENT_AUTHOR_ACCOUNT_ID,
  CAL_RT_EVENT_SUBSTRING, CALENDAR_USER_EMAIL, CAL_FILTER_ATTENDEE_RESPONSE,
  STEADY_INCLUDE_ALL_DAY_MEETINGS, STEADY_MEETINGS_EXCLUDE_RT_EVENTS,
  STEADY_NEW_TICKET_MESSAGE.
"""


def copy_to_clipboard(text: str) -> bool:
    """
    Copy UTF-8 text to the system clipboard using the first available backend.

    Tries ``wl-copy``, ``xclip``, then ``pbcopy``. Returns True on success.
    """
    data = text.encode("utf-8")
    commands: list[list[str]] = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["pbcopy"],
    ]
    for cmd in commands:
        exe = cmd[0]
        if not shutil.which(exe):
            continue
        try:
            subprocess.run(cmd, input=data, check=True, capture_output=True)
            return True
        except (OSError, subprocess.CalledProcessError):
            continue
    return False


@click.command(
    help=(
        "Build a Markdown Steady update from JIRA saved filters and calendar "
        "data (local .ics file or secret iCal URL)."
    ),
    epilog=EPILOG,
)
@click.option(
    "--date",
    "date_opt",
    metavar="YYYY-MM-DD",
    default=None,
    help=(
        "Anchor date for “Today” (STEADY_TIMEZONE or TZ). "
        "The “Tomorrow” section targets the next weekday (Mon–Fri), e.g. Friday → Monday. "
        "Default: current calendar day in that zone."
    ),
)
@click.option(
    "--clipboard/--no-clipboard",
    default=False,
    help="Copy the generated Markdown to the clipboard (best-effort).",
)
@click.option(
    "--dotenv",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Explicit path to a .env file (otherwise loads .env from the CWD).",
)
def main(date_opt: str | None, clipboard: bool, dotenv: Path | None) -> None:
    """Generate Markdown and write it to stdout (and optionally the clipboard)."""
    try:
        settings = Settings.from_env(dotenv_path=dotenv)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    tz = ZoneInfo(settings.timezone_name)
    if date_opt:
        try:
            anchor = date.fromisoformat(date_opt)
        except ValueError as exc:
            raise click.ClickException(
                f"Invalid --date {date_opt!r}; use YYYY-MM-DD."
            ) from exc
    else:
        anchor = datetime.now(tz).date()

    try:
        cal = load_calendar_source(
            ical_path=settings.ical_path,
            ical_url=settings.ical_url,
        )
        client = JiraClient(settings)
        body = build_markdown(settings, client, cal, anchor)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    sys.stdout.write(body)
    if clipboard:
        if copy_to_clipboard(body):
            click.echo("\n(Copied to clipboard.)", err=True)
        else:
            click.echo(
                "\n(Warning: clipboard copy failed; install wl-copy, xclip, or pbcopy.)",
                err=True,
            )


if __name__ == "__main__":
    main()
