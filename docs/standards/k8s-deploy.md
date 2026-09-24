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
    uses: quadseven/infra-public/.github/workflows/deploy.k8s.yml@<sha>
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
| `cluster-access` | `tailnet` | `tailnet`: join the tailnet + write `kubeconfig-b64`. `in-cluster`: the runner is a pod in the target cluster; use its ServiceAccount token (see below) |
| `tailnet-tag` | `tag:ci` | ACL tag for the ephemeral CI join (`tailnet` mode only) |
| `seed-app-secrets` | `false` | if true, OIDC -> read SSM + literals -> delete-then-create the app Secret before rollout |
| `app-secret-name` | `""` | the Secret to seed; required when `seed-app-secrets` |
| `ssm-secrets` | `""` | newline `KEY=/ssm/path` pairs, read with-decryption (private paths arrive here, never hardcoded) |
| `extra-secrets` | `""` | newline `KEY=value` literals appended to the Secret (e.g. Cognito ids) |
| `aws-region` | `us-east-1` | region for the SSM reads |
| `run-migrate` | `false` | if true, apply `migrate-manifest` as a Job + wait for completion before rollout |
| `migrate-manifest` / `migrate-job-name` | `""` | the migrate Job manifest path + name; required when `run-migrate` |
| `migrate-timeout` | `300s` | `kubectl wait --for=condition=complete` timeout |

Secrets: `registry-username`, `registry-password`, `ts-authkey` and `kubeconfig-b64`
(both required in `tailnet` mode, omitted in `in-cluster` mode), and `aws-role-arn` (OIDC role for the SSM reads; passed as a secret so the
account-id-bearing ARN stays masked; required when `seed-app-secrets`).

A migrate/secret-seeding caller grants `id-token: write` (OIDC) in addition to
`contents: read`:

```yaml
jobs:
  deploy:
    permissions: { contents: read, id-token: write }
    uses: quadseven/infra-public/.github/workflows/deploy.k8s.yml@<sha>
    with:
      # ... build/deploy inputs ...
      seed-app-secrets: true
      app-secret-name: <secret>
      ssm-secrets: |
        DB_URL=/path/to/db-url
        API_KEY=/path/to/api-key
      extra-secrets: |
        SOME_ID=${{ vars.SOME_ID }}
      run-migrate: true
      migrate-manifest: <dir>/migrate-job.yaml
      migrate-job-name: <service>-migrate
    secrets:
      # ... + ...
      aws-role-arn: ${{ secrets.AWS_ROLE_ARN }}
```

## In-cluster mode (self-hosted runner inside the target cluster)

A runner that is itself a pod in the target cluster does not need the tailnet
or a kubeconfig secret. Set `cluster-access: in-cluster`, point `runner` at
that pool, and drop `ts-authkey` / `kubeconfig-b64` from the caller (passing
`kubeconfig-b64` in this mode fails the job rather than being ignored):

```yaml
    with:
      runner: <your-in-cluster-arm64-pool>
      cluster-access: in-cluster
      # ... the usual inputs ...
    secrets:
      registry-username: ${{ secrets.REGISTRY_USERNAME }}
      registry-password: ${{ secrets.REGISTRY_PASSWORD }}
```

The job writes a kubeconfig that points at the in-cluster API service address
and reads the pod's projected ServiceAccount token by path (the token is never
copied). Before the image build, a preflight runs `kubectl version` and
`kubectl auth whoami`, checks that `namespace` exists, and runs
`kubectl auth can-i` for each verb the run uses: the fixed steps (secrets,
rollout, migrate Job, smoke pod) plus create and patch for every object type
the rendered `kustomize-dir` contains. It fails the job if the API is
unreachable, the token is rejected, the namespace is missing, or any verb is
denied. A deploy that cannot reach or change the cluster therefore
fails red instead of reporting a green run that changed nothing.

The runner pool must provide:

- `serviceAccountName` on the runner pod, with a Role in `namespace` covering
  what the deploy touches (secrets, services, serviceaccounts, deployments,
  replicasets, pods, pods/log, events, plus jobs when `run-migrate` and pods
  create/delete when `smoke-pod-name`), and get/patch on that one Namespace
  object (a ClusterRole with `resourceNames`). The Namespace must already
  exist; in-cluster mode does not create it.
- `kubectl` on PATH in the runner image (the stock runner image lacks it),
  plus `aws` when `seed-app-secrets` is on.
- A Docker daemon for the build (for example a dind sidecar) and a network
  path from the pod to `registry-host`.

The preflight is deliberately stricter than a single apply needs: it asks for
get, create and patch on every rendered type even when the object already
exists, so the same identity works for a first install and every later deploy.

Trust boundary: the ServiceAccount token is mounted into the runner pod, so
every job that runs on that pool (including `prebuild-run`, the image build,
and any CI job sharing the pool) can read it. Give the pool to one trusted
repository, keep the Role scoped to the target namespace, and do not run
untrusted pull-request code on a pool that carries a deploy identity.

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

## Secret handling

The SSM-seed reads values at runtime into a 0600 env-file and never echoes them
(`set +x`); only the SSM *paths* (not values) live in the caller's `ssm-secrets`
input. The Secret is delete-then-created (not `apply`-merged) so a changed key
set never leaves stale data. The OIDC role ARN is a `secret`, not an input, so
it stays masked.
