"""Tests for OAuth token models (M15.3).

The ``TokenSet`` model is the in-memory shape of a Yandex OAuth token
pair. It is also the unit of persistence in the OS keychain, so the
serde round-trip is on the critical path: a corrupt write means the
operator must re-run ``yadirect-agent auth login``, which is the very
friction M15.3 exists to remove.

What we pin here:

- Secrets are wrapped in ``SecretStr`` so they cannot leak into logs
  or ``repr()``. M15.3 is the first surface that touches refreshable
  tokens directly, so the redaction discipline starts here.
- Round-trip via ``to_storage_dict`` / ``from_storage_dict`` preserves
  the secret values byte-for-byte (anything else would silently log
  the operator out).
- ``needs_refresh`` is conservative: a fresh token reports False, an
  already-expired token reports True, and the leeway window pulls the
  refresh forward so we never present a token that will expire mid-
  request.
- Invariants that ``apply-plan`` and the safety pipeline rely on:
  ``expires_at`` is timezone-aware, ``obtained_at <= expires_at``,
  and ``scope`` is non-empty. A token with no scope is one that the
  agent cannot use against either the Direct API or Metrika — fail
  loudly at construction, not silently at the first 403.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr, ValidationError

from yadirect_agent.models.auth import TokenSet


def _ts(
    *,
    access: str = "AQAA-access",
    refresh: str = "1.AQAA-refresh",
    obtained_at: datetime | None = None,
    expires_at: datetime | None = None,
    scope: tuple[str, ...] = ("direct:api", "metrika:read", "metrika:write"),
) -> TokenSet:
    now = obtained_at or datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    return TokenSet(
        access_token=SecretStr(access),
        refresh_token=SecretStr(refresh),
        token_type="bearer",
        scope=scope,
        obtained_at=now,
        expires_at=expires_at or now + timedelta(days=365),
    )


class TestTokenSetConstruction:
    def test_minimal_valid_tokenset(self) -> None:
        ts = _ts()

        assert ts.access_token.get_secret_value() == "AQAA-access"
        assert ts.refresh_token.get_secret_value() == "1.AQAA-refresh"
        assert ts.token_type == "bearer"
        assert ts.scope == ("direct:api", "metrika:read", "metrika:write")

    def test_repr_does_not_leak_secrets(self) -> None:
        ts = _ts(access="real-secret-value", refresh="real-refresh-value")

        rendered = repr(ts) + str(ts)

        assert "real-secret-value" not in rendered
        assert "real-refresh-value" not in rendered

    def test_naive_expires_at_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"timezone[- ]aware"):
            TokenSet(
                access_token=SecretStr("a"),
                refresh_token=SecretStr("r"),
                token_type="bearer",
                scope=("direct:api",),
                obtained_at=datetime.now(UTC),
                expires_at=datetime(2099, 1, 1),  # naive
            )

    def test_naive_obtained_at_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"timezone[- ]aware"):
            TokenSet(
                access_token=SecretStr("a"),
                refresh_token=SecretStr("r"),
                token_type="bearer",
                scope=("direct:api",),
                obtained_at=datetime(2026, 4, 28, 12, 0),  # naive
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )

    def test_empty_scope_rejected(self) -> None:
        with pytest.raises(ValidationError, match="scope"):
            _ts(scope=())

    def test_obtained_at_after_expires_at_rejected(self) -> None:
        now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
        with pytest.raises(ValidationError, match=r"obtained_at.*expires_at"):
            _ts(obtained_at=now, expires_at=now - timedelta(seconds=1))


class TestNeedsRefresh:
    def test_fresh_token_does_not_need_refresh(self) -> None:
        now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
        ts = _ts(obtained_at=now, expires_at=now + timedelta(days=30))

        assert ts.needs_refresh(now=now) is False

    def test_already_expired_token_needs_refresh(self) -> None:
        now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
        ts = _ts(obtained_at=now - timedelta(days=2), expires_at=now - timedelta(seconds=1))

        assert ts.needs_refresh(now=now) is True

    def test_token_within_leeway_needs_refresh(self) -> None:
        # Default leeway pulls refresh forward by 60s so we never present
        # a token that will expire mid-request.
        now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
        ts = _ts(obtained_at=now - timedelta(hours=1), expires_at=now + timedelta(seconds=30))

        assert ts.needs_refresh(now=now) is True

    def test_custom_leeway(self) -> None:
        now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
        ts = _ts(obtained_at=now - timedelta(hours=1), expires_at=now + timedelta(seconds=30))

        # 10s leeway: 30s remaining > 10s, fresh enough.
        assert ts.needs_refresh(now=now, leeway_seconds=10) is False

    def test_negative_leeway_rejected(self) -> None:
        ts = _ts()
        with pytest.raises(ValueError, match="leeway"):
            ts.needs_refresh(now=ts.obtained_at, leeway_seconds=-1)


class TestStorageRoundTrip:
    def test_round_trip_preserves_secret_values(self) -> None:
        original = _ts(access="access-xyz", refresh="refresh-abc")

        serialised = json.dumps(original.to_storage_dict())
        restored = TokenSet.from_storage_dict(json.loads(serialised))

        # Equality on the model itself is what callers will check; SecretStr
        # __eq__ compares the underlying value.
        assert restored == original
        # And explicitly: the secret round-tripped, not just the mask.
        assert restored.access_token.get_secret_value() == "access-xyz"
        assert restored.refresh_token.get_secret_value() == "refresh-abc"

    def test_storage_dict_contains_iso_strings(self) -> None:
        # ISO-8601 with timezone designator is the only datetime
        # representation that survives JSON without ambiguity. Pin the
        # contract so a future "let's switch to epoch seconds" change
        # has to update this test.
        ts = _ts()

        d = ts.to_storage_dict()

        assert isinstance(d["expires_at"], str)
        assert d["expires_at"].endswith("+00:00") or d["expires_at"].endswith("Z")
        assert isinstance(d["obtained_at"], str)

    def test_storage_dict_exposes_secrets_for_persistence(self) -> None:
        # The whole point of to_storage_dict over model_dump is that
        # the secret VALUES go to the keychain — not the SecretStr
        # masks. If this regresses, every login silently writes
        # "**********" to keychain and the next "auth status" finds
        # garbage.
        ts = _ts(access="real-access", refresh="real-refresh")

        d = ts.to_storage_dict()

        assert d["access_token"] == "real-access"
        assert d["refresh_token"] == "real-refresh"

    def test_from_storage_dict_rejects_missing_required_fields(self) -> None:
        ts = _ts()
        d = ts.to_storage_dict()
        del d["access_token"]

        with pytest.raises(ValidationError):
            TokenSet.from_storage_dict(d)

    def test_from_storage_dict_rejects_unknown_fields(self) -> None:
        # extra="forbid" — a corrupt or maliciously-edited keychain
        # entry with extra payload should fail loudly, not silently
        # ignore.
        ts = _ts()
        d = ts.to_storage_dict()
        d["evil_extra"] = "ignored?"

        with pytest.raises(ValidationError):
            TokenSet.from_storage_dict(d)
