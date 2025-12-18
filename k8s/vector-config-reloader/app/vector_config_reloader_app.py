import os, signal, re, logging, sys
from kubernetes import client, config, watch
from utils import LiteralStr, YamlUtils

VECTOR_CONFIG_PATH = "/etc/vector/vector.yaml"
VECTOR_BASE_CONFIG_PATH = "/etc/vector-base/vector.yaml"
RELOADER_CONFIG_PATH = "/etc/reloader/config.yaml"

DCGM_EXPORTER_SOURCE_NAME = "dcgm_exporter_scrape"
DCGM_EXPORTER_APP_LABEL = "nvidia-dcgm-exporter"
NODE_METRICS_VECTOR_TRANSFORM_NAME = "enrich_node_metrics"
NODE_METRICS_VECTOR_TRANSFORM_SOURCE = LiteralStr("""
if exists(.tags.Hostname) {
parts, _ = split(.tags.Hostname, ".")
host_prefix = get(parts, [0]) ?? ""
prefix_parts, _ = split(host_prefix, "-")
nodepool_id_parts, _ = slice(prefix_parts, 0, length(prefix_parts) - 1)
.tags.nodepool, _ = join(nodepool_id_parts, "-")
} else if exists(.tags.host) {
prefix_parts, _ = split(.tags.host, "-")
nodepool_id_parts, _ = slice(prefix_parts, 0, length(prefix_parts) - 1)
.tags.nodepool, _ = join(nodepool_id_parts, "-")
}
.tags.cluster_id = "${CRUSOE_CLUSTER_ID}"
.tags.vm_id = "${VM_ID}"
.tags.crusoe_resource = "vm"
""")
CUSTOM_METRICS_VECTOR_TRANSFORM_NAME = "enrich_custom_metrics"
CUSTOM_METRICS_SCRAPE_ANNOTATION = "crusoe.custom_metrics.enable_scrape"
CUSTOM_METRICS_PORT_ANNOTATION = "crusoe.custom_metrics.port"
CUSTOM_METRICS_PATH_ANNOTATION = "crusoe.custom_metrics.path"
CUSTOM_METRICS_SCRAPE_INTERVAL_ANNOTATION = f"crusoe.custom_metrics.scrape_interval"
CUSTOM_METRICS_VECTOR_TRANSFORM = {
    "type": "remap",
    "inputs": [],
    "source": LiteralStr("""
if exists(.tags.Hostname) {
parts, _ = split(.tags.Hostname, ".")
host_prefix = get(parts, [0]) ?? ""
prefix_parts, _ = split(host_prefix, "-")
nodepool_id_parts, _ = slice(prefix_parts, 0, length(prefix_parts) - 1)
.tags.nodepool, _ = join(nodepool_id_parts, "-")
}
.tags.cluster_id = "${CRUSOE_CLUSTER_ID}"
.tags.vm_id = "${VM_ID}"
.tags.crusoe_resource = "custom_metrics"
""")
}

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
        self.k8s_event_watcher = watch.Watch()

        reloader_cfg = YamlUtils.load_yaml_config(RELOADER_CONFIG_PATH)
        self.dcgm_exporter_port = reloader_cfg["dcgm_metrics"]["port"]
        self.dcgm_exporter_path = reloader_cfg["dcgm_metrics"]["path"]
        self.dcgm_exporter_scrape_interval = reloader_cfg["dcgm_metrics"]["scrape_interval"]
        self.default_custom_metrics_config = reloader_cfg["custom_metrics"]
        self.sink_endpoint = reloader_cfg["sink"]["endpoint"]
        self.custom_metrics_sink_config = {
            "type": "prometheus_remote_write",
            "inputs": [CUSTOM_METRICS_VECTOR_TRANSFORM_NAME],
            "endpoint": self.sink_endpoint,
            "auth": {"strategy": "bearer", "token": "${CRUSOE_MONITORING_TOKEN}"},
            "healthcheck": {"enabled": False},
            "compression": "snappy",
            "tls": {"verify_certificate": True, "verify_hostname": True},
        }

        LOG.setLevel(reloader_cfg["log_level"])

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

    def handle_sigterm(self, sig, frame):
        self.running = False

    def get_dcgm_exporter_scrape_endpoint(self, pod_ip) -> str:
        return f"http://{pod_ip}:{self.dcgm_exporter_port}{self.dcgm_exporter_path}"

    def get_custom_metrics_endpoint_cfg(self, pod) -> dict:
        pod_ip = pod.status.pod_ip
        pod_name = pod.metadata.name
        annotations = pod.metadata.annotations
        port = int(annotations.get(CUSTOM_METRICS_PORT_ANNOTATION, self.default_custom_metrics_config["port"]))
        path = annotations.get(CUSTOM_METRICS_PATH_ANNOTATION, self.default_custom_metrics_config["path"])
        interval = int(annotations.get(CUSTOM_METRICS_SCRAPE_INTERVAL_ANNOTATION, self.default_custom_metrics_config["scrape_interval"]))
        if interval < SCRAPE_INTERVAL_MIN_THRESHOLD:
            LOG.warning(f"For pod {pod_name}, scrape interval set to: {interval} (less than 5 seconds), defaulting to {SCRAPE_INTERVAL_MIN_THRESHOLD}")
            interval = SCRAPE_INTERVAL_MIN_THRESHOLD
        return {
            "url": f"http://{pod_ip}:{port}{path}",
            "pod_name": pod_name,
            "scrape_interval_secs": interval,
            "scrape_timeout_secs": int(interval * SCRAPE_TIMEOUT_PERCENTAGE)
        }

    def set_dcgm_exporter_scrape_config(self, vector_cfg: dict, dcgm_exporter_scrape_endpoint: str):
        if dcgm_exporter_scrape_endpoint is None:
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

    def set_custom_metrics_scrape_config(self, vector_cfg: dict, custom_metrics_eps: list):
        if not custom_metrics_eps:
            return
        sources = vector_cfg.get("sources")
        transforms = vector_cfg.get("transforms")
        enrich_custom_metrics = transforms.setdefault(CUSTOM_METRICS_VECTOR_TRANSFORM_NAME, CUSTOM_METRICS_VECTOR_TRANSFORM)
        inputs = set(enrich_custom_metrics.get("inputs", []))

        for endpoint in custom_metrics_eps:
            source_name = f"{VectorConfigReloader.sanitize_name(endpoint['pod_name'])}_scrape"
            sources[source_name] = {
                "type": "prometheus_scrape",
                "endpoints": [endpoint["url"]],
                "scrape_interval_secs": endpoint["scrape_interval_secs"],
                "scrape_timeout_secs": endpoint["scrape_timeout_secs"]
            }
            inputs.add(source_name)
        enrich_custom_metrics["inputs"] = sorted(inputs)
        vector_cfg["sinks"]["cms_gateway_custom_metrics"] = self.custom_metrics_sink_config

    def remove_custom_metrics_scrape_config(self, vector_cfg: dict, custom_metrics_ep: dict):
        source_name = f"{VectorConfigReloader.sanitize_name(custom_metrics_ep['pod_name'])}_scrape"
        vector_cfg.get("sources", {}).pop(source_name, None)
        inputs = set(vector_cfg["transforms"][CUSTOM_METRICS_VECTOR_TRANSFORM_NAME].get("inputs", []))
        inputs.discard(source_name)
        vector_cfg["transforms"][CUSTOM_METRICS_VECTOR_TRANSFORM_NAME]["inputs"] = sorted(inputs)
        if not vector_cfg["transforms"][CUSTOM_METRICS_VECTOR_TRANSFORM_NAME]["inputs"]:
            vector_cfg.get("sinks", {}).pop("cms_gateway_custom_metrics", None)

    def bootstrap_config(self):
        base_cfg = YamlUtils.load_yaml_config(VECTOR_BASE_CONFIG_PATH)

        dcgm_exporter_ep = None
        custom_metrics_eps = []
        for pod in self.k8s_api_client.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={self.node_name},status.phase=Running").items:
            if VectorConfigReloader.is_custom_metrics_pod(pod):
                custom_metrics_eps.append(self.get_custom_metrics_endpoint_cfg(pod))
            elif VectorConfigReloader.is_dcgm_exporter_pod(pod):
                dcgm_exporter_ep = self.get_dcgm_exporter_scrape_endpoint(pod.status.pod_ip)
            else:
                LOG.info(f"Pod {pod.metadata.name} is not a relevant metrics exporter.")

        self.set_custom_metrics_scrape_config(base_cfg, custom_metrics_eps)
        self.set_dcgm_exporter_scrape_config(base_cfg, dcgm_exporter_ep)

        # set endpoint as per env
        base_cfg["sinks"]["cms_gateway_node_metrics"]["endpoint"] = self.sink_endpoint

        LOG.debug(f"Writing vector config {str(base_cfg)}")
        # always update the node metrics transform source to handle LiteralStr issue
        base_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["source"] = NODE_METRICS_VECTOR_TRANSFORM_SOURCE
        YamlUtils.save_yaml(VECTOR_CONFIG_PATH, base_cfg)
        LOG.info(f"Vector config bootstrapped!")

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
            else:
                LOG.info(f"Pod {pod.metadata.name} is not a relevant metrics exporter.")
                return
        elif VectorConfigReloader.is_pod_terminating(pod):
            if VectorConfigReloader.is_custom_metrics_pod(pod):
                self.remove_custom_metrics_scrape_config(current_vector_cfg, self.get_custom_metrics_endpoint_cfg(pod))
            elif VectorConfigReloader.is_dcgm_exporter_pod(pod):
                self.remove_dcgm_exporter_scrape_config(current_vector_cfg)
            else:
                LOG.info(f"Pod {pod.metadata.name} is not a relevant metrics exporter.")
                return

        LOG.debug(f"Writing vector config: {str(current_vector_cfg)}")
        # always update the node metrics transform source to handle LiteralStr issue
        current_vector_cfg["transforms"][NODE_METRICS_VECTOR_TRANSFORM_NAME]["source"] = NODE_METRICS_VECTOR_TRANSFORM_SOURCE
        YamlUtils.save_yaml(VECTOR_CONFIG_PATH, current_vector_cfg)
        LOG.info(f"Vector config reloaded!")

    def execute(self):
        signal.signal(signal.SIGINT, self.handle_sigterm)
        signal.signal(signal.SIGTERM, self.handle_sigterm)

        self.bootstrap_config()

        try:
            stream = self.k8s_event_watcher.stream(
                self.k8s_api_client.list_pod_for_all_namespaces,
                field_selector=f"spec.nodeName={self.node_name}",
                _request_timeout=0
            )
            for event in stream:
                self.handle_pod_event(event)
                if not self.running:
                    self.k8s_event_watcher.stop()
                    break
        except client.ApiException as e:
            LOG.error(f"k8s event watcher error: {e}")

        LOG.info("Exiting config reloader.")

if __name__ == "__main__":
    VectorConfigReloader().execute()
