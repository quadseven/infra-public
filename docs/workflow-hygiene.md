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
`infrastructure` -> `infra` in 2026-07). It has **ten** rules.

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

Deliberately **not** ported - each would be dead or wrong code here:

- **Rule 2** dead-cluster reference checking - k8s-ts is a private-infra
  teardown artifact.
- **Rule 4** ARC-runner-routing policy - infra-public's own CI uses plain
  GitHub-hosted runners.
- **Rule 10** PR-preview reachability for auto-applying stacks - scoped by the
  canonical rule to `iac.pulumi.*.yml` workflows. This repo has no such file;
  it has no Pulumi anywhere (`grep -rl pulumi .github/workflows/` is empty),
  because it deploys nothing. Not applicable, rather than unchecked.

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

Nothing enforces that policy, and the count above has now been wrong more than
once: this doc said **seven** while the canonical set grew to **ten**, and the
pull request that corrected it to nine was itself overtaken before it merged -
Rule 10 landed in `14832e7` on 2026-09-12, mid-review. Three rules arrived
without the ledger noticing any of them.

A local re-derivation is available, and it is a **convenience, not the guard**:

```
grep -cE "^  [0-9]+\. " ../infra/.github/scripts/workflow_hygiene.py
```

It needs a sibling checkout of `quadseven/infra`, which is **private**. It
therefore cannot run in this repo's CI, and a convention a human must remember
is exactly what has already failed here. It also reads the **working tree**, so
a sibling checkout that is merely out of date answers confidently with the old
number and nothing says so - prefer
`git -C ../infra show origin/main:.github/scripts/workflow_hygiene.py | grep -cE "^  [0-9]+\. "`,
after fetching.

**The real guard has to live in `infra`, not here** - visibility only runs one
way. `infra` can read this public repo's ledger and assert that every canonical
rule number appears in the ported, not-ported, or explicitly-undecided list
above; this repo cannot read `infra` at all. Infra already has the machinery:
`rule_census()` and `TestRuleListMatchesImplementation` in
`.github/scripts/tests/test_workflow_hygiene.py`, which today guard the
canonical list against the canonical *code*. Extending that one step outward to
the downstream ledger is tracked as infra-public#71's remaining work.
