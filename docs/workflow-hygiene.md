# Workflow hygiene: rule source and sync policy

`infra-public` lints its own `.github/workflows/*.yml` on every PR that
touches them (`.github/workflows/check.workflow-hygiene.yml`, running
`.github/scripts/workflow_hygiene.py`). This exists because infra-public
is the shared reusable-workflow library every fleet repo inherits via
`uses: quadseven/infra-public/...` - drift here is inherited by every
consumer, and nothing local caught it before this gate (infra-public#46).

## Authoritative rule set

The canonical, full rule set lives in the private fleet repo:
`infra/.github/scripts/workflow_hygiene.py` (renamed from `infrastructure`
on 2026-07-08). It has **twelve** rules as of 2026-09-15.

`infra-public`'s copy ports five of them verbatim (same regexes, same
`# hygiene: allow-*` exception-comment convention):

1. SHA-pinning - every third-party `uses:` must be a full 40-hex commit SHA.
5. curl timeouts - every real `curl`, in any workflow field (not only `run:`
   blocks) and in `.sh` files, needs `--max-time`/`-m` and `--connect-timeout`.
   Its verdicts are checked against the canonical's over a shared corpus in
   `.github/scripts/fixtures/rule5_parity/`.
6. `set -e` in standalone shell scripts under `.github/`.
7. per-job `timeout-minutes:` on any job with `runs-on:`.
9. GHA template injection - no `${{ github.event.* }}` or
   `${{ steps.*.outputs.* }}` inside a `run:` script; pass it via `env:`.
   Exception: `# hygiene: allow-interpolation <reason>`. Ported 2026-09-22 so
   public fleet repos that run this copy are covered for it.

### Local-only, with no canonical counterpart

One rule is this repo's own and is numbered off the canonical scheme:

- **1b.** EOL Node major detection (#18) - a SHA-pinned action whose `# vN`
  comment names a known-EOL major (`actions/checkout` v4,
  `actions/setup-node` v4) passes the Rule 1 SHA check but still runs on
  Node-20. The comment is reduced to its bare major before the lookup, so
  `# v4.2.2` cannot evade the `{"v4"}` table (#72, same failure mode as #64).

It is listed because a ledger that accounts only for *canonical* rules cannot
account for this file's own behaviour - someone counting entries against the
canonical twelve would find an enforced rule in none of the three lists.
Having no canonical number is precisely why it goes stale quietly: no upstream
change ever prompts a re-read of it.

Deliberately **not** ported - each would be dead or wrong code here:

- **Rule 2** dead-cluster reference checking - k8s-ts is a private-infra
  teardown artifact.
- **Rule 4** ARC-runner-routing policy - infra-public's own CI uses plain
  GitHub-hosted runners.

### Not yet dispositioned

**Rules 3, 8, 10, 11 and 12 are in neither list above** - nobody has
decided whether they apply here. Not obviously inapplicable, so this is a
gap, not a rejection (infra-public#71):

- **3.** Environment-scoped secrets - a job reading a deploy-role secret must
  declare `environment:`. A live instance already happened here (#51).
- **8.** Working-tree branch switch before a local action - a `run:` step that
  switches branches breaks any later `uses: ./.github/actions/...`.
- **10.** PR-preview reachability for auto-applying stacks - scoped upstream
  to `iac.pulumi.*.yml`. This repo has no Pulumi anywhere
  (`grep -rl pulumi .github/workflows/` is empty), so this is probably
  *not applicable* rather than undecided - but it should say so explicitly.
- **11.** One version label per pinned action - reinforces Rule 1, which *is*
  ported. The strongest port candidate of the five.
- **12.** No `gh` CLI in an `arc-*` pool job - same reasoning as Rule 4's
  rejection; this repo has no ARC pool.

## Keeping the two in sync

There is no automated sync between the two copies - a rule change in the
private repo's `workflow_hygiene.py` does not automatically propagate here.
When one of the four shared rules changes there (regex tightened, a new
exception marker added, a bug fixed), port the equivalent change here by
hand and update `.github/scripts/workflow_hygiene_test.py`'s fixtures to
match. The private repo's copy remains the source of truth for the rule
*definitions*; this copy is the source of truth for infra-public's own
compliance with them.

Nothing enforces that policy, and the count above has now been wrong three
times over: this doc said **seven** while the canonical set grew past it, and
two successive corrections (to nine, then to ten) were each overtaken before
they merged - Rule 10 landed 2026-09-12, Rule 11 on 2026-09-14 at 07:41 and
Rule 12 the same day at 12:33. Five rules arrived without the ledger noticing
any of them.

A local re-derivation exists, and it is a **convenience, not the guard**:

```
git -C ../infra show origin/main:.github/scripts/workflow_hygiene.py \
  | grep -cE "^  [0-9]+\. "
```

Use the `git show origin/main` form rather than reading the working tree: a
sibling checkout that is merely out of date answers confidently with the old
number and nothing says so. Either way it needs a checkout of `infra`, which
is **private**, so it can never run in this repo's CI - and a convention a
human must remember is exactly what has already failed here five times.

**The real guard has to live in `infra`, not here**, because visibility only
runs one way: `infra` can read this public repo's ledger, but this repo
cannot read `infra` at all. Infra already has the machinery - `rule_census()`
and `TestRuleListMatchesImplementation` (infra #2721), which today guard the
canonical list against the canonical *code*. Extending that one step outward
to assert every canonical rule number appears in exactly one of the three
lists above is infra-public#71's remaining work.

Prefer the per-rule ledger above to the total. A count has no anchor to the
thing it counts, so an addition upstream falsifies it silently; a missing
*entry* is something a reader can see.
