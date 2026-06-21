# Standard: deploy a container to Kubernetes

The single reusable for shipping an already-buildable service to a Kubernetes
cluster from a private registry. One `workflow_call` replaces the per-service
deploy boilerplate every workload otherwise re-derives: tailnet join, native
build + push, registry-pull seed, image-placeholder pin, `kubectl apply -k`, a
rollout gate, and an in-cluster smoke probe. Update the reusable once; every
caller gets the fix on its next SHA bump.

## Why this exists

Each new workload was hand-copying ~250 lines of deploy YAML and, worse,
re-discovering the same cluster landmines (single-arch images that never
schedule on arm64, AppArmor-off on bring-your-own nodes, `kubectl logs`
unreachable when the apiserver cannot reach kubelet). This standard bakes the
proven shape in one place so a new service deploys by supplying only its name,
image, kustomize dir, and secrets.

## Usage

```yaml
# .github/workflows/app.<service>.deploy-k8s.yml in a service repo
name: App - <service> - Deploy (k8s)
on:
  push: { branches: [main], paths: ['services/<service>/**'] }
  workflow_dispatch: {}
permissions:
  contents: read
concurrency:
  group: app-<service>-deploy-k8s
  cancel-in-progress: false
jobs:
  deploy:
    uses: githumps/infra-public/.github/workflows/deploy.k8s.yml@<sha>
    with:
      namespace: <namespace>
      service-name: <service>
      image-repo: <service>
      registry-host: ${{ vars.DEPLOY_REGISTRY_HOST }}
      build-context: services/<service>
      dockerfile: services/<service>/Dockerfile
      kustomize-dir: services/<service>/k8s
      smoke-pod-name: <service>-smoke
      smoke-curl-cmd: "curl -fsS --max-time 10 --retry 3 --retry-connrefused http://<service>:8080/health"
    secrets:
      registry-username: ${{ secrets.REGISTRY_USERNAME }}
      registry-password: ${{ secrets.REGISTRY_PASSWORD }}
      ts-authkey: ${{ secrets.TS_AUTHKEY }}
      kubeconfig-b64: ${{ secrets.KUBECONFIG_B64 }}
```

A frontend-bundling or shared-vendoring service adds a pre-build hook:

```yaml
    with:
      # ... as above ...
      node-version: "22"
      prebuild-run: |
        ( cd <frontend-dir> && npm install --no-audit --no-fund && ./build.sh --bundle )
        rm -rf services/<service>/dist && cp -R <frontend-dir>/dist services/<service>/dist
```

## Key inputs

| Input | Default | Notes |
|---|---|---|
| `namespace`, `service-name`, `image-repo` | - | required; the workload identity |
| `registry-host` | - | required; pass `vars.DEPLOY_REGISTRY_HOST` (a hostname, never defaulted here so no private host leaks into this public repo) |
| `build-context`, `dockerfile`, `kustomize-dir` | - | required; build inputs + the `apply -k` dir |
| `build-args` | `""` | newline-separated `KEY=VALUE` |
| `node-version` | `""` | if set, `setup-node` before the prebuild hook |
| `prebuild-run` | `""` | shell run in the build context before `docker build` (frontend bundle / shared vendoring). Passed via env, not template interpolation |
| `runner` | `ubuntu-24.04-arm` | native CPU for the image arch; never `setup-qemu` |
| `rollout-timeout` | `180s` | `kubectl rollout status` timeout |
| `smoke-pod-name` / `smoke-curl-cmd` | `""` | optional one-shot curl probe of the in-cluster Service |
| `registry-pull-secret` | `registry-pull` | docker-registry imagePullSecret seeded in the namespace |
| `tailnet-tag` | `tag:ci` | ACL tag for the ephemeral CI join |

Secrets: `registry-username`, `registry-password`, `ts-authkey`, `kubeconfig-b64`.

## Cluster invariants the manifests must hold

- **Image placeholders.** Deployment/Job `*.yaml` in `kustomize-dir` carry
  `REGISTRY_PLACEHOLDER/<repo>:TAG_PLACEHOLDER`; the workflow pins them and fails
  the deploy if any placeholder survives.
- **No appArmorProfile** when nodes run AppArmor-disabled (it rejects the pod).
  Keep `seccompProfile: RuntimeDefault` + non-root + `readOnlyRootFilesystem` +
  drop-ALL caps. A reusable kustomize component for this baseline lives in the
  consuming repo, not here.
- **`terminationMessagePolicy: FallbackToLogsOnError`** on every container, so
  the rollout diagnostics can read the crash reason over the apiserver when
  `kubectl logs` to the node is blocked.

## Not covered (yet)

Alembic/migrate Jobs and seeding app-config secrets from a cloud secret store
are intentionally out of scope until a caller needs them and a verified path is
added here. Keep those caller-side for now.
