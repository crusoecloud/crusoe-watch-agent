# Changelog

## 1.0.13 (2026-06-29)

**Features**
- Enable CME bundling by default for all installations ([fe2d9b4](https://github.com/crusoecloud/crusoe-watch-agent/commit/fe2d9b4))

**Bug Fixes**
- Fix API requests failing for proxy-enabled deployments ([7c323ce](https://github.com/crusoecloud/crusoe-watch-agent/commit/7c323ce))


## 1.0.12 (2026-05-26)

**Features**
- Add `crusoe_vm_boot_time` metric to all standalone VM configurations ([5e7fde5](https://github.com/crusoecloud/crusoe-watch-agent/commit/5e7fde5))

**Improvements**
- Auto-detect VM type (NVIDIA, AMD, or CPU-only) during installation, removing the need to select the correct hardware-specific script ([4e34d79](https://github.com/crusoecloud/crusoe-watch-agent/commit/4e34d79))


## 1.0.11 (2026-05-19)

- Pin Vector to a stable release across Docker and native installation modes ([61bc649](https://github.com/crusoecloud/crusoe-watch-agent/commit/61bc649))


## 1.0.10 (2026-05-15)

**Features**

- Add `agent_version` field to all collected logs ([fe138ed](https://github.com/crusoecloud/crusoe-watch-agent/commit/fe138ed))

**Improvements**

- Add structured error codes to bug report collection for programmatic diagnostics ([4efaee6](https://github.com/crusoecloud/crusoe-watch-agent/commit/4efaee6))

**Bug Fixes**

- Fix JournalD and containerd log parsing that produced spurious fields like `log.ip` and `log.100` ([fe138ed](https://github.com/crusoecloud/crusoe-watch-agent/commit/fe138ed))
- Fix agent upgrades not restarting Vector, leaving new configuration and binaries inactive ([e9c2cb7](https://github.com/crusoecloud/crusoe-watch-agent/commit/e9c2cb7))
- Fix monitoring token corruption when reinstalling with a different installation method ([5c3a606](https://github.com/crusoecloud/crusoe-watch-agent/commit/5c3a606))


## 1.0.9 (2026-05-11)

**Features**
- Support `crusoe-metrics-exporter` in native (`--no-docker`) install mode ([c88112d](https://github.com/crusoecloud/crusoe-watch-agent/commit/c88112d))

**Bug Fixes**
- Remove unsupported `--no-docker` option from AMD installer ([b78e114](https://github.com/crusoecloud/crusoe-watch-agent/commit/b78e114))


## 1.0.8 (2026-05-08)

- Update crusoe-metrics-exporter to 0.2.0, adding new metrics


## 1.0.7 (2026-05-06)

- Pin VM installer to a validated release version ([1c6d17d](https://github.com/crusoecloud/crusoe-watch-agent/commit/1c6d17d))


## 1.0.6 (2026-04-25)

**Features**
- Add region selection to the installer; region is required when the metrics exporter is enabled ([a8b1266](https://github.com/crusoecloud/crusoe-watch-agent/commit/a8b1266))
- Add metrics-exporter support to the AMD installer ([a8b1266](https://github.com/crusoecloud/crusoe-watch-agent/commit/a8b1266))

**Improvements**
- Enable HTTP/2 for Vector sink connections ([772573c](https://github.com/crusoecloud/crusoe-watch-agent/commit/772573c))
- Add four new DCGM metrics: `DCGM_FI_DEV_CLOCKS_EVENT_REASONS`, `DCGM_FI_DEV_FAN_SPEED`, `DCGM_FI_DEV_PCIE_LINK_GEN`, `DCGM_FI_DEV_PCIE_LINK_WIDTH` ([db16b11](https://github.com/crusoecloud/crusoe-watch-agent/commit/db16b11))

**Bug Fixes**
- Fix installer failure caused by inaccessible script signature files ([4c0ae2d](https://github.com/crusoecloud/crusoe-watch-agent/commit/4c0ae2d))
- Fix crash when log entries are missing pod labels ([583c02d](https://github.com/crusoecloud/crusoe-watch-agent/commit/583c02d))
- Fix NFS metrics collection by running the metrics-exporter container with host PID namespace ([39157f8](https://github.com/crusoecloud/crusoe-watch-agent/commit/39157f8))


## 1.0.5 (2026-04-06)

- Add AMD GPU support: `crusoe_watch_agent_amd.sh` installs monitoring on AMD GPU VMs via Docker or native mode, and collects ROCm diagnostics, amd-smi metrics, and firmware info ([2b2fb6c](https://github.com/crusoecloud/crusoe-watch-agent/commit/2b2fb6c))
- Add Crusoe metrics exporter integration ([f31e40a](https://github.com/crusoecloud/crusoe-watch-agent/commit/f31e40a))


## 1.0.4 (2026-03-26)

- Scrape journald logs from the current time on startup rather than from the beginning of the journal ([598168d](https://github.com/crusoecloud/crusoe-watch-agent/commit/598168d))


## 1.0.3 (2026-03-18)

- Add AMD GPU metrics collection ([a44f472](https://github.com/crusoecloud/crusoe-watch-agent/commit/a44f472))
- Fix log severity levels to use standard mappings ([f4e70f6](https://github.com/crusoecloud/crusoe-watch-agent/commit/f4e70f6))


## 1.0.2 (2026-03-13)

- Fix log collector failing to pull the correct container image during installation ([71503a7](https://github.com/crusoecloud/crusoe-watch-agent/commit/71503a7))


## 1.0.1 (2026-03-13)

**Features**

- Add native binary installation mode via `--no-docker` flag, enabling deployment without Docker ([3bb4d1d](https://github.com/crusoecloud/crusoe-watch-agent/commit/3bb4d1d))
- Add AMD GPU monitoring with metrics and log collection for VM deployments ([03423fe](https://github.com/crusoecloud/crusoe-watch-agent/commit/03423fe), [8c2fe91](https://github.com/crusoecloud/crusoe-watch-agent/commit/8c2fe91))
- Add NVIDIA GPU bug report collection for VM deployments ([9861d48](https://github.com/crusoecloud/crusoe-watch-agent/commit/9861d48))
- Collect journald logs from VMs ([3d3e95b](https://github.com/crusoecloud/crusoe-watch-agent/commit/3d3e95b))
- Add host process metrics collection ([ff885db](https://github.com/crusoecloud/crusoe-watch-agent/commit/ff885db))

**Improvements**

- Rename agent from `crusoe-watch` to `crusoe-watch-agent` ([c646813](https://github.com/crusoecloud/crusoe-watch-agent/commit/c646813))
- Send `cri:resource-type/resource-id` as tenant ID for correct resource routing ([91edc06](https://github.com/crusoecloud/crusoe-watch-agent/commit/91edc06))
- Filter internal metrics from telemetry output ([1a34f0a](https://github.com/crusoecloud/crusoe-watch-agent/commit/1a34f0a))

**Bug Fixes**

- Fix malformed token refresh ([1165d24](https://github.com/crusoecloud/crusoe-watch-agent/commit/1165d24))
- Fix NVLink metrics config file path ([16a5a1a](https://github.com/crusoecloud/crusoe-watch-agent/commit/16a5a1a))
- Fix file reference errors in installer ([6b72246](https://github.com/crusoecloud/crusoe-watch-agent/commit/6b72246))


## 1.0.0 (2025-12-17)

- Add VM installer script for deploying the Crusoe Watch Agent on Linux VMs ([766ae9a](https://github.com/crusoecloud/crusoe-watch-agent/commit/766ae9a))
- Support Docker-based deployment with systemd service management for Vector and DCGM Exporter ([766ae9a](https://github.com/crusoecloud/crusoe-watch-agent/commit/766ae9a))
- Collect GPU metrics via DCGM Exporter on GPU VMs ([766ae9a](https://github.com/crusoecloud/crusoe-watch-agent/commit/766ae9a))
- Forward metrics to Crusoe monitoring backend using token-based authentication ([766ae9a](https://github.com/crusoecloud/crusoe-watch-agent/commit/766ae9a))
