"""JIRA REST API client: filter search, issue routing, and issue comments."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
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
    development: list[JiraIssue] = []
    rt: list[JiraIssue] = []
    for issue in issues:
        if client.is_deploy_issue(issue):
            deploy.append(issue)
            continue
        if client.is_rt_summary(issue):
            rt.append(issue)
        else:
            development.append(issue)
    return deploy, development, rt
