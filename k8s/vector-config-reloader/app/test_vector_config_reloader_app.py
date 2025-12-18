import yaml
import pytest
import types

from vector_config_reloader_app import (
    VectorConfigReloader,
    CUSTOM_METRICS_SCRAPE_ANNOTATION,
    CUSTOM_METRICS_PORT_ANNOTATION,
    CUSTOM_METRICS_PATH_ANNOTATION,
    CUSTOM_METRICS_SCRAPE_INTERVAL_ANNOTATION,
    CUSTOM_METRICS_VECTOR_TRANSFORM_NAME,
)

class DummyPod:
    def __init__(self, name, ns, ip=None, labels=None, ann=None, phase="Running"):
        self.metadata = type("M", (), {})()
        self.metadata.name = name
        self.metadata.namespace = ns
        self.metadata.annotations = ann or {}
        self.metadata.labels = labels or {}
        self.status = type("S", (), {})()
        self.status.phase = phase
        self.status.pod_ip = ip
        # Add spec with node name for bootstrap tests
        self.spec = type("Spec", (), {})()
        self.spec.node_name = "test-node"

@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Ensure required env vars and k8s/config dependencies are patched for tests."""
    # Required by VectorConfigReloader.__init__
    monkeypatch.setenv("NODE_NAME", "test-node")

    # Patch kubernetes config/client/watch used in module to simple dummies
    class _DummyCoreV1Api:
        def __init__(self):
            self._pods = []

        def list_pod_for_all_namespaces(self, **kwargs):
            return types.SimpleNamespace(items=self._pods)

    class _DummyWatch:
        def stream(self, *args, **kwargs):
            return []

    # Patch into module namespace
    monkeypatch.setattr("vector_config_reloader_app.client.CoreV1Api", _DummyCoreV1Api)
    monkeypatch.setattr("vector_config_reloader_app.watch.Watch", lambda: _DummyWatch())
    monkeypatch.setattr("vector_config_reloader_app.config.load_incluster_config", lambda: None)

    # Provide temp files for config paths
    reloader_cfg = {
        "dcgm_metrics": {"port": 9400, "path": "/metrics", "scrape_interval": 30},
        "custom_metrics": {"port": 9100, "path": "/metrics", "scrape_interval": 30},
        "sink": {"endpoint": "https://cms-monitoring.example.com/ingest"},
        "log_level": "INFO",
    }
    base_vector_cfg = {
        "sources": {"host_metrics": {"type": "host_metrics"}},
        "transforms": {
            "enrich_node_metrics": {"type": "remap", "inputs": ["host_metrics"], "source": "."}
        },
        "sinks": {"cms_gateway_node_metrics": {"type": "prometheus_remote_write", "inputs": ["enrich_node_metrics"], "endpoint": ""}},
    }

    reloader_cfg_path = tmp_path / "reloader.yaml"
    base_cfg_path = tmp_path / "vector-base.yaml"
    vector_cfg_out_path = tmp_path / "vector.yaml"
    reloader_cfg_path.write_text(yaml.safe_dump(reloader_cfg))
    base_cfg_path.write_text(yaml.safe_dump(base_vector_cfg))

    # Patch module-level paths to our temp files
    monkeypatch.setattr("vector_config_reloader_app.RELOADER_CONFIG_PATH", str(reloader_cfg_path))
    monkeypatch.setattr("vector_config_reloader_app.VECTOR_BASE_CONFIG_PATH", str(base_cfg_path))
    monkeypatch.setattr("vector_config_reloader_app.VECTOR_CONFIG_PATH", str(vector_cfg_out_path))

    yield


def test_sanitize_name_replaces_invalid_chars():
    # underscores for invalid characters
    assert VectorConfigReloader.sanitize_name("pod.name/with:weird- chars") == "pod_name_with_weird__chars"
    assert VectorConfigReloader.sanitize_name("ABC123_-") == "ABC123__"


def test_pod_identification_helpers():
    # custom metrics pod: requires annotation set to "true"
    pod_custom = DummyPod("cm", "ns", ip="10.0.0.1", ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "true"})
    assert VectorConfigReloader.is_custom_metrics_pod(pod_custom)

    pod_non_custom = DummyPod("nocm", "ns", ip="10.0.0.2", ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "false"})
    assert not VectorConfigReloader.is_custom_metrics_pod(pod_non_custom)

    # dcgm exporter pod: label app=nvidia-dcgm-exporter
    pod_dcgm = DummyPod("dcgm", "ns", ip="10.0.0.3", labels={"app": "nvidia-dcgm-exporter"})
    assert VectorConfigReloader.is_dcgm_exporter_pod(pod_dcgm)

    pod_non_dcgm = DummyPod("nodcgm", "ns", ip="10.0.0.4", labels={"app": "other"})
    assert not VectorConfigReloader.is_dcgm_exporter_pod(pod_non_dcgm)


def _new_reloader_with_pods(monkeypatch, pods):
    """Helper to create a VectorConfigReloader with injected pod list."""
    r = VectorConfigReloader()
    # Inject pods into dummy k8s client created in fixture
    r.k8s_api_client._pods = pods
    return r


def test_get_custom_metrics_endpoint_cfg_defaults_and_min_threshold(monkeypatch):
    r = _new_reloader_with_pods(monkeypatch, [])

    # No port/path/interval annotations -> defaults from reloader config
    pod_default = DummyPod("svc-a-123", "ns", ip="10.1.1.1", ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "true"})
    cfg_default = r.get_custom_metrics_endpoint_cfg(pod_default)
    assert cfg_default["url"] == "http://10.1.1.1:9100/metrics"
    # scrape_interval should be integer seconds
    assert isinstance(cfg_default["scrape_interval_secs"], int)
    assert cfg_default["scrape_interval_secs"] == 30
    assert cfg_default["scrape_timeout_secs"] == int(30 * 0.7)

    # Interval below threshold should clamp to 5 seconds
    pod_low = DummyPod(
        "svc-b-456",
        "ns",
        ip="10.1.1.2",
        ann={
            CUSTOM_METRICS_SCRAPE_ANNOTATION: "true",
            CUSTOM_METRICS_PORT_ANNOTATION: "9200",
            CUSTOM_METRICS_PATH_ANNOTATION: "/m",
            CUSTOM_METRICS_SCRAPE_INTERVAL_ANNOTATION: "3",
        },
    )
    cfg_low = r.get_custom_metrics_endpoint_cfg(pod_low)
    assert cfg_low["url"] == "http://10.1.1.2:9200/m"
    assert cfg_low["scrape_interval_secs"] == 5
    assert cfg_low["scrape_timeout_secs"] == int(5 * 0.7)


def test_set_and_remove_custom_metrics_scrape_config(monkeypatch):
    r = _new_reloader_with_pods(monkeypatch, [])

    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}

    eps = [
        {"url": "http://10.2.0.1:9100/metrics", "pod_name": "svc-x-1", "scrape_interval_secs": 15, "scrape_timeout_secs": 10},
        {"url": "http://10.2.0.2:9100/metrics", "pod_name": "svc-y-2", "scrape_interval_secs": 20, "scrape_timeout_secs": 14},
    ]

    r.set_custom_metrics_scrape_config(vector_cfg, eps)

    # Sources created for each endpoint
    assert "svc_x_1_scrape" in vector_cfg["sources"]
    assert "svc_y_2_scrape" in vector_cfg["sources"]

    # Transform created with inputs sorted
    assert CUSTOM_METRICS_VECTOR_TRANSFORM_NAME in vector_cfg["transforms"]
    inputs = vector_cfg["transforms"][CUSTOM_METRICS_VECTOR_TRANSFORM_NAME]["inputs"]
    assert inputs == sorted(inputs) and set(inputs) == {"svc_x_1_scrape", "svc_y_2_scrape"}

    # Sink added
    assert "cms_gateway_custom_metrics" in vector_cfg["sinks"]

    # Remove one endpoint -> transform input updated, sink remains
    r.remove_custom_metrics_scrape_config(vector_cfg, eps[0])
    inputs_after_one = vector_cfg["transforms"][CUSTOM_METRICS_VECTOR_TRANSFORM_NAME]["inputs"]
    assert inputs_after_one == ["svc_y_2_scrape"]
    assert "cms_gateway_custom_metrics" in vector_cfg["sinks"]

    # Remove last endpoint -> transform empty and sink removed
    r.remove_custom_metrics_scrape_config(vector_cfg, eps[1])
    assert vector_cfg["transforms"][CUSTOM_METRICS_VECTOR_TRANSFORM_NAME]["inputs"] == []
    assert "cms_gateway_custom_metrics" not in vector_cfg["sinks"]
