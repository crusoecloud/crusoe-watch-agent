import os, signal, re, logging, threading, sys, time, hashlib, json
from dataclasses import dataclass
from pathlib import Path
from kubernetes import client, config
from utils import (
    LiteralStr,
    YamlUtils,
    JSONFormatter,
    errors_total,
    start_http_server,
)
from amd_exporter import AmdExporterManager, AMD_EXPORTER_SOURCE_NAME

CUSTOM_METRICS_CM_NAMESPACE = "crusoe-monitoring"
VECTOR_CONFIG_PATH = "/etc/vector/vector.yaml"
VECTOR_BASE_CONFIG_PATH = "/etc/vector-base/vector.yaml"
RELOADER_CONFIG_PATH = "/etc/reloader/config.yaml"

DCGM_EXPORTER_SOURCE_NAME = "dcgm_exporter_scrape"

# Log sources and pipeline constants (each is referenced from multiple places
# inside set_logs_config to wire the source -> filter -> transform -> sink graph)
ENRICH_LOGS_TRANSFORM_NAME = "enrich_logs"
PARSE_JOURNALD_LOGS_TRANSFORM_NAME = "parse_journald_logs"
PARSE_CRUSOE_CONTAINER_LOGS_TRANSFORM_NAME = "parse_crusoe_container_logs"
PARSE_INTERNAL_LOGS_TRANSFORM_NAME = "parse_internal_logs"
FILTER_CRUSOE_LOG_COLLECTOR_LOGS_TRANSFORM_NAME = "filter_crusoe_log_collector_logs"
FILTER_VECTOR_CONFIG_RELOADER_LOGS_TRANSFORM_NAME = "filter_vector_config_reloader_logs"
FILTER_JOURNALD_NOISE_TRANSFORM_NAME = "filter_journald_noise"
JOURNALD_LOGS_SOURCE_NAME = "journald_logs"
KUBERNETES_LOGS_SOURCE_NAME = "kubernetes_logs"
VECTOR_INTERNAL_LOGS_SOURCE_NAME = "vector_internal_logs"
NODE_METRICS_VECTOR_TRANSFORM_NAME = "enrich_node_metrics"

CUSTOM_METRICS_CONFIG_MAP_NAME = "crusoe-custom-metrics-config"
CUSTOM_METRICS_CONFIG_MAP_KEY = "custom-metrics-config.yaml"
CUSTOM_METRICS_SCRAPE_ANNOTATION = "crusoe.ai/scrape"
CUSTOM_METRICS_PORT_ANNOTATION = "crusoe.ai/port"
CUSTOM_METRICS_PATH_ANNOTATION = "crusoe.ai/path"
CUSTOM_METRICS_APP_ID_ANNOTATION = "crusoe.ai/app_id"
CUSTOM_METRICS_DEFAULT_SCRAPE_INTERVAL = 30

SCRAPE_INTERVAL_MIN_THRESHOLD = 5
SCRAPE_TIMEOUT_PERCENTAGE = 0.7
RECONCILE_INTERVAL_SECS = 60

# Pod classification labels (used in pod fingerprints and logging)
POD_TYPE_CUSTOM = "custom_metrics"
POD_TYPE_DCGM = "dcgm_exporter"
POD_TYPE_KSM = "kube_state_metrics"
POD_TYPE_SLURM = "slurm_metrics"
POD_TYPE_CME = "crusoe_metrics_exporter"
POD_TYPE_AMD = "amd_exporter"

# Most exporters identify themselves via the standard `app.kubernetes.io/name`
# label. DCGM is the exception (uses legacy `app=nvidia-dcgm-exporter`);
# custom metrics uses the `crusoe.ai/scrape` annotation; AMD also requires a
# namespace match (handled by amd_manager).
APP_KUBERNETES_NAME_TO_TYPE = {
    "kube-state-metrics": POD_TYPE_KSM,
    "slurmctld": POD_TYPE_SLURM,
    "crusoe-metrics-exporter": POD_TYPE_CME,
}

@dataclass
class ExporterRuntimeConfig:
    """Per-exporter runtime knobs loaded from reloader_cfg.

    Covers the four fields every exporter section repeats: enabled flag, port,
    one-or-many scrape paths, and scrape interval. `paths` is always a list;
    single-path exporters wrap their `path` value during loading.
    """
    enabled: bool
    port: int
    paths: list
    scrape_interval: int

    @classmethod
    def from_dict(cls, cfg: dict, *, default_enabled=True, default_port=None,
                   default_paths=("/metrics",), default_scrape_interval=60):
        # Accept either "paths" (list, e.g. Slurm) or "path" (string, all others)
        if "paths" in cfg:
            paths = list(cfg["paths"])
        elif "path" in cfg:
            paths = [cfg["path"]]
        else:
            paths = list(default_paths)
        return cls(
            enabled=cfg.get("enabled", default_enabled),
            port=cfg.get("port", default_port),
            paths=paths,
            scrape_interval=cfg.get("scrape_interval", default_scrape_interval),
        )

    def build_endpoints(self, pod_ip):
        return [f"http://{pod_ip}:{self.port}{p}" for p in self.paths]


@dataclass
class ClusterExporterSpec:
    """Spec for a cluster-scoped exporter that owns its own (source, transform, sink) pipeline.

    Covers KSM / Slurm / CME — they all follow the same shape and only differ in
    naming, runtime config, transform VRL, and sink config. DCGM and AMD do NOT
    use this spec because they tap into the shared node-metrics transform
    instead of having a dedicated sink.
    """
    name: str           # human-readable, used for logs
    runtime: ExporterRuntimeConfig
    source_name: str
    transform_name: str
    sink_name: str
    transform_source: object  # LiteralStr
    sink_config: dict


# Logging setup
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(
    level=logging.INFO,  # overridden later by config's log_level
    handlers=[handler]
)
LOG = logging.getLogger(__name__)

class VectorConfigReloader:
    def __init__(self):
        self.node_name = os.environ.get("NODE_NAME")
        if not self.node_name:
            raise RuntimeError("NODE_NAME not set")

        self.running = True
        self._config_lock = threading.Lock()
        config.load_incluster_config()
        self.k8s_api_client = client.CoreV1Api()

        # State tracked across poll cycles for change detection
        self._tracked_pod_fingerprint = None  # frozenset; None means "first cycle, force reload"
        self._tracked_cm_checksum = None
        self._tracked_cm_data = {}

        reloader_cfg = YamlUtils.load_yaml_config(RELOADER_CONFIG_PATH)
        self.dcgm_cfg = ExporterRuntimeConfig.from_dict(
            reloader_cfg["dcgm_metrics"],
        )
        self.ksm_cfg = ExporterRuntimeConfig.from_dict(
            reloader_cfg.get("kube_state_metrics", {}),
            default_port=8080,
        )
        self.slurm_cfg = ExporterRuntimeConfig.from_dict(
            reloader_cfg.get("slurm_metrics", {}),
            default_enabled=False,
            default_port=6817,
            default_paths=["/metrics/jobs", "/metrics/jobs-users-accts", "/metrics/nodes", "/metrics/partitions", "/metrics/scheduler"],
        )
        self.cme_cfg = ExporterRuntimeConfig.from_dict(
            reloader_cfg.get("crusoe_metrics_exporter", {}),
            default_port=9500,
        )
        self.amd_manager = AmdExporterManager(reloader_cfg.get("amd_metrics", {}))
        self.custom_metrics_enabled = reloader_cfg["custom_metrics"].get("enabled", True)
        self.logs_enabled = reloader_cfg.get("logs", {}).get("enabled", True)
        self.default_custom_metrics_config = reloader_cfg["custom_metrics"]
        sink_endpoint = reloader_cfg["sink"].get("endpoint") or "https://cms-monitoring.crusoecloud.com"
        self.infra_sink_endpoint = f"{sink_endpoint}/ingest"
        self.custom_sink_endpoint = f"{sink_endpoint}/custom"
        self.cluster_sink_endpoint = f"{sink_endpoint}/cluster"
        self.sink_proxy_cfg = reloader_cfg["sink"].get("proxy", {}) or {}

        # Note: cluster metrics would need greater buffer allowance in the future when VMagent
        # is replaced with CWA, as metrics are proportional to the number of nodes.
        self.sink_buffer_config = {
            "type": "disk",
            "max_size": 268435488,  # 256 MiB
            "when_full": "block",
        }

        LOG.setLevel(reloader_cfg["log_level"])

        # Template for per-pod custom metrics sinks. `inputs` is filled in per
        # pod inside set_custom_metrics_scrape_config (one transform per pod).
        self.custom_metrics_sink_config = self._build_prom_remote_write_sink(
            self.custom_sink_endpoint, "cri:custom_metrics/${CRUSOE_CLUSTER_ID}", with_proxy=True,
        )
        # KSM sink intentionally does NOT use the proxy — it talks to the
        # in-cluster ingest path directly. Slurm/CME do route via proxy when configured.
        self.kube_state_metrics_sink_config = self._build_prom_remote_write_sink(
            self.cluster_sink_endpoint, "cri:cmk/${CRUSOE_CLUSTER_ID}", with_proxy=False,
        )
        self.slurm_metrics_sink_config = self._build_prom_remote_write_sink(
            self.cluster_sink_endpoint, "cri:cmk/${CRUSOE_CLUSTER_ID}", with_proxy=True,
        )
        self.crusoe_metrics_exporter_sink_config = self._build_prom_remote_write_sink(
            self.infra_sink_endpoint, "cri:vm/${VM_ID}", with_proxy=True,
        )

        # Specs for cluster-scoped exporters (each owns a source -> transform -> sink pipeline).
        # Note: CME's spec is finalized after vm_id is read from node labels (see below).
        self.ksm_spec = ClusterExporterSpec(
            name="kube_state_metrics",
            runtime=self.ksm_cfg,
            source_name="kube_state_metrics_scrape",
            transform_name="enrich_kube_state_metrics",
            sink_name="kube_state_metrics_sink",
            transform_source=LiteralStr("""
.tags.cluster_id = "${CRUSOE_CLUSTER_ID}"
.tags.project_id = "${CRUSOE_PROJECT_ID}"
.tags.crusoe_resource = "cmk"
.tags.metrics_source = "kube-state-metrics"
"""),
            sink_config=self.kube_state_metrics_sink_config,
        )
        self.slurm_spec = ClusterExporterSpec(
            name="slurm_metrics",
            runtime=self.slurm_cfg,
            source_name="slurm_metrics_scrape",
            transform_name="enrich_slurm_metrics",
            sink_name="slurm_metrics_sink",
            transform_source=LiteralStr("""
.tags.cluster_id = "${CRUSOE_CLUSTER_ID}"
.tags.project_id = "${CRUSOE_PROJECT_ID}"
.tags.crusoe_resource = "cmk"
.tags.metrics_source = "slurm-metrics"
"""),
            sink_config=self.slurm_metrics_sink_config,
        )

        self.logs_ingest_endpoint = f"{sink_endpoint}/logs/ingest"

        try:
            node = self.k8s_api_client.read_node(self.node_name)
            labels = node.metadata.labels
            self.vm_id = labels.get("crusoe.ai/instance.id", None)
            self.nodepool_id = labels.get("crusoe.ai/nodepool.id", None)
            self.instance_type = labels.get("beta.kubernetes.io/instance-type", None)
            self.pod_id = labels.get("crusoe.ai/pod.id", None)
            self.project_id = labels.get("crusoe.ai/project.id", None)
            self.hostname = labels.get("kubernetes.io/hostname", None)
        except Exception as e:
            cluster_id = os.environ.get('CRUSOE_CLUSTER_ID', 'unknown')
            vm_id = os.environ.get('VM_ID', 'unknown')
            project_id = os.environ.get('CRUSOE_PROJECT_ID', 'unknown')
            errors_total.labels(error_type="k8s_api_fetch_node_labels").inc()
            LOG.error(
                f"Fetch node labels failed. "
                f"cluster_id={cluster_id} vm_id={vm_id} project_id={project_id} error={str(e)}"
            )
            sys.exit(1)

        self.node_metrics_vector_transform_source = LiteralStr(f"""
.tags.nodepool = "{self.nodepool_id}"
.tags.cluster_id = "${{CRUSOE_CLUSTER_ID}}"
.tags.vm_id = "{self.vm_id}"
.tags.vm_instance_type = "{self.instance_type}"
.tags.node = "{self.hostname}"
if "{self.pod_id or ''}" != "" {{ .tags.pod_id = "{self.pod_id or ''}" }}
.tags.crusoe_resource = "vm"
.tags.metrics_source = "node-metrics"
""")

        self.cme_spec = ClusterExporterSpec(
            name="crusoe_metrics_exporter",
            runtime=self.cme_cfg,
            source_name="crusoe_metrics_exporter_scrape",
            transform_name="enrich_crusoe_metrics_exporter",
            sink_name="crusoe_metrics_exporter_sink",
            transform_source=LiteralStr(f"""
.tags.cluster_id = "${{CRUSOE_CLUSTER_ID}}"
.tags.project_id = "${{CRUSOE_PROJECT_ID}}"
.tags.vm_id = "{self.vm_id}"
.tags.crusoe_resource = "vm_custom_infra"
.tags.metrics_source = "crusoe-metrics-exporter"
"""),
            sink_config=self.crusoe_metrics_exporter_sink_config,
        )

        self.filter_log_collector_logs_transform_source = LiteralStr('''
.kubernetes.pod_namespace == "crusoe-system" && starts_with(string!(.kubernetes.pod_name), "crusoe-log-collector")
''')

        self.filter_vector_config_reloader_logs_transform_source = LiteralStr('''
.kubernetes.pod_namespace == "crusoe-system" && starts_with(string!(.kubernetes.pod_name), "crusoe-watch-agent") && .kubernetes.container_name == "vector-config-reloader"
''')

        # Drop containerd's harmless `max 0` cgroup-parse spam from hugetlb.events.
        # Misleadingly tagged level=error and unactionable from outside containerd.
        self.filter_journald_noise_transform_source = LiteralStr('''
!((string(.SYSLOG_IDENTIFIER) ?? "") == "containerd" && contains(string(.message) ?? "", "as a uint from Cgroup file"))
''')

        self.enrich_logs_transform_source = LiteralStr('''
# enrich_logs converges all parser branches and assembles the standardized envelope:
# the raw event verbatim under `payload`, Crusoe identity under `crusoe`, and
# _msg/_time/level/log_source at the top level.

# Pull the agent-derived scratch fields off the event before wrapping; what remains
# is the raw log, preserved verbatim under payload.
parsed_msg = null
if exists(._msg) {
    parsed_msg = del(._msg)
}
parsed_time = null
if exists(._time) {
    parsed_time = del(._time)
}
cwa_level = null
if exists(.level) {
    cwa_level = del(.level)
}
cwa_log_source = null
if exists(.log_source) {
    cwa_log_source = del(.log_source)
}

raw = .
. = {}
.payload = raw

# Crusoe identity metadata; remaining identity fields are set by CML Ingress.
.crusoe = { "agent": "crusoe-watch-agent", "chart_version": "${CHART_VERSION}" }

if cwa_log_source != null {
    .log_source = cwa_log_source
}

if parsed_msg != null {
    ._msg = parsed_msg
} else if exists(.payload.message) {
    ._msg = .payload.message
}

if parsed_time != null {
    ._time = parsed_time
} else if exists(.payload.__REALTIME_TIMESTAMP) {
    ._time = .payload.__REALTIME_TIMESTAMP
} else if exists(.payload.timestamp) {
    ._time = .payload.timestamp
}

# Normalize level to canonical enum (mirrors GCP Cloud Logging SEVERITY_TRANSLATIONS).
# Unrecognized values are omitted.
level_synonyms = {
    "emergency": "emergency", "emerg": "emergency",
    "alert": "alert", "a": "alert",
    "critical": "critical", "crit": "critical", "fatal": "critical", "c": "critical", "f": "critical",
    "error": "error", "err": "error", "severe": "error", "e": "error",
    "warning": "warning", "warn": "warning", "w": "warning",
    "notice": "notice", "n": "notice",
    "info": "info", "information": "info", "i": "info",
    "debug": "debug", "trace": "debug", "trace_int": "debug",
    "fine": "debug", "finer": "debug", "finest": "debug", "config": "debug", "d": "debug"
}
if cwa_level != null {
    lvl = downcase(string!(cwa_level))
    normalized = get(level_synonyms, [lvl]) ?? null
    if normalized != null {
        .level = normalized
    }
}
''')

        self.parse_journald_logs_transform_source = LiteralStr('''
.log_source = "journald"

# Infallible bind — events without MESSAGE would otherwise abort the transform.
msg = string(.message) ?? ""

# Map syslog PRIORITY to a canonical level, staged in scratch .level (enrich_logs
# normalizes it to the top-level `level`). The raw event keeps PRIORITY untouched.
# Coerce defensively: journald emits PRIORITY as a string, but other
# upstreams (e.g. forwarded syslog) may not.
priority = to_string(.PRIORITY) ?? ""
if priority == "0" {
    .level = "emergency"
} else if priority == "1" {
    .level = "alert"
} else if priority == "2" {
    .level = "critical"
} else if priority == "3" {
    .level = "error"
} else if priority == "4" {
    .level = "warning"
} else if priority == "5" {
    .level = "notice"
} else if priority == "6" {
    .level = "info"
} else if priority == "7" {
    .level = "debug"
}

# Default: parsed message = MESSAGE verbatim. The logfmt/klog blocks below may extract a
# cleaner _msg / level / time into scratch. We extract ONLY those three — we do not
# materialize other structured fields onto the event, so payload stays raw (the full
# MESSAGE is preserved at payload.message; use unpack_logfmt at query time if needed).
._msg = msg

# Narrow logfmt whitelist. parse_logfmt is intentionally permissive
# (see vector#6418), so we only run it on identifiers known to emit
# real logfmt-shaped MESSAGE and gate on a `^\\S+=` sniff.
logfmt_emitters = ["containerd", "dockerd", "etcd"]
syslog_id = string(.SYSLOG_IDENTIFIER) ?? ""

if includes(logfmt_emitters, syslog_id) && match(msg, r'^\\S+=') {
    parsed, err = parse_logfmt(msg)
    if err == null && is_object(parsed) {
        structured_fields = object(parsed)
        if exists(structured_fields.msg) {
            ._msg = string!(structured_fields.msg)
        }
        if exists(structured_fields.time) {
            parsed_time, ts_err = parse_timestamp(string!(structured_fields.time), format: "%+")
            if ts_err == null {
                ._time = parsed_time
            }
        }
        if exists(structured_fields.level) {
            .level = downcase(string!(structured_fields.level))
        }
    }
}

# klog prefix parser. Without this, kubelet stdout is always level=info
# (journald PRIORITY is fixed at 6); parse_klog reads the leading severity
# letter. We extract only message/level/timestamp into scratch — the raw line
# stays at payload.message.
klog_emitters = ["kubelet", "kube-proxy"]
if includes(klog_emitters, syslog_id) {
    parsed_klog, klog_err = parse_klog(msg)
    if klog_err == null && is_object(parsed_klog) {
        if exists(parsed_klog.message) {
            ._msg = string!(parsed_klog.message)
        }
        if exists(parsed_klog.level) {
            .level = string!(parsed_klog.level)
        }
        if exists(parsed_klog.timestamp) {
            ._time = parsed_klog.timestamp
        }
    }
}

# Nothing is added to or removed from the raw event: enrich_logs wraps it verbatim under
# payload and lifts the scratch ._msg / ._time / .level / .log_source out.
''')

        self.parse_crusoe_container_logs_transform_source = LiteralStr('''
if exists(.kubernetes.pod_labels.app) && starts_with(string!(.kubernetes.pod_labels.app), "crusoe-log-collector") {
    .log_source = "crusoe-log-collector"

    # Parse JSON log format (efficient, no regex needed)
    if exists(.message) {
        parsed, err = parse_json(string!(.message))

        if err == null {
            # Extract level and message from JSON
            if exists(parsed.level) {
                .level = string!(parsed.level)
            }
            if exists(parsed.message) {
                # Stage the parsed message for enrich_logs to lift to top-level _msg;
                # the raw JSON line stays in .message (preserved as payload.message).
                ._msg = string!(parsed.message)
            }
            # Note: parsed.timestamp exists but we use kubernetes timestamp
        }
    }
}

if (to_string(.kubernetes.container_name) ?? "") == "vector-config-reloader" {
    .log_source = "cwa-config-reloader"

    # Parse JSON log format (efficient, no regex needed)
    if exists(.message) {
        parsed, err = parse_json(string!(.message))

        if err == null {
            # Extract level and message from JSON
            if exists(parsed.level) {
                .level = string!(parsed.level)
            }
            if exists(parsed.message) {
                # Stage the parsed message for enrich_logs to lift to top-level _msg;
                # the raw JSON line stays in .message (preserved as payload.message).
                ._msg = string!(parsed.message)
            }
            # Note: parsed.timestamp exists but we use kubernetes timestamp
        }
    }
}
''')

        self.parse_internal_logs_transform_source = LiteralStr('''
.log_source = "crusoe-watch-agent"

if exists(.metadata.level) {
    .level = downcase(string!(.metadata.level))
}
''')

    @staticmethod
    def sanitize_name(name: str) -> str:
        # replace invalid chars with underscores
        return re.sub(r'[^a-zA-Z0-9_]', '_', name)

    @staticmethod
    def is_pod_active(pod):
        """A pod is considered active if it's Running and not scheduled for deletion."""
        return (
            pod.status.phase == "Running"
            and not getattr(pod.metadata, "deletion_timestamp", None)
        )

    def classify_pod(self, pod):
        """Return a string label for the pod type, or None if not relevant.

        Custom metrics is opt-in via annotation; DCGM uses the legacy `app`
        label; KSM/Slurm/CME use the standard `app.kubernetes.io/name` label
        and are looked up via APP_KUBERNETES_NAME_TO_TYPE; AMD additionally
        requires a namespace match (handled inside amd_manager).
        """
        annotations = pod.metadata.annotations or {}
        if annotations.get(CUSTOM_METRICS_SCRAPE_ANNOTATION) == "true":
            return POD_TYPE_CUSTOM

        labels = pod.metadata.labels or {}
        if labels.get("app") == "nvidia-dcgm-exporter":
            return POD_TYPE_DCGM

        pod_type = APP_KUBERNETES_NAME_TO_TYPE.get(labels.get("app.kubernetes.io/name"))
        if pod_type:
            return pod_type

        if self.amd_manager.is_exporter_pod(pod):
            return POD_TYPE_AMD

        return None

    def _build_prom_remote_write_sink(self, endpoint: str, tenant_id: str, *, with_proxy: bool) -> dict:
        """Build a prometheus_remote_write sink config.

        All metric sinks share auth, TLS, batching, and healthcheck settings;
        only endpoint, tenant_id, and whether the sink routes via the proxy
        differ between exporters.
        """
        cfg = {
            "type": "prometheus_remote_write",
            "endpoint": endpoint,
            "tenant_id": tenant_id,
            "auth": {"strategy": "bearer", "token": "${CRUSOE_MONITORING_TOKEN}"},
            "healthcheck": {"enabled": False},
            "compression": "snappy",
            "request": {"concurrency": "adaptive"},
            "batch": {"max_bytes": 500000, "aggregate": False},
            "tls": {"verify_certificate": True, "verify_hostname": True, "alpn_protocols": ["h2", "http/1.1"]},
        }
        if with_proxy and self.sink_proxy_cfg.get("enabled"):
            cfg["proxy"] = self.sink_proxy_cfg
        return cfg

    def handle_sigterm(self, sig, frame):
        LOG.info("Received SIGTERM/SIGINT, shutting down poll loop.")
        self.running = False

    def get_dcgm_exporter_scrape_endpoint(self, pod_ip) -> str:
        # DCGM is single-path; use the first (and only) endpoint.
        return self.dcgm_cfg.build_endpoints(pod_ip)[0]

    def get_deployment_metrics_config(self, deployment_name: str, cm_data: dict) -> dict:
        """Resolve per-deployment custom metrics rules from cached configmap data."""
        if not deployment_name:
            return {}
        config_yaml = (cm_data or {}).get(CUSTOM_METRICS_CONFIG_MAP_KEY, "")
        if not config_yaml:
            return {}
        cfg = YamlUtils.load_yaml_string(config_yaml)
        return cfg.get(deployment_name, {})

    def build_deployment_transform_source(self, deployment_config: dict, endpoint_config: dict) -> str:
        vrl_lines = []
        allowlist = deployment_config.get("allowlist", [])
        droplist = deployment_config.get("droplist", [])
        drop_labels = deployment_config.get("dropLabels", [])
        add_labels = deployment_config.get("addLabels", [])

        if allowlist:
            allowed_metrics = ", ".join([f'"{m}"' for m in allowlist])
            vrl_lines.append(f'allowed_metrics = [{allowed_metrics}]')
            vrl_lines.append('if !includes(allowed_metrics, .name) { abort }')
        elif droplist:
            dropped_metrics = ", ".join([f'"{m}"' for m in droplist])
            vrl_lines.append(f'dropped_metrics = [{dropped_metrics}]')
            vrl_lines.append('if includes(dropped_metrics, .name) { abort }')

        for label in drop_labels:
            vrl_lines.append(f'del(.tags.{label})')

        for label_entry in add_labels:
            if isinstance(label_entry, dict):
                for key, value in label_entry.items():
                    vrl_lines.append(f'.tags.{key} = "{value}"')

        # add only crusoe_resource tag if app_id is present
        if endpoint_config.get("app_id"):
            vrl_lines.append(f'.tags.pod_ip = "{endpoint_config["pod_ip"]}"')
            vrl_lines.append(f'.tags.pod_name = "{endpoint_config["pod_name"]}"')
            vrl_lines.append(f'.tags.crusoe_resource = "custom_internal"')
            vrl_lines.append('.tags.cluster_id = "${CRUSOE_CLUSTER_ID}"')
            vrl_lines.append(f'.tags.app_id = "{endpoint_config["app_id"]}"')
        else:
            vrl_lines.append(f'.tags.nodepool = "{self.nodepool_id}"')
            vrl_lines.append('.tags.cluster_id = "${CRUSOE_CLUSTER_ID}"')
            vrl_lines.append(f'.tags.vm_id = "{self.vm_id}"')
            vrl_lines.append(f'.tags.vm_instance_type = "{self.instance_type}"')
            vrl_lines.append(f'if "{self.pod_id or ""}" != "" {{ .tags.pod_id = "{self.pod_id or ""}" }}')
            vrl_lines.append('.tags.crusoe_resource = "custom_metrics"')
            vrl_lines.append('.tags.metrics_source = "custom-metrics"')
            vrl_lines.append(f'.tags.pod_ip = "{endpoint_config["pod_ip"]}"')
            vrl_lines.append(f'.tags.pod_name = "{endpoint_config["pod_name"]}"')

        return "\n".join(vrl_lines)

    def get_custom_metrics_endpoint_cfg(self, pod) -> dict:
        pod_ip = pod.status.pod_ip
        pod_name = pod.metadata.name
        annotations = pod.metadata.annotations
        port = int(annotations.get(CUSTOM_METRICS_PORT_ANNOTATION, self.default_custom_metrics_config["port"]))
        path = annotations.get(CUSTOM_METRICS_PATH_ANNOTATION, self.default_custom_metrics_config["path"])
        app_id = annotations.get(CUSTOM_METRICS_APP_ID_ANNOTATION, "")

        parts = pod_name.rsplit("-", 2)
        deployment_name = parts[0] if len(parts) == 3 else ""
        return {
            "url": f"http://{pod_ip}:{port}{path}",
            "pod_ip": pod_ip,
            "pod_name": pod_name,
            "deployment_name": deployment_name,
            "app_id": app_id
        }

    def set_dcgm_exporter_scrape_config(self, vector_cfg: dict, dcgm_exporter_scrape_endpoint: str):
        if dcgm_exporter_scrape_endpoint is None:
            return
        if not self.dcgm_cfg.enabled:
            LOG.info("DCGM metrics disabled, skipping scrape config")
            return
        vector_cfg.setdefault("sources", {})[DCGM_EXPORTER_SOURCE_NAME] = {
            "type": "prometheus_scrape",
            "endpoints": [dcgm_exporter_scrape_endpoint],
            "scrape_interval_secs": self.dcgm_cfg.scrape_interval,
            "scrape_timeout_secs": int(self.dcgm_cfg.scrape_interval * SCRAPE_TIMEOUT_PERCENTAGE)
        }
        inputs = set(vector_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["inputs"])
        if DCGM_EXPORTER_SOURCE_NAME not in inputs:
            vector_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["inputs"].append(DCGM_EXPORTER_SOURCE_NAME)

    def _apply_cluster_exporter(self, vector_cfg: dict, spec: ClusterExporterSpec, pod_ip: str):
        """Wire a cluster-scoped exporter (KSM/Slurm/CME) into the Vector config."""
        if pod_ip is None:
            return
        if not spec.runtime.enabled:
            LOG.info(f"{spec.name} disabled, skipping scrape config")
            return
        vector_cfg.setdefault("sources", {})[spec.source_name] = {
            "type": "prometheus_scrape",
            "endpoints": spec.runtime.build_endpoints(pod_ip),
            "scrape_interval_secs": spec.runtime.scrape_interval,
            "scrape_timeout_secs": int(spec.runtime.scrape_interval * SCRAPE_TIMEOUT_PERCENTAGE),
        }
        vector_cfg.setdefault("transforms", {})[spec.transform_name] = {
            "type": "remap",
            "inputs": [spec.source_name],
            "source": spec.transform_source,
        }
        # Wire the sink to read from this exporter's transform; copy so we don't
        # mutate the spec's stored sink_config across reconcile cycles.
        sink = dict(spec.sink_config, inputs=[spec.transform_name])
        sink["buffer"] = self.sink_buffer_config
        vector_cfg.setdefault("sinks", {})[spec.sink_name] = sink

    def set_logs_config(self, vector_cfg: dict):
        """Set log sources, transforms, and sink for journald and agent logs."""
        if not self.logs_enabled:
            LOG.info("Logs disabled, skipping logs config")
            return
        sources = vector_cfg.setdefault("sources", {})
        transforms = vector_cfg.setdefault("transforms", {})
        sinks = vector_cfg.setdefault("sinks", {})

        sources[JOURNALD_LOGS_SOURCE_NAME] = {
            "type": "journald",
            "journal_directory": "/var/log/journal",
            "since_now": True
        }

        sources[KUBERNETES_LOGS_SOURCE_NAME] = {
            "type": "kubernetes_logs",
            "include_paths_glob_patterns": [
                "/var/log/pods/crusoe-system_crusoe-log-collector-*/*/*.log",
                "/var/log/pods/crusoe-system_crusoe-watch-agent-*/vector-config-reloader/*.log"
            ]
        }

        sources[VECTOR_INTERNAL_LOGS_SOURCE_NAME] = {
            "type": "internal_logs"
        }

        transforms[FILTER_CRUSOE_LOG_COLLECTOR_LOGS_TRANSFORM_NAME] = {
            "type": "filter",
            "inputs": [KUBERNETES_LOGS_SOURCE_NAME],
            "condition": {
                "type": "vrl",
                "source": self.filter_log_collector_logs_transform_source
            }
        }

        transforms[FILTER_VECTOR_CONFIG_RELOADER_LOGS_TRANSFORM_NAME] = {
            "type": "filter",
            "inputs": [KUBERNETES_LOGS_SOURCE_NAME],
            "condition": {
                "type": "vrl",
                "source": self.filter_vector_config_reloader_logs_transform_source
            }
        }

        transforms[FILTER_JOURNALD_NOISE_TRANSFORM_NAME] = {
            "type": "filter",
            "inputs": [JOURNALD_LOGS_SOURCE_NAME],
            "condition": {
                "type": "vrl",
                "source": self.filter_journald_noise_transform_source
            }
        }

        # Parallel branches: each parser takes only its source(s). Then enrich_logs
        # converges all parsers, builds the crusoe identity namespace (agent,
        # chart_version, cluster_id), and runs common cleanup (envelope wrapping,
        # _time/_msg fallbacks, level normalization).
        transforms[PARSE_JOURNALD_LOGS_TRANSFORM_NAME] = {
            "type": "remap",
            "inputs": [FILTER_JOURNALD_NOISE_TRANSFORM_NAME],
            "source": self.parse_journald_logs_transform_source
        }

        transforms[PARSE_CRUSOE_CONTAINER_LOGS_TRANSFORM_NAME] = {
            "type": "remap",
            "inputs": [
                FILTER_CRUSOE_LOG_COLLECTOR_LOGS_TRANSFORM_NAME,
                FILTER_VECTOR_CONFIG_RELOADER_LOGS_TRANSFORM_NAME,
            ],
            "source": self.parse_crusoe_container_logs_transform_source
        }

        transforms[PARSE_INTERNAL_LOGS_TRANSFORM_NAME] = {
            "type": "remap",
            "inputs": [VECTOR_INTERNAL_LOGS_SOURCE_NAME],
            "source": self.parse_internal_logs_transform_source
        }

        transforms[ENRICH_LOGS_TRANSFORM_NAME] = {
            "type": "remap",
            "inputs": [
                PARSE_JOURNALD_LOGS_TRANSFORM_NAME,
                PARSE_CRUSOE_CONTAINER_LOGS_TRANSFORM_NAME,
                PARSE_INTERNAL_LOGS_TRANSFORM_NAME,
            ],
            "source": self.enrich_logs_transform_source
        }

        sink_config = {
            "type": "http",
            "inputs": [ENRICH_LOGS_TRANSFORM_NAME],
            "uri": self.logs_ingest_endpoint,
            "framing": {"method": "newline_delimited"},
            "compression": "snappy",
            "healthcheck": {"enabled": False},
            "request": {
                "headers": {
                    "X-Crusoe-Vm-Id": "${VM_ID:-unknown}",
                    "User-Agent": "CrusoeWatchAgent/CMK-${CHART_VERSION}",
                },
            },
            "auth": {"strategy": "bearer", "token": "${CRUSOE_MONITORING_TOKEN}"},
            "encoding": {"codec": "json"},
            "batch": {"max_bytes": 100000},
            "tls": {"verify_certificate": True, "verify_hostname": True, "alpn_protocols": ["h2", "http/1.1"]}
        }
        if self.sink_proxy_cfg.get("enabled"):
            sink_config["proxy"] = self.sink_proxy_cfg
        sinks["crusoe_ingest"] = sink_config

        LOG.debug("Logs config set for all log sources (journald, kubernetes, vector internal)")

    def set_custom_metrics_scrape_config(self, vector_cfg: dict, custom_metrics_eps: list, cm_data: dict):
        if not custom_metrics_eps:
            return
        if not self.custom_metrics_enabled:
            LOG.info("Custom metrics disabled, skipping scrape config")
            return
        sources = vector_cfg.setdefault("sources", {})
        transforms = vector_cfg.setdefault("transforms", {})
        sinks = vector_cfg.setdefault("sinks", {})

        for endpoint in custom_metrics_eps:
            pod_name_sanitized = VectorConfigReloader.sanitize_name(endpoint['pod_name'])
            source_name = f"{pod_name_sanitized}_scrape"

            deployment_name = endpoint.get("deployment_name", "")
            deployment_config = self.get_deployment_metrics_config(deployment_name, cm_data)

            scrape_interval_secs = max(deployment_config.get("scrape_interval_secs", CUSTOM_METRICS_DEFAULT_SCRAPE_INTERVAL), SCRAPE_INTERVAL_MIN_THRESHOLD)
            if scrape_interval_secs < SCRAPE_INTERVAL_MIN_THRESHOLD:
                LOG.warning(f'For pod {endpoint["pod_name"]}, scrape interval set to: {scrape_interval_secs} (less than 5 seconds), defaulting to {SCRAPE_INTERVAL_MIN_THRESHOLD}')

            sources[source_name] = {
                "type": "prometheus_scrape",
                "endpoints": [endpoint["url"]],
                "scrape_interval_secs": scrape_interval_secs,
                "scrape_timeout_secs": int(scrape_interval_secs * SCRAPE_TIMEOUT_PERCENTAGE)
            }

            transform_name = f"{pod_name_sanitized}_transform"
            transform_source = self.build_deployment_transform_source(deployment_config, endpoint)
            transforms[transform_name] = {
                "type": "remap",
                "inputs": [source_name],
                "drop_on_abort": True,
                "source": LiteralStr(transform_source)
            }

            sink_name = f"{pod_name_sanitized}_sink"
            sink_config = self.custom_metrics_sink_config.copy()
            sink_config["inputs"] = [transform_name]
            if endpoint.get("app_id"):
                LOG.info(f"Managed custom metrics pod detected: '{endpoint['pod_name']}'")
                sink_config["endpoint"] = f"{self.custom_sink_endpoint}/{endpoint['app_id']}"
            sinks[sink_name] = sink_config
            LOG.debug(f"Created custom metrics pipeline for pod '{endpoint['pod_name']}'")

    # -------- Polling, fingerprinting, and reconcile --------

    def _list_active_relevant_pods(self):
        """List all Running, non-terminating pods on the node and classify them.

        Returns: list of (pod, pod_type) tuples, only including relevant pods.
        Raises on API failure so the caller can decide retry behavior.
        """
        try:
            pods = self.k8s_api_client.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={self.node_name},status.phase=Running"
            ).items
        except Exception as e:
            errors_total.labels(error_type="k8s_api_list_pods").inc()
            LOG.error(f"Failed to list pods on node {self.node_name}: {e}")
            raise

        relevant = []
        skipped_terminating = 0
        skipped_irrelevant = 0
        skipped_no_ip = 0
        for pod in pods:
            if not VectorConfigReloader.is_pod_active(pod):
                skipped_terminating += 1
                continue
            pod_type = self.classify_pod(pod)
            if pod_type is None:
                skipped_irrelevant += 1
                continue
            if not pod.status.pod_ip:
                skipped_no_ip += 1
                LOG.warning(f"Pod {pod.metadata.name} ({pod_type}) is Running but has no pod IP yet; will retry next cycle.")
                continue
            relevant.append((pod, pod_type))

        LOG.debug(
            f"Listed {len(pods)} pods on node, "
            f"relevant={len(relevant)}, "
            f"skipped_terminating={skipped_terminating}, "
            f"skipped_irrelevant={skipped_irrelevant}, "
            f"skipped_no_ip={skipped_no_ip}"
        )
        return relevant

    def _compute_pod_fingerprint(self, classified_pods):
        """Build a frozenset of (name, ip, type) for the relevant pod set.

        Name catches Deployment-style recreation (new random suffix); IP catches
        StatefulSet-style in-place restart (stable name, new IP); type guards
        against a pod morphing between exporter classifications. In-place
        annotation edits on custom metrics pods (e.g. `kubectl annotate` to
        change port/path) are intentionally not tracked — they're rare, and a
        pod recreation is the standard way to roll those changes.
        """
        return frozenset(
            (pod.metadata.name, pod.status.pod_ip, pod_type)
            for pod, pod_type in classified_pods
        )

    def _fetch_configmap_data(self):
        """Fetch the custom metrics ConfigMap and return its data dict.

        Returns None on API failure so the caller can fall back to the last
        known good cache rather than wiping per-deployment rules.
        """
        try:
            cm = self.k8s_api_client.read_namespaced_config_map(
                name=CUSTOM_METRICS_CONFIG_MAP_NAME,
                namespace=CUSTOM_METRICS_CM_NAMESPACE,
            )
            return cm.data or {}
        except client.ApiException as e:
            if e.status == 404:
                LOG.warning(
                    f"ConfigMap '{CUSTOM_METRICS_CONFIG_MAP_NAME}' not found in '{CUSTOM_METRICS_CM_NAMESPACE}'; "
                    f"using empty config."
                )
                return {}
            errors_total.labels(error_type="k8s_api_read_configmap").inc()
            LOG.error(f"Failed to read ConfigMap '{CUSTOM_METRICS_CONFIG_MAP_NAME}': {e}")
            return None
        except Exception as e:
            errors_total.labels(error_type="k8s_api_read_configmap").inc()
            LOG.error(f"Unexpected error reading ConfigMap '{CUSTOM_METRICS_CONFIG_MAP_NAME}': {e}")
            return None

    @staticmethod
    def _compute_cm_checksum(cm_data: dict) -> str:
        serialized = json.dumps(cm_data or {}, sort_keys=True).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    def _log_pod_fingerprint_diff(self, old_fp, new_fp):
        if old_fp is None:
            summary = sorted((name, t) for (name, _ip, t) in new_fp)
            LOG.info(f"Initial pod set: {summary}")
            return
        # Group by name to detect IP changes for the same pod (StatefulSet restart case)
        old_by_name = {name: (ip, t) for (name, ip, t) in old_fp}
        new_by_name = {name: (ip, t) for (name, ip, t) in new_fp}
        added = sorted((name, new_by_name[name][1]) for name in new_by_name.keys() - old_by_name.keys())
        removed = sorted((name, old_by_name[name][1]) for name in old_by_name.keys() - new_by_name.keys())
        ip_changed = sorted(
            (name, old_by_name[name][0], new_by_name[name][0])
            for name in old_by_name.keys() & new_by_name.keys()
            if old_by_name[name][0] != new_by_name[name][0]
        )
        if added:
            LOG.info(f"Pod set diff: added={added}")
        if removed:
            LOG.info(f"Pod set diff: removed={removed}")
        if ip_changed:
            LOG.info(f"Pod set diff: ip_changed={ip_changed}")

    def _build_and_write_config(self, classified_pods, cm_data):
        """Rebuild the full Vector config from the base template and write atomically.

        Always operates on a fresh copy of the base config so we never
        accumulate stale entries from previous cycles.
        """
        base_cfg = YamlUtils.load_yaml_config(VECTOR_BASE_CONFIG_PATH)

        # Map each cluster-scoped exporter type to its spec for unified handling
        cluster_specs_by_type = {
            POD_TYPE_KSM: self.ksm_spec,
            POD_TYPE_SLURM: self.slurm_spec,
            POD_TYPE_CME: self.cme_spec,
        }
        cluster_pod_ips = {}  # pod_type -> pod_ip
        dcgm_exporter_ep = None
        amd_exporter_ep = None
        custom_metrics_eps = []
        for pod, pod_type in classified_pods:
            ip = pod.status.pod_ip
            if pod_type == POD_TYPE_CUSTOM:
                custom_metrics_eps.append(self.get_custom_metrics_endpoint_cfg(pod))
            elif pod_type == POD_TYPE_DCGM:
                dcgm_exporter_ep = self.get_dcgm_exporter_scrape_endpoint(ip)
            elif pod_type == POD_TYPE_AMD:
                amd_exporter_ep = self.amd_manager.build_endpoint(ip)
            elif pod_type in cluster_specs_by_type:
                cluster_pod_ips[pod_type] = ip

        self.set_custom_metrics_scrape_config(base_cfg, custom_metrics_eps, cm_data)
        self.set_dcgm_exporter_scrape_config(base_cfg, dcgm_exporter_ep)
        for pod_type, spec in cluster_specs_by_type.items():
            self._apply_cluster_exporter(base_cfg, spec, cluster_pod_ips.get(pod_type))
        self.set_logs_config(base_cfg)
        if self.amd_manager.enabled and amd_exporter_ep:
            self.amd_manager.set_scrape(base_cfg, amd_exporter_ep, NODE_METRICS_VECTOR_TRANSFORM_NAME, SCRAPE_TIMEOUT_PERCENTAGE)

        base_cfg["sinks"]["cms_gateway_node_metrics"]["endpoint"] = self.infra_sink_endpoint
        if self.sink_proxy_cfg.get("enabled"):
            base_cfg["sinks"]["cms_gateway_node_metrics"]["proxy"] = self.sink_proxy_cfg

        base_cfg["sinks"]["cms_gateway_node_metrics"]["buffer"] = self.sink_buffer_config

        # Always reassign so LiteralStr is preserved through any deepcopy semantics
        base_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["source"] = self.node_metrics_vector_transform_source

        with self._config_lock:
            YamlUtils.save_yaml(VECTOR_CONFIG_PATH, base_cfg)

        counts_by_type = {}
        for _, pod_type in classified_pods:
            counts_by_type[pod_type] = counts_by_type.get(pod_type, 0) + 1
        LOG.info(f"Vector config written. Scraped pods by type: {counts_by_type or 'none'}")

    def reconcile_once(self):
        """Run a single reconcile cycle: poll pods, poll configmap, reload if changed.

        Order: pods -> configmap -> reload. A failure in either fetch is logged
        and the cycle is skipped (state is not updated, so the next cycle will
        re-evaluate cleanly).
        """
        cycle_start = time.monotonic()
        LOG.debug("Reconcile cycle starting.")

        # 1. Poll pods
        try:
            classified_pods = self._list_active_relevant_pods()
        except Exception:
            return  # already logged
        new_fingerprint = self._compute_pod_fingerprint(classified_pods)
        pods_changed = new_fingerprint != self._tracked_pod_fingerprint

        # 2. Poll configmap
        cm_data = self._fetch_configmap_data()
        if cm_data is None:
            LOG.warning("ConfigMap fetch failed; reusing last known data for this cycle.")
            cm_data = self._tracked_cm_data
            cm_changed = False
        else:
            new_cm_checksum = self._compute_cm_checksum(cm_data)
            cm_changed = new_cm_checksum != self._tracked_cm_checksum

        # 3. Reload only if anything changed (or first cycle)
        if not pods_changed and not cm_changed:
            elapsed = time.monotonic() - cycle_start
            LOG.info(f"Reconcile: no changes detected (pods={len(classified_pods)}, cycle_ms={int(elapsed*1000)}).")
            return

        reasons = []
        if pods_changed:
            reasons.append("pod set changed")
            self._log_pod_fingerprint_diff(self._tracked_pod_fingerprint, new_fingerprint)
        if cm_changed:
            reasons.append("configmap changed")
            old_summary = "<none>" if self._tracked_cm_checksum is None else self._tracked_cm_checksum[:12]
            new_summary = self._compute_cm_checksum(cm_data)[:12]
            LOG.info(f"ConfigMap checksum changed: {old_summary} -> {new_summary}")

        LOG.info(f"Reconcile triggering reload. Reasons: {', '.join(reasons)}.")
        try:
            self._build_and_write_config(classified_pods, cm_data)
        except Exception as e:
            errors_total.labels(error_type="reload").inc()
            LOG.error(f"Reload failed: {e}. State not advanced; will retry next cycle.")
            self._fallback_if_no_config()
            return

        # Only advance tracked state on successful write so failures retry next cycle
        self._tracked_pod_fingerprint = new_fingerprint
        if cm_changed:
            self._tracked_cm_checksum = self._compute_cm_checksum(cm_data)
            self._tracked_cm_data = cm_data
        elapsed = time.monotonic() - cycle_start
        LOG.info(f"Reconcile complete. cycle_ms={int(elapsed*1000)}.")

    def _fallback_if_no_config(self):
        """If no Vector config file exists yet, copy the base as a last-resort fallback.

        Only triggered when a reload fails and Vector has nothing to start with.
        """
        if Path(VECTOR_CONFIG_PATH).exists():
            return
        LOG.error(f"No Vector config exists at {VECTOR_CONFIG_PATH}; falling back to base config.")
        try:
            import shutil
            with self._config_lock:
                shutil.copy(VECTOR_BASE_CONFIG_PATH, VECTOR_CONFIG_PATH)
        except Exception as e:
            LOG.error(f"Fallback copy of base config failed: {e}")

    def run_poll_loop(self):
        LOG.info(f"Starting poll loop. Interval: {RECONCILE_INTERVAL_SECS}s.")
        while self.running:
            try:
                self.reconcile_once()
            except Exception as e:
                errors_total.labels(error_type="reconcile_loop").inc()
                LOG.error(f"Unexpected error in reconcile cycle: {e}")
            # Interruptible sleep so SIGTERM doesn't have to wait the full interval
            for _ in range(RECONCILE_INTERVAL_SECS):
                if not self.running:
                    break
                time.sleep(1)
        LOG.info("Poll loop exiting.")

    def execute(self):
        # Treat unset, empty, or non-numeric VCR_METRICS_PORT the same: fall back to default.
        raw_port = (os.environ.get('VCR_METRICS_PORT') or '').strip()
        try:
            metrics_port = int(raw_port) if raw_port else 9091
        except ValueError:
            LOG.warning(f"Invalid VCR_METRICS_PORT={raw_port!r}; falling back to 9091")
            metrics_port = 9091

        try:
            start_http_server(metrics_port)
            LOG.info(f"Started Prometheus metrics server on port {metrics_port}")
        except Exception as e:
            LOG.warning(f"Failed to start metrics server on port {metrics_port}: {e}", exc_info=True)

        signal.signal(signal.SIGINT, self.handle_sigterm)
        signal.signal(signal.SIGTERM, self.handle_sigterm)

        # Run the loop on the main thread so the process exits cleanly with it
        self.run_poll_loop()

        LOG.info("Exiting config reloader.")

if __name__ == "__main__":
    VectorConfigReloader().execute()
