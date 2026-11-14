"""Tests for the GitHub Directors MCP server helpers and tools."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
import respx

import server


@pytest.fixture
def token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")


@pytest.fixture
async def clean_client() -> AsyncIterator[None]:
    await server.aclose_client()
    yield
    await server.aclose_client()


@pytest.fixture
async def github_mock() -> AsyncIterator[respx.MockRouter]:
    # Use an async context: the sync respx_mock pytest fixture deadlocks with pytest-asyncio.
    async with respx.mock(assert_all_called=False) as router:
        yield router


def _payload(result: list) -> dict:
    assert len(result) == 1
    return json.loads(result[0].text)


def test_import_does_not_require_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # Re-evaluate the auth helper; importing the module must not have crashed.
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN environment variable is required"):
        server.get_auth_headers()


def test_parse_link_rel_next_and_last() -> None:
    header = (
        '<https://api.github.com/orgs/acme/members?page=2>; rel="next", '
        '<https://api.github.com/orgs/acme/members?page=5>; rel="last"'
    )
    assert server.parse_link_rel(header, "next") == (
        "https://api.github.com/orgs/acme/members?page=2"
    )
    assert server.parse_link_rel(header, "last") == (
        "https://api.github.com/orgs/acme/members?page=5"
    )
    assert server.parse_link_rel(header, "prev") is None
    assert server.parse_link_rel(None, "next") is None
    assert server.parse_link_rel("", "next") is None


def test_parse_link_rel_unquoted() -> None:
    header = "<https://api.github.com/orgs/acme/teams?page=2>; rel=next"
    assert server.parse_link_rel(header, "next") == (
        "https://api.github.com/orgs/acme/teams?page=2"
    )


@pytest.mark.asyncio
async def test_request_without_token_fails_clearly(
    monkeypatch: pytest.MonkeyPatch, clean_client: None
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN environment variable is required"):
        await server.github_request("GET", "/orgs/acme")


@pytest.mark.asyncio
async def test_pagination_follows_link_and_respects_max_results(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    page1_requests = {"count": 0}
    page2_requests = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page") or 1)
        if page == 1:
            page1_requests["count"] += 1
            return httpx.Response(
                200,
                json=[
                    {"login": "alice", "id": 1, "type": "User"},
                    {"login": "bob", "id": 2, "type": "User"},
                ],
                headers={
                    "Link": (
                        '<https://api.github.com/orgs/acme/members?per_page=100&page=2>; rel="next", '
                        '<https://api.github.com/orgs/acme/members?per_page=100&page=2>; rel="last"'
                    )
                },
            )
        page2_requests["count"] += 1
        return httpx.Response(
            200,
            json=[{"login": "carol", "id": 3, "type": "User"}],
        )

    github_mock.get(url__regex=r"https://api\.github\.com/orgs/acme/members(?:\?|$)").mock(
        side_effect=handler
    )

    all_members = await server.github_paged_request("/orgs/acme/members")
    assert [m["login"] for m in all_members] == ["alice", "bob", "carol"]
    assert page2_requests["count"] == 1

    page1_requests["count"] = 0
    page2_requests["count"] = 0
    capped = await server.github_paged_request("/orgs/acme/members", max_results=2)
    assert [m["login"] for m in capped] == ["alice", "bob"]
    assert page2_requests["count"] == 0


@pytest.mark.asyncio
async def test_list_org_members_marks_admin_roles(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        role = request.url.params.get("role")
        if role == "admin":
            return httpx.Response(
                200, json=[{"login": "alice", "id": 1, "type": "User"}]
            )
        if role == "member":
            return httpx.Response(
                200,
                json=[
                    {"login": "bob", "id": 2, "type": "User"},
                    {"login": "carol", "id": 3, "type": "User"},
                ],
            )
        return httpx.Response(
            200,
            json=[
                {"login": "alice", "id": 1, "type": "User"},
                {"login": "bob", "id": 2, "type": "User"},
                {"login": "carol", "id": 3, "type": "User"},
            ],
        )

    github_mock.get(url__regex=r"https://api\.github\.com/orgs/acme/members(?:\?|$)").mock(
        side_effect=handler
    )

    result = _payload(await server.call_tool("list_org_members", {"org": "acme"}))
    roles = {m["login"]: m["role"] for m in result["members"]}
    assert result["count"] == 3
    assert roles == {"alice": "admin", "bob": "member", "carol": "member"}

    admins = _payload(
        await server.call_tool("list_org_members", {"org": "acme", "role": "admin"})
    )
    assert [m["login"] for m in admins["members"]] == ["alice"]
    assert all(m["role"] == "admin" for m in admins["members"])

    members_only = _payload(
        await server.call_tool("list_org_members", {"org": "acme", "role": "member"})
    )
    assert [m["login"] for m in members_only["members"]] == ["bob", "carol"]
    assert all(m["role"] == "member" for m in members_only["members"])


@pytest.mark.asyncio
async def test_list_org_teams_omits_empty_count_unless_requested(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    def list_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page") or 1)
        if page == 1:
            return httpx.Response(
                200,
                json=[{"name": "Core", "slug": "core", "privacy": "closed", "description": "core team"}],
                headers={
                    "Link": '<https://api.github.com/orgs/acme/teams?per_page=100&page=2>; rel="next"'
                },
            )
        return httpx.Response(
            200,
            json=[{"name": "Docs", "slug": "docs", "privacy": "closed", "description": None}],
        )

    github_mock.get(url__regex=r"https://api\.github\.com/orgs/acme/teams(?:\?|$)").mock(
        side_effect=list_handler
    )
    github_mock.get("https://api.github.com/orgs/acme/teams/core").mock(
        return_value=httpx.Response(200, json={"members_count": 4})
    )
    github_mock.get("https://api.github.com/orgs/acme/teams/docs").mock(
        return_value=httpx.Response(200, json={"members_count": 2})
    )

    listed = _payload(await server.call_tool("list_org_teams", {"org": "acme"}))
    assert listed["count"] == 2
    assert [t["slug"] for t in listed["teams"]] == ["core", "docs"]
    assert all("members_count" not in t for t in listed["teams"])

    with_counts = _payload(
        await server.call_tool(
            "list_org_teams", {"org": "acme", "include_member_counts": True}
        )
    )
    counts = {t["slug"]: t["members_count"] for t in with_counts["teams"]}
    assert counts == {"core": 4, "docs": 2}


@pytest.mark.asyncio
async def test_list_team_members_paginates(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page") or 1)
        if page == 1:
            return httpx.Response(
                200,
                json=[{"login": "alice", "id": 1}],
                headers={
                    "Link": (
                        '<https://api.github.com/orgs/acme/teams/core/members?page=2>; rel="next"'
                    )
                },
            )
        return httpx.Response(200, json=[{"login": "bob", "id": 2}])

    github_mock.get(
        url__regex=r"https://api\.github\.com/orgs/acme/teams/core/members(?:\?|$)"
    ).mock(side_effect=handler)

    result = _payload(
        await server.call_tool(
            "list_team_members", {"org": "acme", "team_slug": "core"}
        )
    )
    assert result["count"] == 2
    assert [m["login"] for m in result["members"]] == ["alice", "bob"]


def _count_response(total: int, item: dict) -> httpx.Response:
    if total == 0:
        return httpx.Response(200, json=[])
    if total == 1:
        return httpx.Response(200, json=[item])
    return httpx.Response(
        200,
        json=[item],
        headers={
            "Link": (
                f'<https://api.github.com/_count?per_page=1&page={total}>; rel="last"'
            )
        },
    )


@pytest.mark.asyncio
async def test_org_report_summary_includes_counts(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    github_mock.get("https://api.github.com/orgs/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "login": "acme",
                "name": "Acme Inc",
                "public_repos": 12,
                "total_private_repos": 7,
                "two_factor_requirement_enabled": True,
            },
        )
    )

    def members_handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("role") == "admin":
            return _count_response(3, {"login": "alice"})
        return _count_response(25, {"login": "alice"})

    github_mock.get(url__regex=r"https://api\.github\.com/orgs/acme/members(?:\?|$)").mock(
        side_effect=members_handler
    )
    github_mock.get(url__regex=r"https://api\.github\.com/orgs/acme/teams(?:\?|$)").mock(
        return_value=_count_response(5, {"slug": "core"})
    )
    github_mock.get(
        url__regex=r"https://api\.github\.com/orgs/acme/outside_collaborators(?:\?|$)"
    ).mock(return_value=_count_response(4, {"login": "ext"}))

    result = _payload(await server.call_tool("get_org_report_summary", {"org": "acme"}))
    assert result == {
        "org": "acme",
        "name": "Acme Inc",
        "member_count": 25,
        "admin_count": 3,
        "team_count": 5,
        "public_repos": 12,
        "private_repos": 7,
        "two_factor_requirement_enabled": True,
        "outside_collaborator_count": 4,
    }
    assert "note" not in result


@pytest.mark.asyncio
async def test_org_report_summary_handles_outside_collaborator_403(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    github_mock.get("https://api.github.com/orgs/acme").mock(
        return_value=httpx.Response(
            200,
            json={
                "login": "acme",
                "name": "Acme Inc",
                "public_repos": 1,
                "total_private_repos": 0,
                "two_factor_requirement_enabled": False,
            },
        )
    )
    github_mock.get(url__regex=r"https://api\.github\.com/orgs/acme/members(?:\?|$)").mock(
        return_value=_count_response(2, {"login": "alice"})
    )
    github_mock.get(url__regex=r"https://api\.github\.com/orgs/acme/teams(?:\?|$)").mock(
        return_value=_count_response(0, {})
    )
    github_mock.get(
        url__regex=r"https://api\.github\.com/orgs/acme/outside_collaborators(?:\?|$)"
    ).mock(return_value=httpx.Response(403, json={"message": "Must be an admin"}))

    result = _payload(await server.call_tool("get_org_report_summary", {"org": "acme"}))
    assert result["member_count"] == 2
    assert result["admin_count"] == 2  # same members mock for admin + all
    assert result["team_count"] == 0
    assert result["outside_collaborator_count"] is None
    assert "403" in result["note"]
    assert "admin" in result["note"].lower()


@pytest.mark.asyncio
async def test_rate_limit_403_and_429_raise_clear_error(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    reset = "1730000000"
    github_mock.get("https://api.github.com/orgs/acme").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
        )
    )
    with pytest.raises(server.GitHubRateLimitError, match="rate limit exceeded"):
        await server.github_request("GET", "/orgs/acme")

    text = (await server.call_tool("get_org_info", {"org": "acme"}))[0].text
    assert "rate limit exceeded" in text.lower()
    assert "HTTP 403" in text
    assert "Rate limit resets at" in text

    github_mock.get("https://api.github.com/orgs/acme").mock(
        return_value=httpx.Response(
            429,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset},
        )
    )
    with pytest.raises(server.GitHubRateLimitError, match="HTTP 429"):
        await server.github_request("GET", "/orgs/acme")


@pytest.mark.asyncio
async def test_forbidden_without_rate_limit_header_is_http_error(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    github_mock.get("https://api.github.com/orgs/acme").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"})
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await server.github_request("GET", "/orgs/acme")
    assert exc_info.value.response.status_code == 403

    text = (await server.call_tool("get_org_info", {"org": "acme"}))[0].text
    assert text.startswith("GitHub API error: 403")
    assert "rate limit" not in text.lower()


@pytest.mark.asyncio
async def test_github_count_empty_and_single_page(
    token: None, clean_client: None, github_mock: respx.MockRouter
) -> None:
    github_mock.get(url__regex=r"https://api\.github\.com/orgs/empty/members(?:\?|$)").mock(
        return_value=httpx.Response(200, json=[])
    )
    github_mock.get(url__regex=r"https://api\.github\.com/orgs/one/members(?:\?|$)").mock(
        return_value=httpx.Response(200, json=[{"login": "alice"}])
    )
    assert await server.github_count("/orgs/empty/members") == 0
    assert await server.github_count("/orgs/one/members") == 1


@pytest.mark.asyncio
async def test_invalid_role_is_reported(token: None, clean_client: None) -> None:
    text = (
        await server.call_tool("list_org_members", {"org": "acme", "role": "owner"})
    )[0].text
    assert text.startswith("Error:")
    assert "role must be one of" in text
