# GitHub Directors MCP Server

Custom Model Context Protocol (MCP) server providing organization-level tools tailored for directors (org owners / admins).

## Features

- `get_org_info` – High-level organization details
- `list_org_members` – List all organization members (paginated). Optional `role` filter (`all`, `admin`, or `member`) and `max_results` cap. Each member includes a `role` field; when `role` is `all` (the default), admins are resolved via the `role=admin` endpoint and labeled accordingly.
- `list_org_teams` – List all teams (paginated), with optional `max_results`. Member counts are omitted by default because GitHub's list-teams endpoint does not return them. Pass `include_member_counts=true` to fetch an accurate `members_count` for each team.
- `list_team_members` – Members of a specific team (paginated), with optional `max_results`.
- `get_org_report_summary` – Director summary including member count, admin count, team count, public and private repo counts, whether 2FA is required, and outside collaborator count. If `/orgs/{org}/outside_collaborators` returns 403 (missing admin permission), the count is `null` and a note explains why.

List tools follow GitHub `Link` headers to fetch every page unless `max_results` stops them early.

## Context Firewall Integration

This server is designed to be used behind a context firewall (e.g. ketriumlabs/context-firewall or similar policy proxies). Directors can grant scoped access to specific tools/orgs via policy.yaml so agents only see what they are approved for. All actions are logged at the GitHub API level via the token.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (the MCP SDK is pinned to 1.x, `mcp>=1.0.0,<2`; mcp 2 renamed the server APIs this project uses)
- `GITHUB_TOKEN` environment variable with `read:org` (or `admin:org` for fuller access, including outside collaborators) scope

The token is checked when a GitHub request is made, not at import time, so the module can be imported in tests or tooling without a token. A missing token raises a clear error on the first API call.

API responses that are HTTP 403 or 429 with `x-ratelimit-remaining: 0` are reported as a rate-limit error and include the reset time when GitHub sends `x-ratelimit-reset`. Other 403s (for example, missing permission) are returned as ordinary GitHub API errors.

The server reuses a single `httpx.AsyncClient` for the process lifetime.

## Usage

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_your_token_here
python server.py
```

Then configure in your MCP client (Claude Desktop, Cursor, etc.):

```json
{
  "mcpServers": {
    "github-directors": {
      "command": "python",
      "args": ["/path/to/server.py"],
      "env": {
        "GITHUB_TOKEN": "${GITHUB_TOKEN}"
      }
    }
  }
}
```

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

`requirements-dev.txt` includes pytest, pytest-asyncio, and respx. GitHub API calls in tests are mocked; no live token is required to run the suite.

## Notes

- Read-oriented by default. Extend with write tools (invite members, create teams) only if directors need them and policies allow.
- Related to ongoing context firewall work: every conversation learns and remembers preferences via memory.
