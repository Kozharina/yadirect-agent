# Changelog

All notable changes to **yadirect-agent** are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
while we are pre-1.0, breaking changes may land in any minor bump and will be
called out explicitly.

The latest unreleased work always lives in the [Unreleased] section at the
top — promoted to a numbered version on release-cut.

## [Unreleased]

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

[Unreleased]: https://github.com/Kozharina/yadirect-agent/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Kozharina/yadirect-agent/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Kozharina/yadirect-agent/releases/tag/v0.1.0
