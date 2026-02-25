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
- **GB200 Support**: Bundled nvidia-bug-report.sh for nodes without GPU Operator

## GB200 Support

The collector automatically detects GB200 nodes via the `node.kubernetes.io/instance-type` label and uses bundled NVIDIA tools instead of GPU Operator driver pods.
- GB200 nodes: Execute `/usr/bin/nvidia-bug-report.sh` locally (bundled in container)
- Other GPUs: Execute via `kubectl exec` into GPU Operator driver pod (existing behavior)

**Implementation:**
- Base image: `nvcr.io/nvidia/cuda:12.8.0-base-ubuntu24.04` (forward compatible with driver 580.95.05)
- Dockerfile installs `nvidia-utils` (550) from NVIDIA CUDA repositories included in base image
- RBAC: Added `nodes.get` permission to read instance-type labels

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

### Accessing Collected Logs

Logs are stored in `/logs` within the container. To access them:

```bash
# List collected logs
kubectl exec -n crusoe-system crusoe-log-collector-<pod-id> -- ls -lh /logs

# Copy log to local machine
kubectl cp crusoe-system/crusoe-log-collector-<pod-id>:/logs/nvidia-bug-report-node1-20260106_143022.log.gz ./
```

## CI/CD

The log collector has automated CI/CD pipelines that build and push Docker images:
Images are pushed to: `ghcr.io/crusoecloud/crusoe-watch-agent/nvidia-log-collector`

