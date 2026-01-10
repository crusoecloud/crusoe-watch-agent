# NVIDIA Log Collector Deployment Guide

## Quick Start

### 1. Deploy to Kubernetes

```bash
cd k8s/log-collector

# Deploy all resources
make deploy

# Check status
make status
```

### 2. Verify Deployment

```bash
# Check DaemonSet
kubectl get daemonset nvidia-log-collector -n default

# Check pods are running on GPU nodes
kubectl get pods -l app=nvidia-log-collector -n default -o wide

# View logs
make logs
```

### 3. Access Collected Logs

```bash
# List collected logs
make list-logs

# Download all logs to local machine
make download-logs

# Logs will be in ./collected-logs/ directory
ls -lh collected-logs/
```

## Deployment Options

### Option 1: Using Makefile (Recommended)

```bash
# Deploy
make deploy

# Check status
make status

# View logs
make logs

# Undeploy
make undeploy
```

### Option 2: Using kubectl directly

```bash
# Apply manifests
kubectl apply -f manifests/rbac.yaml
kubectl apply -f manifests/configmap.yaml
kubectl apply -f manifests/daemonset.yaml

# Check deployment
kubectl get all -l app=nvidia-log-collector

# Remove deployment
kubectl delete -f manifests/
```

### Option 3: Custom Namespace

```bash
# Deploy to custom namespace
kubectl create namespace gpu-monitoring
kubectl apply -f manifests/ -n gpu-monitoring

# Update namespace in manifests if needed
sed -i 's/namespace: default/namespace: gpu-monitoring/g' manifests/*.yaml
```

## Configuration

### Modify Collection Interval

Edit `manifests/daemonset.yaml`:

```yaml
env:
  - name: COLLECTION_INTERVAL
    value: "7200"  # 2 hours instead of 1 hour
```

Apply changes:
```bash
kubectl apply -f manifests/daemonset.yaml
```

### Change NVIDIA Namespace

If your NVIDIA GPU Operator uses a different namespace:

```yaml
env:
  - name: NVIDIA_NAMESPACE
    value: "gpu-operator"  # Custom namespace
```

### Enable Debug Logging

```yaml
env:
  - name: LOG_LEVEL
    value: "DEBUG"
```

### Run Once Mode

For one-time collection instead of periodic:

```bash
# Using Makefile
make run-once

# Or manually
kubectl set env daemonset/nvidia-log-collector RUN_ONCE=true
kubectl rollout restart daemonset/nvidia-log-collector
```

## Building Custom Image

### Local Build

```bash
# Build locally
make build

# Test
make test

# Build with custom tag
make build IMAGE_TAG=v0.1.0
```

### Push to Registry

```bash
# Build and push to GHCR
make push REGISTRY=ghcr.io/crusoecloud/crusoe-watch-agent IMAGE_TAG=v0.1.0

# Update DaemonSet to use new image
kubectl set image daemonset/nvidia-log-collector \
  log-collector=ghcr.io/crusoecloud/crusoe-watch-agent/nvidia-log-collector:v0.1.0
```

### Using Different Registry

```bash
# Build for different registry
make build IMAGE_NAME=nvidia-log-collector IMAGE_TAG=latest

# Tag for your registry
docker tag nvidia-log-collector:latest myregistry.io/nvidia-log-collector:latest

# Push
docker push myregistry.io/nvidia-log-collector:latest

# Update DaemonSet
kubectl set image daemonset/nvidia-log-collector \
  log-collector=myregistry.io/nvidia-log-collector:latest
```

## Verification Steps

### 1. Check Pod Discovery

```bash
# View logs to see if NVIDIA driver pod was found
kubectl logs -l app=nvidia-log-collector -n default | grep "Found NVIDIA driver pod"
```

Expected output:
```
2026-01-09 10:30:00 INFO: Found NVIDIA driver pod: nvidia-gpu-driver-ubuntu22.04-796555cf6d-5n9wg
```

### 2. Check Log Collection

```bash
# Watch logs during collection
make logs-follow
```

Expected flow:
```
INFO: Starting log collection cycle
INFO: Found NVIDIA driver pod: nvidia-gpu-driver-ubuntu22.04-796555cf6d-5n9wg
INFO: Executing nvidia-bug-report.sh in pod ...
INFO: nvidia-bug-report.sh completed successfully
INFO: Downloading /tmp/nvidia-bug-report-gpu-node-1-20260109_103045.log.gz from pod
INFO: Successfully downloaded log to /logs/nvidia-bug-report-gpu-node-1-20260109_103045.log.gz
INFO: Log collection completed successfully
INFO: Sleeping for 3600 seconds until next collection
```

### 3. Verify Log Files

```bash
# List logs in all pods
make list-logs

# Download to local
make download-logs

# Check file size and format
ls -lh collected-logs/*/nvidia-bug-report-*.log.gz
file collected-logs/*/nvidia-bug-report-*.log.gz
```

### 4. Test Log Contents

```bash
# Extract and view log
cd collected-logs/<pod-name>/
gunzip -c nvidia-bug-report-*.log.gz | head -100
```

## Troubleshooting

### Pods not starting

**Check pod status:**
```bash
kubectl get pods -l app=nvidia-log-collector -n default
kubectl describe pod <pod-name> -n default
```

**Common issues:**
- No GPU nodes: Check nodeSelector matches your GPU nodes
- RBAC issues: Verify ServiceAccount and ClusterRole are created
- Image pull issues: Check image name and registry access

### Cannot find NVIDIA driver pod

**Check if driver pods exist:**
```bash
kubectl get pods -n nvidia-gpu-operator | grep nvidia-gpu-driver
```

**Verify pod naming:**
```bash
# If driver pods have different prefix, update:
kubectl set env daemonset/nvidia-log-collector \
  NVIDIA_DRIVER_POD_PREFIX=<actual-prefix>
```

**Check namespace:**
```bash
# If driver pods are in different namespace:
kubectl set env daemonset/nvidia-log-collector \
  NVIDIA_NAMESPACE=<actual-namespace>
```

### Permission denied on exec

**Check RBAC:**
```bash
# Verify ClusterRole and ClusterRoleBinding
kubectl get clusterrole nvidia-log-collector
kubectl get clusterrolebinding nvidia-log-collector

# Test permissions
kubectl auth can-i create pods/exec \
  --as=system:serviceaccount:default:nvidia-log-collector \
  -n nvidia-gpu-operator
```

**Fix RBAC if needed:**
```bash
kubectl apply -f manifests/rbac.yaml
kubectl rollout restart daemonset/nvidia-log-collector
```

### nvidia-bug-report.sh not found

**Verify script exists in driver pod:**
```bash
kubectl exec -n nvidia-gpu-operator <driver-pod-name> -- which nvidia-bug-report.sh
```

**If missing, the driver image may not include it:**
- Check NVIDIA driver version
- May need to install nvidia-bug-report package
- Or use alternative diagnostic commands

### Logs not downloading

**Check exec and tar work:**
```bash
# Test tar from driver pod
kubectl exec -n nvidia-gpu-operator <driver-pod-name> -- tar --version

# Test file exists
kubectl exec -n nvidia-gpu-operator <driver-pod-name> -- ls -l /tmp/nvidia-bug-report-*.log.gz
```

**Check collector pod logs:**
```bash
kubectl logs <collector-pod-name> | grep -i error
```

### Storage issues

**Check hostPath permissions:**
```bash
# On the node
ssh <node-ip>
ls -ld /var/log/nvidia-bug-reports
```

**Use different storage:**

Option 1 - PersistentVolume:
```yaml
volumes:
  - name: logs
    persistentVolumeClaim:
      claimName: nvidia-logs-pvc
```

Option 2 - EmptyDir (pod-local):
```yaml
volumes:
  - name: logs
    emptyDir: {}
```

## Advanced Usage

### Collect from specific nodes only

Add node affinity:
```yaml
spec:
  template:
    spec:
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: node-role.kubernetes.io/gpu-worker
                operator: In
                values:
                - "true"
```

### Custom collection schedule

Use CronJob instead of DaemonSet:
```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: nvidia-log-collector
spec:
  schedule: "0 2 * * *"  # Daily at 2 AM
  jobTemplate:
    spec:
      template:
        spec:
          # Same spec as DaemonSet
          # ... but with RUN_ONCE=true
```

### Integrate with monitoring

Add Prometheus annotations:
```yaml
metadata:
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: "8080"
```

Then expose metrics in the app (would require code changes).

### Send logs to external storage

Mount cloud storage credentials:
```yaml
env:
  - name: AWS_ACCESS_KEY_ID
    valueFrom:
      secretKeyRef:
        name: aws-credentials
        key: access-key-id

volumes:
  - name: aws-credentials
    secret:
      secretName: aws-credentials
```

Then modify app to upload to S3/GCS after collection.

## Cleanup

### Remove deployment

```bash
make undeploy
```

### Remove all resources including logs

```bash
# Delete DaemonSet and RBAC
kubectl delete -f manifests/

# Delete logs from nodes (be careful!)
kubectl get nodes -l nvidia.com/gpu=true -o name | cut -d/ -f2 | while read node; do
  ssh $node "sudo rm -rf /var/log/nvidia-bug-reports/*"
done
```

### Clean local artifacts

```bash
make clean
```

## Monitoring

### Set up alerting

Example Prometheus alert:
```yaml
- alert: NvidiaLogCollectionFailed
  expr: |
    time() - nvidia_log_collector_last_success_timestamp > 7200
  annotations:
    summary: "NVIDIA log collection failing on {{ $labels.node }}"
```

### Log rotation

Add to DaemonSet:
```yaml
lifecycle:
  postStart:
    exec:
      command:
      - /bin/sh
      - -c
      - |
        # Keep only last 5 logs
        cd /logs && ls -t nvidia-bug-report-*.log.gz | tail -n +6 | xargs rm -f
```

## Support

For issues or questions:
1. Check logs: `make logs`
2. Check status: `make status`
3. Review troubleshooting section above
4. Open issue on GitHub: https://github.com/crusoecloud/crusoe-watch-agent/issues