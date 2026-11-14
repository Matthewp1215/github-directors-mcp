#!/usr/bin/env python3
"""Drive the GitHub Directors MCP tools in-process for a short firewall demo.

Requires only GITHUB_TOKEN in the environment. The script selects this
directory's policy and audit log so you do not need other env vars.

    export GITHUB_TOKEN=ghp_your_token_here
    python demo/run_demo.py

Use --mock to exercise the same flow against httpx.MockTransport (no network).
That path is for verification when a token is not available; it will set a
placeholder token if GITHUB_TOKEN is unset.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
ROOT = DEMO_DIR.parent
POLICY_PATH = DEMO_DIR / "policy.demo.yaml"
AUDIT_PATH = DEMO_DIR / "audit.log"
ALLOWED_ORG = "github"
DENIED_ORG = "octocat-not-allowed"
MEMBER_CAP = 8

# Make sure the repo root is importable even if the script is invoked by path.
sys.path.insert(0, str(ROOT))


def configure_environment(mock: bool) -> None:
    os.environ["DIRECTORS_MCP_POLICY"] = str(POLICY_PATH)
    os.environ["DIRECTORS_MCP_AUDIT_LOG"] = str(AUDIT_PATH)
    if mock and not os.getenv("GITHUB_TOKEN"):
        os.environ["GITHUB_TOKEN"] = "mock-token"


def load_server():
    import policy
    import server

    server.set_policy(policy.load_policy(str(POLICY_PATH)))
    # Honor the demo audit path even if the file also sets a relative default.
    current = server.get_policy()
    server.set_policy(
        policy.Policy(
            allowed_orgs=current.allowed_orgs,
            allow_all_orgs=current.allow_all_orgs,
            read_only=current.read_only,
            allowed_tools=current.allowed_tools,
            denied_tools=current.denied_tools,
            audit_log=str(AUDIT_PATH),
        )
    )
    return policy, server


def install_mock_transport(server) -> None:
    import httpx

    def count_response(total: int, item: dict) -> httpx.Response:
        if total <= 1:
            return httpx.Response(200, json=[item] if total == 1 else [])
        return httpx.Response(
            200,
            json=[item],
            headers={"Link": f'<https://api.github.com/_count?per_page=1&page={total}>; rel="last"'},
        )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        role = request.url.params.get("role")
        if path == f"/orgs/{ALLOWED_ORG}":
            return httpx.Response(
                200,
                json={
                    "login": ALLOWED_ORG,
                    "name": "GitHub",
                    "public_repos": 400,
                    "total_private_repos": 0,
                    "two_factor_requirement_enabled": True,
                },
            )
        if path == f"/orgs/{ALLOWED_ORG}/members":
            if role == "admin":
                return count_response(3, {"login": "octocat", "id": 1, "type": "User"})
            if request.url.params.get("per_page") == "1" and "page" not in request.url.params:
                return count_response(25, {"login": "octocat", "id": 1, "type": "User"})
            return httpx.Response(
                200,
                json=[
                    {"login": "octocat", "id": 1, "type": "User"},
                    {"login": "monalisa", "id": 2, "type": "User"},
                ],
            )
        if path == f"/orgs/{ALLOWED_ORG}/teams":
            return count_response(5, {"slug": "employees", "name": "Employees", "privacy": "closed"})
        if path == f"/orgs/{ALLOWED_ORG}/outside_collaborators":
            return count_response(2, {"login": "ext-collab"})
        return httpx.Response(404, json={"message": f"No mock for {request.method} {path}"})

    server.set_client(
        httpx.AsyncClient(
            base_url=server.GITHUB_API,
            transport=httpx.MockTransport(handler),
            timeout=10.0,
        )
    )


def register_demo_write_tool(server):
    """Temporary write tool so the demo can show a read-only refusal."""
    from mcp.types import TextContent, Tool

    name = "invite_org_member"

    async def handler(arguments: dict) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps({"invited": arguments}))]

    server.register_tool(
        name,
        "write",
        Tool(
            name=name,
            description="Demo-only write tool (not a real GitHub invite).",
            inputSchema={
                "type": "object",
                "properties": {"org": {"type": "string"}},
                "required": ["org"],
            },
        ),
        handler,
    )
    return name


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def show_result(result) -> str:
    text = result[0].text if result else ""
    print(text)
    return text


def print_audit_log(path: Path, limit: int = 10) -> None:
    if not path.is_file():
        print(f"No audit log found at {path}")
        return
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    shown = lines[-limit:]
    print(f"Showing last {len(shown)} of {len(lines)} entries from {path}:")
    print()
    for line in shown:
        event = json.loads(line)
        print(json.dumps(event, indent=2))
        print()


async def run(mock: bool) -> int:
    if not mock and not os.getenv("GITHUB_TOKEN"):
        print(
            "GITHUB_TOKEN is required to run the live demo.\n"
            "Export a token, or run `python demo/run_demo.py --mock` to verify the flow offline.",
            file=sys.stderr,
        )
        return 1

    configure_environment(mock)
    if AUDIT_PATH.exists():
        AUDIT_PATH.unlink()

    policy, server = load_server()
    write_tool = register_demo_write_tool(server)
    if mock:
        install_mock_transport(server)
        print("Running in --mock mode (no live GitHub requests).")
    else:
        print(f"Running live against api.github.com with policy {POLICY_PATH}.")

    try:
        banner(f"(a) Allowed org report summary for '{ALLOWED_ORG}'")
        summary_text = show_result(
            await server.call_tool("get_org_report_summary", {"org": ALLOWED_ORG})
        )
        if summary_text.startswith(("GitHub API error", "Error:", "Denied:")):
            print(
                "\nThe report summary was allowed by policy but GitHub returned an error "
                "(some fields need org membership or admin:org). Falling back to get_org_info:"
            )
            show_result(await server.call_tool("get_org_info", {"org": ALLOWED_ORG}))

        banner(f"(b) Member list for '{ALLOWED_ORG}' (max_results={MEMBER_CAP})")
        show_result(
            await server.call_tool(
                "list_org_members",
                {"org": ALLOWED_ORG, "role": "all", "max_results": MEMBER_CAP},
            )
        )

        banner(f"(c) Denied call for org '{DENIED_ORG}' (not on the allowlist)")
        show_result(await server.call_tool("get_org_info", {"org": DENIED_ORG}))

        banner(f"(d) Write tool '{write_tool}' refused in read-only mode")
        show_result(await server.call_tool(write_tool, {"org": ALLOWED_ORG}))

        banner("(e) Audit log")
        print_audit_log(AUDIT_PATH)
        return 0
    finally:
        server.unregister_tool(write_tool)
        await server.aclose_client()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the GitHub Directors MCP firewall demo.")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use in-memory HTTP mocks instead of the live GitHub API.",
    )
    args = parser.parse_args()
    return asyncio.run(run(mock=args.mock))


if __name__ == "__main__":
    raise SystemExit(main())
