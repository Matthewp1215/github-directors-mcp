#!/usr/bin/env python3
"""
GitHub Directors MCP Server
Custom MCP server providing organization-level tools for directors:
- Org reports
- Member management
- Team management

Related to context firewall concepts: tools are read-oriented by default with clear scopes.
Requires GITHUB_TOKEN environment variable with appropriate org permissions (admin or owner for some actions).
The token is checked when a GitHub API request is made, so the module can be imported without one.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

GITHUB_API = "https://api.github.com"
PER_PAGE_MAX = 100
_LINK_REL_RE = re.compile(r'rel\s*=\s*"?([^"]+)"?', re.IGNORECASE)

_client: httpx.AsyncClient | None = None

server = Server("github-directors")


class GitHubRateLimitError(Exception):
    """Raised when GitHub API rate limit has been exhausted."""

    def __init__(self, status_code: int, reset: str | None = None):
        self.status_code = status_code
        self.reset = reset
        parts = [f"GitHub API rate limit exceeded (HTTP {status_code})."]
        if reset:
            try:
                reset_dt = datetime.fromtimestamp(int(reset), tz=timezone.utc)
                parts.append(f"Rate limit resets at {reset_dt.isoformat()}.")
            except (TypeError, ValueError):
                parts.append(f"Rate limit resets at {reset}.")
        super().__init__(" ".join(parts))


def parse_link_rel(link_header: str | None, rel: str) -> str | None:
    """Return the URL for a rel value from a GitHub/RFC 5988 Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        part = part.strip()
        if not part.startswith("<"):
            continue
        try:
            gt = part.index(">")
        except ValueError:
            continue
        url = part[1:gt]
        params = part[gt + 1 :]
        match = _LINK_REL_RE.search(params)
        if match and match.group(1).strip().lower() == rel.lower():
            return url
    return None


def get_auth_headers() -> dict[str, str]:
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN environment variable is required. "
            "Set it before calling GitHub Directors tools."
        )
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating it on first use."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(base_url=GITHUB_API, timeout=30.0)
    return _client


def set_client(client: httpx.AsyncClient | None) -> None:
    """Replace the shared client (used by tests). Does not close the previous client."""
    global _client
    _client = client


async def aclose_client() -> None:
    """Close and clear the shared client."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _raise_for_rate_limit(resp: httpx.Response) -> None:
    if resp.status_code in (403, 429) and resp.headers.get("x-ratelimit-remaining") == "0":
        raise GitHubRateLimitError(resp.status_code, resp.headers.get("x-ratelimit-reset"))


async def github_request_raw(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """Perform a GitHub API request and return the raw response (after rate-limit checks)."""
    headers = get_auth_headers()
    extra_headers = kwargs.pop("headers", None)
    if extra_headers:
        headers.update(extra_headers)
    client = get_client()
    resp = await client.request(method, url, headers=headers, **kwargs)
    _raise_for_rate_limit(resp)
    resp.raise_for_status()
    return resp


async def github_request(method: str, path: str, **kwargs: Any) -> Any:
    resp = await github_request_raw(method, path, **kwargs)
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


async def github_paged_request(
    path: str,
    params: dict[str, Any] | None = None,
    max_results: int | None = None,
) -> list[Any]:
    """Fetch all pages of a list endpoint, following Link rel=next headers.

    An optional max_results cap stops pagination once enough items are collected.
    """
    if max_results is not None and max_results <= 0:
        return []

    query: dict[str, Any] | None = dict(params or {})
    query.setdefault("per_page", PER_PAGE_MAX)
    url: str | None = path
    results: list[Any] = []
    pages = 0

    while url:
        pages += 1
        if pages > 1000:
            raise RuntimeError(f"Pagination exceeded 1000 pages for {path}")
        # Only send params on the first request. Later pages use the absolute
        # Link URL, and an empty params dict would strip its query string.
        resp = await github_request_raw("GET", url, params=query)
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError(f"Expected a list response from {url}, got {type(data).__name__}")
        for item in data:
            results.append(item)
            if max_results is not None and len(results) >= max_results:
                return results
        url = parse_link_rel(resp.headers.get("link"), "next")
        query = None

    return results


async def github_count(path: str, params: dict[str, Any] | None = None) -> int:
    """Count items on a list endpoint using Link rel=last with per_page=1."""
    query = dict(params or {})
    query["per_page"] = 1
    resp = await github_request_raw("GET", path, params=query)
    last_url = parse_link_rel(resp.headers.get("link"), "last")
    if last_url:
        page_vals = parse_qs(urlparse(last_url).query).get("page")
        if page_vals:
            return int(page_vals[0])
    data = resp.json()
    return len(data) if isinstance(data, list) else 0


def _member_payload(member: dict[str, Any], role: str) -> dict[str, Any]:
    return {
        "login": member.get("login"),
        "id": member.get("id"),
        "type": member.get("type"),
        "role": role,
    }


async def fetch_org_members(
    org: str,
    role: str = "all",
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    if role not in {"all", "admin", "member"}:
        raise ValueError("role must be one of: all, admin, member")

    members_path = f"/orgs/{org}/members"
    if role in {"admin", "member"}:
        members = await github_paged_request(
            members_path, params={"role": role}, max_results=max_results
        )
        return [_member_payload(m, role) for m in members]

    members = await github_paged_request(members_path, max_results=max_results)
    admins = await github_paged_request(members_path, params={"role": "admin"})
    admin_logins = {a.get("login") for a in admins}
    return [
        _member_payload(m, "admin" if m.get("login") in admin_logins else "member")
        for m in members
    ]


async def fetch_org_teams(
    org: str,
    max_results: int | None = None,
    include_member_counts: bool = False,
) -> list[dict[str, Any]]:
    raw_teams = await github_paged_request(f"/orgs/{org}/teams", max_results=max_results)
    teams: list[dict[str, Any]] = [
        {
            "name": t.get("name"),
            "slug": t.get("slug"),
            "privacy": t.get("privacy"),
            "description": t.get("description"),
        }
        for t in raw_teams
    ]
    if include_member_counts:
        for team in teams:
            slug = team.get("slug")
            if not slug:
                team["members_count"] = 0
                continue
            detail = await github_request("GET", f"/orgs/{org}/teams/{slug}")
            team["members_count"] = (detail or {}).get("members_count", 0)
    return teams


async def fetch_team_members(
    org: str,
    team_slug: str,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    members = await github_paged_request(
        f"/orgs/{org}/teams/{team_slug}/members", max_results=max_results
    )
    return [{"login": m.get("login"), "id": m.get("id")} for m in members]


async def fetch_outside_collaborator_count(org: str) -> tuple[int | None, str | None]:
    try:
        return await github_count(f"/orgs/{org}/outside_collaborators"), None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return None, (
                "Outside collaborator count is unavailable (HTTP 403). "
                "This endpoint requires organization admin permission."
            )
        raise


async def fetch_org_report_summary(org: str) -> dict[str, Any]:
    info = await github_request("GET", f"/orgs/{org}")
    member_count = await github_count(f"/orgs/{org}/members")
    admin_count = await github_count(f"/orgs/{org}/members", params={"role": "admin"})
    team_count = await github_count(f"/orgs/{org}/teams")
    outside_count, outside_note = await fetch_outside_collaborator_count(org)

    summary: dict[str, Any] = {
        "org": org,
        "name": info.get("name") if isinstance(info, dict) else None,
        "member_count": member_count,
        "admin_count": admin_count,
        "team_count": team_count,
        "public_repos": info.get("public_repos") if isinstance(info, dict) else None,
        "private_repos": info.get("total_private_repos") if isinstance(info, dict) else None,
        "two_factor_requirement_enabled": (
            info.get("two_factor_requirement_enabled") if isinstance(info, dict) else None
        ),
        "outside_collaborator_count": outside_count,
    }
    if outside_note:
        summary["note"] = outside_note
    return summary


def _json_content(payload: Any) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, indent=2))]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_org_info",
            description="Get high-level information about a GitHub organization (for directors).",
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login name"}
                },
                "required": ["org"],
            },
        ),
        Tool(
            name="list_org_members",
            description=(
                "List all members of an organization (paginated). "
                "Supports a role filter (all/admin/member) and labels each member's role. "
                "When role is 'all', admins are resolved via the role=admin endpoint."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login name"},
                    "role": {
                        "type": "string",
                        "enum": ["all", "admin", "member"],
                        "default": "all",
                        "description": "Filter members by role. 'all' labels each member as admin or member.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Optional cap on the number of members returned after pagination.",
                    },
                },
                "required": ["org"],
            },
        ),
        Tool(
            name="list_org_teams",
            description=(
                "List all teams in an organization (paginated). "
                "Member counts are omitted by default because the list endpoint does not return them. "
                "Set include_member_counts to fetch an accurate count per team."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login name"},
                    "max_results": {
                        "type": "integer",
                        "description": "Optional cap on the number of teams returned after pagination.",
                    },
                    "include_member_counts": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "If true, fetch each team individually to include an accurate members_count."
                        ),
                    },
                },
                "required": ["org"],
            },
        ),
        Tool(
            name="list_team_members",
            description="List all members of a specific team (paginated).",
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string"},
                    "team_slug": {"type": "string", "description": "Team slug (URL-friendly name)"},
                    "max_results": {
                        "type": "integer",
                        "description": "Optional cap on the number of members returned after pagination.",
                    },
                },
                "required": ["org", "team_slug"],
            },
        ),
        Tool(
            name="get_org_report_summary",
            description=(
                "Director-level summary: member count, admin count, team count, "
                "public/private repo counts, 2FA requirement, and outside collaborator count."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string"}
                },
                "required": ["org"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "get_org_info":
            data = await github_request("GET", f"/orgs/{arguments['org']}")
            summary = {
                "login": data.get("login"),
                "name": data.get("name"),
                "description": data.get("description"),
                "public_repos": data.get("public_repos"),
                "followers": data.get("followers"),
                "created_at": data.get("created_at"),
                "two_factor_requirement_enabled": data.get("two_factor_requirement_enabled"),
            }
            return _json_content(summary)

        elif name == "list_org_members":
            members = await fetch_org_members(
                arguments["org"],
                role=arguments.get("role") or "all",
                max_results=_optional_int(arguments.get("max_results")),
            )
            return _json_content({"count": len(members), "members": members})

        elif name == "list_org_teams":
            teams = await fetch_org_teams(
                arguments["org"],
                max_results=_optional_int(arguments.get("max_results")),
                include_member_counts=bool(arguments.get("include_member_counts", False)),
            )
            return _json_content({"count": len(teams), "teams": teams})

        elif name == "list_team_members":
            members = await fetch_team_members(
                arguments["org"],
                arguments["team_slug"],
                max_results=_optional_int(arguments.get("max_results")),
            )
            return _json_content({"count": len(members), "members": members})

        elif name == "get_org_report_summary":
            return _json_content(await fetch_org_report_summary(arguments["org"]))

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except GitHubRateLimitError as e:
        return [TextContent(type="text", text=str(e))]
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"GitHub API error: {e.response.status_code} - {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def main():
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await aclose_client()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
