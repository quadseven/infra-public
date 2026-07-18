# Workflow hygiene: rule source and sync policy

`infra-public` lints its own `.github/workflows/*.yml` on every PR that
touches them (`.github/workflows/check.workflow-hygiene.yml`, running
`.github/scripts/workflow_hygiene.py`). This exists because infra-public
is the shared reusable-workflow library every fleet repo inherits via
`uses: quadseven/infra-public/...` - drift here is inherited by every
consumer, and nothing local caught it before this gate (infra-public#46).

## Authoritative rule set

The canonical, full rule set lives in the private fleet repo:
`infrastructure/.github/scripts/workflow_hygiene.py`. It has seven rules.

`infra-public`'s copy ports four of them verbatim (same regexes, same
`# hygiene: allow-*` exception-comment convention):

1. SHA-pinning - every third-party `uses:` must be a full 40-hex commit SHA.
5. curl timeouts - every real `curl` in a `run:` block needs `--max-time`/`-m`
   and `--connect-timeout`.
6. `set -e` in standalone shell scripts under `.github/`.
7. per-job `timeout-minutes:` on any job with `runs-on:`.

Deliberately **not** ported (private-infra-specific, would be dead or wrong
code here): dead-cluster reference checking and ARC-runner-routing policy -
infra-public's own CI uses plain GitHub-hosted runners.

## Keeping the two in sync

There is no automated sync between the two copies - a rule change in the
private repo's `workflow_hygiene.py` does not automatically propagate here.
When one of the four shared rules changes there (regex tightened, a new
exception marker added, a bug fixed), port the equivalent change here by
hand and update `.github/scripts/workflow_hygiene_test.py`'s fixtures to
match. The private repo's copy remains the source of truth for the rule
*definitions*; this copy is the source of truth for infra-public's own
compliance with them.
