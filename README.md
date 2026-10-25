# infra-public

Reusable CI/CD building blocks: GitHub Actions reusable workflows, the
guard scripts behind them, and one small Python library. Everything here
takes its configuration as inputs and secrets, so you can call it from your
own repository with your own runners, registries and credentials.

Read [`AGENTS.md`](AGENTS.md) before contributing: this repository is
public, and that file is the rule for everything written to it.

## Calling a reusable workflow

Pin to a full commit SHA, never a branch or tag:

```yaml
jobs:
  check:
    uses: quadseven/infra-public/.github/workflows/check.python.yml@<commit-sha>
    with:
      working-directory: .
```

Every input, secret and output of every reusable is listed in
[`docs/reusables.md`](docs/reusables.md). That page is generated from the
workflows and CI fails when it is stale, so it is the reference to trust.
The `canary.*.yml` workflows in `.github/workflows/` are working callers
you can copy.

## Reusable workflows

| Workflow | What it does |
| --- | --- |
| `check.python.yml` | ruff lint and format check, uv install, pytest |
| `check.rust.yml` | `cargo fmt --check`, `cargo clippy -D warnings`, `cargo test` |
| `build.container.yml` | single-arch image build on a native runner (no QEMU), optional ECR push, digest output |
| `build.container-multiarch.yml` | native amd64 and arm64 builds merged into one manifest list |
| `ship.python.yml` | `check.python.yml`, then `build.container-multiarch.yml`, gated on the check |
| `deploy.k8s.yml` | build, push and roll out a container to a Kubernetes cluster, with a rollout gate and smoke probe |
| `deploy.pages.yml` | publish a directory to GitHub Pages; `dry_run: true` uploads the Pages artifact and stops before the publish |
| `_reusable.smoke-test.yml` | curl a list of endpoints with retries and assert status and body |
| `_reusable.upstream-drift-check.yml` | open an issue when a pinned upstream repo moves past its pin |
| `_reusable.fleet-drift.yml` | weekly: take the majority value of each shared CI setting across a list of repos and keep one issue per repo that drifts, never asking a newer pin to downgrade |
| `_reusable.leak-scan.yml` | refuse a PR, issue or tree that carries private identifiers (hostnames, private IPs, local paths, names) |
| `_reusable.ci-config-freshness.yml` | fail a PR whose branch is missing CI config commits (workflows, actions, rulesets, validation contract) that landed on the base after it forked; see [`docs/ci-config-freshness.md`](docs/ci-config-freshness.md) |
| `check.issue-close-completeness.yml` | reopen an issue closed as completed while its checkboxes are unticked |
| `sync.public-repo-docs.yml` | open a PR that syncs a repo's `AGENTS.md`/`CLAUDE.md` with `templates/public-repo/` |

## What you bring

Some paths need infrastructure that does not ship with this repository.
None is needed for the lint, test and build-only paths.

- **Registry push.** `build.container*.yml`, `ship.python.yml` and
  `deploy.k8s.yml` push to ECR through an AWS role you trust for GitHub
  OIDC (`role_to_assume` / `aws-role-arn`), or to GHCR with the caller's
  `packages: write`. With `push: false` they only build.
- **Pages site.** `deploy.pages.yml` publishes to a repository with GitHub
  Pages enabled (source: GitHub Actions) and a `github-pages` environment,
  with the caller's `pages: write` and `id-token: write`. With
  `dry_run: true` it only builds and uploads the Pages artifact.
- **Cluster access.** `deploy.k8s.yml` reaches the cluster either by joining
  a Tailscale tailnet with your auth key and using your kubeconfig, or from
  a runner that already runs inside the cluster (`cluster-access:
  in-cluster`).
- **Leak-scan deny list.** `_reusable.leak-scan.yml` checks generic shapes
  with no setup. Its optional second layer reads extra terms (names,
  product words) from an SSM parameter through an AWS role you supply.
- **Runners.** Every reusable takes a runner input. The defaults are
  GitHub-hosted runners; point them at your own pool if you have one.

## Scripts and packages

- `.github/scripts/check_private_leaks.py` - the leak scanner the leak-scan
  workflow runs. `--staged`, `--diff BASE...HEAD`, `--text-file` or
  `--tree`.
- `.github/scripts/workflow_hygiene.py` - lints workflows and shell scripts
  (SHA pins, curl timeouts, `set -e`, job timeouts, template injection and
  more). See [`docs/workflow-hygiene.md`](docs/workflow-hygiene.md).
- `.github/scripts/validation_contract.py` - checks that
  `.loom/validation.conf` lists only commands a pull-request workflow
  really runs, and with `--run` runs them.
- `.github/scripts/reusable_docs.py` - generates `docs/reusables.md`.
- `.github/scripts/ci_config_freshness.py` - the check behind
  `_reusable.ci-config-freshness.yml`: lists base-branch commits to CI
  config after the merge-base. `--base`, `--head`, `--paths`.
- `python/spark_cave/` - a stdlib-only Python library for a queue-based
  job handoff between a cloud service and an on-prem worker. See its
  [README](python/spark_cave/README.md).

## Running the checks locally

Every check CI runs on a pull request is listed, in order, in
[`.loom/validation.conf`](.loom/validation.conf). To run them all:

```sh
python3 .github/scripts/validation_contract.py --run
```

It needs Python 3.11 or later and Node 24. `python/spark_cave` has its own
suite: from that directory, `uv run --frozen pytest -q`.
