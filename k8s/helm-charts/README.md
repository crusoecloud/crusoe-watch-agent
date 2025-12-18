# Crusoe Watch Agent (Helm)
This chart:
1. runs a job to generate and store a monitoring token.
2. deploys vector.dev based telemetry agent with a vector-config-reloader container.

## Quickstart

Get credentials for your CMK cluster:

```bash
crusoe kubernetes clusters get-credentials <cluster-name> --project-id <project-id>

kubectl config current-context  # validate your current context
```

Install agent: 
```bash
helm repo add crusoe-watch-agent https://crusoecloud.github.io/crusoe-watch-agent/k8s/helm-charts

helm repo update

helm install crusoe-watch-agent crusoe-watch-agent/crusoe-watch-agent --namespace crusoe-system
```
