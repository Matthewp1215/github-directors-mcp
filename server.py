#!/usr/bin/env python3
"""
GitHub Directors MCP Server
Custom MCP server providing organization-level tools for directors:
- Org reports
- Member management
- Team management

Related to context firewall concepts: tools are read-oriented by default with clear scopes.
Requires GITHUB_TOKEN environment variable with appropriate org permissions (admin or owner for some actions).
"""

import os
import json
from typing import Any
import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# GitHub API base
GITHUB_API = "https://api.github.com"
TOKEN = os.getenv("GITHUB_TOKEN")

if not TOKEN:
    raise RuntimeError("GITHUB_TOKEN environment variable is required")

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

server = Server("github-directors")

async def github_request(method: str, path: str, **kwargs) -> Any:
    async with httpx.AsyncClient() as client:
        resp = await client.request(method, f"{GITHUB_API}{path}", headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()

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
                "required": ["org"]
            }
        ),
        Tool(
            name="list_org_members",
            description="List members of an organization, including role info where available.",
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login name"},
                    "per_page": {"type": "integer", "description": "Results per page (max 100)", "default": 30}
                },
                "required": ["org"]
            }
        ),
        Tool(
            name="list_org_teams",
            description="List all teams in an organization.",
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string", "description": "Organization login name"}
                },
                "required": ["org"]
            }
        ),
        Tool(
            name="list_team_members",
            description="List members of a specific team.",
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string"},
                    "team_slug": {"type": "string", "description": "Team slug (URL-friendly name)"}
                },
                "required": ["org", "team_slug"]
            }
        ),
        Tool(
            name="get_org_report_summary",
            description="Generate a simple director-level summary report: member count, team count, public repos count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "org": {"type": "string"}
                },
                "required": ["org"]
            }
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
            return [TextContent(type="text", text=json.dumps(summary, indent=2))]

        elif name == "list_org_members":
            per_page = arguments.get("per_page", 30)
            data = await github_request("GET", f"/orgs/{arguments['org']}/members", params={"per_page": per_page})
            members = [{"login": m.get("login"), "id": m.get("id"), "type": m.get("type")} for m in data]
            return [TextContent(type="text", text=json.dumps({"count": len(members), "members": members}, indent=2))]

        elif name == "list_org_teams":
            data = await github_request("GET", f"/orgs/{arguments['org']}/teams")
            teams = [{"name": t.get("name"), "slug": t.get("slug"), "privacy": t.get("privacy"), "members_count": t.get("members_count")} for t in data]
            return [TextContent(type="text", text=json.dumps({"count": len(teams), "teams": teams}, indent=2))]

        elif name == "list_team_members":
            data = await github_request("GET", f"/orgs/{arguments['org']}/teams/{arguments['team_slug']}/members")
            members = [{"login": m.get("login"), "id": m.get("id")} for m in data]
            return [TextContent(type="text", text=json.dumps({"count": len(members), "members": members}, indent=2))]

        elif name == "get_org_report_summary":
            org = arguments["org"]
            info = await github_request("GET", f"/orgs/{org}")
            # members = await github_request("GET", f"/orgs/{org}/members", params={"per_page": 1})
            teams = await github_request("GET", f"/orgs/{org}/teams")
            summary = {
                "org": org,
                "name": info.get("name"),
                "public_repos": info.get("public_repos"),
                "total_private_repos": info.get("total_private_repos"),
                "teams_count": len(teams),
                "note": "Member count requires pagination or admin access for full list; this is a director summary snapshot."
            }
            return [TextContent(type="text", text=json.dumps(summary, indent=2))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"GitHub API error: {e.response.status_code} - {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {str(e)}")]

async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
