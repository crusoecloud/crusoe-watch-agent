import os, signal, re, logging, threading, sys
from kubernetes import client, config, watch
from utils import LiteralStr, YamlUtils
from amd_exporter import AmdExporterManager

CUSTOM_METRICS_CM_NAMESPACE = "crusoe-monitoring"
VECTOR_CONFIG_PATH = "/etc/vector/vector.yaml"
VECTOR_BASE_CONFIG_PATH = "/etc/vector-base/vector.yaml"
RELOADER_CONFIG_PATH = "/etc/reloader/config.yaml"

DCGM_EXPORTER_SOURCE_NAME = "dcgm_exporter_scrape"
DCGM_EXPORTER_APP_LABEL = "nvidia-dcgm-exporter"

# Log sources and pipeline constants
DMESG_LOGS_SOURCE_NAME = "dmesg_logs"
CRUSOE_INGEST_SINK_NAME = "crusoe_ingest"
ENRICH_LOGS_TRANSFORM_NAME = "enrich_logs"
JOURNALD_LOGS_SOURCE_NAME = "journald_logs"
KUBE_STATE_METRICS_SOURCE_NAME = "kube_state_metrics_scrape"
KUBE_STATE_METRICS_TRANSFORM_NAME = "enrich_kube_state_metrics"
KUBE_STATE_METRICS_SINK_NAME = "kube_state_metrics_sink"
KUBE_STATE_METRICS_APP_LABEL = "kube-state-metrics"
KUBE_STATE_METRICS_TRANSFORM_SOURCE = LiteralStr("""
.tags.cluster_id = "${CRUSOE_CLUSTER_ID}"
.tags.project_id = "${CRUSOE_PROJECT_ID}"
.tags.crusoe_resource = "cmk"
.tags.metrics_source = "kube-state-metrics"
""")
NODE_METRICS_VECTOR_TRANSFORM_NAME = "enrich_node_metrics"
CUSTOM_METRICS_VECTOR_TRANSFORM_NAME = "enrich_custom_metrics"
CUSTOM_METRICS_CONFIG_MAP_NAME = "crusoe-custom-metrics-config"
CUSTOM_METRICS_CONFIG_MAP_KEY = "custom-metrics-config.yaml"
CUSTOM_METRICS_SCRAPE_ANNOTATION = "crusoe.ai/scrape"
CUSTOM_METRICS_PORT_ANNOTATION = "crusoe.ai/port"
CUSTOM_METRICS_PATH_ANNOTATION = "crusoe.ai/path"
CUSTOM_METRICS_DEFAULT_SCRAPE_INTERVAL = 30

SCRAPE_INTERVAL_MIN_THRESHOLD = 5
SCRAPE_TIMEOUT_PERCENTAGE = 0.7
MAX_EVENT_WATCHER_RETRIES = 5

logging.basicConfig(
    level=logging.INFO,  # overridden later by config's log_level
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

class VectorConfigReloader:
    def __init__(self):
        self.node_name = os.environ.get("NODE_NAME")
        if not self.node_name:
            raise RuntimeError("NODE_NAME not set")

        self.running = True
        config.load_incluster_config()
        self.k8s_api_client = client.CoreV1Api()
        self.k8s_pod_event_watcher = watch.Watch()
        self.k8s_config_map_event_watcher = watch.Watch()
        self.custom_metrics_config_map_cache = {}

        reloader_cfg = YamlUtils.load_yaml_config(RELOADER_CONFIG_PATH)
        self.dcgm_metrics_enabled = reloader_cfg["dcgm_metrics"].get("enabled", True)
        self.dcgm_exporter_port = reloader_cfg["dcgm_metrics"]["port"]
        self.dcgm_exporter_path = reloader_cfg["dcgm_metrics"]["path"]
        self.dcgm_exporter_scrape_interval = reloader_cfg["dcgm_metrics"]["scrape_interval"]
        ksm_cfg = reloader_cfg.get("kube_state_metrics", {})
        self.ksm_enabled = ksm_cfg.get("enabled", True)
        self.ksm_port = ksm_cfg.get("port", 8080)
        self.ksm_path = ksm_cfg.get("path", "/metrics")
        self.ksm_scrape_interval = ksm_cfg.get("scrape_interval", 60)
        self.amd_manager = AmdExporterManager(reloader_cfg.get("amd_metrics", {}))
        self.custom_metrics_enabled = reloader_cfg["custom_metrics"].get("enabled", True)
        self.logs_enabled = reloader_cfg.get("logs", {}).get("enabled", True)
        self.default_custom_metrics_config = reloader_cfg["custom_metrics"]
        self.infra_sink_endpoint = f'{reloader_cfg["sink"]["endpoint"]}/ingest'
        self.custom_sink_endpoint = f'{reloader_cfg["sink"]["endpoint"]}/custom'
        self.sink_proxy_cfg = reloader_cfg["sink"].get("proxy", {}) or {}
        self.custom_metrics_sink_config = {
            "type": "prometheus_remote_write",
            "inputs": [CUSTOM_METRICS_VECTOR_TRANSFORM_NAME],
            "endpoint": self.custom_sink_endpoint,
            "tenant_id": "cri:custom_metrics/${CRUSOE_CLUSTER_ID}",
            "auth": {"strategy": "bearer", "token": "${CRUSOE_MONITORING_TOKEN}"},
            "healthcheck": {"enabled": False},
            "compression": "snappy",
            "request": {"concurrency": "adaptive"},
            "batch": {"max_bytes": 500000},
            "tls": {"verify_certificate": True, "verify_hostname": True},
        }

        LOG.setLevel(reloader_cfg["log_level"])

        # set proxy if enabled
        if self.sink_proxy_cfg.get("enabled"):
            self.custom_metrics_sink_config["proxy"] = self.sink_proxy_cfg

        self.kube_state_metrics_sink_config = {
            "type": "prometheus_remote_write",
            "inputs": [KUBE_STATE_METRICS_TRANSFORM_NAME],
            "endpoint": self.infra_sink_endpoint,
            "tenant_id": "cri:cmk/${CRUSOE_CLUSTER_ID}",
            "auth": {"strategy": "bearer", "token": "${CRUSOE_MONITORING_TOKEN}"},
            "healthcheck": {"enabled": False},
            "compression": "snappy",
            "request": {"concurrency": "adaptive"},
            "batch": {"max_bytes": 500000},
            "tls": {"verify_certificate": True, "verify_hostname": True},
        }

        self.logs_ingest_endpoint = f'{reloader_cfg["sink"]["endpoint"]}/logs/ingest'

        try:
            node = self.k8s_api_client.read_node(self.node_name)
            labels = node.metadata.labels
            self.vm_id = labels.get("crusoe.ai/instance.id", None)
            self.nodepool_id = labels.get("crusoe.ai/nodepool.id", None)
            self.instance_type = labels.get("beta.kubernetes.io/instance-type", None)
            self.pod_id = labels.get("crusoe.ai/pod.id", None)
            self.project_id = labels.get("crusoe.ai/project.id", None)
            self.hostname = labels.get("kubernetes.io/hostname", None)
        except client.exceptions.ApiException as e:
            LOG.error(f"Failed to fetch node labels: {e}. Exiting!")
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

        self.custom_metrics_vector_transform = {
            "type": "remap",
            "inputs": [],
            "source": LiteralStr(f"""
.tags.nodepool = "{self.nodepool_id}"
.tags.cluster_id = "${{CRUSOE_CLUSTER_ID}}"
.tags.vm_id = "{self.vm_id}"
.tags.vm_instance_type = "{self.instance_type}"
if "{self.pod_id or ''}" != "" {{ .tags.pod_id = "{self.pod_id or ''}" }}
.tags.crusoe_resource = "custom_metrics"
.tags.metrics_source = "custom-metrics"
""")
        }

        self.enrich_logs_transform_source = LiteralStr('''
.agent = "crusoe-watch-agent"
.host = get_hostname!()
.crusoe_cluster_id = "${CRUSOE_CLUSTER_ID}"

if .source_type == "journald" {
    .log_source = "journald"
    if .PRIORITY == "0" || .PRIORITY == "1" {
        .level = "error"
    } else if .PRIORITY == "2" || .PRIORITY== "3" {
        .level = "critical"
    } else if .PRIORITY == "4" || .PRIORITY == "5" {
        .level = "warning"
    } else if .PRIORITY == "6" {
        .level = "info"
    } else if .PRIORITY == "7" {
        .level = "debug"
    } else {
        .level = "undefined"
    }
} else if .source_type == "file" {
    if contains(string!(.file), "dmesg") || contains(string!(.file), "kern.log") {
        .log_source = "dmesg"
    } else {
        .log_source = "generic_file"
    }
}
del(.source_type)

if exists(.__REALTIME_TIMESTAMP) {
    ._time = .__REALTIME_TIMESTAMP
} else if exists(.timestamp) {
    ._time = .timestamp
}

if exists(.message) {
    ._msg = del(.message)
}

# Normalize level to lowercase
if exists(.level) {
  .level = downcase(string!(.level))
} else {
  .level = "undefined"
}
''')




    @staticmethod
    def sanitize_name(name: str) -> str:
        # replace invalid chars with underscores
        return re.sub(r'[^a-zA-Z0-9_]', '_', name)

    @staticmethod
    def is_pod_active(pod):
        return pod.status.phase == "Running"

    @staticmethod
    def is_pod_terminating(pod):
        return pod.status.phase == "Terminating"

    @staticmethod
    def is_custom_metrics_pod(pod):
        annotations = pod.metadata.annotations or {}
        return annotations and CUSTOM_METRICS_SCRAPE_ANNOTATION in annotations and annotations[CUSTOM_METRICS_SCRAPE_ANNOTATION] == "true"

    @staticmethod
    def is_dcgm_exporter_pod(pod):
        labels = pod.metadata.labels or {}
        return labels and "app" in labels and labels["app"] == DCGM_EXPORTER_APP_LABEL

    @staticmethod
    def is_kube_state_metrics_pod(pod):
        labels = pod.metadata.labels or {}
        return labels.get("app.kubernetes.io/name") == KUBE_STATE_METRICS_APP_LABEL

    def handle_sigterm(self, sig, frame):
        self.running = False

    def get_dcgm_exporter_scrape_endpoint(self, pod_ip) -> str:
        return f"http://{pod_ip}:{self.dcgm_exporter_port}{self.dcgm_exporter_path}"

    def get_kube_state_metrics_scrape_endpoint(self, pod_ip) -> str:
        return f"http://{pod_ip}:{self.ksm_port}{self.ksm_path}"

    def get_deployment_metrics_config(self, deployment_name: str) -> dict:
        if not deployment_name:
            return {}
        try:
            config_map = self.k8s_api_client.read_namespaced_config_map(
                name=CUSTOM_METRICS_CONFIG_MAP_NAME,
                namespace=CUSTOM_METRICS_CM_NAMESPACE
            )
            config_map_data = config_map.data or {}
        except client.ApiException as e:
            LOG.warning(f"Failed to fetch ConfigMap '{CUSTOM_METRICS_CONFIG_MAP_NAME}': {e}")
            return {}
        config_yaml = config_map_data.get(CUSTOM_METRICS_CONFIG_MAP_KEY, "")
        if not config_yaml:
            return {}
        config = YamlUtils.load_yaml_string(config_yaml)
        return config.get(deployment_name, {})

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

        parts = pod_name.rsplit("-", 2)
        deployment_name = parts[0] if len(parts) == 3 else ""
        return {
            "url": f"http://{pod_ip}:{port}{path}",
            "pod_ip": pod_ip,
            "pod_name": pod_name,
            "deployment_name": deployment_name,
        }

    def set_dcgm_exporter_scrape_config(self, vector_cfg: dict, dcgm_exporter_scrape_endpoint: str):
        if dcgm_exporter_scrape_endpoint is None:
            return
        if not self.dcgm_metrics_enabled:
            LOG.info("DCGM metrics disabled, skipping scrape config")
            return
        vector_cfg.setdefault("sources", {})[DCGM_EXPORTER_SOURCE_NAME] = {
            "type": "prometheus_scrape",
            "endpoints": [dcgm_exporter_scrape_endpoint],
            "scrape_interval_secs": self.dcgm_exporter_scrape_interval,
            "scrape_timeout_secs": int(self.dcgm_exporter_scrape_interval * SCRAPE_TIMEOUT_PERCENTAGE)
        }
        inputs = set(vector_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["inputs"])
        if DCGM_EXPORTER_SOURCE_NAME not in inputs:
            vector_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["inputs"].append(DCGM_EXPORTER_SOURCE_NAME)

    def remove_dcgm_exporter_scrape_config(self, vector_cfg: dict):
        vector_cfg.get("sources", {}).pop(DCGM_EXPORTER_SOURCE_NAME, None)
        inputs = set(vector_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME].get("inputs", []))
        inputs.discard(DCGM_EXPORTER_SOURCE_NAME)
        vector_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["inputs"] = sorted(inputs)

    def set_kube_state_metrics_scrape_config(self, vector_cfg: dict, ksm_scrape_endpoint: str):
        if ksm_scrape_endpoint is None:
            return
        if not self.ksm_enabled:
            LOG.info("Kube state metrics disabled, skipping scrape config")
            return
        vector_cfg.setdefault("sources", {})[KUBE_STATE_METRICS_SOURCE_NAME] = {
            "type": "prometheus_scrape",
            "endpoints": [ksm_scrape_endpoint],
            "scrape_interval_secs": self.ksm_scrape_interval,
            "scrape_timeout_secs": int(self.ksm_scrape_interval * SCRAPE_TIMEOUT_PERCENTAGE)
        }
        vector_cfg.setdefault("transforms", {})[KUBE_STATE_METRICS_TRANSFORM_NAME] = {
            "type": "remap",
            "inputs": [KUBE_STATE_METRICS_SOURCE_NAME],
            "source": KUBE_STATE_METRICS_TRANSFORM_SOURCE
        }
        vector_cfg.setdefault("sinks", {})[KUBE_STATE_METRICS_SINK_NAME] = self.kube_state_metrics_sink_config

    def set_logs_config(self, vector_cfg: dict):
        """Set log sources, transforms, and sink for journald and dmesg logs."""
        if not self.logs_enabled:
            LOG.info("Logs disabled, skipping logs config")
            return
        sources = vector_cfg.setdefault("sources", {})
        transforms = vector_cfg.setdefault("transforms", {})
        sinks = vector_cfg.setdefault("sinks", {})

        # Add journald_logs source
        sources[JOURNALD_LOGS_SOURCE_NAME] = {
            "type": "journald",
            "journal_directory": "/var/log/journal"
        }

        # Add dmesg_logs source
        sources[DMESG_LOGS_SOURCE_NAME] = {
            "type": "file",
            "include": ["/var/log/dmesg", "/var/log/kern.log"]
        }

        # Add enrich_logs transform
        transforms[ENRICH_LOGS_TRANSFORM_NAME] = {
            "type": "remap",
            "inputs": [JOURNALD_LOGS_SOURCE_NAME, DMESG_LOGS_SOURCE_NAME],
            "source": self.enrich_logs_transform_source
        }

        # Add crusoe_ingest sink
        sink_config = {
            "type": "http",
            "inputs": [ENRICH_LOGS_TRANSFORM_NAME],
            "uri": self.logs_ingest_endpoint,
            "framing": {"method": "newline_delimited"},
            "compression": "snappy",
            "healthcheck": {"enabled": False},
            "request": {
                "headers": {"X-Crusoe-Vm-Id": "${VM_ID:-unknown}"}
            },
            "auth": {"strategy": "bearer", "token": "${CRUSOE_MONITORING_TOKEN}"},
            "encoding": {"codec": "json"},
            "batch": {"max_bytes": 100000}
        }
        if self.sink_proxy_cfg.get("enabled"):
            sink_config["proxy"] = self.sink_proxy_cfg
        sinks[CRUSOE_INGEST_SINK_NAME] = sink_config

        LOG.info("Logs config set for journald and dmesg sources")

    def remove_kube_state_metrics_scrape_config(self, vector_cfg: dict):
        vector_cfg.get("sources", {}).pop(KUBE_STATE_METRICS_SOURCE_NAME, None)
        vector_cfg.get("transforms", {}).pop(KUBE_STATE_METRICS_TRANSFORM_NAME, None)
        vector_cfg.get("sinks", {}).pop(KUBE_STATE_METRICS_SINK_NAME, None)

    def set_custom_metrics_scrape_config(self, vector_cfg: dict, custom_metrics_eps: list):
        if not custom_metrics_eps:
            return
        if not self.custom_metrics_enabled:
            LOG.info("Custom metrics disabled, skipping scrape config")
            return
        sources = vector_cfg.get("sources")
        transforms = vector_cfg.get("transforms")
        sinks = vector_cfg.get("sinks")

        for endpoint in custom_metrics_eps:
            pod_name_sanitized = VectorConfigReloader.sanitize_name(endpoint['pod_name'])
            source_name = f"{pod_name_sanitized}_scrape"

            deployment_name = endpoint.get("deployment_name", "")
            deployment_config = self.get_deployment_metrics_config(deployment_name)

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
            LOG.info(f"Created transform '{transform_name}' for pod '{endpoint['pod_name']}'")

            sink_name = f"{pod_name_sanitized}_sink"
            sink_config = self.custom_metrics_sink_config.copy()
            sink_config["inputs"] = [transform_name]
            sinks[sink_name] = sink_config
            LOG.info(f"Created custom metrics pipeline for pod '{endpoint['pod_name']}'")

    def remove_custom_metrics_scrape_config(self, vector_cfg: dict, custom_metrics_ep: dict):
        pod_name_sanitized = VectorConfigReloader.sanitize_name(custom_metrics_ep['pod_name'])
        source_name = f"{pod_name_sanitized}_scrape"
        transform_name = f"{pod_name_sanitized}_transform"
        sink_name = f"{pod_name_sanitized}_sink"

        vector_cfg.get("sources", {}).pop(source_name, None)
        vector_cfg.get("transforms", {}).pop(transform_name, None)
        vector_cfg.get("sinks", {}).pop(sink_name, None)

        LOG.info(f"Removed custom metrics pipeline for pod '{custom_metrics_ep['pod_name']}'")

    def refresh_custom_metrics_config(self):
        """Refresh only custom metrics transforms when ConfigMap changes."""
        current_cfg = YamlUtils.load_yaml_config(VECTOR_CONFIG_PATH)

        # Find all running custom metrics pods
        custom_metrics_eps = []
        for pod in self.k8s_api_client.list_pod_for_all_namespaces(
            field_selector=f"spec.nodeName={self.node_name},status.phase=Running"
        ).items:
            if VectorConfigReloader.is_custom_metrics_pod(pod):
                custom_metrics_eps.append(self.get_custom_metrics_endpoint_cfg(pod))

        # Remove existing custom metrics components
        for ep in custom_metrics_eps:
            self.remove_custom_metrics_scrape_config(current_cfg, ep)

        # Re-add with new config (fetches fresh deployment config from ConfigMap)
        self.set_custom_metrics_scrape_config(current_cfg, custom_metrics_eps)

        YamlUtils.save_yaml(VECTOR_CONFIG_PATH, current_cfg)
        LOG.info("Custom metrics config refreshed from ConfigMap")

    def bootstrap_config(self):
        base_cfg = YamlUtils.load_yaml_config(VECTOR_BASE_CONFIG_PATH)

        dcgm_exporter_ep = None
        amd_exporter_ep = None
        ksm_ep = None
        custom_metrics_eps = []
        for pod in self.k8s_api_client.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={self.node_name},status.phase=Running").items:
            if VectorConfigReloader.is_custom_metrics_pod(pod):
                custom_metrics_eps.append(self.get_custom_metrics_endpoint_cfg(pod))
            elif VectorConfigReloader.is_dcgm_exporter_pod(pod):
                dcgm_exporter_ep = self.get_dcgm_exporter_scrape_endpoint(pod.status.pod_ip)
            elif VectorConfigReloader.is_kube_state_metrics_pod(pod):
                ksm_ep = self.get_kube_state_metrics_scrape_endpoint(pod.status.pod_ip)
            elif self.amd_manager.is_exporter_pod(pod):
                amd_exporter_ep = self.amd_manager.build_endpoint(pod.status.pod_ip)
            else:
                LOG.info(f"Pod {pod.metadata.name} is not a relevant metrics exporter.")

        self.set_custom_metrics_scrape_config(base_cfg, custom_metrics_eps)
        self.set_dcgm_exporter_scrape_config(base_cfg, dcgm_exporter_ep)
        self.set_kube_state_metrics_scrape_config(base_cfg, ksm_ep)
        self.set_logs_config(base_cfg)
        if self.amd_manager.enabled and amd_exporter_ep:
            self.amd_manager.set_scrape(base_cfg, amd_exporter_ep, NODE_METRICS_VECTOR_TRANSFORM_NAME, SCRAPE_TIMEOUT_PERCENTAGE)

        # set endpoint as per env
        base_cfg["sinks"]["cms_gateway_node_metrics"]["endpoint"] = self.infra_sink_endpoint

        # set proxy config if enabled in reloader config
        if self.sink_proxy_cfg.get("enabled"):
            base_cfg["sinks"]["cms_gateway_node_metrics"]["proxy"] = self.sink_proxy_cfg

        LOG.debug(f"Writing vector config {str(base_cfg)}")
        # always update the node metrics transform source to handle LiteralStr issue
        base_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["source"] = self.node_metrics_vector_transform_source
        YamlUtils.save_yaml(VECTOR_CONFIG_PATH, base_cfg)
        LOG.info(f"Vector config bootstrapped!")

    def handle_custom_metrics_config_map_event(self, event):
        custom_metrics_config_map = event["object"]
        event_type = event["type"]
        custom_metrics_config_map_name = custom_metrics_config_map.metadata.name

        LOG.info(f"Custom metrics ConfigMap event received: {event_type} for {custom_metrics_config_map_name}")

        # Store the custom metrics configmap data
        self.custom_metrics_config_map_cache[custom_metrics_config_map_name] = custom_metrics_config_map.data

        # Refresh custom metrics config when configmap changes
        if event_type in ("ADDED", "MODIFIED"):
            LOG.info(f"Custom metrics ConfigMap {custom_metrics_config_map_name} changed, refreshing custom metrics config...")
            self.refresh_custom_metrics_config()
        elif event_type == "DELETED":
            LOG.error(f"CRITICAL: Custom metrics ConfigMap {custom_metrics_config_map_name} was deleted! This should never happen. Keeping internal cache intact.")

    def handle_pod_event(self, event):
        pod = event["object"]
        if not (VectorConfigReloader.is_pod_active(pod) or VectorConfigReloader.is_pod_terminating(pod)):
            LOG.info(f"Pod {pod.metadata.name} state is neither running nor terminating.")
            return
        
        current_vector_cfg = YamlUtils.load_yaml_config(VECTOR_CONFIG_PATH)

        if VectorConfigReloader.is_pod_active(pod):
            if VectorConfigReloader.is_custom_metrics_pod(pod):
                self.set_custom_metrics_scrape_config(current_vector_cfg, [self.get_custom_metrics_endpoint_cfg(pod)])
            elif VectorConfigReloader.is_dcgm_exporter_pod(pod):
                self.set_dcgm_exporter_scrape_config(current_vector_cfg, self.get_dcgm_exporter_scrape_endpoint(pod.status.pod_ip))
            elif VectorConfigReloader.is_kube_state_metrics_pod(pod):
                self.set_kube_state_metrics_scrape_config(current_vector_cfg, self.get_kube_state_metrics_scrape_endpoint(pod.status.pod_ip))
            elif self.amd_manager.is_exporter_pod(pod):
                if self.amd_manager.enabled:
                    self.amd_manager.set_scrape(current_vector_cfg, self.amd_manager.build_endpoint(pod.status.pod_ip), NODE_METRICS_VECTOR_TRANSFORM_NAME, SCRAPE_TIMEOUT_PERCENTAGE)
            else:
                LOG.info(f"Pod {pod.metadata.name} is not a relevant metrics exporter.")
                return
        elif VectorConfigReloader.is_pod_terminating(pod):
            if VectorConfigReloader.is_custom_metrics_pod(pod):
                self.remove_custom_metrics_scrape_config(current_vector_cfg, self.get_custom_metrics_endpoint_cfg(pod))
            elif VectorConfigReloader.is_dcgm_exporter_pod(pod):
                self.remove_dcgm_exporter_scrape_config(current_vector_cfg)
            elif VectorConfigReloader.is_kube_state_metrics_pod(pod):
                self.remove_kube_state_metrics_scrape_config(current_vector_cfg)
            elif self.amd_manager.is_exporter_pod(pod):
                self.amd_manager.remove_scrape(current_vector_cfg, NODE_METRICS_VECTOR_TRANSFORM_NAME)
            else:
                LOG.info(f"Pod {pod.metadata.name} is not a relevant metrics exporter.")
                return

        LOG.debug(f"Writing vector config: {str(current_vector_cfg)}")
        # always update the node metrics transform source to handle LiteralStr issue
        current_vector_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["source"] = self.node_metrics_vector_transform_source
        YamlUtils.save_yaml(VECTOR_CONFIG_PATH, current_vector_cfg)
        LOG.info(f"Vector config reloaded!")

    def run_pod_event_handler(self):
        try:
            stream = self.k8s_pod_event_watcher.stream(
                self.k8s_api_client.list_pod_for_all_namespaces,
                field_selector=f"spec.nodeName={self.node_name}",
                _request_timeout=0
            )
            for event in stream:
                try:
                    self.handle_pod_event(event)
                except Exception as e:
                    LOG.error(f"Failed to handle pod event: {e}")
                if not self.running:
                    self.k8s_pod_event_watcher.stop()
                    break
        except client.ApiException as e:
            LOG.error(f"k8s pod event watcher error: {e}")

    def run_config_map_event_handler(self):
        try:
            stream = self.k8s_config_map_event_watcher.stream(
                self.k8s_api_client.list_namespaced_config_map,
                namespace=CUSTOM_METRICS_CM_NAMESPACE,
                _request_timeout=0
            )
            for event in stream:
                try:
                    self.handle_custom_metrics_config_map_event(event)
                except Exception as e:
                    LOG.error(f"Failed to handle pod event: {e}")
                if not self.running:
                    self.k8s_config_map_event_watcher.stop()
                    break
        except client.ApiException as e:
            LOG.error(f"k8s config_map event watcher error: {e}")

    def execute(self):
        signal.signal(signal.SIGINT, self.handle_sigterm)
        signal.signal(signal.SIGTERM, self.handle_sigterm)

        LOG.info("Bootstraping vector config...")
        self.bootstrap_config()

        pod_event_handler_thread = threading.Thread(target=self.run_pod_event_handler)
        cm_event_handler_thread = threading.Thread(target=self.run_config_map_event_handler)

        LOG.info("Starting pod event handler thread...")
        pod_event_handler_thread.start()

        LOG.info("Starting config map event handler thread...")
        cm_event_handler_thread.start()

        pod_event_handler_thread.join()
        LOG.info("Pod event handler thread completed.")
        cm_event_handler_thread.join()
        LOG.info("ConfigMap event handler thread completed.")

        LOG.info("Exiting config reloader.")

if __name__ == "__main__":
    VectorConfigReloader().execute()
