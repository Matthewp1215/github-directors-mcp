# GitHub Directors MCP Server

Custom Model Context Protocol (MCP) server providing organization-level tools tailored for directors (org owners / admins).

See [DEVELOPMENT_NOTES.md](DEVELOPMENT_NOTES.md) for owner preferences, decisions, current branch state, and next ideas.

## Features

- `get_org_info` – High-level organization details
- `list_org_members` – List all organization members (paginated). Optional `role` filter (`all`, `admin`, or `member`) and `max_results` cap. Each member includes a `role` field; when `role` is `all` (the default), admins are resolved via the `role=admin` endpoint and labeled accordingly.
- `list_org_teams` – List all teams (paginated), with optional `max_results`. Member counts are omitted by default because GitHub's list-teams endpoint does not return them. Pass `include_member_counts=true` to fetch an accurate `members_count` for each team.
- `list_team_members` – Members of a specific team (paginated), with optional `max_results`.
- `get_org_report_summary` – Director summary including member count, admin count, team count, public and private repo counts, whether 2FA is required, and outside collaborator count. If `/orgs/{org}/outside_collaborators` returns 403 (missing admin permission), the count is `null` and a note explains why.

List tools follow GitHub `Link` headers to fetch every page unless `max_results` stops them early.

## Context Firewall

The server enforces a fail-closed policy before any GitHub request is made. Copy `policy.example.yaml` to `policy.yaml`, or point `DIRECTORS_MCP_POLICY` at a YAML or JSON file. Environment variables override file values.

### Organization allowlist

- `allowed_orgs` is a case-insensitive list of organization logins.
- A tool call whose `org` argument is not on the list is denied with a clear message and never reaches GitHub.
- If no allowlist is configured, every org-scoped call is denied unless you set `allow_all_orgs: true`.

### Read-only mode and tool lists

- `read_only` defaults to `true`. Every tool is tagged in a registry as `read` or `write`. Write tools are hidden from `list_tools` and refused if invoked while read-only is on. All built-in tools are reads today; the write path is in place for future mutations.
- Optional `allowed_tools` and `denied_tools` lists further restrict which tools are advertised and callable. `denied_tools` always wins.

### Audit log

Each tool call appends one JSON Lines record to `./audit.log` (override with `audit_log` in the policy file or `DIRECTORS_MCP_AUDIT_LOG`). Records include a UTC ISO timestamp, tool name, arguments with secrets redacted, the decision (`allowed` or `denied`, plus a reason), the outcome (`ok` or `error`), HTTP status when a GitHub request was made, and `duration_ms`. The GitHub token is never logged. If the log cannot be written, the tool call still returns normally and the failure is reported on stderr (never stdout, which is the MCP stdio channel).

### Environment variables

| Variable | Purpose |
| --- | --- |
| `DIRECTORS_MCP_POLICY` | Path to the policy file (default `./policy.yaml`) |
| `DIRECTORS_MCP_ALLOWED_ORGS` | Comma-separated org allowlist |
| `DIRECTORS_MCP_ALLOW_ALL_ORGS` | `true` / `false` |
| `DIRECTORS_MCP_READ_ONLY` | `true` / `false` |
| `DIRECTORS_MCP_ALLOWED_TOOLS` | Comma-separated tool allowlist |
| `DIRECTORS_MCP_DENIED_TOOLS` | Comma-separated tool denylist |
| `DIRECTORS_MCP_AUDIT_LOG` | JSON Lines audit log path |

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
cp policy.example.yaml policy.yaml   # edit allowed_orgs first; the default is fail-closed
export GITHUB_TOKEN=ghp_your_token_here
# optional: export DIRECTORS_MCP_POLICY=/path/to/policy.yaml
# optional: export DIRECTORS_MCP_AUDIT_LOG=/path/to/audit.log
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
        "GITHUB_TOKEN": "${GITHUB_TOKEN}",
        "DIRECTORS_MCP_POLICY": "/path/to/policy.yaml",
        "DIRECTORS_MCP_AUDIT_LOG": "/path/to/audit.log"
      }
    }
  }
}
```


## Demo

A short in-process walkthrough lives in `demo/`. It loads `demo/policy.demo.yaml` (allowlist: the public `github` org, `read_only: true`), calls the tool handlers, and prints the audit log. The only required environment variable is `GITHUB_TOKEN`.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export GITHUB_TOKEN=ghp_your_token_here
python demo/run_demo.py
```

The script shows (a) an allowed `get_org_report_summary` for `github`, (b) a capped member list, (c) a denied call for an org that is not on the allowlist, (d) a demo write tool refused while read-only is on, and (e) the latest `demo/audit.log` entries. To try your own org, edit `allowed_orgs` in `demo/policy.demo.yaml`.

To exercise the same flow offline (no token, no network):

```bash
pip install -r requirements-dev.txt   # not required for the live demo
python demo/run_demo.py --mock
```

Claude Desktop or Cursor config for the demo policy:

```json
{
  "mcpServers": {
    "github-directors": {
      "command": "python",
      "args": ["/path/to/github-directors-mcp/server.py"],
      "env": {
        "GITHUB_TOKEN": "${GITHUB_TOKEN}",
        "DIRECTORS_MCP_POLICY": "/path/to/github-directors-mcp/demo/policy.demo.yaml",
        "DIRECTORS_MCP_AUDIT_LOG": "/path/to/github-directors-mcp/demo/audit.log"
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

- Read-oriented by default (`read_only: true`). Add write tools only if directors need them, tag them as `write` in the registry, and turn off read-only in policy when they should be callable.
- Runtime dependencies: `mcp` (1.x), `httpx`, and `pyyaml`.
