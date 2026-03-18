# GPU Driver Log Collector

A Kubernetes DaemonSet application that collects GPU driver bug reports from nodes running NVIDIA or AMD GPUs.

## Overview

This application runs as a separate DaemonSet (`crusoe-log-collector`) in your Kubernetes cluster and periodically collects diagnostic logs from GPU driver pods. It supports both NVIDIA and AMD GPUs with vendor-specific bug report utilities. The logs are stored locally in the collector pod and can be accessed for troubleshooting GPU-related issues.

**Deployment:** Integrated into the `crusoe-watch-agent` Helm chart as a standalone DaemonSet with its own ServiceAccount and RBAC.

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

## GPU Type Detection

The collector automatically adapts its behavior based on the `GPU_TYPE` environment variable:

### NVIDIA
- **GB200 nodes**: Detects via `node.kubernetes.io/instance-type` label, executes `/usr/bin/nvidia-bug-report.sh` locally (bundled)
- **Other NVIDIA GPUs** (A100, L40S, H100): Executes via `kubectl exec` into GPU Operator driver pod

### AMD
- **All AMD nodes**: Always uses bundled mode, executes `/usr/bin/amd-bug-report.sh` locally

**Implementation:**
- RBAC: Requires `nodes.get` permission to read instance-type labels
- Separate Dockerfiles minimize image bloat (CUDA tools vs ROCm tools)

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

The application is configured via environment variables. The Helm chart automatically derives GPU-specific values from the `gpuType` field in `values.yaml`.

### Helm Configuration (values.yaml)

To switch between NVIDIA and AMD, change ONE field:
```yaml
logCollector:
  gpuType: "nvidia"  # Change to "amd" for AMD clusters
```

This automatically sets:
- `GPU_TYPE`, `DRIVER_NAMESPACE`, `DRIVER_POD_PREFIX` environment variables
- Node affinity (nvidia.com/gpu.present or amd.com/gpu.present)
- Tolerations (nvidia.com/gpu or amd.com/gpu)
- Volume names and paths (/var/log/nvidia-bug-reports or /var/log/amd-bug-reports)

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

Logs are stored in `/logs` within the container. To access them:

```bash
# List collected logs
kubectl exec -n crusoe-system crusoe-log-collector-<pod-id> -- ls -lh /logs

# Copy NVIDIA log to local machine
kubectl cp crusoe-system/crusoe-log-collector-<pod-id>:/logs/nvidia-bug-report-node1-20260106_143022.log.gz ./

# Copy AMD log to local machine
kubectl cp crusoe-system/crusoe-log-collector-<pod-id>:/logs/amd-bug-report-node1-20260106_143022.log.gz ./
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
- `amd-smi ras -v`: ECC error counts

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

The log collector has separate Dockerfiles for each GPU vendor to minimize image bloat:

- **Dockerfile.nvidia**: Uses `nvcr.io/nvidia/cuda:12.8.0-base-ubuntu24.04` with NVIDIA CUDA tools
- **Dockerfile.amd**: Uses `rocm/dev-ubuntu-24.04:6.3-complete` with ROCm tools

**CI/CD:**
Images are pushed to: `ghcr.io/crusoecloud/crusoe-watch-agent/log-collector` (NVIDIA) and `ghcr.io/crusoecloud/crusoe-watch-agent/amd-log-collector` (AMD)

