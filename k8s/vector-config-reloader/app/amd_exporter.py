from typing import Dict
from utils import LiteralStr

AMD_EXPORTER_SOURCE_NAME = "amd_exporter_scrape"
AMD_FILTER_TRANSFORM_NAME = "amd_allowed_filter"
DEFAULT_AMD_APP_LABEL = "metrics-exporter"
AMD_LABEL_KEY = "app.kubernetes.io/name"
DEFAULT_AMD_NAMESPACE = "kube-amd-gpu"
DEFAULT_AMD_SCRAPE_INTERVAL = 60


class AmdExporterManager:
    def __init__(self, cfg: Dict):
        self.enabled = cfg.get("enabled", True)
        self.port = cfg.get("port", 5000)
        self.path = cfg.get("path", "/metrics")
        self.scrape_interval = cfg.get("scrape_interval", DEFAULT_AMD_SCRAPE_INTERVAL)
        self.app_label = DEFAULT_AMD_APP_LABEL
        self.namespace = DEFAULT_AMD_NAMESPACE

    def is_exporter_pod(self, pod) -> bool:
        labels = pod.metadata.labels or {}
        if not labels:
            return False
        return (
                pod.metadata.namespace == self.namespace
                and labels.get(AMD_LABEL_KEY) == self.app_label
        )

    def build_endpoint(self, pod_ip: str) -> str:
        return f"http://{pod_ip}:{self.port}{self.path}"

    def set_scrape(self, vector_cfg: dict, endpoint: str, transform_name: str, timeout_percentage: float):
        if not endpoint:
            return
        vector_cfg.setdefault("sources", {})[AMD_EXPORTER_SOURCE_NAME] = {
            "type": "prometheus_scrape",
            "endpoints": [endpoint],
            "scrape_interval_secs": self.scrape_interval,
            "scrape_timeout_secs": int(self.scrape_interval * timeout_percentage),
        }
        transforms = vector_cfg.setdefault("transforms", {})
        # Add a filter transform that keeps only the specified AMD metrics via VRL condition
        filter_vrl = LiteralStr(
            """
metrics_allowlist = [
    "gpu_used_visible_vram",
    "gpu_total_visible_vram",
    "gpu_gfx_activity",
    "gpu_power_usage",
    "gpu_umc_activity",
    "gpu_prof_tensor_active_percent",
    "pcie_bandwidth",
    "gpu_junction_temperature",
    "gpu_xgmi_link_rx",
    "gpu_xgmi_link_tx",
    "pcie_replay_count",
    "gpu_ecc_uncorrect_total",
    "gpu_ecc_correct_total",
    "gpu_prof_occupancy_percent",
    "gpu_prof_sm_active",
]
includes(metrics_allowlist, .name)
"""
        )
        transforms[AMD_FILTER_TRANSFORM_NAME] = {
            "type": "filter",
            "inputs": [AMD_EXPORTER_SOURCE_NAME],
            "condition": {
                "type": "vrl",
                "source": filter_vrl,
            },
        }
        # Wire the filter output into the target node transform
        inputs = set(transforms[transform_name]["inputs"])
        if AMD_FILTER_TRANSFORM_NAME not in inputs:
            transforms[transform_name]["inputs"].append(AMD_FILTER_TRANSFORM_NAME)

    def remove_scrape(self, vector_cfg: dict, transform_name: str):
        vector_cfg.get("sources", {}).pop(AMD_EXPORTER_SOURCE_NAME, None)
        transforms = vector_cfg.get("transforms", {})
        inputs = set(transforms.get(transform_name, {}).get("inputs", []))
        inputs.discard(AMD_FILTER_TRANSFORM_NAME)
        if transform_name in transforms:
            transforms[transform_name]["inputs"] = sorted(inputs)
        transforms.pop(AMD_FILTER_TRANSFORM_NAME, None)