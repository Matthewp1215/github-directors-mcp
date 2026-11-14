"""Context-firewall policy loading, access checks, redaction, and audit logging."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

DEFAULT_POLICY_PATH = "policy.yaml"
DEFAULT_AUDIT_LOG = "audit.log"

POLICY_PATH_ENV = "DIRECTORS_MCP_POLICY"
AUDIT_LOG_ENV = "DIRECTORS_MCP_AUDIT_LOG"
ALLOWED_ORGS_ENV = "DIRECTORS_MCP_ALLOWED_ORGS"
ALLOW_ALL_ORGS_ENV = "DIRECTORS_MCP_ALLOW_ALL_ORGS"
READ_ONLY_ENV = "DIRECTORS_MCP_READ_ONLY"
ALLOWED_TOOLS_ENV = "DIRECTORS_MCP_ALLOWED_TOOLS"
DENIED_TOOLS_ENV = "DIRECTORS_MCP_DENIED_TOOLS"

REDACTED = "***REDACTED***"
_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|credential|private[_-]?key)",
    re.IGNORECASE,
)
_GITHUB_TOKEN_RE = re.compile(r"\b(ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]+")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class PolicyDenied(Exception):
    """Raised when a tool call is blocked by the context-firewall policy."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class Policy:
    """Fail-closed director policy. An empty allowlist denies every org unless allow_all_orgs is set."""

    allowed_orgs: frozenset[str] = field(default_factory=frozenset)
    allow_all_orgs: bool = False
    read_only: bool = True
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] = field(default_factory=frozenset)
    audit_log: str = DEFAULT_AUDIT_LOG


_policy: Policy | None = None


def get_policy() -> Policy:
    """Return the active policy, loading from the configured file/env on first use."""
    global _policy
    if _policy is None:
        _policy = load_policy()
    return _policy


def set_policy(policy: Policy) -> None:
    """Replace the active policy (used by tests and startup)."""
    global _policy
    _policy = policy


def reset_policy() -> None:
    """Clear the cached policy so the next get_policy() reloads."""
    global _policy
    _policy = None


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"Invalid boolean policy value: {value!r}")


def _as_name_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        raise ValueError(f"Expected a list of names, got {type(value).__name__}")
    return [str(part).strip() for part in parts if str(part).strip()]


def _optional_name_set(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return frozenset()
    return frozenset(name.lower() for name in _as_name_list(value))


def _org_set(value: Any) -> frozenset[str]:
    return frozenset(name.lower() for name in _as_name_list(value))


def load_policy_file(path: str) -> dict[str, Any]:
    """Load a YAML or JSON policy file. An empty file yields an empty dict."""
    text = _read_text(path)
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".json":
        data = json.loads(text) if text.strip() else {}
    elif suffix in {".yaml", ".yml"}:
        data = _load_yaml(text)
    else:
        try:
            data = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            data = _load_yaml(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Policy file {path} must contain a mapping/object at the top level.")
    return data


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _load_yaml(text: str) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - documented runtime dependency
        raise RuntimeError("PyYAML is required to load YAML policy files. Install pyyaml.") from exc
    return yaml.safe_load(text)


def load_policy(path: str | None = None) -> Policy:
    """Load policy from a YAML/JSON file, then apply environment-variable overrides."""
    explicit = path is not None or os.getenv(POLICY_PATH_ENV)
    policy_path = path or os.getenv(POLICY_PATH_ENV) or DEFAULT_POLICY_PATH
    data: dict[str, Any] = {}
    if os.path.isfile(policy_path):
        data = load_policy_file(policy_path)
    elif explicit and not os.path.isfile(policy_path):
        print(
            f"Policy file not found: {policy_path}. Using fail-closed defaults.",
            file=sys.stderr,
        )

    allowed_orgs = _org_set(data.get("allowed_orgs"))
    if os.getenv(ALLOWED_ORGS_ENV) is not None:
        allowed_orgs = _org_set(os.getenv(ALLOWED_ORGS_ENV))

    allow_all_orgs = _as_bool(data.get("allow_all_orgs"), False)
    if os.getenv(ALLOW_ALL_ORGS_ENV) is not None:
        allow_all_orgs = _as_bool(os.getenv(ALLOW_ALL_ORGS_ENV), False)

    read_only = _as_bool(data.get("read_only"), True)
    if os.getenv(READ_ONLY_ENV) is not None:
        read_only = _as_bool(os.getenv(READ_ONLY_ENV), True)

    allowed_tools = _optional_name_set(data.get("allowed_tools"))
    if os.getenv(ALLOWED_TOOLS_ENV) is not None:
        allowed_tools = _optional_name_set(os.getenv(ALLOWED_TOOLS_ENV))

    denied_tools = _optional_name_set(data.get("denied_tools")) or frozenset()
    if os.getenv(DENIED_TOOLS_ENV) is not None:
        denied_tools = _optional_name_set(os.getenv(DENIED_TOOLS_ENV)) or frozenset()

    audit_log = str(data.get("audit_log") or DEFAULT_AUDIT_LOG)
    if os.getenv(AUDIT_LOG_ENV):
        audit_log = os.getenv(AUDIT_LOG_ENV) or audit_log

    return Policy(
        allowed_orgs=allowed_orgs,
        allow_all_orgs=allow_all_orgs,
        read_only=read_only,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        audit_log=audit_log,
    )


def evaluate_access(
    policy: Policy,
    tool_name: str,
    arguments: dict[str, Any] | None,
    tool_access: str | None,
) -> AccessDecision:
    """Decide whether a tool call is allowed. Does not perform any GitHub request."""
    arguments = arguments or {}
    lowered = tool_name.lower()

    if lowered in policy.denied_tools:
        return AccessDecision(False, f"tool '{tool_name}' is on the denied_tools list.")

    if policy.allowed_tools is not None and lowered not in policy.allowed_tools:
        return AccessDecision(False, f"tool '{tool_name}' is not on the allowed_tools list.")

    if policy.read_only and tool_access == "write":
        return AccessDecision(
            False,
            f"tool '{tool_name}' is a write tool and the server is in read-only mode.",
        )

    org = arguments.get("org") if isinstance(arguments, dict) else None
    if org:
        if policy.allow_all_orgs:
            return AccessDecision(True, "allowed")
        if not policy.allowed_orgs:
            return AccessDecision(
                False,
                "no organizations are allowed. Configure allowed_orgs in the policy file "
                "or set allow_all_orgs: true.",
            )
        if str(org).lower() not in policy.allowed_orgs:
            return AccessDecision(
                False, f"organization '{org}' is not on the allowed_orgs list."
            )

    return AccessDecision(True, "allowed")


def tool_is_visible(policy: Policy, tool_name: str, tool_access: str) -> bool:
    """Return whether a tool should appear in list_tools for the given policy."""
    return evaluate_access(policy, tool_name, arguments={}, tool_access=tool_access).allowed


def _scrub_string(value: str, token: str | None) -> str:
    if token and token in value:
        value = value.replace(token, REDACTED)
    if value.lower().startswith("bearer ") and value[7:]:
        return "Bearer " + REDACTED
    return _GITHUB_TOKEN_RE.sub(REDACTED, value)


def redact_value(value: Any, token: str | None = None) -> Any:
    """Recursively redact secrets and GitHub tokens from a JSON-like value."""
    if isinstance(value, dict):
        return redact_arguments(value, token)
    if isinstance(value, list):
        return [redact_value(item, token) for item in value]
    if isinstance(value, str):
        return _scrub_string(value, token)
    return value


def redact_arguments(arguments: dict[str, Any] | None, token: str | None = None) -> dict[str, Any]:
    """Return a copy of tool arguments with secret keys and token values redacted."""
    redacted: dict[str, Any] = {}
    for key, value in (arguments or {}).items():
        if _SECRET_KEY_RE.search(str(key)):
            redacted[key] = REDACTED
        else:
            redacted[key] = redact_value(value, token)
    return redacted


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_audit_event(event: dict[str, Any], path: str | None = None) -> None:
    """Append one JSON Lines audit event. Failures are reported on stderr, never stdout."""
    log_path = path or get_policy().audit_log
    try:
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=True)
    except Exception as exc:  # noqa: BLE001 - must never break the tool call
        print(f"Failed to serialize audit log event: {exc}", file=sys.stderr)
        return
    try:
        parent = os.path.dirname(os.path.abspath(log_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception as exc:  # noqa: BLE001 - must never break the tool call
        print(f"Failed to write audit log {log_path}: {exc}", file=sys.stderr)
        print(line, file=sys.stderr)


def audit_tool_call(
    *,
    tool: str,
    arguments: dict[str, Any] | None,
    decision: str,
    reason: str,
    outcome: str,
    http_status: int | None,
    duration_ms: int,
    path: str | None = None,
) -> None:
    """Write a standard audit record, redacting secrets and never including GITHUB_TOKEN."""
    token = os.getenv("GITHUB_TOKEN")
    event = {
        "timestamp": utc_timestamp(),
        "tool": tool,
        "arguments": redact_arguments(arguments, token),
        "decision": decision,
        "reason": reason,
        "outcome": outcome,
        "http_status": http_status,
        "duration_ms": duration_ms,
    }
    write_audit_event(event, path=path)
