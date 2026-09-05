# Workflow hygiene: rule source and sync policy

`infra-public` lints its own `.github/workflows/*.yml` on every PR that
touches them (`.github/workflows/check.workflow-hygiene.yml`, running
`.github/scripts/workflow_hygiene.py`). This exists because infra-public
is the shared reusable-workflow library every fleet repo inherits via
`uses: quadseven/infra-public/...` - drift here is inherited by every
consumer, and nothing local caught it before this gate (infra-public#46).

## Authoritative rule set

The canonical, full rule set lives in the private fleet repo:
`infra/.github/scripts/workflow_hygiene.py` (the repo was renamed
`infrastructure` -> `infra` in 2026-07). It has **nine** rules.

`infra-public`'s copy ports four of them verbatim (same regexes, same
`# hygiene: allow-*` exception-comment convention):

1. SHA-pinning - every third-party `uses:` must be a full 40-hex commit SHA.
5. curl timeouts - every real `curl` in a `run:` block needs `--max-time`/`-m`
   and `--connect-timeout`.
6. `set -e` in standalone shell scripts under `.github/`.
7. per-job `timeout-minutes:` on any job with `runs-on:`.

Plus one rule that is **local to this repo** and has no canonical counterpart:

- **1b.** EOL Node major detection - a SHA-pinned action whose `# vN` comment
  names a known-EOL major (`actions/checkout` v4, `actions/setup-node` v4)
  passes the SHA check but still runs on Node-20. Added for #18; the comment
  is reduced to its bare major before the lookup (#72, #78).

Deliberately **not** ported (private-infra-specific, would be dead or wrong
code here): **Rule 2** dead-cluster reference checking (k8s-ts is a
private-infra teardown artifact) and **Rule 4** ARC-runner-routing policy -
infra-public's own CI uses plain GitHub-hosted runners.

### Not yet dispositioned

**Rules 3, 8 and 9 are in neither list above** - nobody has decided whether
they apply here. They are not obviously inapplicable, so this is a gap, not a
rejection (infra-public#71):

- **3.** Environment-scoped secrets - a job reading a deploy-role secret must
  declare `environment:`.
- **8.** Working-tree branch switch before a local action - a `run:` step that
  switches branches breaks any later `uses: ./.github/actions/...`.
- **9.** GHA template injection - no `${{ }}` interpolation of attacker-
  controlled values directly inside a `run:` block.

## Keeping the two in sync

There is no automated sync between the two copies - a rule change in the
private repo's `workflow_hygiene.py` does not automatically propagate here.
When one of the four shared rules changes there (regex tightened, a new
exception marker added, a bug fixed), port the equivalent change here by
hand and update `.github/scripts/workflow_hygiene_test.py`'s fixtures to
match. Rule 1b is ours alone - it has nothing upstream to track. The
private repo's copy remains the source of truth for the rule *definitions*;
this copy is the source of truth for infra-public's own compliance with them.

Nothing enforces that policy, which is why the count above was wrong twice
(seven -> nine) before anyone noticed. Re-derive it rather than trusting it:

```
grep -cE "^  [0-9]+\. " ../infra/.github/scripts/workflow_hygiene.py
```
