# NVIDIA GPU Driver Log Collector

A Kubernetes DaemonSet application that collects NVIDIA GPU driver bug reports from nodes running NVIDIA GPU drivers.

## Overview

This application runs as a DaemonSet in your Kubernetes cluster and periodically collects diagnostic logs from NVIDIA GPU driver pods using the `nvidia-bug-report.sh` utility. The logs are stored locally in the collector pod and can be accessed for troubleshooting GPU-related issues.

## Features

- **Automatic Discovery**: Finds NVIDIA GPU driver pods running on the same node
- **Log Collection**: Executes `nvidia-bug-report.sh` in the driver pod
- **File Download**: Transfers generated log files to the collector pod
- **Periodic Collection**: Runs on a configurable schedule or one-time
- **Multi-Node Support**: Deploys as DaemonSet for cluster-wide coverage

## Architecture

```
┌─────────────────────────────────────────┐
│         Kubernetes Node                  │
├─────────────────────────────────────────┤
│                                          │
│  ┌───────────────────────────────┐      │
│  │  nvidia-gpu-operator NS       │      │
│  │  ┌─────────────────────────┐  │      │
│  │  │ nvidia-gpu-driver pod   │  │      │
│  │  │ - nvidia-bug-report.sh  │◄─┼──┐   │
│  │  └─────────────────────────┘  │  │   │
│  └───────────────────────────────┘  │   │
│                                      │   │
│  ┌───────────────────────────────┐  │   │
│  │  default NS (or custom)       │  │   │
│  │  ┌─────────────────────────┐  │  │   │
│  │  │ log-collector pod       │  │  │   │
│  │  │ - Executes command ─────┼──┼──┘   │
│  │  │ - Downloads logs        │  │      │
│  │  │ - Stores in /logs       │  │      │
│  │  └─────────────────────────┘  │      │
│  └───────────────────────────────┘      │
└─────────────────────────────────────────┘
```

## Requirements

- Kubernetes cluster with NVIDIA GPU nodes
- NVIDIA GPU Operator installed (or standalone GPU driver pods)
- RBAC permissions for pod listing and exec operations
- Python 3.11+ (in container)

## Configuration

The application is configured via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `NODE_NAME` | *required* | Name of the node (injected via downward API) |
| `LOG_OUTPUT_DIR` | `/logs` | Directory to store collected logs |
| `NVIDIA_NAMESPACE` | `nvidia-gpu-operator` | Namespace where GPU driver pods run |
| `NVIDIA_DRIVER_POD_PREFIX` | `nvidia-gpu-driver` | Prefix of GPU driver pod names |
| `COLLECTION_INTERVAL` | `3600` | Seconds between collections (1 hour) |
| `RUN_ONCE` | `false` | If true, run once and exit |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `MAX_LOGS_TO_KEEP` | `5` | Maximum number of old logs to keep (prevents disk space issues) |

## Deployment

### Using Helm (Recommended)

```bash
helm install nvidia-log-collector ./k8s/log-collector/helm \
  --namespace default \
  --create-namespace
```

### Using kubectl

```bash
kubectl apply -f k8s/log-collector/manifests/
```

### DaemonSet Example

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: nvidia-log-collector
  namespace: default
spec:
  selector:
    matchLabels:
      app: nvidia-log-collector
  template:
    metadata:
      labels:
        app: nvidia-log-collector
    spec:
      serviceAccountName: nvidia-log-collector
      containers:
      - name: log-collector
        image: ghcr.io/crusoecloud/crusoe-watch-agent/nvidia-log-collector:latest
        env:
        - name: NODE_NAME
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        - name: COLLECTION_INTERVAL
          value: "3600"  # 1 hour
        - name: LOG_LEVEL
          value: "INFO"
        volumeMounts:
        - name: logs
          mountPath: /logs
        resources:
          requests:
            cpu: 100m
            memory: 128Mi
          limits:
            cpu: 500m
            memory: 512Mi
      volumes:
      - name: logs
        hostPath:
          path: /var/log/nvidia-bug-reports
          type: DirectoryOrCreate
      nodeSelector:
        nvidia.com/gpu: "true"  # Only run on GPU nodes
```

### RBAC Requirements

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: nvidia-log-collector
  namespace: default
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: nvidia-log-collector
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: nvidia-log-collector
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: nvidia-log-collector
subjects:
- kind: ServiceAccount
  name: nvidia-log-collector
  namespace: default
```

## Usage

### Manual Execution (One-time Collection)

Deploy with `RUN_ONCE=true`:

```bash
kubectl set env daemonset/nvidia-log-collector RUN_ONCE=true -n default
```

### Periodic Collection

Default behavior - runs every hour (configurable via `COLLECTION_INTERVAL`).

### Accessing Collected Logs

Logs are stored in `/logs` within the container. To access them:

```bash
# List collected logs
kubectl exec -n default nvidia-log-collector-<pod-id> -- ls -lh /logs

# Copy log to local machine
kubectl cp default/nvidia-log-collector-<pod-id>:/logs/nvidia-bug-report-node1-20260106_143022.log.gz ./
```

If using hostPath volume:

```bash
# Logs are available on the node at /var/log/nvidia-bug-reports
```

## Development

### Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export NODE_NAME=test-node
export LOG_OUTPUT_DIR=/tmp/nvidia-logs
export NVIDIA_NAMESPACE=nvidia-gpu-operator
export RUN_ONCE=true

# Run the collector (requires kubeconfig)
python3 app/log_collector_app.py
```

### Running Tests

```bash
# Install test dependencies
pip install pytest

# Run tests
pytest app/test_log_collector_app.py -v
```

### Building Docker Image

```bash
cd k8s/log-collector
docker build -t nvidia-log-collector:dev .

# Test the image
docker run --rm \
  -e NODE_NAME=test-node \
  -e RUN_ONCE=true \
  nvidia-log-collector:dev
```

## Troubleshooting

### Pod can't find NVIDIA driver pod

**Symptom**: Log shows "No running NVIDIA driver pod found"

**Solutions**:
1. Verify NVIDIA GPU Operator is installed:
   ```bash
   kubectl get pods -n nvidia-gpu-operator
   ```
2. Check driver pod naming - update `NVIDIA_DRIVER_POD_PREFIX` if different
3. Verify the collector pod is on a GPU node:
   ```bash
   kubectl get pod <collector-pod> -o jsonpath='{.spec.nodeName}'
   ```

### Permission denied errors

**Symptom**: "Forbidden: pod exec is not allowed"

**Solutions**:
1. Verify RBAC is correctly configured:
   ```bash
   kubectl auth can-i create pods/exec --as=system:serviceaccount:default:nvidia-log-collector
   ```
2. Check ServiceAccount is attached to the pod:
   ```bash
   kubectl get pod <collector-pod> -o jsonpath='{.spec.serviceAccountName}'
   ```

### nvidia-bug-report.sh not found

**Symptom**: "nvidia-bug-report.sh: command not found"

**Solutions**:
1. Verify the driver container has the script:
   ```bash
   kubectl exec -n nvidia-gpu-operator <driver-pod> -- which nvidia-bug-report.sh
   ```
2. Check the correct container is being targeted (update `_get_driver_container_name` logic if needed)

### Logs not downloading

**Symptom**: "No data received when downloading"

**Solutions**:
1. Check if log was generated in the driver pod:
   ```bash
   kubectl exec -n nvidia-gpu-operator <driver-pod> -- ls -lh /tmp/nvidia-bug-report-*.log.gz
   ```
2. Verify sufficient disk space in driver pod
3. Check network connectivity between namespaces

## Log File Format

Generated log files follow this naming convention:
```
nvidia-bug-report-<node-name>-<timestamp>.log.gz
```

Example:
```
nvidia-bug-report-gpu-node-1-20260106_143022.log.gz
```

The log file is a compressed tar.gz containing:
- GPU driver information
- System configuration
- GPU status and diagnostics
- Recent kernel logs
- NVIDIA driver logs

## Performance Considerations

- **Execution time**: nvidia-bug-report.sh typically takes 30-60 seconds
- **Log file size**: 5-50 MB compressed (varies by configuration)
- **Resource usage**:
  - CPU: ~100-200m during collection, idle otherwise
  - Memory: ~100-200 MB
  - Network: Minimal (only during log transfer)

## Security Considerations

1. **Privileged Access**: Requires exec access to GPU driver pods
2. **Namespace Access**: Reads pods across namespaces (nvidia-gpu-operator)
3. **Log Contents**: May contain sensitive system information
4. **Storage**: Secure the log storage location appropriately

## Contributing

When making changes:
1. Update tests in `test_log_collector_app.py`
2. Run tests locally before submitting
3. Update this README if adding features
4. Follow the existing code style

## License

Same as crusoe-watch-agent repository.