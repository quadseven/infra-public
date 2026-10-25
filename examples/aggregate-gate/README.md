# Aggregate gate: one required check that fails closed

`_reusable.aggregate-gate.yml` gives a repository a single check,
`gate / aggregate`, that a ruleset can require on its own. It is green only
when every other leg of CI really succeeded.

## Why one check

A ruleset that requires many checks by name blocks a merge only when one of
them reports a failure. These all leave a pull request mergeable:

- a required check is renamed, so the old name never reports;
- a workflow has a path filter, so on this PR it never runs at all (and the
  PR is stuck forever instead, if that check is required);
- a job is skipped by an `if:`, which GitHub reports as passing;
- a run is cancelled, or a job sits queued and never starts.

The aggregate gate treats every one of those as a failure, and the ruleset
only has to know one name.

## Two modes

**fold** (default, recommended). One workflow calls the repo's gate
workflows as jobs and ends with a `gate` job that `needs:` all of them. The
gate reads `toJSON(needs)` and fails when any leg is `failure`,
`cancelled`, has no result, or is `skipped` without being listed in
`allow-skipped`. `expected` names legs that must be in the results, so a
leg dropped from `needs:` by mistake is caught.

**poll**. The gate is its own workflow and waits, through the API, for the
commit's other check runs and commit statuses. It ignores its own run and
any name matching `ignore` (for example a review bot), fails fast on any
other conclusion than `success`, fails a check still queued after
`start-timeout-minutes` and an `expected` name that never appeared, and
fails anything still running at `wait-minutes`. Use it when the legs cannot
be turned into called workflows (checks from other apps, other CI
systems). It needs `checks: read` and `statuses: read`. A workflow that is
path-filtered away creates no check at all, so name the checks that must
always run in `expected`.

The logic is `.github/scripts/aggregate_gate.py`, unit-tested by
`.github/scripts/aggregate_gate_test.py`. Every input is listed in
[`docs/reusables.md`](../../docs/reusables.md).

## Adopting it (fold)

1. Add `on: workflow_call:` to each gate workflow (`lint.yml`, `test.yml`,
   ...). A called workflow takes the caller's event, so `github.event_name`
   and `github.event.pull_request` still work inside it.
2. Copy [`ci-gate.yml`](ci-gate.yml) to `.github/workflows/ci-gate.yml`
   and list one job per gate workflow. Keep three things:
   - no path filters on `ci-gate.yml`;
   - `if: always()` on the `gate` job (not `!cancelled()`: a cancelled run
     must report a red gate, not nothing);
   - every other job in the gate's `needs:`.
3. Remove the `pull_request` and `push` triggers from the called workflows
   so each leg runs once, inside `ci-gate`. Path filtering moves into the
   leg (a job `if:` on changed files) and that leg goes in `allow-skipped`.
4. Open a PR and check that `gate / aggregate` reports, and that breaking a
   leg turns it red.

## Requiring it in a ruleset

The required check's name is `<caller job id or name> / aggregate`, so
`gate / aggregate` with the template. Require it from GitHub Actions, so
nothing else can post a status with the same name.

In the web UI: Settings, Rules, Rulesets, your branch ruleset, "Require
status checks to pass", add `gate / aggregate`, and pick GitHub Actions as
its source.

Through the API (`15368` is the GitHub Actions app's integration id):

```json
{
  "type": "required_status_checks",
  "parameters": {
    "strict_required_status_checks_policy": false,
    "do_not_enforce_on_create": false,
    "required_status_checks": [
      { "context": "gate / aggregate", "integration_id": 15368 }
    ]
  }
}
```

That rule goes in the ruleset's `rules` array
(`PUT /repos/<owner>/<repo>/rulesets/<id>`). If the ruleset is managed as
code, change it there instead, or the next apply reverts it.

## Migrating an existing ruleset to the single check

1. Land `ci-gate.yml` first, with the old required checks still in place,
   and let a few PRs run both.
2. Add `gate / aggregate` to the ruleset's required checks, next to the old
   ones.
3. Remove the old per-workflow checks from the ruleset, leaving
   `gate / aggregate` as the only required status.
4. Remove the old triggers from the called workflows (step 3 above), so a
   leg no longer runs twice.

Doing it in this order means there is never a moment when the branch is
protected by nothing, and a ruleset never requires a name that has stopped
reporting.
