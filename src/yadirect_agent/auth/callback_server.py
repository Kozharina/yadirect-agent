"""Local one-shot OAuth callback server (M15.3).

Yandex OAuth's authorization endpoint redirects the user's browser
back to ``REDIRECT_URI`` (``http://localhost:8765/callback``) after
they click "Разрешить". This module runs the tiny HTTP/1.1 server
that catches that redirect, extracts the auth ``code`` from the
query string, validates the ``state`` matches what we sent, and
hands the code back to the login orchestrator.

Security pins:

- **Loopback only**. The default ``host`` is ``127.0.0.1`` and the
  constructor refuses to bind anywhere else. The OAuth code in
  the redirect URL is short-lived but real — leaking it to
  anyone on the same Wi-Fi is a credential exposure incident.
- **One-shot**. The server resolves a single ``Future[str]`` on
  the first valid callback; subsequent requests still get a
  response so the browser tab does not hang, but the future
  cannot be re-resolved.
- **State-match required**. CSRF defence. The orchestrator
  generates a fresh random state per login, hands it to both
  ``build_authorization_url`` and the constructor here, and the
  server rejects any callback that does not echo it back exactly.
- **Method / path locked**. ``GET /callback`` is the only valid
  request shape; everything else 404 / 405. The server is not a
  general-purpose local web service.

The server uses ``asyncio.start_server`` + minimal hand-rolled
HTTP/1.1 parsing because the alternative — pulling in ``aiohttp``
purely for one endpoint — would be a 4 MB dependency for ~30 lines
of work. ``http.server`` (stdlib) would force us to bridge a
threadpool back into asyncio, which is brittle.
"""

# ruff: noqa: RUF001, RUF003
# This module ships browser-facing Russian HTML for the operator
# (per project language convention in CLAUDE.md). RUF001 / RUF003
# flag every Cyrillic ``о`` / ``б`` / ``с`` etc. as ambiguous with
# their Latin / digit lookalikes; suppressing at file scope keeps
# the strings readable. Same approach as services/semantics.py.

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Self
from urllib.parse import parse_qs, urlparse

import structlog

# Pages we render to the operator's browser. Plain HTML, no JS,
# no external resources — works in any browser including the
# nominally text-only ones the Yandex consent page might bounce
# the user into. Operator-facing Cyrillic per project language
# convention; the file-level noqa above suppresses RUF001/RUF003.
_SUCCESS_HTML = (
    "<!doctype html>\n"
    '<html lang="ru"><head><meta charset="utf-8">\n'
    "<title>yadirect-agent — успешно</title></head>\n"
    '<body style="font-family: system-ui, sans-serif; max-width: 480px; margin: 4em auto;">\n'
    "<h1>Готово</h1>\n"
    "<p>yadirect-agent получил доступ к вашему аккаунту. "
    "Можно закрыть эту вкладку — токен сохранён в OS keychain.</p>\n"
    "</body></html>\n"
)

_DENIED_HTML = (
    "<!doctype html>\n"
    '<html lang="ru"><head><meta charset="utf-8">\n'
    "<title>yadirect-agent — отменено</title></head>\n"
    '<body style="font-family: system-ui, sans-serif; max-width: 480px; margin: 4em auto;">\n'
    "<h1>Доступ не выдан</h1>\n"
    "<p>Yandex сообщил об ошибке или вы нажали «Запретить». "
    "Можно закрыть эту вкладку и при необходимости повторить "
    "<code>yadirect-agent auth login</code>.</p>\n"
    "</body></html>\n"
)

# Loopback only. The OAuth code in the redirect URL is sensitive;
# binding to a non-loopback interface would expose it to the LAN.
_ALLOWED_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


class OAuthCallbackError(Exception):
    """Raised when the OAuth callback signals failure.

    Three flavours collapsed into one type because the recovery
    path is the same in all cases — ``yadirect-agent auth login``
    again, possibly after fixing the underlying issue:

    - The user clicked "Запретить" (Yandex sends ``?error=...``).
    - The ``state`` parameter does not match (CSRF mismatch — could
      be a stale browser tab from an earlier attempt or, much
      worse, an active interception attempt).
    - The callback is missing ``code`` entirely (malformed).
    """


class LocalCallbackServer:
    """One-shot HTTP/1.1 server for the Yandex OAuth callback.

    Use as an async context manager:

        server = LocalCallbackServer(expected_state=state, port=8765)
        async with server:
            webbrowser.open(auth_url)
            code = await server.wait_for_code(timeout_seconds=300)
    """

    def __init__(
        self,
        *,
        expected_state: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        if host not in _ALLOWED_HOSTS:
            msg = (
                f"refusing non-loopback host {host!r}: the OAuth callback URL "
                "carries a sensitive auth code. Use 127.0.0.1."
            )
            raise ValueError(msg)
        if not expected_state:
            msg = "expected_state must be non-empty (CSRF defence)"
            raise ValueError(msg)
        self._expected_state = expected_state
        self._host = host
        self._port = port
        self._server: asyncio.base_events.Server | None = None
        self._code_future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._logger = structlog.get_logger(__name__).bind(component="oauth_callback")

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        if self._server is None:
            msg = "server not started — call .start() or use async with"
            raise RuntimeError(msg)
        sockets = self._server.sockets
        if not sockets:
            msg = "server has no bound sockets"
            raise RuntimeError(msg)
        port = sockets[0].getsockname()[1]
        return int(port)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with suppress(asyncio.CancelledError):
                await self._server.wait_closed()
            self._server = None
        # If the future never resolved (no callback arrived), cancel it
        # so any awaiter sees ``asyncio.CancelledError``. Tests that
        # explicitly want the timeout path use ``wait_for_code`` with
        # a short timeout BEFORE leaving the context manager.
        if not self._code_future.done():
            self._code_future.cancel()

    async def wait_for_code(self, *, timeout_seconds: float) -> str:
        """Block until the callback arrives, the operator gives up, or time runs out.

        Raises ``asyncio.TimeoutError`` when ``timeout_seconds`` elapses
        without a callback, or ``OAuthCallbackError`` when Yandex
        rejected / state mismatched / code missing.
        """
        return await asyncio.wait_for(asyncio.shield(self._code_future), timeout=timeout_seconds)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            try:
                parts = request_line.decode("ascii").rstrip("\r\n").split(" ", 2)
            except UnicodeDecodeError:
                await self._respond(writer, 400, "Bad Request", b"<h1>400</h1>")
                return
            if len(parts) != 3:
                await self._respond(writer, 400, "Bad Request", b"<h1>400</h1>")
                return
            method, path, _http_version = parts

            # Drain headers — we do not inspect them; we only need
            # the request line to dispatch.
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break

            if method != "GET":
                await self._respond(writer, 405, "Method Not Allowed", b"<h1>405</h1>")
                return
            parsed = urlparse(path)
            if parsed.path != "/callback":
                await self._respond(writer, 404, "Not Found", b"<h1>404</h1>")
                return

            params = parse_qs(parsed.query)
            self._dispatch(params, writer_handle=writer)
        finally:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()

    def _dispatch(
        self,
        params: dict[str, list[str]],
        *,
        writer_handle: asyncio.StreamWriter,
    ) -> None:
        """Inspect query params and either resolve the future or set an error."""
        # ``_dispatch`` must respond AND set the future without
        # awaiting (the writer is already in the response state).
        # We schedule the actual write via ``asyncio.create_task``
        # for parity with the await pattern, but avoid blocking the
        # handler on a slow client.
        error = (params.get("error") or [""])[0]
        code = (params.get("code") or [""])[0]
        state = (params.get("state") or [""])[0]

        if error:
            self._logger.warning("oauth_callback.yandex_error", error=error)
            self._respond_sync(writer_handle, 200, "OK", _DENIED_HTML.encode())
            self._fail_future(OAuthCallbackError(f"Yandex OAuth error: {error}"))
            return

        if not code:
            self._logger.warning("oauth_callback.missing_code")
            self._respond_sync(writer_handle, 400, "Bad Request", _DENIED_HTML.encode())
            self._fail_future(OAuthCallbackError("callback missing 'code' parameter"))
            return

        if state != self._expected_state:
            self._logger.warning("oauth_callback.state_mismatch")
            self._respond_sync(writer_handle, 400, "Bad Request", _DENIED_HTML.encode())
            self._fail_future(
                OAuthCallbackError(
                    "CSRF state mismatch: callback state does not match the value sent"
                    " at authorize time. Possible stale browser tab — re-run auth login."
                )
            )
            return

        self._logger.info("oauth_callback.code_captured")
        self._respond_sync(writer_handle, 200, "OK", _SUCCESS_HTML.encode())
        if not self._code_future.done():
            self._code_future.set_result(code)

    def _fail_future(self, exc: BaseException) -> None:
        if not self._code_future.done():
            self._code_future.set_exception(exc)

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        body: bytes,
    ) -> None:
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(head)
        writer.write(body)
        await writer.drain()

    @staticmethod
    def _respond_sync(
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        body: bytes,
    ) -> None:
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(head)
        writer.write(body)


__all__: list[Any] = [
    "LocalCallbackServer",
    "OAuthCallbackError",
]
