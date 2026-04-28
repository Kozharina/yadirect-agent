"""Yandex OAuth token model (M15.3).

The ``TokenSet`` is the in-memory shape of an OAuth token pair
returned by ``oauth.yandex.ru/token``. It is also the unit of
persistence in the OS keychain (one JSON-blob per slot, atomic
read/write — the alternative of writing access/refresh under
separate keys opens a race window where a crash between the two
writes leaves the keychain in an inconsistent state).

Two symmetric public methods carry the storage contract:

- ``to_storage_dict`` returns a JSON-friendly ``dict[str, Any]`` with
  secrets exposed as raw strings and datetimes as ISO-8601 with
  explicit timezone designators. The keychain layer wraps it in
  ``json.dumps``.
- ``from_storage_dict`` constructs back from such a dict, with
  ``extra="forbid"`` enforcing the contract: a corrupt or
  maliciously-edited keychain entry fails loudly rather than
  silently dropping unknown fields.

Why ``SecretStr`` for the tokens: ``repr(token_set)`` lands in
structlog in countless code paths. SecretStr ensures the access and
refresh values render as ``**********``. The plaintext leaves this
object only via ``get_secret_value()`` on the keychain write path
and on the wire-call path inside ``clients/oauth.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class TokenSet(BaseModel):
    """A Yandex OAuth access + refresh token pair with metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    access_token: SecretStr
    refresh_token: SecretStr
    token_type: str = Field(default="bearer", min_length=1)
    scope: tuple[str, ...]
    obtained_at: datetime
    expires_at: datetime

    @field_validator("scope")
    @classmethod
    def _scope_non_empty(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            msg = "scope must contain at least one OAuth scope"
            raise ValueError(msg)
        return v

    @field_validator("obtained_at", "expires_at")
    @classmethod
    def _datetime_must_be_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "datetime must be timezone-aware (UTC preferred)"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _obtained_before_expires(self) -> TokenSet:
        if self.obtained_at > self.expires_at:
            msg = "obtained_at must be <= expires_at"
            raise ValueError(msg)
        return self

    def needs_refresh(
        self,
        *,
        now: datetime | None = None,
        leeway_seconds: int = 60,
    ) -> bool:
        """Return True if the token is expired or within ``leeway_seconds`` of expiry.

        The leeway window pulls refresh forward so we never present a
        token that will expire mid-request. Default 60s is conservative
        for the Yandex Direct API where a single call can take 10s+
        under load (multi-page reports, rate-limited retries).

        ``now`` is keyword-only so callers cannot pass a positional
        wrong-meaning value; tests pin a fixed instant for determinism.
        """
        if leeway_seconds < 0:
            msg = "leeway_seconds must be non-negative"
            raise ValueError(msg)
        current = now if now is not None else datetime.now(UTC)
        threshold = self.expires_at - timedelta(seconds=leeway_seconds)
        return current >= threshold

    def to_storage_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly dict for keychain persistence.

        Secrets are exposed as raw strings — ``SecretStr.get_secret_value()``
        — because the destination IS the secure store. ``model_dump_json``
        would render ``**********`` instead, silently corrupting every
        write.
        """
        return {
            "access_token": self.access_token.get_secret_value(),
            "refresh_token": self.refresh_token.get_secret_value(),
            "token_type": self.token_type,
            "scope": list(self.scope),
            "obtained_at": self.obtained_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> TokenSet:
        """Construct from a JSON-friendly dict produced by ``to_storage_dict``.

        Goes through ``model_validate`` so every field validator and
        ``extra="forbid"`` apply: a corrupt or maliciously-edited
        keychain entry fails here rather than presenting a
        partially-valid token at use time.
        """
        return cls.model_validate(data)


__all__ = ["TokenSet"]
