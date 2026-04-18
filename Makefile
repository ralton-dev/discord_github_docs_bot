# ---------------------------------------------------------------------------
# gitdoc Makefile
#
# Default registry is ghcr.io/ralton-dev (public images, no regcred needed).
# Override for forks or alternate registries:
#
#   make build REGISTRY=ghcr.io/<org>
#   make buildx-push REGISTRY=ghcr.io/<org>
#
# Versioning: the most recent annotated or lightweight git tag is the
# single source of truth. `make bump-version VERSION=<semver>` tags the
# repo and rewrites Chart.yaml + values.yaml image defaults atomically
# so the chart, the Makefile, and CI agree on the version forever.
#
# CI publishes on tag push (see .github/workflows/release.yml); local
# build targets are mostly for ad-hoc verification.
# ---------------------------------------------------------------------------

REGISTRY  ?= ghcr.io/ralton-dev
SERVICES  := discord-bot rag ingestion
PLATFORMS ?= linux/amd64,linux/arm64

# Resolved short SHA — empty when not in a git repo (e.g. tarball checkout).
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null)

# Version resolution. Use the latest git tag as the semver source of truth;
# `v0.1.0` → `0.1.0`, etc. Outside a tagged checkout (or outside git
# entirely) fall back to `0.0.0-dev` so local builds still produce a
# well-formed tag string. Override on the command line for one-off builds:
#   make build VERSION=0.2.0
VERSION ?= $(shell \
  v=$$(git describe --tags --abbrev=0 2>/dev/null); \
  if [ -n "$$v" ]; then echo $${v\#v}; else echo 0.0.0-dev; fi \
)

# TAG defaults to <semver>-<short-sha>; falls back to plain semver outside git.
ifeq ($(strip $(GIT_SHA)),)
TAG ?= $(VERSION)
else
TAG ?= $(VERSION)-$(GIT_SHA)
endif

.PHONY: build buildx-push push print-tag print-version bump-version lint helm-lint helm-template helm-install test integration-test

## print-version: echo the resolved semver (from the latest git tag)
print-version:
	@echo $(VERSION)

## print-tag: echo the resolved image tag (useful for CI)
print-tag:
	@echo $(TAG)

## bump-version: cut a new release. Rewrites Chart.yaml + values.yaml
## image defaults and creates an annotated tag `v<VERSION>`. Usage:
##   make bump-version VERSION=0.2.0
## Pushes are left to the operator:
##   git push origin v<VERSION>   # triggers .github/workflows/release.yml
bump-version:
	@test -n "$(VERSION)" || (echo "usage: make bump-version VERSION=<semver>"; exit 1)
	@case "$(VERSION)" in *-dev) \
		echo "refusing to tag a dev placeholder ($(VERSION)); pass VERSION=x.y.z"; exit 1;; esac
	@if git rev-parse "v$(VERSION)" >/dev/null 2>&1; then \
		echo "tag v$(VERSION) already exists"; exit 1; fi
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "working tree dirty; commit or stash before bumping"; exit 1; fi
	@echo "=> rewriting Chart.yaml version + appVersion to $(VERSION)"
	@sed -i.bak -E 's/^(version:).*/\1 $(VERSION)/' deploy/helm/gitdoc/Chart.yaml
	@sed -i.bak -E 's/^(appVersion:).*/\1 "$(VERSION)"/' deploy/helm/gitdoc/Chart.yaml
	@echo "=> rewriting values.yaml image tag defaults to $(VERSION)"
	@sed -i.bak -E 's/(^    tag: )"[^"]+"/\1"$(VERSION)"/' deploy/helm/gitdoc/values.yaml
	@rm -f deploy/helm/gitdoc/Chart.yaml.bak deploy/helm/gitdoc/values.yaml.bak
	@git add deploy/helm/gitdoc/Chart.yaml deploy/helm/gitdoc/values.yaml
	@git commit -m "chore(release): bump version to $(VERSION)"
	@git tag -a "v$(VERSION)" -m "Release v$(VERSION)"
	@echo
	@echo "Bumped to v$(VERSION). Next:"
	@echo "  git push origin main"
	@echo "  git push origin v$(VERSION)   # triggers release.yml"

## build: single-arch local build (host arch). Fast, loads into local daemon.
build:
	@for svc in $(SERVICES); do \
		echo "=> building $$svc ($(REGISTRY)/gitdoc-$$svc:$(TAG))"; \
		docker build -t $(REGISTRY)/gitdoc-$$svc:$(TAG) services/$$svc || exit 1; \
	done

## push: push already-built single-arch images to the registry.
push:
	@for svc in $(SERVICES); do \
		docker push $(REGISTRY)/gitdoc-$$svc:$(TAG) || exit 1; \
	done

## buildx-push: multi-arch (linux/amd64, linux/arm64) build AND push in one step.
## Multi-platform manifests cannot be loaded into the local daemon, so we push
## directly. Requires `docker buildx create --use` to have been run once.
buildx-push:
	@docker buildx inspect --bootstrap >/dev/null 2>&1 || { \
		echo "docker buildx not configured; run: docker buildx create --use"; exit 1; }
	@for svc in $(SERVICES); do \
		echo "=> buildx ($(PLATFORMS)) push $$svc ($(REGISTRY)/gitdoc-$$svc:$(TAG))"; \
		docker buildx build \
			--platform=$(PLATFORMS) \
			--tag $(REGISTRY)/gitdoc-$$svc:$(TAG) \
			--push \
			services/$$svc || exit 1; \
	done

## test: run the pytest unit-test suite from the repo root.
## Install test deps first:
##   pip install -r requirements-dev.txt \
##               -r services/ingestion/requirements.txt \
##               -r services/discord-bot/requirements.txt \
##               -r services/rag/requirements.txt
##
## Integration tests (Docker-backed) are excluded by pytest.ini's default
## addopts; run them via `make integration-test`.
test:
	pytest

## integration-test: run the Docker-backed end-to-end suite.
## Requires a running Docker daemon. See tests/integration/README.md.
integration-test:
	pytest -m integration -o addopts="-q" tests/integration

helm-lint:
	helm lint deploy/helm/gitdoc

helm-template:
	helm template gitdoc-example deploy/helm/gitdoc \
		-f deploy/helm/gitdoc/values-example.yaml

# Install one instance per repo:
#   make helm-install REPO=project-a
helm-install:
	@test -n "$(REPO)" || (echo "usage: make helm-install REPO=<slug>"; exit 1)
	helm upgrade --install gitdoc-$(REPO) deploy/helm/gitdoc \
		--namespace gitdoc-$(REPO) --create-namespace \
		-f deploy/helm/gitdoc/values-$(REPO).yaml
