"""Authentication package (M15.3).

Houses the local OAuth login flow:

- ``keychain.py`` — ``KeyringTokenStore`` reads/writes the OAuth
  TokenSet to the OS keychain (Keychain on macOS, Credential
  Manager on Windows, Secret Service on Linux).
- ``callback_server.py`` — local one-shot HTTP server that captures
  the redirect from Yandex OAuth.
- ``login_flow.py`` — orchestrates PKCE → server → exchange →
  store and is what ``yadirect-agent auth login`` invokes.

The OAuth wire calls themselves live one layer down in
``clients/oauth.py`` so the keychain / browser / server / CLI
layers stay free of HTTP concerns.
"""
