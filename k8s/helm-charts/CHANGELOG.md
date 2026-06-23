# Changelog

## 0.3.28 (2026-06-23)

**Bug Fixes**
- Fix ALPN negotiation for all metric and log sinks when egress proxy is enabled ([2649f91](https://github.com/crusoecloud/crusoe-watch-agent/commit/2649f91))
- Route kube-state-metrics through the egress proxy when `proxy.enabled` is set, consistent with all other metric sinks ([2649f91](https://github.com/crusoecloud/crusoe-watch-agent/commit/2649f91))
- Remove `fix-logs-permissions` init container from the log-collector DaemonSet, which pulled `busybox:latest` from Docker Hub and could fail due to image pull rate limits ([f0b8895](https://github.com/crusoecloud/crusoe-watch-agent/commit/f0b8895))

**Improvements**
- Restructure collected log events to wrap the raw original event verbatim under a `.payload` envelope ([a1381be](https://github.com/crusoecloud/crusoe-watch-agent/commit/a1381be))
- Switch Vector image source from `timberio/vector` to `ghcr.io/vectordotdev/vector` to avoid Docker Hub pull rate limits ([f0b8895](https://github.com/crusoecloud/crusoe-watch-agent/commit/f0b8895))


## 0.3.27 (2026-06-12)

- Fix Vector failing to detect config updates written in the same second as startup, which caused metrics like DCGM to be silently missed ([cfce70f](https://github.com/crusoecloud/crusoe-watch-agent/commit/cfce70f))


## 0.3.26 (2026-06-09)

**Features**
- Add 256 MiB persistent disk buffers to metrics sinks to prevent data loss during temporary outages ([f9d0214](https://github.com/crusoecloud/crusoe-watch-agent/commit/f9d0214))
- Infer cluster region from node hostname when installing CWA with CME, removing the need to specify it explicitly ([d4da1e8](https://github.com/crusoecloud/crusoe-watch-agent/commit/d4da1e8))
- Route log-collector API requests through egress proxy when proxy is enabled ([049a2f7](https://github.com/crusoecloud/crusoe-watch-agent/commit/049a2f7))

**Bug Fixes**
- Fix NVIDIA bug report download failing on large payloads ([ff55028](https://github.com/crusoecloud/crusoe-watch-agent/commit/ff55028))


## 0.3.25 (2026-05-22)

**Bug Fixes**

- Add pod identifier to internal custom metrics to avoid timeseries collision ([3ab5957](https://github.com/crusoecloud/crusoe-watch-agent/commit/3ab5957))


## 0.3.24 (2026-05-19)

- Pin Vector to a stable release across all deployments ([61bc649](https://github.com/crusoecloud/crusoe-watch-agent/commit/61bc649))


## 0.3.23 (2026-05-15)

**Bug Fixes**
- Fix log parsing for JournalD and Kubelet logs that produced spurious structured fields such as `log.ip` and `log.100` ([fe138ed](https://github.com/crusoecloud/crusoe-watch-agent/commit/fe138ed))
- Fix scrape configs failing to update when pods were added, removed, or restarted on a node ([16460a6](https://github.com/crusoecloud/crusoe-watch-agent/commit/16460a6))

**Improvements**
- Add `chart_version` fields to all collected logs ([fe138ed](https://github.com/crusoecloud/crusoe-watch-agent/commit/fe138ed))
- Filter containerd hugetlb cgroup noise from JournalD log collection ([fe138ed](https://github.com/crusoecloud/crusoe-watch-agent/commit/fe138ed))
- Add structured error codes to bug report collection for programmatic failure diagnosis ([4efaee6](https://github.com/crusoecloud/crusoe-watch-agent/commit/4efaee6))


## 0.3.22 (2026-05-08)

- Update crusoe-metrics-exporter to 0.2.0, adding new metrics

## 0.3.21 (2026-04-23)

**Features**
- Collect Vector agent and config-reloader logs and forward them to CMS ([5be0968](https://github.com/crusoecloud/crusoe-watch-agent/commit/5be0968))
- Expose a Prometheus metrics endpoint on the config-reloader for error monitoring ([0fd0b62](https://github.com/crusoecloud/crusoe-watch-agent/commit/0fd0b62))

**Improvements**
- Enable HTTP/2 for sink connections ([772573c](https://github.com/crusoecloud/crusoe-watch-agent/commit/772573c))

**Bug Fixes**
- Fix elevated agent pod restart counts caused by unhandled Kubernetes API 410 Gone errors ([0fd0b62](https://github.com/crusoecloud/crusoe-watch-agent/commit/0fd0b62))
- Fix Vector startup race condition when the config file is not yet present on disk ([0fd0b62](https://github.com/crusoecloud/crusoe-watch-agent/commit/0fd0b62))
- Fix exception thrown when log events are missing pod labels ([583c02d](https://github.com/crusoecloud/crusoe-watch-agent/commit/583c02d))


## 0.3.20 (2026-04-08)

- Fix NFS metrics collection by sharing the host PID namespace ([39157f8](https://github.com/crusoecloud/crusoe-watch-agent/commit/39157f8))


## 0.3.19 (2026-04-06)

- Add AMD GPU log collection support for both Kubernetes and VM deployments ([2b2fb6c](https://github.com/crusoecloud/crusoe-watch-agent/commit/2b2fb6c))


## 0.3.18 (2026-04-03)

Internal improvements and maintenance.


## 0.3.17 (2026-04-02)

- Fix Helm chart packaging ([be22453](https://github.com/crusoecloud/crusoe-watch-agent/commit/be22453))


## 0.3.16 (2026-04-02)

- Add Crusoe metrics exporter integration to the Helm chart ([f31e40a](https://github.com/crusoecloud/crusoe-watch-agent/commit/f31e40a))


## 0.3.15 (2026-03-26)

- Collect journald logs starting from the current time rather than replaying historical entries ([598168d](https://github.com/crusoecloud/crusoe-watch-agent/commit/598168d))


## 0.3.14 (2026-03-18)

- Map journald log severity levels to standard syslog names (emergency, alert, critical, error, warning, notice, info, debug) instead of coarse groupings ([f4e70f6](https://github.com/crusoecloud/crusoe-watch-agent/commit/f4e70f6))


## 0.3.13 (2026-03-16)

- Add AMD GPU metrics collection support to the vector config reloader ([a44f472](https://github.com/crusoecloud/crusoe-watch-agent/commit/a44f472), [f9047c3](https://github.com/crusoecloud/crusoe-watch-agent/commit/f9047c3))

## 0.3.12 (2026-03-12)

- Fix NVIDIA bug report collection on B200 GPU nodes ([d83735e](https://github.com/crusoecloud/crusoe-watch-agent/commit/d83735e), [3e3a9ab](https://github.com/crusoecloud/crusoe-watch-agent/commit/3e3a9ab))
- Add `app_id` tag to custom internal metrics ([fbd23fd](https://github.com/crusoecloud/crusoe-watch-agent/commit/fbd23fd))


## 0.3.11 (2026-03-06)

**Features**

- Add support for scraping managed custom metrics from services in the `crusoe-monitoring` namespace ([e065611](https://github.com/crusoecloud/crusoe-watch-agent/commit/e065611))
- Add Slurm metrics collection ([bc258d1](https://github.com/crusoecloud/crusoe-watch-agent/commit/bc258d1))
- Add monitoring token generation and GPU log collection for GB200 nodes ([5982b23](https://github.com/crusoecloud/crusoe-watch-agent/commit/5982b23))

**Improvements**

- Remove kmsg log collection ([c33069c](https://github.com/crusoecloud/crusoe-watch-agent/commit/c33069c))


## 0.3.10 (2026-02-20)

- Add configurable node affinity for log-collector DaemonSet pods ([e40dd8a](https://github.com/crusoecloud/crusoe-watch-agent/commit/e40dd8a))


## 0.3.9 (2026-02-20)

- Add tolerations support to allow the agent to run on tainted nodes ([f95a51e](https://github.com/crusoecloud/crusoe-watch-agent/commit/f95a51e), [3516d56](https://github.com/crusoecloud/crusoe-watch-agent/commit/3516d56))


## 0.3.8 (2026-02-19)

- Fix log collector accumulating too many files on disk ([1aada99](https://github.com/crusoecloud/crusoe-watch-agent/commit/1aada99))
- Filter Vector internal metrics to only forward a curated set of health and performance signals ([1a34f0a](https://github.com/crusoecloud/crusoe-watch-agent/commit/1a34f0a))


## 0.3.7 (2026-02-13)

- Fix log-collector pod startup failure caused by incorrectly optional secret reference ([db0a6f1](https://github.com/crusoecloud/crusoe-watch-agent/commit/db0a6f1))


## 0.3.6 (2026-02-13)

- Fix monitoring token flag passed to log collector ([77f2dda](https://github.com/crusoecloud/crusoe-watch-agent/commit/77f2dda))


## 0.3.5 (2026-02-13)

- Fix GPU bug report execution to correctly parse command arguments ([14b4708](https://github.com/crusoecloud/crusoe-watch-agent/commit/14b4708))


## 0.3.4 (2026-02-11)

- Improve log-collector structured logging: logs now emit JSON with explicit `level` and `message` fields for easier filtering and querying ([6d44b93](https://github.com/crusoecloud/crusoe-watch-agent/commit/6d44b93))
- Parse log-collector log levels in the Vector pipeline so structured fields are correctly propagated to your logging backend ([6d44b93](https://github.com/crusoecloud/crusoe-watch-agent/commit/6d44b93))


## 0.3.3 (2026-02-11)

- Fix log collection from `crusoe-log-collector` pods by updating the Kubernetes log source to use path-based glob patterns targeting the correct pod log directories ([dadeeac](https://github.com/crusoecloud/crusoe-watch-agent/commit/dadeeac))


## 0.3.2 (2026-02-09)

- Collect Kubernetes pod logs and journald logs from cluster nodes and forward them to the Crusoe logging backend ([70289bc](https://github.com/crusoecloud/crusoe-watch-agent/commit/70289bc), [8eab8e9](https://github.com/crusoecloud/crusoe-watch-agent/commit/8eab8e9))
- Add `crusoe_cluster_id` field to all forwarded log entries ([8eab8e9](https://github.com/crusoecloud/crusoe-watch-agent/commit/8eab8e9))
- Add Helm values to individually enable or disable each scraper (DCGM, kube-state-metrics, Slurm, custom metrics, logs) ([83b026d](https://github.com/crusoecloud/crusoe-watch-agent/commit/83b026d))
- Skip installing the custom metrics ConfigMap when custom metrics are disabled ([83b026d](https://github.com/crusoecloud/crusoe-watch-agent/commit/83b026d))
- Log collector decompresses `.loggz` GPU log archives before upload ([353ac5d](https://github.com/crusoecloud/crusoe-watch-agent/commit/353ac5d))
- Use `cri:resource-type/resource-id` format for metrics tenant routing ([91edc06](https://github.com/crusoecloud/crusoe-watch-agent/commit/91edc06))
- Normalize log level fields to lowercase across all log sources ([70289bc](https://github.com/crusoecloud/crusoe-watch-agent/commit/70289bc))
- Label log entries with no level field as `undefined` instead of `info` ([d51cada](https://github.com/crusoecloud/crusoe-watch-agent/commit/d51cada))


## 0.3.1 (2026-01-28)

**Bug Fixes**

- Report failure status to the API when log collection succeeds but the upload fails, eliminating silent upload errors ([45fa2c0](https://github.com/crusoecloud/crusoe-watch-agent/commit/45fa2c0))

**Improvements**

- Increase log collector memory and CPU limits to reduce pod eviction risk on GPU nodes ([45fa2c0](https://github.com/crusoecloud/crusoe-watch-agent/commit/45fa2c0))
- Cap log sink batch size at ~100KB to prevent oversized payloads to the monitoring backend ([b526ce2](https://github.com/crusoecloud/crusoe-watch-agent/commit/b526ce2))
- Build vector-config-reloader for all architectures, enabling deployment on arm64 nodes ([b526ce2](https://github.com/crusoecloud/crusoe-watch-agent/commit/b526ce2))


## 0.3.0 (2026-01-26)

- Update vector-config-reloader to improve log collection handling ([eb91884](https://github.com/crusoecloud/crusoe-watch-agent/commit/eb91884))


## 0.2.8 (2026-01-26)

- Enable log collection from journald, dmesg/kernel, and NVIDIA diagnostic reports, forwarding them to the Crusoe monitoring backend ([2795431](https://github.com/crusoecloud/crusoe-watch-agent/commit/2795431))
- Add `vm_instance_type` tag to all collected metrics ([2795431](https://github.com/crusoecloud/crusoe-watch-agent/commit/2795431))
- Update log collector API endpoints to the production Crusoe monitoring service ([30accc5](https://github.com/crusoecloud/crusoe-watch-agent/commit/30accc5))


## 0.2.7 (2026-01-21)

**Features**

- Add NVIDIA GPU driver log collection via a new DaemonSet that captures driver bug reports from GPU Operator pods ([d5b21dc](https://github.com/crusoecloud/crusoe-watch-agent/commit/d5b21dc))
- Add AMD GPU metrics collection support ([cac8aa0](https://github.com/crusoecloud/crusoe-watch-agent/commit/cac8aa0))
- Add kube-state-metrics scraping to the dynamic Vector config ([842c572](https://github.com/crusoecloud/crusoe-watch-agent/commit/842c572))
- Add host process metrics collection ([ff885db](https://github.com/crusoecloud/crusoe-watch-agent/commit/ff885db))
- Apply node labels to scraped metrics for richer per-node attribution ([84f0dee](https://github.com/crusoecloud/crusoe-watch-agent/commit/84f0dee))
- Enable relabeling rules for custom metrics ([1bd8222](https://github.com/crusoecloud/crusoe-watch-agent/commit/1bd8222))

**Improvements**

- Reload Vector config immediately when a ConfigMap is updated, reducing the delay before config changes take effect ([dff0022](https://github.com/crusoecloud/crusoe-watch-agent/commit/dff0022))


## 0.2.6 (2025-12-18)

- Add Helm chart for deploying the watch agent as a Vector DaemonSet on Kubernetes ([7e32a3e](https://github.com/crusoecloud/crusoe-watch-agent/commit/7e32a3e))
- Add vector-config-reloader sidecar that dynamically generates Vector scrape configs based on live cluster state ([7e32a3e](https://github.com/crusoecloud/crusoe-watch-agent/commit/7e32a3e))
- Add monitoring token Job with RBAC for automated auth secret provisioning at install time ([7e32a3e](https://github.com/crusoecloud/crusoe-watch-agent/commit/7e32a3e))
