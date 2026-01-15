# NVIDIA GPU Driver Log Collector

A Kubernetes DaemonSet application that collects NVIDIA GPU driver bug reports from nodes running NVIDIA GPU drivers.

## Overview

This application runs as a separate DaemonSet (`crusoe-log-collector`) in your Kubernetes cluster and periodically collects diagnostic logs from NVIDIA GPU driver pods using the `nvidia-bug-report.sh` utility. The logs are stored locally in the collector pod and can be accessed for troubleshooting GPU-related issues.

**Deployment:** Integrated into the `crusoe-watch-agent` Helm chart as a standalone DaemonSet with its own ServiceAccount and RBAC.

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
6. **Authentication**: All API calls use Bearer token authentication with `CRUSOE_MONITORING_TOKEN`

**API Endpoints:**
- `GET /check-tasks?vm_id=<VM_ID>` - Check for pending collection tasks
  - Response: `{"status": "success", "event_id": "12345"}` if task available
  - **Headers**: `Authorization: Bearer <CRUSOE_MONITORING_TOKEN>`
- `POST /upload-logs` - Report collection results (combines upload and status)
  - **Success case** (multipart/form-data): `file`, `vm_id`, `event_id`, `node_name`, `status` ("success"), `message`
  - **Failure case** (JSON): `vm_id`, `event_id`, `status` ("failed"), `message`, `node_name`
  - **Headers**: `Authorization: Bearer <CRUSOE_MONITORING_TOKEN>`

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
| `CRUSOE_MONITORING_TOKEN` | *from secret* | Authentication token for API calls (injected from `crusoe-monitoring-token` secret; required for API-driven mode) |
| `LOG_OUTPUT_DIR` | `/logs` | Directory to store collected logs |
| `NVIDIA_NAMESPACE` | `nvidia-gpu-operator` | Namespace where GPU driver pods run |
| `NVIDIA_DRIVER_POD_PREFIX` | `nvidia-gpu-driver` | Prefix of GPU driver pod names |
| `COLLECTION_INTERVAL` | `3600` | Seconds between collections (1 hour) - used in scheduled mode |
| `RUN_ONCE` | `false` | If true, run once and exit |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `MAX_LOGS_TO_KEEP` | `1` | Maximum number of old logs to keep (prevents disk space issues) |
| `API_ENABLED` | `false` | Enable API-driven mode instead of scheduled collection |
| `API_BASE_URL` | `https://cms-logging.com` | Base URL for the log collection API |
| `API_POLL_INTERVAL` | `60` | Seconds between API polls for new tasks |
| `COLLECTION_TIMEOUT` | `300` | Maximum seconds (5 minutes) for log collection before timeout |

## Deployment

### Via crusoe-watch-agent Helm Chart (Recommended)

The log collector is integrated into the `crusoe-watch-agent` Helm chart. Enable it in your `values.yaml`:

```yaml
logCollector:
  enabled: true  # Set to true to deploy the log collector
```

Then install or upgrade the chart:

```bash
# Install
helm install crusoe-watch-agent ./k8s/helm-charts \
  --namespace crusoe-system \
  --create-namespace

# Upgrade existing installation
helm upgrade crusoe-watch-agent ./k8s/helm-charts \
  --namespace crusoe-system
```

The log collector will be deployed as a separate DaemonSet (`crusoe-log-collector`) with its own ServiceAccount and RBAC.

### DaemonSet Configuration Example

The log collector DaemonSet is configured via Helm values. Key configuration in `values.yaml`:

```yaml
logCollector:
  enabled: true
  name: crusoe-log-collector

  image:
    repository: ghcr.io/crusoecloud/crusoe-watch-agent/log-collector
    tag: pr-12
    pullPolicy: IfNotPresent

  env:
    LOG_OUTPUT_DIR: "/logs"
    NVIDIA_NAMESPACE: "nvidia-gpu-operator"
    NVIDIA_DRIVER_POD_PREFIX: "nvidia-gpu-driver"
    COLLECTION_INTERVAL: "3600"
    RUN_ONCE: "false"
    LOG_LEVEL: "INFO"
    MAX_LOGS_TO_KEEP: "1"
    API_ENABLED: "false"
    API_BASE_URL: "https://cms-logging.com"
    API_POLL_INTERVAL: "60"
    COLLECTION_TIMEOUT: "300"

  resources:
    requests:
      cpu: 50m
      memory: 64Mi
    limits:
      cpu: 100m
      memory: 128Mi

  securityContext:
    runAsUser: 0  # Run as root to read DMI files
    allowPrivilegeEscalation: false
    capabilities:
      drop:
        - ALL

  volumeMounts:
  - name: logs
    mountPath: /logs
  - name: sysfs
    mountPath: /host/sys
    readOnly: true

  volumes:
  - name: logs
    hostPath:
      path: /var/log/nvidia-bug-reports
      type: DirectoryOrCreate
  - name: sysfs
    hostPath:
      path: /sys
      type: Directory

  nodeSelector:
    nvidia.com/gpu: "true"
```

**Key Features:**
- **Separate ServiceAccount**: Uses `crusoe-log-collector` ServiceAccount (not shared with Vector)
- **Root Access**: Runs as `runAsUser: 0` to read DMI files (`/sys/class/dmi/id/product_uuid`)
- **Secret Access**: Automatically mounts `crusoe-monitoring-token` secret for API authentication
- **Resource Limits**: Conservative resource allocation (50m/64Mi requests, 100m/128Mi limits)

### RBAC Requirements

The log collector uses a dedicated ServiceAccount with minimal required permissions:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: crusoe-log-collector
  namespace: crusoe-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: crusoe-log-collector
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["create", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: crusoe-log-collector
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: crusoe-log-collector
subjects:
- kind: ServiceAccount
  name: crusoe-log-collector
  namespace: crusoe-system
```

**Important**: The log collector uses its own ServiceAccount (`crusoe-log-collector`), separate from the Vector agent's ServiceAccount (`crusoe-watch-agent`). This prevents RBAC conflicts when both components are deployed together.

## Usage

### Scheduled Collection (Default)

Default behavior - runs every hour (configurable via `COLLECTION_INTERVAL` in Helm values):

```bash
# Deploy with default scheduled mode (via Helm)
helm upgrade crusoe-watch-agent ./k8s/helm-charts \
  --namespace crusoe-system \
  --set logCollector.enabled=true
```

### API-Driven Collection

Enable API-driven mode to collect logs on-demand based on API tasks. Update your Helm values:

```yaml
logCollector:
  enabled: true
  env:
    API_ENABLED: "true"
    API_BASE_URL: "https://cms-logging.com"
    API_POLL_INTERVAL: "60"
    COLLECTION_TIMEOUT: "300"
```

Or use `kubectl set env`:

```bash
kubectl set env daemonset/crusoe-log-collector \
  API_ENABLED=true \
  API_BASE_URL=https://cms-logging.com \
  API_POLL_INTERVAL=60 \
  COLLECTION_TIMEOUT=300 \
  -n crusoe-system
```

In this mode, the collector will:
- Poll the API every 60 seconds for new tasks
- Authenticate API calls with Bearer token from `CRUSOE_MONITORING_TOKEN`
- Execute log collection when an `event_id` is received
- Include the `event_id` in the log filename
- Report results to the API in a single call (upload logs + status for success, or just status for failure)
- Timeout after 5 minutes if collection takes too long

### Manual Execution (One-time Collection)

Deploy with `RUN_ONCE=true`:

```bash
kubectl set env daemonset/crusoe-log-collector RUN_ONCE=true -n crusoe-system
```

### Accessing Collected Logs

Logs are stored in `/logs` within the container. To access them:

```bash
# List collected logs
kubectl exec -n crusoe-system crusoe-log-collector-<pod-id> -- ls -lh /logs

# Copy log to local machine
kubectl cp crusoe-system/crusoe-log-collector-<pod-id>:/logs/nvidia-bug-report-node1-20260106_143022.log.gz ./
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
   kubectl auth can-i create pods/exec --as=system:serviceaccount:crusoe-system:crusoe-log-collector
   ```
2. Check ServiceAccount is attached to the pod:
   ```bash
   kubectl get pod <collector-pod> -o jsonpath='{.spec.serviceAccountName}'
   ```
   Expected: `crusoe-log-collector`

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
  - CPU: 50m request, 100m limit (~10-50m during collection, near-idle otherwise)
  - Memory: 64Mi request, 128Mi limit (~50-100 MB typical usage)
  - Network: Minimal (only during log transfer and API calls)

## Security Considerations

1. **Root Access**: The container runs as `runAsUser: 0` (root) to read DMI files (`/sys/class/dmi/id/product_uuid`) for VM ID detection
   - Capabilities are dropped (`drop: ALL`) to minimize attack surface
   - `allowPrivilegeEscalation: false` prevents further privilege escalation
2. **Privileged Access**: Requires exec access to GPU driver pods in the `nvidia-gpu-operator` namespace
3. **Namespace Access**: Reads pods across namespaces (nvidia-gpu-operator) via ClusterRole
4. **Authentication**: API calls use Bearer token authentication with `CRUSOE_MONITORING_TOKEN`
5. **Log Contents**: May contain sensitive system information - secure the log storage location appropriately
6. **RBAC Isolation**: Uses dedicated ServiceAccount (`crusoe-log-collector`) separate from other components to prevent permission conflicts

## Contributing

When making changes:
1. Update tests in `test_log_collector_app.py`
2. Run tests locally before submitting
3. Update this README if adding features
4. Follow the existing code style

## License

Same as crusoe-watch-agent repository.