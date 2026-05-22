NAMESPACE ?= llmmllab
REGISTRY  ?= 192.168.0.71:31500
IMAGE     ?= $(REGISTRY)/mcp-server-web
TAG       ?= latest
APP       ?= mcp-server-web

.PHONY: install start start-http test validate build push deploy logs restart clean

## ── Development ──────────────────────────────────────────────

install: ## Install Python dependencies
	uv sync

start: ## Run MCP server locally (stdio transport)
	MCP_TRANSPORT=stdio uv run python server.py

start-http: ## Run MCP server locally (HTTP transport, port 8000)
	uv run python server.py

test: ## Run tests
	uv run pytest

validate: ## Type-check with pyright
	uv run pyright

## ── Docker ───────────────────────────────────────────────────

build: ## Build multi-arch Docker image
	docker buildx build --platform linux/amd64,linux/arm64 -t $(IMAGE):$(TAG) .

push: ## Build and push multi-arch Docker image to registry
	docker buildx build --platform linux/amd64,linux/arm64 -t $(IMAGE):$(TAG) --push .

## ── Kubernetes ───────────────────────────────────────────────

deploy: push ## Build, push, and deploy to k8s (full install.sh)
	./k8s/install.sh

apply: ## Apply k8s manifests only (no build/push)
	kubectl apply -f k8s/deployment.yaml

restart: ## Rolling restart of the deployment
	kubectl rollout restart deployment/$(APP) -n $(NAMESPACE)

logs: ## Tail pod logs
	kubectl logs -n $(NAMESPACE) -l app=$(APP) -f --tail=100

status: ## Show pod status
	kubectl get pods -n $(NAMESPACE) -l app=$(APP) -o wide
