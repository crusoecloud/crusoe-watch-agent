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

## Upgrading

Check the latest available version and update your local chart index:

```bash
helm search repo crusoe-watch-agent/crusoe-watch-agent --versions | head -n 2
helm repo update
```

Upgrade to the latest version:

```bash
helm upgrade crusoe-watch-agent crusoe-watch-agent/crusoe-watch-agent --namespace crusoe-system
```

Verify the upgrade:

```bash
kubectl get pods -n crusoe-system
```

### Resetting to chart defaults

If previously set values are causing issues (e.g. container images not updating), use `--reset-values` to discard all previously set values and use only the chart defaults:

```bash
helm upgrade crusoe-watch-agent crusoe-watch-agent/crusoe-watch-agent \
  --namespace crusoe-system \
  --reset-values
```

## Troubleshooting

### Checking installed versions

Show the installed chart and app version:

```bash
helm list --namespace crusoe-system --filter '^crusoe-watch-agent$'
```

The `CHART` column shows the chart version (e.g. `crusoe-watch-agent-0.4.2`) and `APP VERSION` shows the agent version.

Show the container images actually running on the cluster (resolves any `kubectl set image` / overlay pins):

```bash
kubectl get ds crusoe-watch-agent --namespace crusoe-system \
  -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"\t"}{.image}{"\n"}{end}'
```

Show the full rendered values currently in use:

```bash
helm get values crusoe-watch-agent --namespace crusoe-system --all
```
