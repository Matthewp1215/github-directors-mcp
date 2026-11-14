# Development notes

Living notes for the GitHub Directors MCP server. Keep this sheet updated as decisions, branch state, or demo status change.

## Owner preferences

Matthew:

- Fail-closed security: if an allowlist is not configured, deny org-scoped calls rather than opening access by default.
- Read-only by default. Write tools stay hidden and refused until policy explicitly turns `read_only` off.
- Local work first. Push, open PRs, or otherwise touch GitHub only with his go-ahead.
- Prefer stacked PRs when work lands in layers (hardening, then firewall, then follow-ups).
- Deliverables should be demo-ready: a policy, a script, and documented commands a director can run with a token.
- Keep this sheet updated.

## Decisions log

### 2026-11-14 — Hardening pass

- Paginated `list_org_members`, `list_org_teams`, and `list_team_members` via GitHub `Link` headers, with an optional `max_results` cap. Follow-up pages must not resend an empty `params` dict (httpx would strip the next URL's query string and loop).
- `list_org_members` gained a `role` filter (`all` / `admin` / `member`) and labels each member's role by fetching admins via `role=admin`.
- `list_org_teams` dropped the always-empty list-endpoint `members_count`; `include_member_counts` fetches an accurate count per team.
- `get_org_report_summary` now includes member, admin, and team counts, public/private repo counts, 2FA requirement, and outside collaborator count (403 on that endpoint is noted, not fatal).
- `GITHUB_TOKEN` is checked at request time so the module can be imported without a token. The process reuses one `httpx.AsyncClient`. HTTP 403/429 with `x-ratelimit-remaining: 0` is reported as a rate-limit error.
- **`mcp` is pinned to `>=1.0.0,<2`**. mcp 2.x renamed `Server` APIs (`list_tools` / `call_tool` decorators) and would break this server.

### 2026-11-14 — Context-firewall guardrails

- Policy loads from YAML or JSON (`DIRECTORS_MCP_POLICY`, default `./policy.yaml`) with environment-variable overrides. See `policy.example.yaml`.
- Org allowlist (`allowed_orgs`, case-insensitive) is fail-closed: no list and no `allow_all_orgs: true` means every org-scoped call is denied, and no GitHub request is made.
- Tools are tagged `read` or `write` in a registry. `read_only` defaults to true. Optional `allowed_tools` / `denied_tools` (deny wins) hide and refuse tools.
- JSON Lines audit log (default `./audit.log`) records UTC ISO timestamp, tool, redacted arguments, decision plus reason, outcome, HTTP status when relevant, and `duration_ms`. The token is never logged. Log-write failures go to stderr and do not fail the tool call.

## Current state

- Branches (local, 2026-11-14):
  - `main` — original first cut (5 read-only org tools, no pagination or firewall).
  - `harden-core-tools` — hardening pass above.
  - `firewall-guardrails` (stacked on `harden-core-tools`) — policy enforcement, audit logging, demo pack, and this notes sheet.
- Pull requests: not opened yet; to be stacked as `harden-core-tools` → `main`, then `firewall-guardrails` → `harden-core-tools` after Matthew's push approval.
- Tests: 28 passed (14 core-tool tests + 14 firewall tests), plus a `python demo/run_demo.py --mock` walkthrough.

## Next ideas

- More director reports: 2FA compliance across members, inactive or stale members, billing/audit-log snapshots.
- Real write tools (invite members, create teams) registered as `write` and only callable when `read_only: false`.
- Policy hot-reload so a running stdio server picks up `policy.yaml` changes without a restart.
