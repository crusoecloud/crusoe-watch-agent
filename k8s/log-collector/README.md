# NVIDIA GPU Driver Log Collector

A Kubernetes DaemonSet application that collects NVIDIA GPU driver bug reports from nodes running NVIDIA GPU drivers.

## Overview

This application runs as a DaemonSet in your Kubernetes cluster and periodically collects diagnostic logs from NVIDIA GPU driver pods using the `nvidia-bug-report.sh` utility. The logs are stored locally in the collector pod and can be accessed for troubleshooting GPU-related issues.

## Features

- **Automatic Discovery**: Finds NVIDIA GPU driver pods running on the same node
- **VM ID Auto-Detection**: Automatically reads VM ID from DMI (`/sys/class/dmi/id/product_uuid`)
- **Log Collection**: Executes `nvidia-bug-report.sh` in the driver pod
- **File Download**: Transfers generated log files to the collector pod
- **Flexible Execution Modes**:
  - **Scheduled Mode**: Runs on a configurable schedule (default)
  - **API-Driven Mode**: Polls an API and collects logs on-demand
  - **One-Time Mode**: Runs once and exits
- **Multi-Node Support**: Deploys as DaemonSet for cluster-wide coverage
- **Timeout Handling**: Automatic timeout protection (5 minutes default)
- **Automatic Cleanup**: Keeps only the most recent logs to prevent disk exhaustion

## Execution Modes

### Scheduled Mode (Default)
The collector runs periodically at a fixed interval (`COLLECTION_INTERVAL`). This is the default mode when `API_ENABLED=false`.

### API-Driven Mode
When `API_ENABLED=true`, the collector operates in event-driven mode:
1. Polls the API endpoint (`/check-tasks?vm_id=<VM_ID>`) at regular intervals
2. When the API returns a task with an `event_id`, collection begins
3. Log filename includes the `event_id` for tracking
4. After collection, results are reported via `/upload-logs` endpoint (single call for both upload and status)
5. Automatic timeout handling after 5 minutes (configurable)

**API Endpoints:**
- `GET /check-tasks?vm_id=<VM_ID>` - Check for pending collection tasks
  - Response: `{"status": "success", "event_id": "12345"}` if task available
- `POST /upload-logs` - Report collection results (combines upload and status)
  - **Success case** (multipart/form-data): `file`, `vm_id`, `event_id`, `node_name`, `status` ("success"), `message`
  - **Failure case** (JSON): `vm_id`, `event_id`, `status` ("failed"), `message`, `node_name`

### One-Time Mode
Set `RUN_ONCE=true` to collect logs once and exit. Useful for testing or manual collection.

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
- Access to `/sys/class/dmi/id/product_uuid` on the host (for VM_ID auto-detection)

## Configuration

The application is configured via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `NODE_NAME` | *required* | Name of the node (injected via downward API) |
| `VM_ID` | *auto-detected* | Unique VM identifier (auto-read from `/host/sys/class/dmi/id/product_uuid` if not set; required for API-driven mode) |
| `LOG_OUTPUT_DIR` | `/logs` | Directory to store collected logs |
| `NVIDIA_NAMESPACE` | `nvidia-gpu-operator` | Namespace where GPU driver pods run |
| `NVIDIA_DRIVER_POD_PREFIX` | `nvidia-gpu-driver` | Prefix of GPU driver pod names |
| `COLLECTION_INTERVAL` | `3600` | Seconds between collections (1 hour) - used in scheduled mode |
| `RUN_ONCE` | `false` | If true, run once and exit |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `MAX_LOGS_TO_KEEP` | `5` | Maximum number of old logs to keep (prevents disk space issues) |
| `API_ENABLED` | `false` | Enable API-driven mode instead of scheduled collection |
| `API_BASE_URL` | `https://cms-logging.com` | Base URL for the log collection API |
| `API_POLL_INTERVAL` | `60` | Seconds between API polls for new tasks |
| `COLLECTION_TIMEOUT` | `300` | Maximum seconds (5 minutes) for log collection before timeout |

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
        - name: host-sys
          mountPath: /host/sys
          readOnly: true
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
      - name: host-sys
        hostPath:
          path: /sys
          type: Directory
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

### Scheduled Collection (Default)

Default behavior - runs every hour (configurable via `COLLECTION_INTERVAL`):

```bash
# Deploy with default scheduled mode
kubectl apply -f k8s/log-collector/manifests/
```

### API-Driven Collection

Enable API-driven mode to collect logs on-demand based on API tasks:

```bash
kubectl set env daemonset/nvidia-log-collector \
  API_ENABLED=true \
  API_BASE_URL=https://cms-logging.com \
  API_POLL_INTERVAL=60 \
  COLLECTION_TIMEOUT=300 \
  VM_ID=<your-vm-id> \
  -n default
```

In this mode, the collector will:
- Poll the API every 60 seconds for new tasks
- Execute log collection when an `event_id` is received
- Include the `event_id` in the log filename
- Report results to the API in a single call (upload logs + status for success, or just status for failure)
- Timeout after 5 minutes if collection takes too long

### Manual Execution (One-time Collection)

Deploy with `RUN_ONCE=true`:

```bash
kubectl set env daemonset/nvidia-log-collector RUN_ONCE=true -n default
```

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

## CI/CD

The log collector has automated CI/CD pipelines that build and push Docker images:

### GitHub Actions (Primary)

The repository uses GitHub Actions for CI/CD. The workflow is defined in `.github/workflows/log-collector-ci.yml`:

- **Triggers**: Pushes to `main`, pull requests, or version tags (`v*`)
- **Test Stage**: Runs unit tests with Python 3.11
- **Build Stage**: Builds Docker image and pushes to GitHub Container Registry (GHCR)
- **Image Tags**:
  - `latest` - Latest release from main branch
  - `main` - Latest commit on main branch
  - `v1.2.3` - Semantic version tags
  - `pr-123` - Pull request builds
  - `main-abc1234` - Commit SHA tags

Images are pushed to: `ghcr.io/crusoecloud/crusoe-watch-agent/nvidia-log-collector`

### GitLab CI (Alternative)

A GitLab CI configuration is also provided in `.gitlab-ci.yml`:

- **Stages**: `test`, `build`
- **Test Job**: Runs unit tests
- **Build Job**: Builds and pushes Docker images to GitLab Container Registry
- **Registry Variables**:
  - `CI_REGISTRY` - GitLab container registry URL
  - `CI_REGISTRY_USER` - Registry username
  - `CI_REGISTRY_PASSWORD` - Registry password (set in GitLab CI/CD settings)

### Manual Build

To build the Docker image locally:

```bash
cd k8s/log-collector
docker build -t nvidia-log-collector:dev .
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

**Scheduled/One-Time Mode:**
```
nvidia-bug-report-<node-name>-<timestamp>.log.gz
```

**API-Driven Mode:**
```
nvidia-bug-report-<node-name>-<event-id>-<timestamp>.log.gz
```

Examples:
```
nvidia-bug-report-gpu-node-1-20260106_143022.log.gz
nvidia-bug-report-gpu-node-1-evt-12345-20260106_143022.log.gz
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