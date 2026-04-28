"""Tests for the ``yadirect-agent auth ...`` CLI subapp (M15.3).

Three commands operators run by hand:

- ``auth login``  — invoke the M15.3 OAuth flow, persist token to
  keychain, print a confirmation.
- ``auth status`` — read the persisted token, print masked summary
  (or JSON), exit 1 when nothing is stored.
- ``auth logout`` — clear the keychain slot, exit 0 idempotently.

CLI tests deliberately stay at the boundary: ``perform_login`` is
monkey-patched into a fake that returns a known TokenSet, the
keychain is replaced with an in-memory dict, and assertions are
about exit codes + visible output. Deeper behaviour belongs in the
layer-specific tests this file mirrors.

Exit-code conventions pinned here so a future cron / wrapper script
can rely on them:

- 0 — success (login completed, status found, revoke completed).
- 1 — "not logged in" on ``auth status`` (cron-friendly: alert
  on this code).
- 2 — login failure (callback timeout, user denied, exchange
  rejected). The CLI surfaces the cause; the operator re-runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import keyring.errors
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from yadirect_agent.auth.callback_server import OAuthCallbackError
from yadirect_agent.auth.keychain import (
    KEYRING_SERVICE_NAME,
    KEYRING_USERNAME,
    KeyringTokenStore,
)
from yadirect_agent.cli.main import app
from yadirect_agent.exceptions import AuthError
from yadirect_agent.models.auth import TokenSet


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    storage: dict[tuple[str, str], str] = {}

    def set_password(service: str, username: str, password: str) -> None:
        storage[(service, username)] = password

    def get_password(service: str, username: str) -> str | None:
        return storage.get((service, username))

    def delete_password(service: str, username: str) -> None:
        key = (service, username)
        if key not in storage:
            raise keyring.errors.PasswordDeleteError(f"no password for {key}")
        del storage[key]

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)
    return storage


def _tokenset() -> TokenSet:
    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    return TokenSet(
        access_token=SecretStr("AQAA-secret-access-value"),
        refresh_token=SecretStr("1.AQAA-secret-refresh"),
        token_type="bearer",
        scope=("direct:api", "metrika:read", "metrika:write"),
        obtained_at=now,
        expires_at=now + timedelta(days=365),
    )


class TestAuthLogin:
    def test_login_success_prints_summary_and_exits_zero(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        token = _tokenset()

        async def fake_perform_login(**_: object) -> TokenSet:
            # Simulate the orchestrator persisting + returning.
            KeyringTokenStore().save(token)
            return token

        monkeypatch.setattr("yadirect_agent.cli.main.perform_login", fake_perform_login)

        result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 0, result.output
        # Operator-visible confirmation. The exact words can drift,
        # but a regression that prints nothing (silent success) is
        # what we want to catch.
        assert "успешно" in result.stdout.lower() or "logged in" in result.stdout.lower()
        # Secret values must NEVER reach stdout.
        assert "AQAA-secret-access-value" not in result.stdout
        assert "AQAA-secret-refresh" not in result.stdout

    def test_login_callback_error_exits_two(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_perform_login(**_: object) -> TokenSet:
            raise OAuthCallbackError("Yandex OAuth error: access_denied")

        monkeypatch.setattr("yadirect_agent.cli.main.perform_login", fake_perform_login)

        result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 2, result.output
        # Operator must see the CAUSE — not just "error".
        # ``result.output`` captures both stdout + stderr; error
        # messages go to stderr per UNIX convention but are part of
        # the operator-visible output.
        assert "access_denied" in result.output

    def test_login_timeout_exits_two_with_helpful_hint(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_perform_login(**_: object) -> TokenSet:
            raise TimeoutError("no callback within 300s")

        monkeypatch.setattr("yadirect_agent.cli.main.perform_login", fake_perform_login)

        result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 2, result.output
        assert "timeout" in result.output.lower() or "истёк" in result.output.lower()

    def test_login_auth_error_exits_two(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def fake_perform_login(**_: object) -> TokenSet:
            raise AuthError("OAuth error: invalid_grant — expired code")

        monkeypatch.setattr("yadirect_agent.cli.main.perform_login", fake_perform_login)

        result = runner.invoke(app, ["auth", "login"])

        assert result.exit_code == 2, result.output
        assert "invalid_grant" in result.output


class TestAuthStatus:
    def test_status_when_not_logged_in_exits_one(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # Cron-friendly: exit code 1 lets a wrapper script fire an
        # alert ("agent has been logged out — re-auth").
        result = runner.invoke(app, ["auth", "status"])

        assert result.exit_code == 1, result.output
        assert "не вошли" in result.output.lower() or "not logged in" in result.output.lower()

    def test_status_when_logged_in_prints_masked_summary(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        KeyringTokenStore().save(_tokenset())

        result = runner.invoke(app, ["auth", "status"])

        assert result.exit_code == 0, result.output
        # Scope present so operator can verify granted permissions.
        assert "direct:api" in result.stdout
        assert "metrika:read" in result.stdout
        # Expiry present so operator can plan ahead.
        assert "2027" in result.stdout  # 2026-04-28 + 365 days = 2027-04-28
        # Secret values must NEVER reach stdout.
        assert "AQAA-secret-access-value" not in result.stdout
        assert "AQAA-secret-refresh" not in result.stdout

    def test_status_json_emits_structured_output(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        import json

        KeyringTokenStore().save(_tokenset())

        result = runner.invoke(app, ["auth", "status", "--json"])

        assert result.exit_code == 0, result.output
        # Last line should be valid JSON; locator avoids any preamble.
        data = json.loads(result.stdout.strip().splitlines()[-1])
        assert data["scope"] == ["direct:api", "metrika:read", "metrika:write"]
        assert data["token_type"] == "bearer"
        assert "expires_at" in data
        # Even JSON output must mask secrets.
        assert "AQAA-secret-access-value" not in result.stdout


class TestAuthLogout:
    def test_logout_clears_record(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        KeyringTokenStore().save(_tokenset())
        assert (KEYRING_SERVICE_NAME, KEYRING_USERNAME) in memory_keyring

        result = runner.invoke(app, ["auth", "logout"])

        assert result.exit_code == 0, result.output
        assert (KEYRING_SERVICE_NAME, KEYRING_USERNAME) not in memory_keyring

    def test_logout_warns_about_server_side_token_persistence(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # The keychain slot is gone but Yandex still considers the
        # refresh token valid — operators MUST see this in the
        # logout message so they don't think they've fully signed
        # out. (Reviewer point: name was misleading; here we pin
        # that the new copy actually says so.)
        KeyringTokenStore().save(_tokenset())

        result = runner.invoke(app, ["auth", "logout"])

        assert result.exit_code == 0, result.output
        assert "yandex.ru/profile/access" in result.output

    def test_logout_when_not_logged_in_is_noop_and_exits_zero(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
    ) -> None:
        # Idempotent: running logout twice in a row, or on a fresh
        # install, must not raise. cron / setup scripts depend on
        # this being a no-op exit-zero.
        result = runner.invoke(app, ["auth", "logout"])

        assert result.exit_code == 0, result.output
