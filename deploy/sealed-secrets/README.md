# deploy/sealed-secrets

Drop sealed-secrets manifests here. They're safe to commit — sealed-secrets
encrypts with the cluster controller's public key; only the controller
can decrypt.

## Naming

One file per gitdoc instance, matching the slug:

```
deploy/sealed-secrets/<slug>.yaml
```

The `SealedSecret`'s `metadata.name` (and the materialised Secret's name)
should also be `gitdoc-<slug>`, which is what you then set as
`secrets.existingSecret` in `deploy/helm/gitdoc/values-<slug>.yaml`.

## Procedure

See `deploy/SECRETS.md` for the full kubeseal workflow: draft plaintext
Secret → `kubeseal --format yaml < draft > <slug>.yaml` → commit → apply.

## .gitignore

Nothing in this directory is gitignored. `deploy/helm/gitdoc/values-*.yaml`
is gitignored (those contain plaintext during bootstrap), but sealed
manifests here are deliberately tracked.
