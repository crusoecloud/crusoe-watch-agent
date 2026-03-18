# GPU Driver Log Collector

A Kubernetes DaemonSet application that collects GPU driver bug reports from nodes running NVIDIA or AMD GPUs.

## Overview

This application runs as **two separate DaemonSets** (`crusoe-log-collector-nvidia` and `crusoe-log-collector-amd`) in your Kubernetes cluster and periodically collects diagnostic logs from GPU nodes. Each DaemonSet is GPU-vendor-specific with its own:
- Docker image (NVIDIA uses CUDA base, AMD uses ROCm tools)
- Node affinity (schedules only on matching GPU nodes)
- Bug report utility (nvidia-bug-report.sh vs amd-bug-report.sh)

This architecture supports **mixed GPU clusters** where both NVIDIA and AMD nodes exist. Each DaemonSet deploys only to its matching nodes, creating 0 pods if no matching nodes exist.

**Deployment:** Integrated into the `crusoe-watch-agent` Helm chart with shared RBAC but separate ServiceAccounts.

## Supported GPU Vendors

### NVIDIA
- **Bug report tool**: `nvidia-bug-report.sh`
- **Driver modes**: GPU Operator (A100, L40S, H100) or Bundled (GB200)
- **Docker image**: Built from `Dockerfile.nvidia` using `nvcr.io/nvidia/cuda:12.8.0-base-ubuntu24.04`

### AMD
- **Bug report tool**: Custom `amd-bug-report.sh` script
- **Driver mode**: Bundled only (no GPU Operator support)
- **Docker image**: Built from `Dockerfile.amd` using `rocm/dev-ubuntu-24.04:6.3-complete`
- **Collects**: `amd-smi`, `rocminfo`, PCIe info, DKMS status, ECC errors, topology

## Features

- **Multi-Vendor Support**: Works with both NVIDIA and AMD GPUs
- **Automatic Discovery**: Finds NVIDIA GPU driver pods using label selector (primary) or name prefix (fallback)
- **VM ID Auto-Detection**: Automatically reads VM ID from DMI (`/sys/class/dmi/id/product_uuid`)
- **Log Collection**: Executes appropriate bug report script based on GPU type
- **File Download**: Transfers generated log files to the collector pod (NVIDIA GPU Operator mode only)
- **Flexible Execution Modes**:
  - **Scheduled Mode**: Runs on a configurable schedule (default)
  - **API-Driven Mode**: Polls an API and collects logs on-demand
  - **One-Time Mode**: Runs once and exits
- **Multi-Node Support**: Deploys as DaemonSet for cluster-wide coverage
- **Timeout Handling**: Automatic timeout protection (5 minutes default)
- **Automatic Cleanup**: Keeps only the most recent logs to prevent disk exhaustion

## Mixed GPU Cluster Support

The log collector supports **heterogeneous clusters** with both NVIDIA and AMD nodes through two independent DaemonSets:

### Node Scheduling
- **NVIDIA DaemonSet**: Schedules only on nodes with `nvidia.com/gpu.present=true` label
- **AMD DaemonSet**: Schedules only on nodes with `feature.node.kubernetes.io/amd-gpu=true` label
- Each DaemonSet creates 0 pods if no matching nodes exist (safe for homogeneous clusters)

### GPU Detection per DaemonSet

**NVIDIA (`crusoe-log-collector-nvidia`):**
- **GB200 nodes**: Detects via `node.kubernetes.io/instance-type` label, executes `/usr/bin/nvidia-bug-report.sh` locally (bundled)
- **Other NVIDIA GPUs** (A100, L40S, H100): Executes via `kubectl exec` into GPU Operator driver pod

**AMD (`crusoe-log-collector-amd`):**
- **All AMD nodes**: Always uses bundled mode, executes `/usr/bin/amd-bug-report.sh` locally

### Implementation 
Details
- Shared ClusterRole with unified permissions for both vendors
- Separate ServiceAccounts per DaemonSet
- Independent enable/disable flags in values.yaml
- Separate Docker images minimize bloat (CUDA vs ROCm tools)

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

### One-Time Mode
Set `RUN_ONCE=true` to collect logs once and exit. Useful for testing or manual collection.

## Architecture

### NVIDIA (GPU Operator Mode)
```
┌─────────────────────────────────────────┐
│         Kubernetes Node                 │
├─────────────────────────────────────────┤
│                                         │
│  ┌───────────────────────────────┐      │
│  │  nvidia-gpu-operator NS       │      │
│  │  ┌─────────────────────────┐  │      │
│  │  │ nvidia-gpu-driver pod   │  │      │
│  │  │ - nvidia-bug-report.sh  │◄─┼──┐   │
│  │  └─────────────────────────┘  │  │   │
│  └───────────────────────────────┘  │   │
│                                     │   │
│  ┌───────────────────────────────┐  │   │
│  │  crusoe-system NS             │  │   │
│  │  ┌─────────────────────────┐  │  │   │
│  │  │ log-collector pod       │  │  │   │
│  │  │ - Executes command ─────┼──┼──┘   │
│  │  │ - Downloads logs        │  │      │
│  │  │ - Stores in /logs       │  │      │
│  │  └─────────────────────────┘  │      │
│  └───────────────────────────────┘      │
└─────────────────────────────────────────┘
```

### AMD or NVIDIA GB200 (Bundled Mode)
```
┌─────────────────────────────────────────┐
│         Kubernetes Node                 │
├─────────────────────────────────────────┤
│                                         │
│  ┌───────────────────────────────┐      │
│  │  crusoe-system NS             │      │
│  │  ┌─────────────────────────┐  │      │
│  │  │ log-collector pod       │  │      │
│  │  │ - amd-bug-report.sh OR  │  │      │
│  │  │   nvidia-bug-report.sh  │  │      │
│  │  │ - Stores in /logs       │  │      │
│  │  └─────────────────────────┘  │      │
│  └───────────────────────────────┘      │
└─────────────────────────────────────────┘
```

## Configuration

The application is configured via environment variables. The Helm chart has separate sections for NVIDIA and AMD log collectors.

### Helm Configuration (values.yaml)

For **mixed GPU clusters**, both DaemonSets can be enabled:
```yaml
logCollector:
  nvidia:
    enabled: true  # Deploys to NVIDIA nodes only
    name: crusoe-log-collector-nvidia
    image:
      repository: ghcr.io/crusoecloud/crusoe-watch-agent/log-collector
      tag: v0.2.12
    # ... other config

  amd:
    enabled: true  # Deploys to AMD nodes only
    name: crusoe-log-collector-amd
    image:
      repository: ghcr.io/crusoecloud/crusoe-watch-agent/amd-log-collector
      tag: v0.2.12
    # ... other config
```

For **homogeneous clusters**, disable the unused vendor:
```yaml
logCollector:
  nvidia:
    enabled: true   # NVIDIA-only cluster
  amd:
    enabled: false  # No AMD nodes
```

Each DaemonSet automatically configures:
- `GPU_TYPE`, `DRIVER_NAMESPACE`, `DRIVER_POD_PREFIX` environment variables
- Node affinity (vendor-specific labels)
- Tolerations (vendor-specific taints)
- Volume paths (/var/log/nvidia-bug-reports or /var/log/amd-bug-reports)

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `NODE_NAME` | *required* | Name of the node (injected via downward API) |
| `VM_ID` | *auto-detected* | Unique VM identifier (auto-read from `/host/sys/class/dmi/id/product_uuid` if not set; required for API-driven mode) |
| `CRUSOE_MONITORING_TOKEN` | *from secret* | Authentication token for API calls (injected from `crusoe-monitoring-token` secret; required for API-driven mode) |
| `GPU_TYPE` | `nvidia` | GPU vendor: `nvidia` or `amd` (auto-set by Helm template) |
| `DRIVER_NAMESPACE` | `nvidia-gpu-operator` | Namespace where GPU driver pods run (auto-set: `{gpu_type}-gpu-operator`) |
| `DRIVER_POD_PREFIX` | `nvidia-gpu-driver` | Prefix of GPU driver pod names (auto-set: `{gpu_type}-gpu-driver`) |
| `LOG_OUTPUT_DIR` | `/logs` | Directory to store collected logs |
| `COLLECTION_INTERVAL` | `3600` | Seconds between collections (1 hour) - used in scheduled mode |
| `RUN_ONCE` | `false` | If true, run once and exit |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `MAX_LOGS_TO_KEEP` | `1` | Maximum number of old logs to keep (prevents disk space issues) |
| `API_ENABLED` | `false` | Enable API-driven mode instead of scheduled collection |
| `API_BASE_URL` | `https://cms-monitoring.crusoecloud.com` | Base URL for the log collection API |
| `API_POLL_INTERVAL` | `60` | Seconds between API polls for new tasks |
| `COLLECTION_TIMEOUT` | `300` | Maximum seconds (5 minutes) for log collection before timeout |

### Accessing Collected Logs

Logs are stored in `/logs` within each collector pod. To access them:

```bash
# List pods by GPU vendor
kubectl get pods -n crusoe-system -l gpu-vendor=nvidia  # NVIDIA pods
kubectl get pods -n crusoe-system -l gpu-vendor=amd     # AMD pods

# List collected NVIDIA logs
kubectl exec -n crusoe-system crusoe-log-collector-nvidia-<pod-id> -- ls -lh /logs

# List collected AMD logs
kubectl exec -n crusoe-system crusoe-log-collector-amd-<pod-id> -- ls -lh /logs

# Copy NVIDIA log to local machine
kubectl cp crusoe-system/crusoe-log-collector-nvidia-<pod-id>:/logs/nvidia-bug-report-node1-20260106_143022.log.gz ./

# Copy AMD log to local machine
kubectl cp crusoe-system/crusoe-log-collector-amd-<pod-id>:/logs/amd-bug-report-node1-20260106_143022.log.gz ./
```

## AMD Bug Report Contents

The AMD bug report script collects comprehensive diagnostic information:

**Basic Info:**
- `lsb_release -sd`: OS distribution and version
- `lshw -c cpu`: CPU model and architecture
- `amd-smi list`: GPU models and UUIDs
- `amd-smi version`: ROCm and SMI versions

**Hardware & Installation:**
- `dkms status`: amdgpu driver status
- `lspci -vnn`: PCIe bus speeds and device IDs
- `uname -a`: Linux kernel version

**Compute Stack:**
- `amd-smi static`: VBIOS, power limits, board metadata
- `rocminfo`: GPU visibility via KFD
- `amd-smi topology`: XGMI/P2P interconnect map

**Health & Reliability:**
- `amd-smi bad-pages`: VRAM memory defects
- `amd-smi metric -m ecc`: ECC error counts

## Driver Pod Discovery (NVIDIA Only)

For NVIDIA GPU Operator mode, the collector uses a two-tier approach to find the driver pod:

1. **Primary (Label Selector)**: Looks for pods with label `app.kubernetes.io/component=nvidia-driver` — this is the standard label set by the official NVIDIA GPU Operator.

2. **Fallback (Name Prefix)**: If no pod is found via label, falls back to matching pods by name prefix (`DRIVER_POD_PREFIX`). This supports custom or legacy deployments.

This approach ensures reliability with standard GPU Operator deployments while maintaining backward compatibility.

**Note:** AMD does not use GPU Operator mode - all AMD bug reports are executed locally in bundled mode.

## Error Reporting

When running in API-driven mode, specific error messages are reported to the control plane:

| Failure | Error Message |
|---------|---------------|
| Driver pod not found (NVIDIA only) | `NVIDIA driver pod not found on node {node} in namespace {namespace}` |
| Execution fails (GPU Operator) | `Failed to execute nvidia-bug-report.sh in driver pod {pod}` |
| Download fails (GPU Operator) | `Failed to download log file from driver pod {pod}` |
| Local execution fails | `Failed to execute bug report locally (bundled driver mode)` |
| Timeout | `Collection timeout after {timeout} seconds` |

## Docker Images

The log collector uses **separate Dockerfiles** for each GPU vendor to minimize image bloat:

- **Dockerfile.nvidia**: Uses `nvcr.io/nvidia/cuda:12.8.0-base-ubuntu24.04` with NVIDIA utilities (~2GB)
- **Dockerfile.amd**: Uses `ubuntu:24.04` base with minimal ROCm packages (`rocm-smi`, `rocminfo`) (~1-2GB)

This approach avoids a combined image that would be 10-15GB with both CUDA and full ROCm dev tools.

**CI/CD:**
Both images are built in parallel via GitHub Actions matrix and pushed to:
- NVIDIA: `ghcr.io/crusoecloud/crusoe-watch-agent/log-collector`
- AMD: `ghcr.io/crusoecloud/crusoe-watch-agent/amd-log-collector`

**Deployment:**
In mixed clusters, Helm deploys both DaemonSets with vendor-specific images. Each DaemonSet only schedules on matching GPU nodes.

