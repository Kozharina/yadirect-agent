# Security policy

yadirect-agent drives an advertising account. A bug in the wrong place
does not crash a service — it spends money or burns a Quality Score
that takes weeks to recover. We treat security reports accordingly.

## Supported versions

The project is pre-alpha; only `main` receives security fixes. Once the
`0.1.0` tag lands, this table will list the supported minor versions.

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |

## Reporting a vulnerability

**Do not open a public GitHub issue for security reports.**

Use GitHub's private vulnerability reporting instead:

1. Go to <https://github.com/Kozharina/yadirect-agent/security/advisories>.
2. Click **Report a vulnerability**.
3. Fill in the advisory form. Describe:
   - the component affected (`clients/`, `services/`, `agent/`, `cli/`,
     `mcp_server/`),
   - the impact scenario — what an attacker or a prompt-injected tool
     call could achieve,
   - reproduction steps against the sandbox (`YANDEX_USE_SANDBOX=true`
     is fine; do not reproduce against a production account),
   - a suggested fix if you have one.

We acknowledge reports within **3 business days**, and aim to ship a
fix within **30 days** for confirmed high-impact issues.

## What counts as a vulnerability

In scope — **please report**:

- A path that causes the agent to execute a write (bid change, budget
  change, campaign state change, keyword add) that the policy layer
  (`agent_policy.yml`, kill-switches) was supposed to block. Bypasses
  of `plan → confirm → execute` are the canonical case.
- Any leak of `SecretStr` values (tokens, API keys) into logs, audit
  events, tool results, PR descriptions, or traceback output.
- A prompt-injection vector: content returned by a Yandex API call or
  a tool result that causes the agent loop to take an action the user
  did not authorise.
- A CI or supply-chain issue: something a malicious dependency update
  could exploit against contributors who run `pytest` locally.

Out of scope:

- Vulnerabilities in Yandex.Direct, Yandex.Metrika, or Anthropic APIs
  themselves — report those to the respective vendors.
- Reports that require an attacker who already has the user's
  `.env`-file contents: the threat model assumes `.env` stays private.
- Best-practice drift that isn't actually exploitable (e.g. "you
  should set header X" without a concrete attack).

## Handling of secrets in reports

Please **never** include real tokens, OAuth codes, or account
identifiers in a report. The sandbox cabinet is enough. If a report
unavoidably contains sensitive material, we rotate on our side before
acknowledging.

## Public disclosure

After a fix lands on `main` and an advisory is published, we:

- Credit the reporter in the advisory (unless anonymity is requested).
- Link the advisory from the release notes for the version containing
  the fix.
- Add a `CVE-` identifier via GitHub's CNA when the severity warrants
  one (typically, anything that bypasses the safety layer).
