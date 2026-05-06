# Acceptance tests — full Anna journey lock (M15.7)

> **Audience**: Anna — non-developer media-buyer, target user of the
> product. **Contract**: from a fresh install with no Yandex token,
> Anna gets to her first `account_health()` finding **without
> touching the terminal beyond `pip install` + `auth login`**, in
> under 10 minutes.

This directory holds **end-to-end functional acceptance tests** that
exercise the assembled product surface (MCP tools + their internal
service stack) on a clean machine state. Distinct from
`tests/unit/` (per-component contracts) and `tests/evals/` (agent
reasoning quality).

## Scope of the existing acceptance test

`test_anna_journey.py::test_anna_journey_to_first_health_finding`
locks down the assembled flow:

1. Fresh in-memory keychain (simulates "Anna just ran
   `auth login` for the first time" — token saved, not yet used).
2. Mocked Direct + Metrika responses at the service layer (no
   real network — runs in CI without credentials).
3. Build the MCP tool registry.
4. Call `start_onboarding(answers=None)` → expect
   `ready_for_profile_qa` (token present, no profile yet).
5. Call `start_onboarding(answers=BusinessProfile{...})` → expect
   `policy_proposed` with policy YAML and an embedded health
   rollup envelope.
6. Call `account_health(days=7)` standalone → expect a
   `HealthReport` with at least the burning-campaign finding.
7. Stopwatch the whole sequence; assert `elapsed_sec < 30`.

The 30-second budget is **deliberately loose**. The CI-runner real
elapsed is ~50ms (no network, all mocks); the assertion exists to
catch a regression that introduces a sleep / busy-wait / massive
import-time cost. The product spec budget (10 minutes) is meant
for a real human reading screens and tapping consent; that's a
different test surface (manual smoke walkthrough, not automated).

## Why not real `pip install`

Real `pip install yadirect-agent` followed by command-line
invocations belongs to a separate **release-validation workflow**
(GitHub Actions step that runs in a fresh container after PyPI
publish). That covers packaging-level regressions (missing files
in wheel, broken entry-point, missing transitive deps). Acceptance
tests here cover the **functional contract** of the assembled
product, which can be exercised in-process without paying the pip
install cost on every PR.

## Running

```bash
make acceptance        # acceptance suite only
make check             # acceptance is included by default
```

## Adding new acceptance scenarios

Each test should:

- Cover a **multi-tool flow** (otherwise it's a unit test).
- Be wholly **self-contained** — no external network, no real
  filesystem outside `tmp_path`, no shared state.
- Have a **stopwatch assertion** with a generous budget so it
  catches catastrophic regressions, not noise.
- Document the **product contract** it locks down in the
  test docstring (what would break for Anna if this test
  starts failing).

Keep the test count small. This directory is for journey-level
guarantees; per-component invariants belong to `tests/unit/`.
