# Steady Status Updater

Command-line tool that builds a **Markdown “Steady” daily status update** from your **Jira** saved filters and a **calendar** source (local `.ics` file or secret iCal URL). Output is written to **stdout** so you can pipe it, save it, or paste it into Slack or Confluence.

## Requirements

- **Python 3.10+** (uses modern union syntax such as `str | None`)
- A Jira Cloud site with **API access** (email + API token)
- **Exactly one** calendar input: either a path to an `.ics` file (`ICAL_PATH`) or an HTTP iCal URL (`ICAL_URL`)

## Installation

1. **Clone** this repository and change into its root directory (the folder that contains `steady_status/` and `requirements.txt`).

2. **Create and activate a virtual environment** (recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Linux/macOS
   # .venv\Scripts\activate    # Windows
   ```

3. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

There is no packaged install step yet; run the tool from the **repository root** so Python can resolve the `steady_status` package (or set `PYTHONPATH` to that root if you run from elsewhere).

## Configuration

Configuration is read from **environment variables**. The app loads a `.env` file from the **current working directory** when present (unless you pass `--dotenv`).

1. **Copy the example env file** and edit it:

   ```bash
   cp .env.example .env
   ```

2. **Fill in required values** (see `.env.example` for comments and optional knobs):

   | Variable | Purpose |
   |----------|---------|
   | `JIRA_BASE_URL` | Jira site base URL, e.g. `https://your-domain.atlassian.net` (no trailing path) |
   | `JIRA_EMAIL` | Account email used with the API token |
   | `JIRA_API_TOKEN` | [Atlassian API token](https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/) |
   | `JIRA_DEPLOY_ISSUE_TYPE_NAME` *or* `JIRA_DEPLOY_ISSUE_TYPE_ID` | Issue type used for Deploy subtasks (at least one must be set) |
   | `ICAL_PATH` *or* `ICAL_URL` | **Exactly one**: local `.ics` file path, **or** secret calendar feed URL |

3. **Optional but common**:

   - **`STEADY_TIMEZONE`** (or `TZ`): IANA zone for “today” and calendar windows (default in example: `America/Chicago`).
   - **`FILTER_TODAY_ID`**, **`FILTER_TOMORROW_DEV_ID`**, **`FILTER_TOMORROW_DEPLOY_ID`**: Jira **saved filter** IDs used for today’s work, tomorrow’s dev queue, and deploy-related items (defaults in `.env.example` match a specific automation setup; replace with your own filter IDs).
   - **`JIRA_RT_SUMMARY_CONTAINS`**: Case-insensitive substring in the summary for “Review & Test” style issues.
   - **`JIRA_RT_APPROVED_COMMENT_HEADER`**: Heading text that marks an approved R&T comment (default: `Testing Completed`).
   - **`CAL_RT_EVENT_SUBSTRING`**: Substring in calendar titles that marks an R&T block (default: `R&T`).
   - **`FILTER_BLOCKED_PARENTS_ID`**, **`JIRA_FLAGGED_FIELD_ID`**, **`JIRA_COMMENT_AUTHOR_ACCOUNT_ID`**: See `.env.example` for blocked/flagged behavior and comment authorship matching.

Keep `.env` out of version control; this repository’s `.gitignore` already ignores it.

## Usage

Run from the **repository root** (where `.env` lives, unless you use `--dotenv`):

```bash
python -m steady_status
```

The generated Markdown is printed to **standard output**.

### CLI options

| Option | Description |
|--------|-------------|
| `--date YYYY-MM-DD` | Anchor date for the “Today” section in your configured timezone. “Tomorrow” is the **next weekday** (Monday–Friday), so e.g. Friday’s run targets Monday. |
| `--clipboard` | After printing, copy the same Markdown to the clipboard (tries `wl-copy`, `xclip`, then `pbcopy`). |
| `--no-clipboard` | Default; do not touch the clipboard. |
| `--dotenv PATH` | Load variables from this `.env` file instead of only `.env` in the current working directory. |

Examples:

```bash
# Today’s report (default anchor = current calendar day in STEADY_TIMEZONE/TZ)
python -m steady_status

# Specific anchor date
python -m steady_status --date 2026-05-15

# Generate and copy to clipboard
python -m steady_status --clipboard

# Save to a file
python -m steady_status > steady-update.md

# Use a non-default env file
python -m steady_status --dotenv /path/to/prod.env
```

### Calendar data

- **`ICAL_PATH`**: Point at a file you refresh yourself (export, `curl` of a secret URL into a file, automation, etc.).
- **`ICAL_URL`**: The tool fetches the feed over HTTP each run.

Google Calendar and other providers are supported as long as the feed or file is valid iCalendar (`.ics`) data. See comments in `.env.example` for trade-offs (titles, privacy, automation).

### Help

```bash
python -m steady_status --help
```

The help text summarizes environment variables; `.env.example` documents them in more detail.

## What the report includes

At a high level, the Markdown combines:

- **Jira** issues from your configured saved filters (today, tomorrow dev/deploy, optional blocked parents).
- **Calendar** events for contextual meeting bullets and R&T detection (including substring and attendee response filtering as configured).

Exact sections and formatting are defined in `steady_status/report.py`.

## Troubleshooting

- **“Missing or empty required environment variable”**: Ensure `.env` is in the working directory or pass `--dotenv`, and that all required keys are set (see [Configuration](#configuration)).
- **“Set exactly one of ICAL_PATH or ICAL_URL”**: Clear the unused variable so only one is non-empty.
- **Clipboard warning**: Install `wl-copy` (Wayland), `xclip` (X11), or use macOS `pbcopy`; otherwise rely on shell redirection or terminal copy.
- **Jira errors**: Confirm `JIRA_BASE_URL`, token permissions, and that saved filter IDs exist and are visible to the API user.

## License

This project is licensed under the **GNU General Public License v3.0**. See [`LICENSE`](LICENSE) for the full text.
