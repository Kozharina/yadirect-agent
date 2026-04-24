"""Tests for the `yadirect-agent doctor` command.

`doctor` runs four independent checks — env / Anthropic ping / Direct
sandbox ping / policy-file presence — and reports a status per check.
Exit code is 0 when everything is ok or warn, 2 if any check fails.

TDD trail: the first test in this file (skeleton exists + green path)
was a RED against missing command; subsequent tests drive the addition
of each check one at a time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

import yadirect_agent.cli.main as cli_module


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def _patch_bootstrap(monkeypatch: pytest.MonkeyPatch, settings: Any) -> None:
    monkeypatch.setattr("yadirect_agent.cli.main.get_settings", lambda: settings)
    monkeypatch.setattr("yadirect_agent.cli.main.configure_logging", lambda _s: None)


# --------------------------------------------------------------------------
# Pair 1 — skeleton: command exists and reports a green outcome when all
# checks pass.
# --------------------------------------------------------------------------


def test_doctor_command_exists_and_is_discoverable(runner: CliRunner) -> None:
    # Smoke: `doctor --help` must not fail and must mention the command.
    result = runner.invoke(cli_module.app, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout.lower()


def test_doctor_exits_zero_when_every_check_is_ok(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    _patch_bootstrap: None,
) -> None:
    # Replace the checks pipeline with a single all-ok fake so we exercise
    # the command's orchestration without depending on individual check
    # implementations.
    from yadirect_agent.cli.doctor import CheckResult

    async def fake_run_checks(_settings: Any) -> list[CheckResult]:
        return [
            CheckResult(name="env", status="ok", detail="all tokens present"),
        ]

    monkeypatch.setattr(cli_module, "_run_doctor_checks", fake_run_checks)

    result = runner.invoke(cli_module.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "ok" in result.output.lower()
    assert "env" in result.output


def test_doctor_exits_nonzero_when_any_check_fails(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    _patch_bootstrap: None,
) -> None:
    from yadirect_agent.cli.doctor import CheckResult

    async def fake_run_checks(_settings: Any) -> list[CheckResult]:
        return [
            CheckResult(name="env", status="ok", detail="ok"),
            CheckResult(name="direct", status="fail", detail="auth error"),
        ]

    monkeypatch.setattr(cli_module, "_run_doctor_checks", fake_run_checks)

    result = runner.invoke(cli_module.app, ["doctor"])

    assert result.exit_code == 2, result.output
    assert "fail" in result.output.lower()


def test_doctor_warn_does_not_set_failure_exit(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    _patch_bootstrap: None,
) -> None:
    # A warning (e.g. policy file missing) is not a fail — we still want
    # the command to exit cleanly so it composes in cron-style pipelines.
    from yadirect_agent.cli.doctor import CheckResult

    async def fake_run_checks(_settings: Any) -> list[CheckResult]:
        return [
            CheckResult(name="env", status="ok", detail="ok"),
            CheckResult(name="policy", status="warn", detail="file missing"),
        ]

    monkeypatch.setattr(cli_module, "_run_doctor_checks", fake_run_checks)

    result = runner.invoke(cli_module.app, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "warn" in result.output.lower()


# --------------------------------------------------------------------------
# Pair 2 — individual checks: env + policy_file.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_check_ok_when_all_tokens_present(settings: Any) -> None:
    from yadirect_agent.cli.doctor import check_env

    result = await check_env(settings)

    assert result.status == "ok"
    assert result.name == "env"


@pytest.mark.asyncio
async def test_env_check_fails_when_direct_token_is_empty(
    settings: Any,
) -> None:
    from pydantic import SecretStr

    from yadirect_agent.cli.doctor import check_env

    settings.yandex_direct_token = SecretStr("")

    result = await check_env(settings)

    assert result.status == "fail"
    assert "direct" in result.detail.lower() or "token" in result.detail.lower()


@pytest.mark.asyncio
async def test_env_check_fails_when_anthropic_key_is_empty(
    settings: Any,
) -> None:
    from pydantic import SecretStr

    from yadirect_agent.cli.doctor import check_env

    settings.anthropic_api_key = SecretStr("")

    result = await check_env(settings)

    assert result.status == "fail"
    assert "anthropic" in result.detail.lower() or "api" in result.detail.lower()


def test_policy_file_check_warns_when_file_missing(settings: Any, tmp_path: Path) -> None:
    from yadirect_agent.cli.doctor import check_policy_file

    settings.agent_policy_path = tmp_path / "does-not-exist.yml"

    result = check_policy_file(settings)

    assert result.status == "warn"
    assert "missing" in result.detail.lower() or "not" in result.detail.lower()


def test_policy_file_check_ok_when_file_exists(settings: Any, tmp_path: Path) -> None:
    from yadirect_agent.cli.doctor import check_policy_file

    path = tmp_path / "agent_policy.yml"
    path.write_text("rollout_stage: 0\n", encoding="utf-8")
    settings.agent_policy_path = path

    result = check_policy_file(settings)

    assert result.status == "ok"


# --------------------------------------------------------------------------
# Pair 3 — ping checks against Anthropic and Direct sandbox.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_check_ok_when_client_returns_a_message(
    settings: Any,
) -> None:
    from yadirect_agent.cli.doctor import check_anthropic

    class _FakeMessage:
        id = "msg_1"
        content: ClassVar[list[Any]] = []
        stop_reason = "end_turn"

    class _FakeMessages:
        async def create(self, **_kwargs: Any) -> _FakeMessage:
            return _FakeMessage()

    class _FakeAnthropic:
        messages = _FakeMessages()

    result = await check_anthropic(settings, client=_FakeAnthropic())

    assert result.status == "ok"
    assert result.name == "anthropic"


@pytest.mark.asyncio
async def test_anthropic_check_fails_when_client_raises(
    settings: Any,
) -> None:
    from yadirect_agent.cli.doctor import check_anthropic

    class _FakeMessages:
        async def create(self, **_kwargs: Any) -> Any:
            msg = "invalid x-api-key"
            raise RuntimeError(msg)

    class _FakeAnthropic:
        messages = _FakeMessages()

    result = await check_anthropic(settings, client=_FakeAnthropic())

    assert result.status == "fail"
    assert "invalid" in result.detail.lower() or "key" in result.detail.lower()


@pytest.mark.asyncio
async def test_direct_check_ok_when_sandbox_returns_campaigns(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yadirect_agent.cli.doctor import check_direct_sandbox
    from yadirect_agent.clients.direct import DirectService

    class _FakeDirect:
        async def __aenter__(self) -> _FakeDirect:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def get_campaigns(self, **_kwargs: Any) -> list:
            return []  # empty sandbox is still a successful handshake

    monkeypatch.setattr(
        "yadirect_agent.cli.doctor.DirectService",
        lambda _s: _FakeDirect(),
    )
    # Guard against accidental import of the real DirectService from
    # another path inside the check.
    monkeypatch.setattr(DirectService, "__init__", lambda *a, **k: None)

    result = await check_direct_sandbox(settings)

    assert result.status == "ok"
    assert result.name == "direct"


@pytest.mark.asyncio
async def test_direct_check_fails_on_auth_error(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yadirect_agent.cli.doctor import check_direct_sandbox
    from yadirect_agent.exceptions import AuthError

    class _FakeDirect:
        async def __aenter__(self) -> _FakeDirect:
            return self

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

        async def get_campaigns(self, **_kwargs: Any) -> list:
            raise AuthError("token revoked", code=52)

    monkeypatch.setattr(
        "yadirect_agent.cli.doctor.DirectService",
        lambda _s: _FakeDirect(),
    )

    result = await check_direct_sandbox(settings)

    assert result.status == "fail"
    assert "auth" in result.detail.lower() or "token" in result.detail.lower()
