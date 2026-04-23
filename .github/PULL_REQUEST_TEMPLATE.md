<!--
Thanks for opening a PR. Every box in "Reviewer checklist" corresponds to an
item in docs/REVIEW.md — please self-check before requesting review.
-->

## What & why

<!-- One-paragraph summary. Link the milestone / issue. -->

Closes: #

## How

<!--
Call out:
- new modules / responsibilities
- any changes to the safety surface (policy schema, kill-switches, audit)
- any blocking I/O introduced (should be none)
-->

## Tests

- [ ] Unit tests added / updated (`pytest -q` green locally)
- [ ] `respx` fixtures cover the happy and failure paths for new HTTP calls
- [ ] Coverage for changed files ≥ 80%

## Checklist (see `docs/REVIEW.md`)

- [ ] `make check` passes (`lint + type + test`)
- [ ] No business logic in `clients/` — only HTTP + type mapping
- [ ] No blocking calls in the async main path
- [ ] No secrets logged or committed
- [ ] Error paths use typed exceptions from `exceptions.py`
- [ ] Destructive / mutating changes go through the plan → confirm → execute flow
- [ ] Public API / flags documented in `README.md` and the relevant doc in `docs/`

## Safety notes

<!--
If this PR adds mutating capability:
- Which kill-switches apply?
- Is the default `agent_policy.yml` sane out of the box?
- Is the operation reversible? If not, say so explicitly.
-->
