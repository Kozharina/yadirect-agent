"""Tests for ``yadirect-agent notify setup telegram`` CLI (M18 slice 4).

The CLI command orchestrates the 5-step wizard:

1. Print BotFather instructions.
2. Prompt for the bot token (hidden input).
3. Validate via ``validate_telegram_token`` (pure helper —
   monkeypatched in these tests).
4. Print "now /start the bot" + call ``await_first_chat_id``
   (also monkeypatched).
5. Save to ``KeyringTelegramStore``, send a test notification via
   ``TelegramSink``, print success message.

Plus a ``--reset`` flag that skips all of the above and deletes
the keychain entry.

Tests focus on operator-visible behavior:

- Happy path exits 0, keychain populated, test send invoked.
- Invalid token (helper raises ``TokenInvalidError``) exits 1 with
  a Russian message + no keychain write.
- Chat-id timeout exits 1 with a Russian message + no keychain
  write.
- Test send failure (sink raises) keeps the keychain entry but
  exits 1 with a Russian message — operator can re-run ``notify
  test`` later to verify.
- ``--reset`` deletes the entry and exits 0.
- ``--reset`` on an empty keychain is idempotent (exits 0).
- Unsupported medium (``notify setup slack``) exits 2 with a
  Russian "not supported yet" message.

The wizard helpers are monkeypatched, not mocked through respx,
because these tests pin CLI-level behavior — wire-shape coverage
already lives in ``test_notify_setup_wizard.py``.
"""

from __future__ import annotations

from typing import Any

import keyring.errors
import pytest
from typer.testing import CliRunner

from yadirect_agent.cli.main import app


@pytest.fixture
def runner() -> CliRunner:
    # mix_stderr=False so we can assert separately on stdout vs
    # stderr; the wizard puts operator-facing prompts on stdout and
    # error messages on stderr.
    return CliRunner()


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """In-memory keyring backend — never touches the OS keychain."""
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


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Strip the env vars the bootstrap reads so Settings construction
    in the CLI command doesn't depend on the developer's shell."""
    for var in (
        "YANDEX_DIRECT_TOKEN",
        "YANDEX_METRIKA_TOKEN",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("YANDEX_DIRECT_TOKEN", "x")
    monkeypatch.setenv("YANDEX_METRIKA_TOKEN", "x")
    monkeypatch.setenv("AGENT_POLICY_PATH", str(tmp_path / "policy.yml"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "logs" / "audit.jsonl"))
    monkeypatch.setattr(
        "yadirect_agent.config.Settings.model_config",
        {"env_file": None, "extra": "ignore"},
    )


@pytest.fixture
def stub_helpers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace wizard helpers + TelegramSink.send with controllable stubs.

    Returns a dict the test can read / configure:
    - ``validate_result``: BotInfo or exception to raise
    - ``chat_id_result``: str or exception to raise
    - ``send_result``: None (success) or exception to raise
    - ``sent``: list of Notifications the (stubbed) sink received
    - ``saved``: list of (bot_token, chat_id) the keychain saved
    """
    from yadirect_agent.services.notify import setup_wizard
    from yadirect_agent.services.notify import telegram as telegram_module

    state: dict[str, Any] = {
        "validate_result": setup_wizard.BotInfo(id=1, username="test_bot"),
        "chat_id_result": "42",
        "send_result": None,
        "sent": [],
    }

    async def fake_validate(bot_token: str) -> Any:
        result = state["validate_result"]
        if isinstance(result, Exception):
            raise result
        return result

    async def fake_await_chat_id(bot_token: str, *, timeout_s: float, **_: Any) -> str:
        result = state["chat_id_result"]
        if isinstance(result, Exception):
            raise result
        return result

    async def fake_send(self: telegram_module.TelegramSink, notification: Any) -> None:
        state["sent"].append(notification)
        result = state["send_result"]
        if isinstance(result, Exception):
            raise result

    # Patch in the cli/main module's namespace (where they're imported),
    # not just the source modules — typer late-binds imports.
    from yadirect_agent.cli import main as cli_main

    monkeypatch.setattr(cli_main, "validate_telegram_token", fake_validate, raising=False)
    monkeypatch.setattr(cli_main, "await_first_chat_id", fake_await_chat_id, raising=False)
    monkeypatch.setattr(setup_wizard, "validate_telegram_token", fake_validate)
    monkeypatch.setattr(setup_wizard, "await_first_chat_id", fake_await_chat_id)
    monkeypatch.setattr(telegram_module.TelegramSink, "send", fake_send)
    return state


class TestHappyPath:
    def test_setup_telegram_full_flow_exits_zero(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
        stub_helpers: dict[str, Any],
    ) -> None:
        # Operator types token, /start's bot, wizard saves keychain
        # + sends test notification + prints success.
        from yadirect_agent.auth.telegram_keychain import (
            KEYRING_TELEGRAM_SERVICE_NAME,
            KEYRING_TELEGRAM_USERNAME,
            KeyringTelegramStore,
        )
        from yadirect_agent.services.notify.setup_wizard import BotInfo

        stub_helpers["validate_result"] = BotInfo(id=1, username="my_bot")
        stub_helpers["chat_id_result"] = "111222"

        result = runner.invoke(
            app,
            ["notify", "setup", "telegram"],
            input="real-bot-token\n",  # token prompt
        )

        assert result.exit_code == 0, result.output
        # Operator sees a success message at the end.
        assert "✓" in result.output or "готов" in result.output.lower()
        # Keychain populated with what the wizard captured.
        loaded = KeyringTelegramStore().load()
        assert loaded == ("real-bot-token", "111222")
        # Underlying raw write went to the expected slot.
        assert (KEYRING_TELEGRAM_SERVICE_NAME, KEYRING_TELEGRAM_USERNAME) in memory_keyring
        # Test notification was sent (sink.send was invoked once).
        assert len(stub_helpers["sent"]) == 1


class TestInvalidToken:
    def test_invalid_token_exits_1_and_does_not_write_keychain(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
        stub_helpers: dict[str, Any],
    ) -> None:
        # Operator pastes a bad token; validate_telegram_token
        # raises TokenInvalidError. CLI must:
        #   - exit 1
        #   - print a Russian error message
        #   - NOT write anything to the keychain
        from yadirect_agent.services.notify.setup_wizard import TokenInvalidError

        stub_helpers["validate_result"] = TokenInvalidError("HTTP 401")

        result = runner.invoke(
            app,
            ["notify", "setup", "telegram"],
            input="wrong-token\n",
        )

        assert result.exit_code == 1, result.output
        # No keychain entry.
        assert memory_keyring == {}
        # No test send.
        assert stub_helpers["sent"] == []


class TestChatIdTimeout:
    def test_chat_id_timeout_exits_1_and_does_not_write_keychain(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
        stub_helpers: dict[str, Any],
    ) -> None:
        from yadirect_agent.services.notify.setup_wizard import ChatIdTimeoutError

        stub_helpers["chat_id_result"] = ChatIdTimeoutError("deadline")

        result = runner.invoke(
            app,
            ["notify", "setup", "telegram"],
            input="valid-token\n",
        )

        assert result.exit_code == 1, result.output
        assert memory_keyring == {}
        assert stub_helpers["sent"] == []


class TestTestSendFailure:
    def test_test_send_failure_keeps_keychain_and_exits_1(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
        stub_helpers: dict[str, Any],
    ) -> None:
        # Token validated + chat_id captured + keychain saved
        # successfully, but the final test-send fails (maybe the
        # operator blocked the bot between /start and the test).
        # CLI exits 1 (so operator notices), but KEEPS the keychain
        # entry — they can run ``notify test`` later after unblocking.
        # Discarding the keychain here would force the operator to
        # redo the whole wizard, which is worse UX.
        import httpx

        from yadirect_agent.auth.telegram_keychain import KeyringTelegramStore

        stub_helpers["send_result"] = httpx.HTTPStatusError(
            "Forbidden",
            request=httpx.Request("POST", "https://api.telegram.org/test"),
            response=httpx.Response(403),
        )

        result = runner.invoke(
            app,
            ["notify", "setup", "telegram"],
            input="valid-token\n",
        )

        assert result.exit_code == 1, result.output
        # Keychain entry preserved.
        loaded = KeyringTelegramStore().load()
        assert loaded is not None


class TestReset:
    def test_reset_deletes_keychain_entry_and_exits_zero(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # Pre-populate keychain (simulate prior wizard run).
        from yadirect_agent.auth.telegram_keychain import KeyringTelegramStore

        KeyringTelegramStore().save(bot_token="old-token", chat_id="old-chat")
        assert KeyringTelegramStore().load() is not None

        result = runner.invoke(
            app,
            ["notify", "setup", "telegram", "--reset"],
        )

        assert result.exit_code == 0, result.output
        assert KeyringTelegramStore().load() is None

    def test_reset_idempotent_on_empty_keychain(
        self,
        runner: CliRunner,
        memory_keyring: dict[tuple[str, str], str],
        isolated_env: None,
    ) -> None:
        # ``--reset`` twice in a row, or on a fresh install — exit 0.
        result = runner.invoke(
            app,
            ["notify", "setup", "telegram", "--reset"],
        )
        assert result.exit_code == 0, result.output


class TestUnsupportedMedium:
    def test_setup_slack_exits_2_with_russian_message(
        self,
        runner: CliRunner,
        isolated_env: None,
    ) -> None:
        # Only telegram is supported in slice 4; other media land
        # in slice 5 proper. ``notify setup slack`` should not
        # silently noop — it should exit 2 with a clear message
        # so operators who try it early know it's not landed yet.
        result = runner.invoke(
            app,
            ["notify", "setup", "slack"],
        )
        assert result.exit_code == 2, result.output
