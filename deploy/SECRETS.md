# Secrets management

gitdoc runs one Kubernetes Secret per instance, containing four keys:

| Key                  | Source                                                         |
| -------------------- | -------------------------------------------------------------- |
| `DISCORD_BOT_TOKEN`  | Discord developer portal → application → Bot → Reset Token     |
| `LITELLM_API_KEY`    | LiteLLM proxy `master_key` or a virtual key on the proxy       |
| `POSTGRES_DSN`       | `postgresql://gitdoc_<slug>:<pw>@<host>:5432/gitdoc_<slug>?sslmode=require` (see `db/provision/README.md`) |
| `GIT_TOKEN`          | GitHub fine-grained PAT with read on the single repo (may be "" for public repos) |

The chart always reads those same key names. What differs is how the
Secret object lands in the namespace:

- **Bootstrap** (first deploy only) — plaintext in `values-<slug>.yaml`;
  Helm renders a Secret from the values.
- **Sealed** (steady state) — operator applies a `SealedSecret` manifest
  to the namespace, the sealed-secrets controller materialises a plain
  `Secret` with the agreed name, and the chart's `secrets.existingSecret`
  points workloads at it.

The default values file leaves both paths open: leave `existingSecret`
empty to bootstrap with plaintext, or set it to skip the chart Secret
entirely.

---

## Decision: sealed-secrets

Chosen over external-secrets for homelab-scale reasons:

- **No external dependency.** external-secrets requires a Vault / AWS
  Secrets Manager / Kubernetes-external secret store to pull from. The
  homelab doesn't run one and doesn't want to.
- **Git-driven workflow.** sealed-secrets encrypts once with the
  controller's public key; the resulting `SealedSecret` manifest is
  safe to commit. A new machine clones the repo and can deploy without
  re-provisioning secrets, as long as the sealed-secrets controller's
  private key is still intact on the cluster.
- **Encrypt-once, decrypt-in-cluster.** Only the cluster's controller
  can decrypt. Lost laptop ≠ leaked credentials.
- **Rotation is "regen → re-seal → re-apply".** No "who owns the Vault
  policy?" question.

Tradeoff: if the sealed-secrets controller private key is lost, every
committed `SealedSecret` in the repo becomes unrecoverable. Back up the
sealing key (see the "Disaster recovery" section below).

---

## Install the sealed-secrets controller (one-time per cluster)

This is a cluster-wide install, done once, not per instance. The chart
does not bundle it — upstream's manifest is authoritative and already
stable.

```sh
# Track latest stable: https://github.com/bitnami-labs/sealed-secrets/releases
kubectl apply -f https://github.com/bitnami-labs/sealed-secrets/releases/download/v0.27.3/controller.yaml
```

This installs the controller into `kube-system` and registers the
`SealedSecret` CRD. Verify:

```sh
kubectl -n kube-system get pods -l name=sealed-secrets-controller
kubectl get crd sealedsecrets.bitnami.com
```

Install `kubeseal` locally to produce sealed manifests:

```sh
# macOS
brew install kubeseal

# Linux (adjust arch)
KS_VERSION=0.27.3
curl -sL https://github.com/bitnami-labs/sealed-secrets/releases/download/v${KS_VERSION}/kubeseal-${KS_VERSION}-linux-amd64.tar.gz \
  | tar xz -C /tmp kubeseal
sudo install /tmp/kubeseal /usr/local/bin/kubeseal
```

Sanity-check by fetching the cluster's public cert (used to encrypt):

```sh
kubeseal --fetch-cert > /tmp/sealed-secrets.pub
```

---

## Per-instance: seal and apply

Assumes the gitdoc instance's namespace `gitdoc-<slug>` exists. If you
haven't run `helm install` yet, create it by hand:

```sh
kubectl create namespace gitdoc-${SLUG}
```

### 1. Draft a plain Secret manifest (DO NOT COMMIT)

Write to a temp file outside the repo so you can't accidentally
`git add` it. Example: `~/tmp/gitdoc-${SLUG}-secret.yaml`.

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: gitdoc-${SLUG}
  namespace: gitdoc-${SLUG}
type: Opaque
stringData:
  DISCORD_BOT_TOKEN: "<real token>"
  LITELLM_API_KEY:   "<real key>"
  POSTGRES_DSN:      "postgresql://gitdoc_${SLUG}:<pw>@<host>:5432/gitdoc_${SLUG}?sslmode=require"
  GIT_TOKEN:         ""
```

The Secret's `metadata.name` is what you'll set as
`secrets.existingSecret` in the Helm values file. Pick once, don't
change it — sealed-secrets is namespaced AND name-scoped by default.

### 2. Seal it with kubeseal

```sh
kubeseal --format yaml \
  < ~/tmp/gitdoc-${SLUG}-secret.yaml \
  > deploy/sealed-secrets/${SLUG}.yaml
```

The output is a `SealedSecret` manifest. It's cluster-encrypted — safe
to commit. Review the diff to make sure no plaintext leaked (kubeseal
never emits plaintext, but check for paranoia).

```sh
git add deploy/sealed-secrets/${SLUG}.yaml
git commit -m "feat(sealed-secrets): add ${SLUG}"
```

### 3. Apply the sealed manifest to the cluster

```sh
kubectl apply -f deploy/sealed-secrets/${SLUG}.yaml
```

The sealed-secrets controller will materialise a regular `Secret`
named `gitdoc-${SLUG}` in namespace `gitdoc-${SLUG}` within a few
seconds. Verify:

```sh
kubectl -n gitdoc-${SLUG} get secret gitdoc-${SLUG}
```

### 4. Point the Helm release at the materialised Secret

In `deploy/helm/gitdoc/values-${SLUG}.yaml`:

```yaml
secrets:
  existingSecret: gitdoc-${SLUG}
  # discordBotToken, litellmApiKey, postgresDsn, gitToken now ignored
  # — the chart does not render its own Secret.
```

Then:

```sh
make helm-install REPO=${SLUG}
```

### 5. Delete the plaintext draft

```sh
shred -u ~/tmp/gitdoc-${SLUG}-secret.yaml   # or `rm -P` on macOS
```

---

## Rotation procedures

All three rotations follow the same shape: regenerate the credential at
the source, re-seal the Secret, re-apply. The gitdoc bot's
`strategy: Recreate` (required by Discord — one gateway session per
token) means a brief gateway gap while the pod restarts; callers see
"Bot is offline" for a few seconds.

### Discord bot token

1. Discord developer portal → application → Bot → **Reset Token**. The
   old token is invalidated immediately.
2. Update the draft Secret with the new `DISCORD_BOT_TOKEN`.
3. Re-seal and re-apply:
   ```sh
   kubeseal --format yaml \
     < ~/tmp/gitdoc-${SLUG}-secret.yaml \
     > deploy/sealed-secrets/${SLUG}.yaml
   kubectl apply -f deploy/sealed-secrets/${SLUG}.yaml
   ```
4. Roll the bot to pick up the new value:
   ```sh
   kubectl -n gitdoc-${SLUG} rollout restart deployment/gitdoc-${SLUG}-bot
   ```

### LiteLLM API key

Same shape. Rotate the key in the LiteLLM proxy config first (keep the
old key valid during the overlap window if your LiteLLM supports dual
keys), then repeat steps 2-4 above. Roll **both** the bot and rag:

```sh
kubectl -n gitdoc-${SLUG} rollout restart deployment/gitdoc-${SLUG}-bot
kubectl -n gitdoc-${SLUG} rollout restart deployment/gitdoc-${SLUG}-rag
```

Ingestion picks up the new key on its next CronJob firing.

### Postgres password

1. Rotate at the source:
   ```sh
   psql "postgresql://<superuser>@<host>:5432/postgres" \
     -c "ALTER ROLE gitdoc_${SLUG} PASSWORD '<new password>';"
   ```
2. Rebuild the DSN with the new password, update the draft Secret,
   re-seal and re-apply (steps 2-3 of the token procedure).
3. Roll the rag Deployment and restart the next ingestion:
   ```sh
   kubectl -n gitdoc-${SLUG} rollout restart deployment/gitdoc-${SLUG}-rag
   ```
4. The next `gitdoc-${SLUG}-ingest` CronJob firing uses the new DSN.
   Force an immediate run if you want to verify:
   ```sh
   kubectl -n gitdoc-${SLUG} create job \
     --from=cronjob/gitdoc-${SLUG}-ingest \
     gitdoc-${SLUG}-ingest-rotate-$(date +%s)
   ```

### Git token

If the PAT expires or leaks: create a fresh PAT on GitHub, update the
draft Secret's `GIT_TOKEN`, re-seal, re-apply. Ingestion picks it up on
its next firing — no need to roll the bot or rag.

---

## Bootstrap (plaintext first deploy)

If sealed-secrets isn't installed yet and you need to get the very
first instance up, the chart accepts plaintext credentials in
`values-<slug>.yaml`:

```yaml
secrets:
  # existingSecret:      unset — chart will render its own Secret
  discordBotToken: "..."
  litellmApiKey:   "..."
  postgresDsn:     "..."
  gitToken:        ""
```

Then `make helm-install REPO=<slug>`. The chart-managed Secret is
functionally identical to the sealed one — same keys, same workloads.

Once sealed-secrets is installed cluster-wide, migrate the instance:

1. Follow the "Per-instance: seal and apply" procedure above, using
   the values currently in `values-<slug>.yaml`.
2. Flip `secrets.existingSecret` to the sealed Secret's name.
3. Clear the plaintext `secrets.discordBotToken` / `litellmApiKey` /
   `postgresDsn` / `gitToken` fields from `values-<slug>.yaml`.
4. `make helm-install REPO=<slug>` — Helm notices the chart-managed
   Secret is no longer rendered and deletes it. Workloads now envFrom
   the sealed-secrets-materialised Secret.

---

## Decommission

When removing an instance:

```sh
# Uninstall the release (removes Deployments, Service, CronJob, ConfigMap).
helm uninstall gitdoc-${SLUG} -n gitdoc-${SLUG}

# Remove the sealed-secrets-materialised Secret.
kubectl -n gitdoc-${SLUG} delete secret gitdoc-${SLUG}

# Remove the SealedSecret custom resource (belt and braces — deleting the
# namespace below does the same).
kubectl -n gitdoc-${SLUG} delete sealedsecret gitdoc-${SLUG}

# Drop the namespace.
kubectl delete namespace gitdoc-${SLUG}

# Remove the sealed manifest from the repo.
git rm deploy/sealed-secrets/${SLUG}.yaml
git commit -m "chore(sealed-secrets): decommission ${SLUG}"
```

Rotate the credentials at the source (Discord, LiteLLM, Postgres) if
shutting the instance down for security reasons — a committed sealed
manifest paired with a restored cluster key could otherwise still
decrypt the old values.

---

## Disaster recovery

Sealed manifests are encrypted with the cluster controller's private
key. If the cluster is destroyed and restored without preserving that
key, every `deploy/sealed-secrets/*.yaml` is rubble.

**Back up the sealing key.** One-time per cluster, after installing
the controller:

```sh
kubectl -n kube-system get secret \
  -l sealedsecrets.bitnami.com/sealed-secrets-key \
  -o yaml > ~/offline-backup/sealed-secrets-key.yaml
```

Store that file outside the repo, in an encrypted backup (1Password
document, LUKS-encrypted USB, etc.) — it decrypts every sealed manifest
you ever produce. If you rotate the key (the controller does this
periodically), snapshot the new one too.

To restore onto a fresh cluster:

1. Install the sealed-secrets controller per the install section.
2. `kubectl apply -f ~/offline-backup/sealed-secrets-key.yaml` before
   applying any committed `SealedSecret` manifests.
3. Restart the controller so it picks up the restored key:
   ```sh
   kubectl -n kube-system rollout restart deployment/sealed-secrets-controller
   ```
4. Re-apply `deploy/sealed-secrets/*.yaml` — decryption works again.

---

## If `kubeseal` is unavailable

Rare but possible (new operator workstation, broken brew mirror, etc.).
Two options:

- **Exec into the controller pod** and use its bundled `kubeseal`:
  ```sh
  kubectl -n kube-system exec deploy/sealed-secrets-controller -- kubeseal --help
  ```
  Pipe the draft Secret in via `kubectl exec -i`.
- **Fall back to the bootstrap path.** Revert `values-<slug>.yaml` to
  plaintext, redeploy, rotate the credentials at the source afterwards.
  Rotating resets any blast radius from the plaintext window.

Do not commit plaintext values files as a stopgap — `.gitignore`
already excludes them, and the cost of an accidental `git add -f` is
higher than a kubeseal reinstall.
