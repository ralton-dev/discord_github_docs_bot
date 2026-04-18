# Container registry

gitdoc publishes three images to **GitHub Container Registry** under the
`ralton-dev` org:

- `ghcr.io/ralton-dev/gitdoc-discord-bot`
- `ghcr.io/ralton-dev/gitdoc-rag`
- `ghcr.io/ralton-dev/gitdoc-ingestion`

These are **public packages** — no `regcred` Secret is needed in the cluster.
The release CI workflow (`.github/workflows/release.yml`) pushes on tag push
using the built-in `GITHUB_TOKEN`, so no extra secrets need to be configured.

> If you ever need to switch registries (Harbor, local `registry:2`, or
> another GHCR org), override `REGISTRY=` on the Makefile, update
> `images.*.repository` in `deploy/helm/gitdoc/values.yaml`, and create a
> `regcred` Secret per the template at the bottom of this file.

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
