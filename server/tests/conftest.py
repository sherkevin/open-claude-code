"""Shared test isolation for host provider authorization.

The shell that starts the test suite may already export ANTHROPIC_* variables
(for example because it is itself running a coding agent).  ``Settings.provider_env``
intentionally lets the process environment override the provider settings file,
so those ambient values make tests that expect an unconfigured provider observe
``credentials_present`` instead of ``credentials_missing``.

Tests that need a provider must declare it explicitly, either through a provider
settings file or through ``monkeypatch.setenv``; this fixture only removes the
host's ambient authorization values so the suite is deterministic regardless of
the caller's environment.  Values are restored when the session ends.
"""

import os

import pytest

HOST_PROVIDER_AUTH_KEYS = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
)


@pytest.fixture(scope="session", autouse=True)
def isolate_host_provider_authorization():
    previous = {key: os.environ.pop(key) for key in HOST_PROVIDER_AUTH_KEYS if key in os.environ}
    yield
    os.environ.update(previous)


@pytest.fixture(scope="session", autouse=True)
def enable_synthetic_provider_mode_for_tests():
    """Allow reserved fake gateways without weakening production defaults."""
    previous = os.environ.get("AGENT_PROVIDER_TEST_MODE")
    os.environ["AGENT_PROVIDER_TEST_MODE"] = "true"
    yield
    if previous is None:
        os.environ.pop("AGENT_PROVIDER_TEST_MODE", None)
    else:
        os.environ["AGENT_PROVIDER_TEST_MODE"] = previous
