# ---------------------------------------------------------------------------
# gitdoc Makefile
#
# Registry is INTENTIONALLY left as a placeholder (ghcr.io/you) until the
# registry decision is made. See implementation_plans/working-memory.md
# ("Registry" open question) and deploy/REGISTRY.md for the three options
# under consideration (GHCR / Harbor / local registry:2). Override with:
#
#   make build REGISTRY=ghcr.io/<org>
#   make buildx-push REGISTRY=ghcr.io/<org>
# ---------------------------------------------------------------------------

REGISTRY  ?= ghcr.io/you
VERSION   ?= 0.1.0
SERVICES  := discord-bot rag-orchestrator ingestion
PLATFORMS ?= linux/amd64,linux/arm64

# Resolved short SHA — empty when not in a git repo (e.g. tarball checkout).
GIT_SHA := $(shell git rev-parse --short HEAD 2>/dev/null)

# TAG defaults to <semver>-<short-sha>; falls back to plain semver outside git.
ifeq ($(strip $(GIT_SHA)),)
TAG ?= $(VERSION)
else
TAG ?= $(VERSION)-$(GIT_SHA)
endif

.PHONY: build buildx-push push print-tag lint helm-lint helm-template helm-install test

## print-tag: echo the resolved image tag (useful for CI)
print-tag:
	@echo $(TAG)

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
##               -r services/rag-orchestrator/requirements.txt
test:
	pytest

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
