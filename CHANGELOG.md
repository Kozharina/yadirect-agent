# Changelog

All notable changes to **yadirect-agent** are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
while we are pre-1.0, breaking changes may land in any minor bump and will be
called out explicitly.

The latest unreleased work always lives in the [Unreleased] section at the
top — promoted to a numbered version on release-cut.

## [Unreleased]

## [0.2.1] — 2026-05-07

### Added

- **M18 slice 1** — first step of the notifications & approvals
  milestone (Phase 2 release 0.3.0 work landing in the 0.2.x line
  to unblock the M21.2 alert path):
  - `Notification` model (`models/notification.py`) with severity
    reused from `models/health.py:Severity`.
  - `TelegramSink` (`services/notify/telegram.py`): outbound Bot
    API send with httpx + tenacity retry (4 attempts, exp backoff
    up to 30 s), HTML parse_mode + stdlib `html.escape`, severity
    emoji prefixes (🔴 / 🟡 / 🔵), `from_settings()` classmethod
    returning `None` when unconfigured.
  - Settings fields `telegram_bot_token` (`SecretStr | None`,
    env `TELEGRAM_BOT_TOKEN`) and `telegram_chat_id`
    (`str | None`, env `TELEGRAM_CHAT_ID`).
  - CLI subcommand `yadirect-agent notify test` for operator
    verification after first BotFather setup. Russian operator
    text per CLAUDE.md `<language_conventions>`.

### Fixed

- **M18 slice 1 doc/CLI bug**: docstrings + `notify test`
  unconfigured hint message documented env vars as
  `YADIRECT_TELEGRAM_BOT_TOKEN` / `YADIRECT_TELEGRAM_CHAT_ID`,
  but pydantic-settings (no `env_prefix` on Settings) actually
  resolves field names uppercased without prefix —
  `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. Fixed across
  `config.py`, `services/notify/telegram.py`, and the CLI hint.
  An operator following the documented variable names from
  v0.2.0's CLI message would have seen the sink stay
  unconfigured forever; v0.2.1 fixes the documented names so
  the env-var path actually wires through.
- **M18 slice 1 traceback bug**: `yadirect-agent notify test`
  with a wrong token / wrong chat_id used to dump the full
  Python traceback from `httpx.HTTPStatusError` to stderr.
  Fixed: CLI now catches `HTTPStatusError` (401 / 400 / 403)
  and `httpx.HTTPError` (network failures) and translates each
  into a one-line Russian operator message + non-zero exit
  (1 for sink-rejected, 2 for unconfigured). Sink itself still
  raises by design — the future Dispatcher (slice 5) needs
  the exception class to fall back to other sinks. Only the
  CLI surface translates.

### Why a patch release (0.2.1, not 0.3.0)

M18 slice 1 ships outbound notifications only; no behaviour
change for v0.2.0 callers (M15.x acceptance path stays
identical). The `notify test` CLI is a new opt-in surface,
not a breaking addition. SemVer: minor "added new optional
feature" would also fit pre-1.0, but patch is honest about
the scope (no rule changes, no protocol changes, no Settings
required-field additions).

## [0.2.0] — 2026-05-06

### Anna's path (M15.x acceptance)

The first PyPI release that the target user (Anna — non-developer media-buyer)
can actually start with. `0.1.0` was the packaging-pipeline proof; `0.2.0` is
the first "I can hand it to a real account owner" cut.

### Added

- **M15.2** — `yadirect-agent install-into-claude-desktop` /
  `uninstall-from-claude-desktop` CLI commands. Idempotent, atomic config
  edit, dry-run support, OS-conventional path detection (#49).
- **M15.3** — Standard OAuth PKCE flow with system keyring storage
  (`auth login` / `auth status` / `auth logout` CLI commands).
  `--timeout-seconds` knob for slow 2FA delivery (#51, #66).
- **M15.3 follow-ups** — auto-refresh on `AuthError(code=52)` in Direct
  client (#67), parity on Metrika 401 (#69), shared
  `refresh_settings_token` helper between Direct and Metrika (#70).
- **M15.4** — Conversational MCP onboarding shipped end-to-end (5 slices,
  #57, #59, #60, #62, #63). `start_onboarding` MCP tool: OAuth probe →
  `BusinessProfile` Q&A → policy YAML proposal → `onboarding_completed`
  audit event → first health-check rollup. The single tool the LLM in
  Claude Desktop calls when the user types "помоги настроить агента".
- **M15.5** — `account_health()` MCP tool mirroring the rule-based CLI
  health check, no LLM required (#56).
- **M15.5.1-5** — Rule-expansion bundle (5 sub-slices). Burning campaigns
  + high CPA (M15.5.1), rejected ads + rejected keywords (#73), low CTR
  with `ym:ad:impressions` plumbing (#74), CTR-drift rule + reusable
  `HealthHistoryStore` (#76).
- **M15.6** — Built-in cross-platform scheduler closed architecturally
  (3 slices: macOS LaunchAgent #65, Linux systemd `--user` timers #71,
  Windows Task Scheduler #72). `yadirect-agent schedule install / status
  / remove` works the same on every platform; `auto` detection picks the
  right backend from `sys.platform`.
- **M15.7** — End-to-end acceptance test
  (`tests/acceptance/test_anna_journey.py`) locks down Anna's full
  conversational journey from `auth login` to first health finding. Runs
  in ~50ms, catches catastrophic regressions before they reach a real
  user (#77).
- **M20** — Human-readable rationale shipped end-to-end (4 slices). Model
  + JSONL store + `yadirect-agent rationale show / list` CLI (M20.1),
  hard-required emission on `@requires_plan` decorator (M20.2, #52),
  `explain_decision` MCP tool (M20.3, #54), auto-populated `policy_slack`
  (M20.4, #55).
- **M21** — LLM cost tracking. Per-call `CostRecord`, JSONL persistence,
  `yadirect-agent cost status` CLI (#50). Hard auto-degrade on budget
  exhaust deferred to M21.2 (blocked on M18 alert path).

### Documentation

- Per-milestone post-ship doc sweeps kept `docs/TECHNICAL_SPEC.md` and
  `docs/BACKLOG.md` in lockstep with what landed (#48, #53, #58, #64,
  #75).
- New `tests/acceptance/README.md` documents the journey-test contract
  for future scenario additions.

### CI / DevOps

- Bumped `actions/download-artifact` group via Dependabot (#68).

### What this release does NOT include

- **M21.2** auto-degrade — blocked on M18 (notification sinks).
- **M18** notifications & approvals — Phase 2 work, opens release 0.3.0.
- **M19** rollback / time machine — Phase 2.
- **M4 / M5 / M6 (full alerts) / M11 / M17** — Phase 2 milestones.
- **M8 / M9 / M10 / M12 / M13 / M16** — Phase 3 milestones.

## [0.1.0] — 2026-04-28

### Initial PyPI release

Packaging-pipeline proof. Wires up the build + Trusted Publisher
workflow; `pip install yadirect-agent` resolves and `--version`
returns `0.1.0` in a clean venv. Functional surface limited to the
M0–M3 + early M15.5.1 work; no `auth`, no `install-into-claude-desktop`,
no scheduler, no onboarding tool. Released as a release-management
checkpoint, not a user-ready cut.

[Unreleased]: https://github.com/Kozharina/yadirect-agent/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/Kozharina/yadirect-agent/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/Kozharina/yadirect-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Kozharina/yadirect-agent/releases/tag/v0.1.0
