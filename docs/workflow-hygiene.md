# Workflow hygiene: rule source and sync policy

`infra-public` lints its own `.github/workflows/*.yml` on every PR that
touches them (`.github/workflows/check.workflow-hygiene.yml`, running
`.github/scripts/workflow_hygiene.py`). This exists because infra-public
is the shared reusable-workflow library every fleet repo inherits via
`uses: quadseven/infra-public/...` - drift here is inherited by every
consumer, and nothing local caught it before this gate (infra-public#46).

## Authoritative rule set

The canonical rule set lives in a private linter that this repo cannot
read. It numbers its rules 1 through 13. This copy ports the ones that
apply here (same regexes, same `# hygiene: allow-*` exception-comment
convention) and declares the rest skipped, with a reason.

The record of which is which is **code, not this page**: three constants at
the top of `workflow_hygiene.py`.

- `CANONICAL_RULE_COUNT` - the highest canonical rule number (13).
- `IMPLEMENTED_RULES` - the canonical rules this copy enforces.
- `SKIPPED_RULES` - the canonical rules it does not, each with its reason.
  The run summary names them (`rules 2, 3, 4, 10 and 13 skipped`), so a
  clean run cannot be read as having checked them.

`workflow_hygiene_test.py` fails if a number from 1 to
`CANONICAL_RULE_COUNT` is in neither list, in both, or not named as
`Rule N` in the linter source (infra-public#125). Earlier versions of this
page kept the ledger in prose, and it drifted at every port (#71, #119,
#125); the test is what keeps it honest now.

### Implemented

1. SHA-pinning - every third-party `uses:` must be a full 40-hex commit SHA.
5. curl timeouts - every real `curl`, in any workflow field and in `.sh`
   files, needs `--max-time`/`-m` and `--connect-timeout`.
6. `set -e` in standalone shell scripts under `.github/`.
7. per-job `timeout-minutes:` on any job with `runs-on:`.
8. a `run:` step that switches branches must restore the tree before a
   later `uses: ./.github/actions/...`.
9. GHA template injection - no `${{ github.event.* }}` or
   `${{ steps.*.outputs.* }}` inside a `run:` script; pass it via `env:`.
   Exception: `# hygiene: allow-interpolation <reason>`.
11. one version label per pinned action SHA, repo-wide.
12. no `gh` CLI in a job on an `arc-*` runner label.

Rules 5, 8, 11 and 12 have parity corpora under
`.github/scripts/fixtures/rule<N>_parity/` that record the canonical
linter's verdicts. Set `CANONICAL_HYGIENE_LINTER` to a local copy of the
canonical script to recheck them.

### Skipped

- **2.** dead-cluster references in live code - this repo names no cluster
  to retire, and the leak scan already refuses private infrastructure
  identifiers on every PR, which is what such a name here would be.
- **3.** environment-scoped secrets - there is no list of them here and no
  manifest to derive one from.
- **4.** ARC runner routing - this repo's own CI uses GitHub-hosted runners.
- **10.** PR-preview reachability for auto-applying stacks - there are no
  Pulumi stacks here.
- **13.** no image push from the shared private runner pool - there is no
  such pool here; a reusable's runner is an input its caller picks.

### Local-only, with no canonical counterpart

- **1b.** EOL Node major detection (#18) - a SHA-pinned action whose `# vN`
  comment names a known-EOL major (`actions/checkout` v4,
  `actions/setup-node` v4) passes the Rule 1 SHA check but still runs on
  Node-20. The comment is reduced to its bare major before the lookup, so
  `# v4.2.2` cannot evade the `{"v4"}` table (#72, same failure mode as #64).

## Keeping the two in sync

There is no automated sync between the two copies. When a shared rule
changes upstream (regex tightened, a new exception marker, a bug fixed),
port the equivalent change here by hand and update the fixtures to match.
When upstream adds a rule, raise `CANONICAL_RULE_COUNT`; the accounting test
then fails until the new number is placed in one of the two lists.

The canonical copy remains the source of truth for the rule *definitions*;
this copy is the source of truth for infra-public's own compliance with
them. Because this repo cannot read the private one, the check that
`CANONICAL_RULE_COUNT` itself is current has to live on the private side.
