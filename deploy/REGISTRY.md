# Container registry

gitdoc publishes three images:

- `gitdoc-discord-bot`
- `gitdoc-rag`
- `gitdoc-ingestion`

The registry host is **not yet chosen** (see
`implementation_plans/working-memory.md`, "Registry" open question). Pick one
of the three options below, then update `images.*.repository` in
`deploy/helm/gitdoc/values.yaml` (and any per-repo `values-<repo>.yaml`)
accordingly.

## Options

### 1. GitHub Container Registry (GHCR)

- Host: `ghcr.io/<org-or-user>`
- Auth: Personal Access Token (PAT) with `write:packages` / `read:packages`,
  or a GitHub Actions-issued token in CI.
- Pros: free for public images, integrates with GitHub auth, no infra to run.
- Cons: requires `regcred` (Kubernetes secret) when images are private.

### 2. Harbor (homelab self-hosted)

- Host: `harbor.<homelab-domain>/gitdoc`
- Auth: Harbor robot account with push/pull on the `gitdoc` project.
- Pros: on-prem, supports replication and vulnerability scanning, one-time
  setup reused across projects.
- Cons: operator burden (certs, backup, upgrades) — worth it only if Harbor
  is already running for other homelab workloads.

### 3. Local `registry:2`

- Host: `registry.<homelab-domain>` (or a `NodePort`/`ClusterIP` exposed
  internally).
- Auth: typically none on the cluster network; restrict via NetworkPolicy.
- Pros: trivial to deploy (single container), no external dependency.
- Cons: no UI, no scanning, no HA; fine for a single-cluster homelab but
  will be outgrown if more operators need it.

## Pushing images

Registry host is passed to the Makefile via `REGISTRY=`:

```sh
# Single-arch local build + push (fast, host arch only)
make build REGISTRY=ghcr.io/<org>
make push  REGISTRY=ghcr.io/<org>

# Multi-arch (amd64 + arm64) build+push in one step
make buildx-push REGISTRY=ghcr.io/<org>

# Echo the resolved tag (e.g. 0.1.0-ab12cd3) — useful in CI
make print-tag
```

The tag defaults to `<VERSION>-<short-sha>` derived from `git rev-parse
--short HEAD`. Bump `VERSION` in the `Makefile` for a new semver level;
the SHA suffix handles traceability within a version.

## `regcred` for private registries

If the chosen registry requires auth for `docker pull` (GHCR private,
Harbor robot account, or any local registry behind basic-auth), create an
`imagePullSecrets`-compatible secret in each gitdoc namespace:

```sh
# Template — substitute the four <...> placeholders.
# Do NOT commit the resulting YAML; it contains credentials.
kubectl -n gitdoc-<repo> create secret docker-registry regcred \
  --docker-server=<registry-host> \
  --docker-username=<user-or-robot> \
  --docker-password=<token-or-password> \
  --docker-email=<unused-but-required>
```

Then reference it from the workload — either by patching the
ServiceAccount:

```sh
kubectl -n gitdoc-<repo> patch serviceaccount default \
  -p '{"imagePullSecrets":[{"name":"regcred"}]}'
```

…or by adding an `imagePullSecrets:` stanza to the chart values (requires a
small templates/*.yaml tweak — track that work under the Helm hardening
task).

Real credential values belong in your secret manager (sealed-secrets,
external-secrets, 1Password, etc.), not this file.
