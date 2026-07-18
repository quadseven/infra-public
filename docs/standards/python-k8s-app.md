# Standard: Python app for Kubernetes

The single, reusable CI standard for a Python service that ships as a container
to a Kubernetes cluster. One workflow call gives you a consistent lint/test pass
and a **multi-arch** image — so the same code, the same ruff/uv conventions, and
the same build behavior apply everywhere. Update the reusable once; every
consumer gets the change on its next SHA bump.

## Why this exists

Two recurring failures this standard removes:

1. **Single-arch images on ARM clusters.** A build on an x86 runner with no
   `platforms:` produces an `amd64`-only image. It pushes fine, passes CI, and
   then **silently fails to schedule** on an `arm64` node (OCI A1.Flex,
   Graviton, Apple silicon) — the pod just never starts and the cause (image
   architecture) is non-obvious. This standard builds **both** arches every
   time, natively (never QEMU), and on a pull request builds both *without*
   pushing — so an arm64-breaking change fails the PR, not a deploy.
2. **Per-repo lint/test drift.** ruff version, format rules, Python version, and
   test command diverge across repos. `python-check` pins them in one place.

## Usage

```yaml
# .github/workflows/ci.yml in a Python service repo
name: CI
on:
  push: { branches: [main] }
  pull_request:
permissions:
  contents: read
  packages: write   # GHCR push
  id-token: write   # ECR-OIDC path (declared by the reusable; grant even for GHCR)
jobs:
  ship:
    uses: quadseven/infra-public/.github/workflows/ship.python.yml@<sha>
    with:
      image: ghcr.io/${{ github.repository }}
      tag: ${{ github.sha }}
      push: ${{ github.event_name != 'pull_request' }}   # PR = build-both-arches only
    secrets: inherit
```

That's the whole standard: lint + test (ruff + uv + pytest) gate the build, then
a native amd64 + arm64 image is published as a manifest list.

### Multi-stage Dockerfiles (one repo, several images)

Call it once per target. Each produces its own multi-arch manifest:

```yaml
jobs:
  web:
    uses: quadseven/infra-public/.github/workflows/ship.python.yml@<sha>
    with: { image: ghcr.io/${{ github.repository }}-web, tag: ${{ github.sha }}, target: web,
            push: "${{ github.event_name != 'pull_request' }}" }
    secrets: inherit
  worker:
    needs: [web]   # share the build cache / avoid double ruff+pytest: set run-check:false here
    uses: quadseven/infra-public/.github/workflows/ship.python.yml@<sha>
    with: { image: ghcr.io/${{ github.repository }}-worker, tag: ${{ github.sha }}, target: worker,
            run-check: false, push: "${{ github.event_name != 'pull_request' }}" }
    secrets: inherit
```

## Key inputs

| Input | Default | Notes |
|---|---|---|
| `image`, `tag` | — | required; full image ref (`ghcr.io/owner/repo`) + tag |
| `push` | `true` | set `false` on PRs to validate both arches without publishing |
| `target` | `""` | Dockerfile stage for multi-stage builds |
| `registry-type` | `ghcr` | GHCR is the default registry; `ecr` (OIDC) and `none` also supported |
| `amd64-runner` / `arm64-runner` | GitHub-hosted | override to a self-hosted/ARC pool (private repos avoiding hosted minutes) |
| `run-check` | `true` | ruff + pytest gate; set `false` to skip (e.g. a 2nd build of the same tree) |
| `python-version`, `lint-paths`, `test-cmd`, `install-cmd`, `ruff` | project defaults | passed to `python-check` |

## What it composes

- [`check.python.yml`](./check.python.yml) — ruff (lint + `format --check`) + uv + pytest.
- [`build.container-multiarch.yml`](./build.container-multiarch.yml) — native amd64 + arm64 (no QEMU), per-arch push-by-digest, manifest-list merge.

Prefer **GHCR** over a self-hosted in-cluster registry: it's free for the org,
natively multi-arch, and removes a stateful component from the cluster.
