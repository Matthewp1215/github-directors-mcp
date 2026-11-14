"""Shared fixtures. Existing API tests run under an open policy so they stay focused on GitHub behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import respx

import policy
import server


POLICY_ENV_VARS = (
    policy.POLICY_PATH_ENV,
    policy.AUDIT_LOG_ENV,
    policy.ALLOWED_ORGS_ENV,
    policy.ALLOW_ALL_ORGS_ENV,
    policy.READ_ONLY_ENV,
    policy.ALLOWED_TOOLS_ENV,
    policy.DENIED_TOOLS_ENV,
)


@pytest.fixture(autouse=True)
def open_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-open for org checks during default tests; audit logs go to a temp file."""
    for name in POLICY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    server.set_policy(
        policy.Policy(
            allow_all_orgs=True,
            read_only=True,
            audit_log=str(tmp_path / "audit.log"),
        )
    )
    yield
    server.reset_policy()


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
