"""JIRA REST API client: filter search, issue routing, and issue comments."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from steady_status.config import Settings


@dataclass(frozen=True)
class JiraIssue:
    """Minimal issue fields needed for report generation."""

    key: str
    summary: str
    status_name: str
    issue_type_id: str
    issue_type_name: str
    parent_key: str | None = None
    parent_summary: str | None = None

    def display_key(self) -> str:
        """Browse key for Markdown (parent story when this row is a subtask)."""
        return self.parent_key if self.parent_key else self.key

    def display_summary(self) -> str:
        """Browse title for Markdown (parent summary when available)."""
        if self.parent_key and self.parent_summary:
            return self.parent_summary
        return self.summary


def _auth_header(email: str, api_token: str) -> str:
    raw = f"{email}:{api_token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _impediment_flag_value_truthy(raw: Any) -> bool:
    """
    True when a Jira issue field indicates the issue is flagged / blocked.

    Jira Cloud usually stores this as the **Flagged** custom field with option
    ``Impediment`` (list of ``{\"value\": \"Impediment\", ...}``). Some sites also
    expose a boolean ``flagged`` field—handled here. Arbitrary non-empty option
    lists are *not* treated as flagged (avoids false positives on other selects).
    """
    if raw is True:
        return True
    if raw is None or raw is False:
        return False
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        return lowered in {"impediment", "true", "yes"}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                option_label = str(item.get("value") or item.get("name") or "").strip().lower()
                if option_label == "impediment":
                    return True
        return False
    if isinstance(raw, dict):
        option_label = str(raw.get("value") or raw.get("name") or "").strip().lower()
        return option_label == "impediment"
    return False


def _normalize_jira_iso8601(created: str) -> str:
    """Normalize Jira ``created`` strings so :func:`datetime.fromisoformat` accepts them."""
    s = created.strip()
    if not s:
        return s
    if s.endswith("Z"):
        return s[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", s):
        return s[:-2] + ":" + s[-2:]
    return s


def _parse_jira_created(created: str) -> datetime:
    """Parse Jira comment/issue timestamps for ordering (UTC when offset is present)."""
    normalized = _normalize_jira_iso8601(created)
    if not normalized:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


class JiraClient:
    """
    Thin wrapper around JIRA Cloud REST API v3.

    Issue search uses POST ``/rest/api/3/search/jql`` (enhanced JQL search). The
    legacy ``/rest/api/3/search`` endpoint returns HTTP 410 Gone on sites where
    Atlassian has removed it (CHANGE-2046).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": _auth_header(
                    settings.jira_email, settings.jira_api_token
                ),
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        self._base = settings.jira_base_url.rstrip("/")
        self._issue_summary_cache: dict[str, str] = {}
        #: Resolved REST ``id`` for the field named "Flagged" (see :meth:`_resolve_flagged_field_id`).
        self._cached_flagged_field_id: str | None = None
        self._flagged_field_metadata_loaded: bool = False

    def issue_url(self, key: str) -> str:
        """Browse URL for an issue key."""
        return f"{self._base}/browse/{key}"

    def search_filter(self, filter_id: str) -> list[JiraIssue]:
        """
        Return all issues from a saved filter id.

        Paginates until every issue in the filter is retrieved.
        """
        jql = f"filter = {filter_id}"
        return self._search_jql(jql)

    def _search_jql(self, jql: str) -> list[JiraIssue]:
        url = f"{self._base}/rest/api/3/search/jql"
        page_size = 50
        out: list[JiraIssue] = []
        next_page_token: str | None = None

        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "maxResults": page_size,
                "fields": ["summary", "status", "issuetype", "parent"],
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            resp = self._session.post(url, json=body, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("issues") or []
            for issue in batch:
                out.append(self._map_issue(issue))

            is_last = data.get("isLast")
            new_token = data.get("nextPageToken")

            if is_last is True:
                break
            if not new_token:
                break
            next_page_token = new_token

        return out

    def _fetch_issue_summary(self, issue_key: str) -> str:
        """
        Load summary for an issue key (cached).

        Used when search results include ``parent`` without nested ``fields.summary``.
        """
        if issue_key in self._issue_summary_cache:
            return self._issue_summary_cache[issue_key]
        url = f"{self._base}/rest/api/3/issue/{issue_key}"
        resp = self._session.get(url, params={"fields": "summary"}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        summary = ((data.get("fields") or {}).get("summary") or "").strip()
        self._issue_summary_cache[issue_key] = summary
        return summary

    def _map_issue(self, issue: dict[str, Any]) -> JiraIssue:
        fields = issue.get("fields") or {}
        key = issue.get("key") or ""
        summary = (fields.get("summary") or "").strip()
        status = (fields.get("status") or {}).get("name") or ""
        it = fields.get("issuetype") or {}
        it_id = str(it.get("id") or "")
        it_name = (it.get("name") or "").strip()

        parent_key: str | None = None
        parent_summary: str | None = None
        parent_raw = fields.get("parent")
        if isinstance(parent_raw, dict):
            pk = (parent_raw.get("key") or "").strip()
            if pk:
                parent_key = pk
                pfields = parent_raw.get("fields")
                if isinstance(pfields, dict):
                    parent_summary = (pfields.get("summary") or "").strip() or None
                if parent_key and not parent_summary:
                    parent_summary = self._fetch_issue_summary(parent_key) or None

        return JiraIssue(
            key=key,
            summary=summary,
            status_name=status.strip(),
            issue_type_id=it_id,
            issue_type_name=it_name,
            parent_key=parent_key,
            parent_summary=parent_summary,
        )

    def is_deploy_issue(self, issue: JiraIssue) -> bool:
        """True when the issue matches the configured Deploy subtask type."""
        s = self._settings
        if s.jira_deploy_issue_type_id and issue.issue_type_id == s.jira_deploy_issue_type_id:
            return True
        if s.jira_deploy_issue_type_name and issue.issue_type_name.lower() == s.jira_deploy_issue_type_name.lower():
            return True
        return False

    def is_rt_summary(self, issue: JiraIssue) -> bool:
        """True when summary indicates Review & Test bucket (substring match)."""
        needle = self._settings.jira_rt_summary_contains.lower()
        if not needle:
            return False
        return needle in issue.summary.lower()

    def is_comment_author_me(self, author: dict[str, Any]) -> bool:
        """
        Return True when the comment author matches the configured reporter identity.

        Matches ``JIRA_EMAIL`` to ``emailAddress`` when present, or
        ``JIRA_COMMENT_AUTHOR_ACCOUNT_ID`` to ``accountId`` when set (for sites
        that hide email on comments).
        """
        s = self._settings
        auth_email = (author.get("emailAddress") or "").strip().lower()
        self_email = s.jira_email.strip().lower()
        if auth_email and auth_email == self_email:
            return True
        account_id = s.jira_comment_author_account_id
        if account_id and author.get("accountId") == account_id:
            return True
        return False

    def is_rt_issue_approved_via_my_comment(self, issue_key: str) -> bool:
        """
        Return True if you posted an issue comment whose body contains the
        configured approval header (default ``Testing Completed``).

        Parses Atlassian Document Format (heading blocks and normalized lines).
        Only comments authored by you (see ``is_comment_author_me``) are considered.
        """
        header = self._settings.jira_rt_approved_comment_header
        url = f"{self._base}/rest/api/3/issue/{issue_key}/comment"
        start_at = 0
        page_size = 50
        while True:
            resp = self._session.get(
                url,
                params={"startAt": start_at, "maxResults": page_size},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            comments = data.get("comments", [])
            total = int(data.get("total", start_at + len(comments)))
            for comment in comments:
                author = comment.get("author") or {}
                if not self.is_comment_author_me(author):
                    continue
                body = comment.get("body")
                if _adf_comment_has_approval_header(body, header):
                    return True
            start_at += len(comments)
            if start_at >= total or not comments:
                break
        return False

    def _resolve_flagged_field_id(self) -> str | None:
        """
        Return the REST field id (e.g. ``customfield_10021``) for Jira's **Flagged** field.

        Uses ``JIRA_FLAGGED_FIELD_ID`` when set. Otherwise calls ``GET /rest/api/3/field``
        once and caches the field whose name is ``Flagged`` (case-insensitive). Many Cloud
        sites never populate the legacy ``flagged`` key on issues even when the ticket is
        flagged—the impediment lives on that custom field instead.
        """
        configured = self._settings.jira_flagged_field_id
        if configured:
            return configured
        if self._flagged_field_metadata_loaded:
            return self._cached_flagged_field_id
        self._flagged_field_metadata_loaded = True
        url = f"{self._base}/rest/api/3/field"
        resp = self._session.get(url, timeout=90)
        resp.raise_for_status()
        for field in resp.json():
            name = (field.get("name") or "").strip().lower()
            if name != "flagged":
                continue
            field_id = field.get("id")
            if isinstance(field_id, str) and field_id.strip():
                self._cached_flagged_field_id = field_id.strip()
                return self._cached_flagged_field_id
        self._cached_flagged_field_id = None
        return None

    def _fields_indicate_flagged(self, fields: dict[str, Any]) -> bool:
        """True when loaded issue ``fields`` represent a flagged / impediment issue."""
        if _impediment_flag_value_truthy(fields.get("flagged")):
            return True
        resolved_id = self._resolve_flagged_field_id()
        if resolved_id and _impediment_flag_value_truthy(fields.get(resolved_id)):
            return True
        return False

    def issue_is_flagged(self, issue_key: str) -> bool:
        """
        Return True when Jira marks ``issue_key`` as flagged (impediment).

        Loads the **Flagged** custom field when discoverable (field named ``Flagged``
        from ``GET /rest/api/3/field``) plus the ``flagged`` property when requested,
        and interprets standard Impediment option shapes—see
        :func:`_impediment_flag_value_truthy`.
        """
        resolved = self._resolve_flagged_field_id()
        field_names_ordered: list[str] = []
        if resolved:
            field_names_ordered.append(resolved)
        field_names_ordered.append("flagged")

        url = f"{self._base}/rest/api/3/issue/{issue_key}"
        params = {"fields": ",".join(field_names_ordered)}
        resp = self._session.get(url, params=params, timeout=60)
        if resp.status_code == 400 and resolved:
            resp = self._session.get(
                url,
                params={"fields": resolved},
                timeout=60,
            )
        resp.raise_for_status()
        issue_fields = resp.json().get("fields") or {}
        return self._fields_indicate_flagged(issue_fields)

    def get_my_most_recent_comment_containing(
        self,
        issue_key: str,
        substring: str,
        *,
        whole_word: bool = False,
    ) -> str | None:
        """
        Return plain text of your newest comment on ``issue_key`` whose body matches ``substring``.

        By default matching is a case-insensitive substring search over ADF plain text.
        With ``whole_word=True``, ``substring`` must appear as a whole word (so e.g.
        "Unblocked" does not match "Blocked").

        If several of your comments match, the one with the latest ``created`` timestamp
        wins. Returns ``None`` when there is no matching comment.
        """
        needle = substring.strip()
        if not needle:
            return None

        url = f"{self._base}/rest/api/3/issue/{issue_key}/comment"
        start_at = 0
        page_size = 50
        best_created: datetime | None = None
        best_text: str | None = None

        while True:
            resp = self._session.get(
                url,
                params={"startAt": start_at, "maxResults": page_size},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            comments = data.get("comments") or []
            total = int(data.get("total", start_at + len(comments)))

            for comment in comments:
                author = comment.get("author") or {}
                if not self.is_comment_author_me(author):
                    continue
                body = comment.get("body")
                body_doc = body if isinstance(body, dict) else {}
                text = _adf_extract_text(body_doc).strip()
                if whole_word:
                    if re.search(rf"\b{re.escape(needle)}\b", text, flags=re.IGNORECASE) is None:
                        continue
                else:
                    if needle.lower() not in text.lower():
                        continue
                created_raw = (comment.get("created") or "").strip()
                created_dt = _parse_jira_created(created_raw)
                if best_created is None or created_dt > best_created:
                    best_created = created_dt
                    best_text = text

            start_at += len(comments)
            if start_at >= total or not comments:
                break

        return best_text


def _adf_extract_text(node: dict[str, Any]) -> str:
    """Concatenate all ADF ``text`` nodes under ``node`` (preserves inline order)."""
    parts: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            if n.get("type") == "text" and "text" in n:
                parts.append(str(n["text"]))
            for child in n.get("content") or []:
                walk(child)
        elif isinstance(n, list):
            for item in n:
                walk(item)

    walk(node)
    return "".join(parts)


def _normalize_header_line(line: str) -> str:
    """
    Strip common Markdown-style heading / bold markers for comparison.

    Used so ``## Testing Completed``, ``**Testing Completed**``, and plain
    ``Testing Completed`` compare equal to the configured header text.
    """
    s = line.strip()
    s = re.sub(r"^#{1,6}\s*", "", s)
    s = re.sub(r"^\*{1,2}", "", s)
    s = re.sub(r"\*{1,2}$", "", s)
    return s.strip()


def _adf_comment_has_approval_header(body: Any, header: str) -> bool:
    """
    True when ADF contains the approval ``header`` as a real heading or as a
    normalized line (e.g. bold-only paragraph mimicking a title).
    """
    if not isinstance(body, dict) or body.get("type") != "doc":
        return False

    def walk_heading_match(node: Any) -> bool:
        if isinstance(node, dict):
            if node.get("type") == "heading":
                if _adf_extract_text(node).strip() == header:
                    return True
            for child in node.get("content") or []:
                if walk_heading_match(child):
                    return True
        elif isinstance(node, list):
            for item in node:
                if walk_heading_match(item):
                    return True
        return False

    if walk_heading_match(body):
        return True

    for block in body.get("content") or []:
        if not isinstance(block, dict):
            continue
        block_text = _adf_extract_text(block)
        for line in block_text.splitlines():
            if _normalize_header_line(line) == header:
                return True
        if _normalize_header_line(block_text) == header:
            return True
    return False


def rt_comment_approval_emoji(client: JiraClient, issue_key: str) -> str:
    """Return ☑️ when your comment includes the approval header; otherwise 🔙."""
    if client.is_rt_issue_approved_via_my_comment(issue_key):
        return "☑️"
    return "🔙"


def partition_development_and_rt(
    issues: Iterable[JiraIssue],
    client: JiraClient,
) -> tuple[list[JiraIssue], list[JiraIssue]]:
    """
    Split **non-deploy** issues into development vs R&T using the same summary
    substring rule as :func:`partition_today_bundle`.

    Deploy issues must be removed by the caller; they are not accepted here.
    """
    development: list[JiraIssue] = []
    rt: list[JiraIssue] = []
    for issue in issues:
        if client.is_rt_summary(issue):
            rt.append(issue)
        else:
            development.append(issue)
    return development, rt


def partition_today_bundle(
    issues: Iterable[JiraIssue],
    client: JiraClient,
) -> tuple[list[JiraIssue], list[JiraIssue], list[JiraIssue]]:
    """
    Split the “today” filter’s issues into deploy, development, and R&T lists.

    Deploy issues are excluded from dev and R&T. Among non-deploy issues, the
    R&T bucket matches the configured summary substring; development is the
    remainder.
    """
    deploy: list[JiraIssue] = []
    non_deploy: list[JiraIssue] = []
    for issue in issues:
        if client.is_deploy_issue(issue):
            deploy.append(issue)
        else:
            non_deploy.append(issue)
    development, rt = partition_development_and_rt(non_deploy, client)
    return deploy, development, rt
