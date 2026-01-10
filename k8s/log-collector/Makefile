.PHONY: help build test deploy undeploy logs clean

IMAGE_NAME ?= nvidia-log-collector
IMAGE_TAG ?= latest
REGISTRY ?= ghcr.io/crusoecloud/crusoe-watch-agent
FULL_IMAGE := $(REGISTRY)/$(IMAGE_NAME):$(IMAGE_TAG)
NAMESPACE ?= default

help: ## Show this help message
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

build: ## Build Docker image
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .
	@echo "Built image: $(IMAGE_NAME):$(IMAGE_TAG)"

test: ## Run unit tests
	pip install -r requirements.txt
	python3 -m pytest app/test_log_collector_app.py -v

test-docker: build ## Run tests in Docker container
	docker run --rm $(IMAGE_NAME):$(IMAGE_TAG) python3 -m pytest test_log_collector_app.py -v

push: build ## Push image to registry
	docker tag $(IMAGE_NAME):$(IMAGE_TAG) $(FULL_IMAGE)
	docker push $(FULL_IMAGE)
	@echo "Pushed image: $(FULL_IMAGE)"

deploy: ## Deploy to Kubernetes
	kubectl apply -f manifests/rbac.yaml
	kubectl apply -f manifests/configmap.yaml
	kubectl apply -f manifests/daemonset.yaml
	@echo "Deployed nvidia-log-collector to namespace: $(NAMESPACE)"

undeploy: ## Remove from Kubernetes
	kubectl delete -f manifests/daemonset.yaml --ignore-not-found
	kubectl delete -f manifests/configmap.yaml --ignore-not-found
	kubectl delete -f manifests/rbac.yaml --ignore-not-found
	@echo "Removed nvidia-log-collector from namespace: $(NAMESPACE)"

status: ## Show deployment status
	@echo "=== DaemonSet Status ==="
	kubectl get daemonset nvidia-log-collector -n $(NAMESPACE) || echo "DaemonSet not found"
	@echo ""
	@echo "=== Pods ==="
	kubectl get pods -n $(NAMESPACE) -l app=nvidia-log-collector
	@echo ""
	@echo "=== Recent Events ==="
	kubectl get events -n $(NAMESPACE) --field-selector involvedObject.name=nvidia-log-collector --sort-by='.lastTimestamp' | tail -10

logs: ## Show logs from collector pods
	@echo "Fetching logs from all nvidia-log-collector pods..."
	kubectl logs -n $(NAMESPACE) -l app=nvidia-log-collector --tail=50 --all-containers=true

logs-follow: ## Follow logs from collector pods
	kubectl logs -n $(NAMESPACE) -l app=nvidia-log-collector -f --all-containers=true

exec: ## Exec into a collector pod (requires POD_NAME env var)
	@if [ -z "$(POD_NAME)" ]; then \
		echo "Error: POD_NAME environment variable not set"; \
		echo "Usage: make exec POD_NAME=<pod-name>"; \
		exit 1; \
	fi
	kubectl exec -it -n $(NAMESPACE) $(POD_NAME) -- /bin/bash

list-logs: ## List collected log files
	@echo "Collected logs in pods:"
	@kubectl get pods -n $(NAMESPACE) -l app=nvidia-log-collector -o name | xargs -I {} sh -c 'echo "\n=== {} ===" && kubectl exec -n $(NAMESPACE) {} -- ls -lh /logs'

download-logs: ## Download all logs from pods to local ./collected-logs/ directory
	@mkdir -p collected-logs
	@kubectl get pods -n $(NAMESPACE) -l app=nvidia-log-collector -o jsonpath='{.items[*].metadata.name}' | \
		xargs -n1 -I {} sh -c 'echo "Downloading logs from {}..." && kubectl cp $(NAMESPACE)/{}:/logs collected-logs/{} 2>/dev/null || echo "  No logs in {}"'
	@echo "Logs downloaded to ./collected-logs/"

run-once: ## Trigger a one-time collection
	kubectl set env daemonset/nvidia-log-collector -n $(NAMESPACE) RUN_ONCE=true
	kubectl rollout restart daemonset/nvidia-log-collector -n $(NAMESPACE)
	@echo "Triggered one-time collection. Check logs with 'make logs'"

clean: ## Clean up local artifacts
	rm -rf collected-logs
	rm -rf __pycache__ app/__pycache__
	rm -rf .pytest_cache
	@echo "Cleaned up local artifacts"