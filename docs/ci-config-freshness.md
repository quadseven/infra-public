# CI config freshness

`_reusable.ci-config-freshness.yml` fails a pull request whose branch is
missing changes to CI config that landed on the base branch after the PR
forked. It names each such commit and tells the author to rebase or merge
the base in.

## Why

A `pull_request` run uses the workflows on the PR's own branch. When the
base branch gains a new or stricter gate after the PR branched off, the PR
keeps running the old gates and can go green on rules the base no longer
accepts. GitHub's "require branches to be up to date" setting closes that
gap by forcing a rebase for every base commit, related or not. This check is
the narrow version: it fails only when the base moved CI config.

## What counts as CI config

By default, any of these paths:

- `.github/workflows/`
- `.github/actions/`
- `.github/rulesets/`
- `.loom/validation.conf`

Pass `paths` (comma- or newline-separated) to replace the list, for example
when a repo keeps its CI scripts in `ci/` or its ruleset export somewhere
else. The default list lives in one place, `DEFAULT_PATHS` in
`.github/scripts/ci_config_freshness.py`.

## How it decides

1. Check out the PR head commit (`github.event.pull_request.head.sha`) with
   full history. Not the default merge ref, which already contains the base.
2. Find the merge-base of that commit and `origin/<base>`, the base branch's
   tip at checkout time.
3. List the commits on the base branch's first-parent history after the
   merge-base that touch a watched path. Any such commit fails the check.

A PR that has rebased onto the base, or merged the base in, has a newer
merge-base and passes. A CI change made on the PR branch itself does not
count: only the base's changes do.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | The branch has every CI config change on the base. |
| 1 | The base has CI config commits the branch is missing. Each is named with its short sha, subject and watched files. |
| 2 | The check could not run: a shallow clone, a ref that does not resolve, no common history, or a bad path list. This is never a pass. |

## Calling it

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened]
jobs:
  freshness:
    uses: quadseven/infra-public/.github/workflows/_reusable.ci-config-freshness.yml@<commit-sha>
    permissions: { contents: read }
    # with:
    #   paths: ".github/workflows/,.github/actions/,ci/"
```

Every input is listed in [`reusables.md`](reusables.md). Make the job a
required check so a stale branch cannot merge.

## Running the script by hand

```sh
git fetch origin
python3 .github/scripts/ci_config_freshness.py --base origin/main
python3 .github/scripts/ci_config_freshness.py --base origin/main --head HEAD --paths ".github/,ci/"
```

## Limits

GitHub does not re-run PR checks when only the base branch moves. The check
re-evaluates on every push to the PR, on reopen and on a manual re-run, so a
base change that lands while a PR sits idle is caught only by one of those, or
by a merge queue that re-runs required checks.
