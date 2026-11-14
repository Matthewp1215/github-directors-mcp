# GitHub Directors MCP Server

Custom Model Context Protocol (MCP) server providing organization-level tools tailored for directors (org owners / admins).

## Features

- `get_org_info` – High-level organization details
- `list_org_members` – List organization members
- `list_org_teams` – List all teams
- `list_team_members` – Members of a specific team
- `get_org_report_summary` – Simple director summary report (repos, teams)

## Context Firewall Integration

This server is designed to be used behind a context firewall (e.g. ketriumlabs/context-firewall or similar policy proxies). Directors can grant scoped access to specific tools/orgs via policy.yaml so agents only see what they are approved for. All actions are logged at the GitHub API level via the token.

## Requirements

- Python 3.10+
- `pip install mcp httpx`
- `GITHUB_TOKEN` environment variable with `read:org` (or `admin:org` for fuller access) scope

## Usage

```bash
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

## Notes

- Read-oriented by default. Extend with write tools (invite members, create teams) only if directors need them and policies allow.
- For production, add pagination, rate-limit handling, and more report endpoints (billing, audit log, etc.).
- Related to ongoing context firewall work: every conversation learns and remembers preferences via memory.
