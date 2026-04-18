REGISTRY ?= ghcr.io/you
TAG      ?= 0.1.0
SERVICES := discord-bot rag-orchestrator ingestion

.PHONY: build push lint helm-lint helm-template helm-install

build:
	@for svc in $(SERVICES); do \
		echo "=> building $$svc"; \
		docker build -t $(REGISTRY)/gitdoc-$$svc:$(TAG) services/$$svc || exit 1; \
	done

push:
	@for svc in $(SERVICES); do \
		docker push $(REGISTRY)/gitdoc-$$svc:$(TAG) || exit 1; \
	done

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
