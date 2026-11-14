"""Tests for context-firewall policy enforcement and audit logging."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from mcp.types import TextContent, Tool

import policy
import server


def _text(result: list) -> str:
    assert len(result) == 1
    return result[0].text


def _deny_if_github(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"GitHub should not be called: {request.method} {request.url}")


@pytest.fixture
def allow_acme(tmp_path: Path) -> policy.Policy:
    pol = policy.Policy(
        allowed_orgs=frozenset({"acme"}),
        read_only=True,
        audit_log=str(tmp_path / "audit.log"),
    )
    server.set_policy(pol)
    return pol


@pytest.fixture
def dummy_write_tool() -> Iterator[str]:
    name = "dummy_write_tool"

    async def handler(arguments: dict) -> list[TextContent]:
        return [TextContent(type="text", text=json.dumps({"ok": True, "org": arguments.get("org")}))]

    server.register_tool(
        name,
        "write",
        Tool(
            name=name,
            description="Test-only write tool used to exercise read-only guardrails.",
            inputSchema={
                "type": "object",
                "properties": {"org": {"type": "string"}},
                "required": ["org"],
            },
        ),
        handler,
    )
    yield name
    server.unregister_tool(name)


def _read_audit(path: str | Path) -> list[dict]:
    text = Path(path).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_default_policy_is_fail_closed_and_read_only() -> None:
    pol = policy.Policy()
    assert pol.allow_all_orgs is False
    assert pol.allowed_orgs == frozenset()
    assert pol.read_only is True
    decision = policy.evaluate_access(pol, "get_org_info", {"org": "acme"}, "read")
    assert decision.allowed is False
    assert "no organizations are allowed" in decision.reason


def test_load_policy_without_file_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing.yaml"
    monkeypatch.chdir(tmp_path)
    loaded = policy.load_policy(str(missing))
    assert loaded.allow_all_orgs is False
    assert loaded.allowed_orgs == frozenset()
    assert loaded.read_only is True


@pytest.mark.asyncio
async def test_fail_closed_default_denies_all_orgs_without_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, github_mock: respx.MockRouter
) -> None:
    monkeypatch.chdir(tmp_path)
    server.set_policy(policy.load_policy(str(tmp_path / "policy.yaml")))
    github_mock.route().mock(side_effect=_deny_if_github)
    result = await server.call_tool("get_org_info", {"org": "acme"})
    message = _text(result)
    assert message.startswith("Denied:")
    assert "no organizations are allowed" in message
    assert github_mock.calls.call_count == 0


@pytest.mark.asyncio
async def test_allowlist_allows_listed_org_and_is_case_insensitive(
    token: None,
    clean_client: None,
    github_mock: respx.MockRouter,
    allow_acme: policy.Policy,
) -> None:
    github_mock.get(url__regex=r"https://api\.github\.com/orgs/ACME$").mock(
        return_value=httpx.Response(200, json={"login": "acme", "name": "Acme Inc"})
    )
    payload = json.loads(_text(await server.call_tool("get_org_info", {"org": "ACME"})))
    assert payload["login"] == "acme"
    assert github_mock.calls.call_count == 1


@pytest.mark.asyncio
async def test_allowlist_denies_unknown_org_without_github_request(
    token: None,
    github_mock: respx.MockRouter,
    allow_acme: policy.Policy,
) -> None:
    github_mock.route().mock(side_effect=_deny_if_github)
    result = await server.call_tool("get_org_info", {"org": "evil-corp"})
    message = _text(result)
    assert message.startswith("Denied:")
    assert "evil-corp" in message
    assert "not on the allowed_orgs list" in message
    assert github_mock.calls.call_count == 0


@pytest.mark.asyncio
async def test_allow_all_orgs_permits_any_org(
    token: None, clean_client: None, github_mock: respx.MockRouter, tmp_path: Path
) -> None:
    server.set_policy(
        policy.Policy(allow_all_orgs=True, audit_log=str(tmp_path / "audit.log"))
    )
    github_mock.get("https://api.github.com/orgs/other").mock(
        return_value=httpx.Response(200, json={"login": "other", "name": "Other"})
    )
    payload = json.loads(_text(await server.call_tool("get_org_info", {"org": "other"})))
    assert payload["login"] == "other"


@pytest.mark.asyncio
async def test_read_only_hides_and_refuses_write_tools(
    dummy_write_tool: str, allow_acme: policy.Policy, github_mock: respx.MockRouter
) -> None:
    github_mock.route().mock(side_effect=_deny_if_github)
    names = {tool.name for tool in await server.list_tools()}
    assert dummy_write_tool not in names
    assert "get_org_info" in names

    message = _text(await server.call_tool(dummy_write_tool, {"org": "acme"}))
    assert message.startswith("Denied:")
    assert "write tool" in message
    assert "read-only" in message
    assert github_mock.calls.call_count == 0


@pytest.mark.asyncio
async def test_write_tool_is_listed_and_callable_when_read_only_off(
    dummy_write_tool: str, tmp_path: Path
) -> None:
    server.set_policy(
        policy.Policy(
            allowed_orgs=frozenset({"acme"}),
            read_only=False,
            audit_log=str(tmp_path / "audit.log"),
        )
    )
    names = {tool.name for tool in await server.list_tools()}
    assert dummy_write_tool in names
    payload = json.loads(_text(await server.call_tool(dummy_write_tool, {"org": "acme"})))
    assert payload == {"ok": True, "org": "acme"}


@pytest.mark.asyncio
async def test_allowed_tools_hides_and_refuses_others(
    allow_acme: policy.Policy, tmp_path: Path, github_mock: respx.MockRouter
) -> None:
    server.set_policy(
        policy.Policy(
            allowed_orgs=frozenset({"acme"}),
            allowed_tools=frozenset({"get_org_info"}),
            audit_log=str(tmp_path / "audit.log"),
        )
    )
    github_mock.route().mock(side_effect=_deny_if_github)
    names = {tool.name for tool in await server.list_tools()}
    assert names == {"get_org_info"}
    message = _text(await server.call_tool("list_org_members", {"org": "acme"}))
    assert message.startswith("Denied:")
    assert "not on the allowed_tools list" in message
    assert github_mock.calls.call_count == 0


@pytest.mark.asyncio
async def test_denied_tools_wins_over_allowed_tools(
    tmp_path: Path, github_mock: respx.MockRouter
) -> None:
    server.set_policy(
        policy.Policy(
            allowed_orgs=frozenset({"acme"}),
            allowed_tools=frozenset({"get_org_info", "list_org_members"}),
            denied_tools=frozenset({"list_org_members"}),
            audit_log=str(tmp_path / "audit.log"),
        )
    )
    github_mock.route().mock(side_effect=_deny_if_github)
    names = {tool.name for tool in await server.list_tools()}
    assert "get_org_info" in names
    assert "list_org_members" not in names
    message = _text(await server.call_tool("list_org_members", {"org": "acme"}))
    assert message.startswith("Denied:")
    assert "denied_tools" in message
    assert github_mock.calls.call_count == 0


@pytest.mark.asyncio
async def test_audit_log_records_allowed_and_denied_calls(
    token: None,
    clean_client: None,
    github_mock: respx.MockRouter,
    allow_acme: policy.Policy,
) -> None:
    github_mock.get("https://api.github.com/orgs/acme").mock(
        return_value=httpx.Response(200, json={"login": "acme", "name": "Acme Inc"})
    )
    await server.call_tool("get_org_info", {"org": "acme"})
    await server.call_tool("get_org_info", {"org": "denied-org"})

    events = _read_audit(allow_acme.audit_log)
    assert len(events) == 2

    allowed, denied = events
    assert allowed["tool"] == "get_org_info"
    assert allowed["arguments"]["org"] == "acme"
    assert allowed["decision"] == "allowed"
    assert allowed["reason"]
    assert allowed["outcome"] == "ok"
    assert allowed["http_status"] == 200
    assert isinstance(allowed["duration_ms"], int)
    assert allowed["duration_ms"] >= 0
    assert allowed["timestamp"].endswith("Z")
    assert "T" in allowed["timestamp"]
    assert "GITHUB_TOKEN" not in json.dumps(allowed)
    assert "test-token" not in json.dumps(allowed)

    assert denied["decision"] == "denied"
    assert denied["arguments"]["org"] == "denied-org"
    assert "not on the allowed_orgs list" in denied["reason"]
    assert denied["outcome"] == "error"
    assert denied["http_status"] is None


@pytest.mark.asyncio
async def test_audit_log_redacts_secrets_and_tokens(
    token: None,
    clean_client: None,
    github_mock: respx.MockRouter,
    allow_acme: policy.Policy,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_liveTokenValue999")
    github_mock.get("https://api.github.com/orgs/acme").mock(
        return_value=httpx.Response(200, json={"login": "acme"})
    )
    await server.call_tool(
        "get_org_info",
        {
            "org": "acme",
            "token": "should-not-appear",
            "password": "hunter2",
            "api_key": "abcd",
            "note": "used ghp_otherToken111 and ghp_liveTokenValue999",
        },
    )
    event = _read_audit(allow_acme.audit_log)[0]
    args = event["arguments"]
    assert args["org"] == "acme"
    assert args["token"] == policy.REDACTED
    assert args["password"] == policy.REDACTED
    assert args["api_key"] == policy.REDACTED
    assert "should-not-appear" not in json.dumps(event)
    assert "hunter2" not in json.dumps(event)
    assert "ghp_otherToken111" not in json.dumps(event)
    assert "ghp_liveTokenValue999" not in json.dumps(event)
    assert policy.REDACTED in args["note"]
    # The surrounding sentence should still be present.
    assert args["note"].startswith("used ")


def test_load_policy_yaml_and_json_and_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(
        "\n".join(
            [
                "allowed_orgs:",
                "  - FromFile",
                "allow_all_orgs: false",
                "read_only: true",
                "allowed_tools:",
                "  - get_org_info",
                "denied_tools:",
                "  - list_org_teams",
                "audit_log: /tmp/from-file.log",
                "",
            ]
        ),
        encoding="utf-8",
    )
    loaded = policy.load_policy(str(yaml_path))
    assert loaded.allowed_orgs == frozenset({"fromfile"})
    assert loaded.allow_all_orgs is False
    assert loaded.read_only is True
    assert loaded.allowed_tools == frozenset({"get_org_info"})
    assert loaded.denied_tools == frozenset({"list_org_teams"})
    assert loaded.audit_log == "/tmp/from-file.log"

    json_path = tmp_path / "policy.json"
    json_path.write_text(
        json.dumps(
            {
                "allowed_orgs": ["JsonOrg"],
                "allow_all_orgs": True,
                "read_only": False,
                "audit_log": "/tmp/from-json.log",
            }
        ),
        encoding="utf-8",
    )
    loaded_json = policy.load_policy(str(json_path))
    assert loaded_json.allowed_orgs == frozenset({"jsonorg"})
    assert loaded_json.allow_all_orgs is True
    assert loaded_json.read_only is False

    monkeypatch.setenv(policy.ALLOWED_ORGS_ENV, "EnvOrg, OtherOrg")
    monkeypatch.setenv(policy.ALLOW_ALL_ORGS_ENV, "true")
    monkeypatch.setenv(policy.READ_ONLY_ENV, "false")
    monkeypatch.setenv(policy.ALLOWED_TOOLS_ENV, "get_org_info,list_org_members")
    monkeypatch.setenv(policy.DENIED_TOOLS_ENV, "list_team_members")
    monkeypatch.setenv(policy.AUDIT_LOG_ENV, str(tmp_path / "env-audit.log"))
    overridden = policy.load_policy(str(yaml_path))
    assert overridden.allowed_orgs == frozenset({"envorg", "otherorg"})
    assert overridden.allow_all_orgs is True
    assert overridden.read_only is False
    assert overridden.allowed_tools == frozenset({"get_org_info", "list_org_members"})
    assert overridden.denied_tools == frozenset({"list_team_members"})
    assert overridden.audit_log == str(tmp_path / "env-audit.log")


def test_audit_write_failure_does_not_crash_and_goes_to_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    event = {"tool": "get_org_info", "decision": "allowed"}
    missing_dir = tmp_path / "no-permission"
    missing_dir.mkdir()
    target = missing_dir / "audit.log"

    def boom(*_args: object, **_kwargs: object) -> object:
        raise PermissionError("nope")

    monkeypatch.setattr("builtins.open", boom)
    policy.write_audit_event(event, path=str(target))
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Failed to write audit log" in captured.err
    assert "nope" in captured.err
    assert "get_org_info" in captured.err
