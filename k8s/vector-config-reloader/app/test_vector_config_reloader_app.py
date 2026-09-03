import json
import os
import re
import shutil
import subprocess
import yaml
import pytest
import types
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError

import vector_config_reloader_app as vcr_mod
from vector_config_reloader_app import (
    VectorConfigReloader,
    CUSTOM_METRICS_SCRAPE_ANNOTATION,
    CUSTOM_METRICS_PORT_ANNOTATION,
    CUSTOM_METRICS_PATH_ANNOTATION,
    CUSTOM_METRICS_CONFIG_MAP_KEY,
    DCGM_EXPORTER_SOURCE_NAME,
    DEFAULT_OPERATOR_LOG_NAMESPACES,
    POD_TYPE_CUSTOM,
    POD_TYPE_DCGM,
    POD_TYPE_KSM,
    POD_TYPE_SLURM,
    POD_TYPE_CME,
)
from utils import YamlUtils


class DummyPod:
    def __init__(self, name, ns, ip=None, labels=None, ann=None, phase="Running",
                 deletion_timestamp=None):
        self.metadata = type("M", (), {})()
        self.metadata.name = name
        self.metadata.namespace = ns
        self.metadata.annotations = ann or {}
        self.metadata.labels = labels or {}
        self.metadata.deletion_timestamp = deletion_timestamp
        self.status = type("S", (), {})()
        self.status.phase = phase
        self.status.pod_ip = ip
        self.spec = type("Spec", (), {})()
        self.spec.node_name = "test-node"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Patch env vars, k8s client, and config paths for hermetic tests."""
    monkeypatch.setenv("NODE_NAME", "test-node")

    class _DummyCoreV1Api:
        def __init__(self):
            self._pods = []
            self._cm_data = {}
            self._cm_raises = None  # set to an Exception instance to simulate failure

        def list_pod_for_all_namespaces(self, **kwargs):
            return types.SimpleNamespace(items=self._pods)

        def read_namespaced_config_map(self, name, namespace):
            if self._cm_raises is not None:
                raise self._cm_raises
            return types.SimpleNamespace(data=self._cm_data)

        def read_node(self, name):
            return types.SimpleNamespace(
                metadata=types.SimpleNamespace(
                    labels={
                        "crusoe.ai/instance.id": "test-vm-id",
                        "crusoe.ai/nodepool.id": "test-nodepool-id",
                        "beta.kubernetes.io/instance-type": "test-instance-type",
                    }
                )
            )

    monkeypatch.setattr("vector_config_reloader_app.client.CoreV1Api", _DummyCoreV1Api)
    monkeypatch.setattr("vector_config_reloader_app.config.load_incluster_config", lambda: None)
    monkeypatch.setattr("vector_config_reloader_app.start_http_server", lambda port: None)

    reloader_cfg = {
        "dcgm_metrics": {"port": 9400, "path": "/metrics", "scrape_interval": 30},
        "custom_metrics": {"port": 9100, "path": "/metrics", "scrape_interval": 30},
        "sink": {"endpoint": "https://cms-monitoring.example.com"},
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

    monkeypatch.setattr("vector_config_reloader_app.RELOADER_CONFIG_PATH", str(reloader_cfg_path))
    monkeypatch.setattr("vector_config_reloader_app.VECTOR_BASE_CONFIG_PATH", str(base_cfg_path))
    monkeypatch.setattr("vector_config_reloader_app.VECTOR_CONFIG_PATH", str(vector_cfg_out_path))

    yield


def _new_reloader_with_pods(pods=None, cm_data=None):
    r = VectorConfigReloader()
    r.k8s_api_client._pods = pods or []
    r.k8s_api_client._cm_data = cm_data or {}
    return r


# -------- Pure helpers --------

def test_sanitize_name_replaces_invalid_chars():
    assert VectorConfigReloader.sanitize_name("pod.name/with:weird- chars") == "pod_name_with_weird__chars"
    assert VectorConfigReloader.sanitize_name("ABC123_-") == "ABC123__"


def test_is_pod_active_excludes_pods_with_deletion_timestamp():
    pod_terminating = DummyPod("test", "ns", ip="10.0.0.1", phase="Running",
                                deletion_timestamp="2026-01-01T00:00:00Z")
    assert not VectorConfigReloader.is_pod_active(pod_terminating)

    pod_failed = DummyPod("test", "ns", ip="10.0.0.1", phase="Failed")
    assert not VectorConfigReloader.is_pod_active(pod_failed)

    pod_running = DummyPod("test", "ns", ip="10.0.0.1", phase="Running")
    assert VectorConfigReloader.is_pod_active(pod_running)


def test_classify_pod():
    r = _new_reloader_with_pods([])
    # Custom metrics requires the annotation set to "true"
    assert r.classify_pod(DummyPod("c", "ns", ip="1.1.1.1", ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "true"})) == POD_TYPE_CUSTOM
    assert r.classify_pod(DummyPod("c2", "ns", ip="1.1.1.1", ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "false"})) is None

    # DCGM uses legacy `app` label
    assert r.classify_pod(DummyPod("d", "ns", ip="1.1.1.2", labels={"app": "nvidia-dcgm-exporter"})) == POD_TYPE_DCGM
    assert r.classify_pod(DummyPod("d2", "ns", ip="1.1.1.2", labels={"app": "other"})) is None

    # KSM / Slurm / CME use `app.kubernetes.io/name`
    assert r.classify_pod(DummyPod("k", "ns", ip="1.1.1.3", labels={"app.kubernetes.io/name": "kube-state-metrics"})) == POD_TYPE_KSM
    assert r.classify_pod(DummyPod("s", "ns", ip="1.1.1.4", labels={"app.kubernetes.io/name": "slurmctld"})) == POD_TYPE_SLURM
    assert r.classify_pod(DummyPod("ce", "ns", ip="1.1.1.5", labels={"app.kubernetes.io/name": "crusoe-metrics-exporter"})) == POD_TYPE_CME
    assert r.classify_pod(DummyPod("u", "ns", ip="1.1.1.6", labels={"app.kubernetes.io/name": "unknown"})) is None

    # Bare pod with no relevant labels/annotations
    assert r.classify_pod(DummyPod("other", "ns", ip="1.1.1.7")) is None


def test_get_custom_metrics_endpoint_cfg_defaults():
    r = _new_reloader_with_pods([])
    pod_default = DummyPod("svc-a-123", "ns", ip="10.1.1.1", ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "true"})
    cfg_default = r.get_custom_metrics_endpoint_cfg(pod_default)
    assert cfg_default["url"] == "http://10.1.1.1:9100/metrics"
    assert cfg_default["pod_name"] == "svc-a-123"
    assert cfg_default["deployment_name"] == "svc"

    pod_custom = DummyPod(
        "myapp-abc-456", "ns", ip="10.1.1.2",
        ann={
            CUSTOM_METRICS_SCRAPE_ANNOTATION: "true",
            CUSTOM_METRICS_PORT_ANNOTATION: "9200",
            CUSTOM_METRICS_PATH_ANNOTATION: "/m",
        },
    )
    cfg_custom = r.get_custom_metrics_endpoint_cfg(pod_custom)
    assert cfg_custom["url"] == "http://10.1.1.2:9200/m"
    assert cfg_custom["deployment_name"] == "myapp"


# -------- Set methods (signature smoke tests) --------

def test_set_custom_metrics_scrape_config_takes_cm_data():
    r = _new_reloader_with_pods([])
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    eps = [
        {"url": "http://10.2.0.1:9100/metrics", "pod_ip": "10.2.0.1", "pod_name": "svc-x-1", "deployment_name": ""},
        {"url": "http://10.2.0.2:9100/metrics", "pod_ip": "10.2.0.2", "pod_name": "svc-y-2", "deployment_name": ""},
    ]
    r.set_custom_metrics_scrape_config(vector_cfg, eps, {})
    assert "svc_x_1_scrape" in vector_cfg["sources"]
    assert "svc_y_2_scrape" in vector_cfg["sources"]
    assert vector_cfg["transforms"]["svc_x_1_transform"]["inputs"] == ["svc_x_1_scrape"]
    assert vector_cfg["sinks"]["svc_x_1_sink"]["inputs"] == ["svc_x_1_transform"]


def test_set_custom_metrics_uses_cm_data_for_deployment_rules():
    r = _new_reloader_with_pods([])
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    cm_data = {
        CUSTOM_METRICS_CONFIG_MAP_KEY: yaml.safe_dump({
            "svc": {"scrape_interval_secs": 15, "allowlist": ["foo_total"]}
        })
    }
    eps = [{"url": "http://10.2.0.1:9100/metrics", "pod_ip": "10.2.0.1",
            "pod_name": "svc-x-1", "deployment_name": "svc"}]
    r.set_custom_metrics_scrape_config(vector_cfg, eps, cm_data)
    assert vector_cfg["sources"]["svc_x_1_scrape"]["scrape_interval_secs"] == 15
    transform_source = str(vector_cfg["transforms"]["svc_x_1_transform"]["source"])
    assert "foo_total" in transform_source


def test_apply_cluster_exporter_writes_full_pipeline():
    r = _new_reloader_with_pods([])
    r.slurm_cfg.enabled = True
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r._apply_cluster_exporter(vector_cfg, r.slurm_spec, "10.3.0.1")
    assert "slurm_metrics_scrape" in vector_cfg["sources"]
    assert "enrich_slurm_metrics" in vector_cfg["transforms"]
    assert "slurm_metrics_sink" in vector_cfg["sinks"]
    # Multi-path exporter writes one endpoint per path
    assert len(vector_cfg["sources"]["slurm_metrics_scrape"]["endpoints"]) == len(r.slurm_cfg.paths)


def test_apply_cluster_exporter_skips_when_disabled():
    r = _new_reloader_with_pods([])
    # Slurm runtime config was built from reloader_cfg in fixture (no slurm_metrics section -> disabled)
    assert r.slurm_cfg.enabled is False
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r._apply_cluster_exporter(vector_cfg, r.slurm_spec, "10.3.0.1")
    assert "slurm_metrics_scrape" not in vector_cfg["sources"]


def test_apply_cluster_exporter_skips_when_pod_ip_missing():
    r = _new_reloader_with_pods([])
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r._apply_cluster_exporter(vector_cfg, r.ksm_spec, None)
    assert "kube_state_metrics_scrape" not in vector_cfg["sources"]


def test_runtime_config_from_dict_handles_path_paths_and_defaults():
    from vector_config_reloader_app import ExporterRuntimeConfig

    # Single "path" wraps to list
    rc = ExporterRuntimeConfig.from_dict({"port": 9100, "path": "/m", "scrape_interval": 30})
    assert rc.paths == ["/m"]
    assert rc.port == 9100
    assert rc.enabled is True

    # Multi "paths" used as-is
    rc = ExporterRuntimeConfig.from_dict({"port": 6817, "paths": ["/a", "/b"]})
    assert rc.paths == ["/a", "/b"]

    # Empty config falls back to defaults
    rc = ExporterRuntimeConfig.from_dict({}, default_enabled=False, default_port=8080)
    assert rc.enabled is False
    assert rc.port == 8080
    assert rc.paths == ["/metrics"]


def test_runtime_config_build_endpoints():
    from vector_config_reloader_app import ExporterRuntimeConfig
    rc = ExporterRuntimeConfig(enabled=True, port=8080, paths=["/a", "/b"], scrape_interval=30)
    assert rc.build_endpoints("10.0.0.1") == ["http://10.0.0.1:8080/a", "http://10.0.0.1:8080/b"]


# -------- Init failure --------

def test_init_exits_when_read_node_fails(monkeypatch):
    class _DeadApi:
        def __init__(self):
            self._pods = []
            self._cm_data = {}
            self._cm_raises = None

        def read_node(self, name):
            raise MaxRetryError(pool=None, url="/api",
                                reason=ConnectTimeoutError(None, "timeout"))

        def list_pod_for_all_namespaces(self, **kwargs):
            return types.SimpleNamespace(items=[])

        def read_namespaced_config_map(self, name, namespace):
            return types.SimpleNamespace(data={})

    monkeypatch.setattr("vector_config_reloader_app.client.CoreV1Api", _DeadApi)
    with pytest.raises(SystemExit) as exc_info:
        VectorConfigReloader()
    assert exc_info.value.code == 1


# -------- Fingerprinting --------

def test_pod_fingerprint_changes_when_pod_added_or_removed():
    r = _new_reloader_with_pods([])
    p1 = DummyPod("dcgm-1", "ns", ip="10.0.0.1", labels={"app": "nvidia-dcgm-exporter"})
    p2 = DummyPod("ksm-1", "ns", ip="10.0.0.2", labels={"app.kubernetes.io/name": "kube-state-metrics"})

    fp_one = r._compute_pod_fingerprint([(p1, POD_TYPE_DCGM)])
    fp_both = r._compute_pod_fingerprint([(p1, POD_TYPE_DCGM), (p2, POD_TYPE_KSM)])
    fp_one_again = r._compute_pod_fingerprint([(p1, POD_TYPE_DCGM)])

    assert fp_one == fp_one_again
    assert fp_one != fp_both


def test_pod_fingerprint_changes_when_pod_ip_changes():
    """StatefulSet recreation case: same name, new IP."""
    r = _new_reloader_with_pods([])
    p_old = DummyPod("dcgm-1", "ns", ip="10.0.0.1", labels={"app": "nvidia-dcgm-exporter"})
    p_new = DummyPod("dcgm-1", "ns", ip="10.0.0.2", labels={"app": "nvidia-dcgm-exporter"})
    fp_old = r._compute_pod_fingerprint([(p_old, POD_TYPE_DCGM)])
    fp_new = r._compute_pod_fingerprint([(p_new, POD_TYPE_DCGM)])
    assert fp_old != fp_new


def test_pod_fingerprint_ignores_in_place_annotation_edits():
    """In-place `kubectl annotate` edits on custom metrics pods are intentionally
    not tracked — pod recreation is the standard rollout path."""
    r = _new_reloader_with_pods([])
    p_old = DummyPod("svc-x-1", "ns", ip="10.0.0.1",
                     ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "true",
                          CUSTOM_METRICS_PORT_ANNOTATION: "9100"})
    p_new = DummyPod("svc-x-1", "ns", ip="10.0.0.1",
                     ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "true",
                          CUSTOM_METRICS_PORT_ANNOTATION: "9200"})
    fp_old = r._compute_pod_fingerprint([(p_old, POD_TYPE_CUSTOM)])
    fp_new = r._compute_pod_fingerprint([(p_new, POD_TYPE_CUSTOM)])
    assert fp_old == fp_new


# -------- Configmap fetch + checksum --------

def test_compute_cm_checksum_is_stable_and_diff_on_change():
    a = {"k": "v1"}
    b = {"k": "v2"}
    assert VectorConfigReloader._compute_cm_checksum(a) == VectorConfigReloader._compute_cm_checksum(a)
    assert VectorConfigReloader._compute_cm_checksum(a) != VectorConfigReloader._compute_cm_checksum(b)
    assert VectorConfigReloader._compute_cm_checksum({}) == VectorConfigReloader._compute_cm_checksum(None)


def test_fetch_configmap_returns_data_when_present():
    r = _new_reloader_with_pods([], cm_data={"foo": "bar"})
    assert r._fetch_configmap_data() == {"foo": "bar"}


def test_fetch_configmap_returns_empty_on_404():
    from kubernetes.client.exceptions import ApiException
    r = _new_reloader_with_pods([])
    r.k8s_api_client._cm_raises = ApiException(status=404, reason="NotFound")
    assert r._fetch_configmap_data() == {}


def test_fetch_configmap_returns_none_on_other_error():
    from kubernetes.client.exceptions import ApiException
    r = _new_reloader_with_pods([])
    r.k8s_api_client._cm_raises = ApiException(status=500, reason="ServerError")
    assert r._fetch_configmap_data() is None


# -------- Pod listing filters --------

def test_list_active_relevant_pods_skips_terminating_irrelevant_and_no_ip():
    pods = [
        DummyPod("dcgm-running", "ns", ip="10.0.0.1", labels={"app": "nvidia-dcgm-exporter"}),
        DummyPod("dcgm-terminating", "ns", ip="10.0.0.2",
                 labels={"app": "nvidia-dcgm-exporter"},
                 deletion_timestamp="2026-01-01T00:00:00Z"),
        DummyPod("irrelevant", "ns", ip="10.0.0.3", labels={"app": "other"}),
        DummyPod("dcgm-no-ip", "ns", ip=None, labels={"app": "nvidia-dcgm-exporter"}),
        DummyPod("dcgm-failed", "ns", ip="10.0.0.4", labels={"app": "nvidia-dcgm-exporter"}, phase="Failed"),
    ]
    r = _new_reloader_with_pods(pods)
    relevant = r._list_active_relevant_pods()
    names = [p.metadata.name for p, _ in relevant]
    assert names == ["dcgm-running"]


# -------- reconcile_once --------

def test_reconcile_first_cycle_writes_config_with_pods():
    pods = [DummyPod("dcgm-1", "ns", ip="10.0.0.1", labels={"app": "nvidia-dcgm-exporter"})]
    r = _new_reloader_with_pods(pods)
    r.reconcile_once()

    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert DCGM_EXPORTER_SOURCE_NAME in cfg["sources"]
    assert r._tracked_pod_fingerprint is not None
    assert r._tracked_cm_checksum is not None


def test_reconcile_no_changes_does_not_rewrite_config():
    pods = [DummyPod("dcgm-1", "ns", ip="10.0.0.1", labels={"app": "nvidia-dcgm-exporter"})]
    r = _new_reloader_with_pods(pods)
    r.reconcile_once()
    mtime_before = os.path.getmtime(vcr_mod.VECTOR_CONFIG_PATH)

    # Ensure mtime resolution can detect a write
    import time as _t
    _t.sleep(0.01)

    r.reconcile_once()
    mtime_after = os.path.getmtime(vcr_mod.VECTOR_CONFIG_PATH)
    assert mtime_before == mtime_after


def test_reconcile_pod_added_triggers_reload():
    r = _new_reloader_with_pods([])
    r.reconcile_once()
    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert DCGM_EXPORTER_SOURCE_NAME not in cfg.get("sources", {})

    r.k8s_api_client._pods = [
        DummyPod("dcgm-1", "ns", ip="10.0.0.1", labels={"app": "nvidia-dcgm-exporter"}),
    ]
    r.reconcile_once()
    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert DCGM_EXPORTER_SOURCE_NAME in cfg["sources"]


def test_reconcile_pod_removed_triggers_reload_and_cleans_config():
    pods = [DummyPod("dcgm-1", "ns", ip="10.0.0.1", labels={"app": "nvidia-dcgm-exporter"})]
    r = _new_reloader_with_pods(pods)
    r.reconcile_once()
    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert DCGM_EXPORTER_SOURCE_NAME in cfg["sources"]

    r.k8s_api_client._pods = []
    r.reconcile_once()
    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert DCGM_EXPORTER_SOURCE_NAME not in cfg.get("sources", {})


def test_reconcile_pod_with_deletion_timestamp_treated_as_removed():
    pod = DummyPod("dcgm-1", "ns", ip="10.0.0.1", labels={"app": "nvidia-dcgm-exporter"})
    r = _new_reloader_with_pods([pod])
    r.reconcile_once()
    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert DCGM_EXPORTER_SOURCE_NAME in cfg["sources"]

    pod_terminating = DummyPod("dcgm-1", "ns", ip="10.0.0.1",
                                labels={"app": "nvidia-dcgm-exporter"},
                                deletion_timestamp="2026-01-01T00:00:00Z")
    r.k8s_api_client._pods = [pod_terminating]
    r.reconcile_once()
    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert DCGM_EXPORTER_SOURCE_NAME not in cfg.get("sources", {})


def test_reconcile_configmap_change_triggers_reload():
    pod = DummyPod("svc-x-1", "ns", ip="10.0.0.1",
                   ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "true"})
    r = _new_reloader_with_pods([pod], cm_data={
        CUSTOM_METRICS_CONFIG_MAP_KEY: yaml.safe_dump({"svc": {"scrape_interval_secs": 15}})
    })
    r.reconcile_once()
    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert cfg["sources"]["svc_x_1_scrape"]["scrape_interval_secs"] == 15

    # Change configmap; pod set unchanged
    r.k8s_api_client._cm_data = {
        CUSTOM_METRICS_CONFIG_MAP_KEY: yaml.safe_dump({"svc": {"scrape_interval_secs": 45}})
    }
    r.reconcile_once()
    cfg = YamlUtils.load_yaml_config(vcr_mod.VECTOR_CONFIG_PATH)
    assert cfg["sources"]["svc_x_1_scrape"]["scrape_interval_secs"] == 45


def test_reconcile_skips_when_pod_list_fails():
    r = _new_reloader_with_pods([])
    r.reconcile_once()  # initial cycle ok

    def boom(**kwargs):
        raise ConnectionError("API down")

    r.k8s_api_client.list_pod_for_all_namespaces = boom
    # Should not raise, just log + return; tracked state unchanged
    r.reconcile_once()


def test_reconcile_uses_last_known_cm_when_fetch_fails():
    from kubernetes.client.exceptions import ApiException
    pod = DummyPod("svc-x-1", "ns", ip="10.0.0.1",
                   ann={CUSTOM_METRICS_SCRAPE_ANNOTATION: "true"})
    r = _new_reloader_with_pods([pod], cm_data={
        CUSTOM_METRICS_CONFIG_MAP_KEY: yaml.safe_dump({"svc": {"scrape_interval_secs": 20}})
    })
    r.reconcile_once()
    saved_cm = dict(r._tracked_cm_data)

    # Now fail the cm fetch on next cycle. The cycle should not crash and
    # tracked_cm_data should remain.
    r.k8s_api_client._cm_raises = ApiException(status=500, reason="ServerError")
    r.reconcile_once()
    assert r._tracked_cm_data == saved_cm


# -------- Buffer configuration --------

def test_sink_buffer_config():
    """All sinks use 256 MiB disk buffer."""
    r = VectorConfigReloader()
    assert r.sink_buffer_config == {
        "type": "disk",
        "max_size": 268435488,  # 256 MiB
        "when_full": "block",
    }


def test_ksm_sink_has_buffer():
    r = VectorConfigReloader()
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r.ksm_cfg.enabled = True
    r._apply_cluster_exporter(vector_cfg, r.ksm_spec, "10.0.0.1")

    assert "kube_state_metrics_sink" in vector_cfg["sinks"]
    sink = vector_cfg["sinks"]["kube_state_metrics_sink"]
    assert sink["buffer"]["max_size"] == 268435488  # 256 MiB


def test_logs_sink_has_no_buffer():
    r = VectorConfigReloader()
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r.set_logs_config(vector_cfg)

    assert "crusoe_ingest" in vector_cfg["sinks"]
    sink = vector_cfg["sinks"]["crusoe_ingest"]
    assert "buffer" not in sink


def test_logs_sink_identifies_agent_via_user_agent():
    """The logs sink sends a User-Agent header identifying the agent and its version."""
    r = VectorConfigReloader()
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r.set_logs_config(vector_cfg)

    headers = vector_cfg["sinks"]["crusoe_ingest"]["request"]["headers"]
    assert headers["User-Agent"] == "CrusoeWatchAgent/CMK-${CHART_VERSION}"


def test_logs_envelope_contract():
    """Standardized envelope: raw event under payload, identity under crusoe,
    _msg/_time/level/log_source at the top level. Scratch (._*) never ships."""
    r = VectorConfigReloader()
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r.set_logs_config(vector_cfg)

    enrich = vector_cfg["transforms"]["enrich_logs"]["source"]
    assert ".payload = raw" in enrich
    assert '.crusoe = { "agent": "crusoe-watch-agent"' in enrich
    # K8s envelope uses chart_version (not agent_version — that's the VM field name).
    assert '"chart_version": "${CHART_VERSION}"' in enrich
    assert '"agent_version"' not in enrich
    # cluster_id is stamped server-side by CML Ingress, not by the agent envelope.
    assert "cluster_id" not in enrich
    assert ".log_source = cwa_log_source" in enrich
    assert ".crusoe.log_source" not in enrich
    assert "._payload" not in enrich
    assert "._crusoe" not in enrich
    # Scratch is pulled out before wrapping, so it never lands in payload.
    assert "cwa_level = del(.level)" in enrich
    assert "cwa_log_source = del(.log_source)" in enrich
    # No flattening of agent metadata to the top level.
    assert '.agent = "crusoe-watch-agent"' not in enrich
    assert ".host = get_hostname" not in enrich
    assert "._msg = parsed_msg" in enrich
    assert "._msg = .payload.message" in enrich
    assert "._time = parsed_time" in enrich

    journald = vector_cfg["transforms"]["parse_journald_logs"]["source"]
    # Drop-nothing: must not delete the raw message/timestamp.
    assert "del(.message)" not in journald
    assert "del(.timestamp)" not in journald
    assert '.log_source = "journald"' in journald
    assert '.level = "info"' in journald

    crusoe = vector_cfg["transforms"]["parse_crusoe_container_logs"]["source"]
    # Parsed JSON message/level staged into scratch; the raw .message line is preserved.
    assert "._msg = string!(parsed.message)" in crusoe
    assert ".level = string!(parsed.level)" in crusoe
    assert ".message = string!(parsed.message)" not in crusoe
    assert '.log_source = "cwa-config-reloader"' in crusoe

    internal = vector_cfg["transforms"]["parse_internal_logs"]["source"]
    assert '.log_source = "crusoe-watch-agent"' in internal

    operator = vector_cfg["transforms"]["parse_operator_logs"]["source"]
    # Nothing is dropped: the raw line ships verbatim under payload.
    assert "del(.message)" not in operator

def test_operator_deployment_logs_collected():
    """The operator namespaces get their own source, restricted to Deployment
    pods by label selector, parsed on their own branch into enrich_logs."""
    r = VectorConfigReloader()
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r.set_logs_config(vector_cfg)

    operator_source = vector_cfg["sources"]["operator_kubernetes_logs"]
    assert operator_source["type"] == "kubernetes_logs"
    # The selector is what excludes the DaemonSets, so it must stay exactly this.
    assert operator_source["extra_label_selector"] == "pod-template-hash"
    assert operator_source["include_paths_glob_patterns"] == [
        f"/var/log/pods/{ns}_*/*/*.log" for ns in DEFAULT_OPERATOR_LOG_NAMESPACES
    ]
    # Start at the tail of files first seen, so installing the agent does not
    # replay weeks of retained operator history into the sink.
    assert operator_source["read_from"] == "end"
    # Crusoe's own components keep reading from the beginning.
    assert "read_from" not in vector_cfg["sources"]["kubernetes_logs"]

    # The crusoe source stays scoped to crusoe's own components.
    globs = vector_cfg["sources"]["kubernetes_logs"]["include_paths_glob_patterns"]
    assert "/var/log/pods/crusoe-system_crusoe-log-collector-*/*/*.log" in globs
    assert not [g for g in globs if "operator" in g]

    # Selector plus namespace glob are exact, so nothing filters in between.
    assert "filter_operator_logs" not in vector_cfg["transforms"]
    assert vector_cfg["transforms"]["parse_operator_logs"]["inputs"] == [
        "operator_kubernetes_logs"
    ]
    assert "parse_operator_logs" in vector_cfg["transforms"]["enrich_logs"]["inputs"]


def _reloader_with_operator_logs_cfg(monkeypatch, tmp_path, **logs_cfg):
    cfg_path = tmp_path / "reloader-operator-ns.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "dcgm_metrics": {"port": 9400, "path": "/metrics", "scrape_interval": 30},
        "custom_metrics": {"port": 9100, "path": "/metrics", "scrape_interval": 30},
        "sink": {"endpoint": "https://cms-monitoring.example.com"},
        "log_level": "INFO",
        "logs": {"enabled": True, **logs_cfg},
    }))
    monkeypatch.setattr("vector_config_reloader_app.RELOADER_CONFIG_PATH", str(cfg_path))
    return VectorConfigReloader()


def test_operator_namespaces_are_configurable(monkeypatch, tmp_path):
    """logs.operator_namespaces overrides the defaults."""
    r = _reloader_with_operator_logs_cfg(
        monkeypatch, tmp_path, operator_namespaces=["my-gpu-operator"],
    )
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r.set_logs_config(vector_cfg)

    assert vector_cfg["sources"]["operator_kubernetes_logs"][
        "include_paths_glob_patterns"
    ] == ["/var/log/pods/my-gpu-operator_*/*/*.log"]


def test_empty_operator_namespaces_drops_the_branch(monkeypatch, tmp_path):
    """An explicit empty namespace list disables operator log collection
    entirely — no source, no dangling transforms, no reference from enrich_logs."""
    r = _reloader_with_operator_logs_cfg(monkeypatch, tmp_path, operator_namespaces=[])
    vector_cfg = {"sources": {}, "transforms": {}, "sinks": {}}
    r.set_logs_config(vector_cfg)

    assert "operator_kubernetes_logs" not in vector_cfg["sources"]
    assert "parse_operator_logs" not in vector_cfg["transforms"]
    assert "parse_operator_logs" not in vector_cfg["transforms"]["enrich_logs"]["inputs"]
    # Crusoe's own log branches are untouched.
    assert "parse_crusoe_container_logs" in vector_cfg["transforms"]["enrich_logs"]["inputs"]


def test_operator_logs_parse_the_deployment_formats():
    """json / klog / zap console cover every format the operator Deployments
    emit."""
    r = VectorConfigReloader()
    source = str(r.parse_operator_logs_transform_source)

    assert "parse_json(" in source
    assert "parse_klog(" in source
    # zap console has no parse function of its own; it is the tab-separated regex.
    assert "(?P<level>[A-Z]+)" in source
    # zapr verbosity and the zap console encoder have no level key of their own.
    assert "json_fields.v" in source
    # klog is tried first: zap console is lenient enough to claim its lines.
    assert source.index("parse_klog(") < source.index("(?P<level>[A-Z]+)")


# --- VRL executed for real, over lines captured from a live GPU cluster ---

_VECTOR_BIN = shutil.which("vector") or os.path.expanduser("~/.vector/bin/vector")

# (label, raw container line, namespace, expected level, expected _msg)
OPERATOR_LOG_SAMPLES = [
    ("zap json / gpu-operator controller",
     '{"level":"error","ts":1788187307.396846,"msg":"Error while syncing state",'
     '"controller":"nvidia-driver-controller"}',
     "nvidia-gpu-operator", "error", "Error while syncing state"),
    # klog structured form quotes the message; the quotes are unwrapped.
    ("klog structured / nfd-worker",
     'I0831 14:41:44.155204       1 nfd-worker.go:551] "feature discovery completed"',
     "nvidia-gpu-operator", "info", 'feature discovery completed'),
    # klog structured form with trailing key=value pairs: only the leading quoted
    # message becomes _msg; the tail stays in the raw line under payload.
    ("klog structured with fields / nfd-master",
     'I0831 12:05:39.100000       1 nfd-master.go:574] "updating NodeFeature object" '
     'nodefeature="nvidia-gpu-operator/np-15a3bbfe-2.us-southcentral1-a.compute.internal"',
     "nvidia-gpu-operator", "info", 'updating NodeFeature object'),
    ("klog printf / gpu-feature-discovery",
     'I0831 14:41:09.118727       1 main.go:277] Creating Labels',
     "nvidia-gpu-operator", "info", "Creating Labels"),
    ("unstructured / device-plugin init",
     'waiting for nvidia container stack to be setup',
     "nvidia-gpu-operator", None, 'waiting for nvidia container stack to be setup'),
    ("unstructured / nvidia-smi table border",
     '|=====================================================================|',
     "nvidia-gpu-operator", None,
     '|=====================================================================|'),
    ("network-operator namespace",
     '{"level":"info","ts":1788187307.0,"msg":"Reconciling NicClusterPolicy"}',
     "nvidia-network-operator", "info", "Reconciling NicClusterPolicy"),
    # zapr omits the level key: `v` is verbosity, `error` marks an error entry.
    ("zapr json v=0 / nv-ipam-node",
     '{"ts":1788193265723.4,"caller":"cleaner/cleaner.go:77","msg":"check for stale IPs","v":0}',
     "nvidia-network-operator", "info", "check for stale IPs"),
    ("zapr json v=2 / verbose",
     '{"ts":1.0,"msg":"verbose detail","v":2}',
     "nvidia-network-operator", "debug", "verbose detail"),
    ("zapr json error / no v",
     '{"ts":1788193277684.0,"msg":"failed to reconcile","error":"connection refused"}',
     "nvidia-network-operator", "error", "failed to reconcile"),
    # nic-feature-discovery uses `err`, not `error`.
    ("zapr json err key / nic-feature-discovery",
     '{"ts":1788198677853.2,"caller":"daemon/daemon.go:89","msg":"failed to write feature labels",'
     '"err":"open /etc/kubernetes/.../features.d/...: permission denied"}',
     "nvidia-network-operator", "error", "failed to write feature labels"),
    # zap console encoder: <ts> <LEVEL> [logger] <message> [{fields}], tab separated.
    ("zap console no logger / network-operator",
     '2026-08-31T16:15:18Z\tINFO\tfetching DOCA driver image tags\t{"repo": "nvcr.io"}',
     "nvidia-network-operator", "info", "fetching DOCA driver image tags"),
    ("zap console with logger",
     '2026-08-31T09:33:10Z\tINFO\tstate.state-doca\tcheck state for stale objects\t{"c": "x"}',
     "nvidia-network-operator", "info", "check state for stale objects"),
    ("zap console no field object",
     '2026-08-31T09:33:10Z\tERROR\tcontrollers.NCP\treconcile failed',
     "nvidia-network-operator", "error", "reconcile failed"),
    # Tabs alone must not be claimed as zap console.
    ("tabbed plain text",
     'some\ttabbed\tplain text',
     "nvidia-network-operator", None, 'some\ttabbed\tplain text'),
    # logfmt and python/supervisord are DaemonSet formats, outside this scope:
    # the line ships raw as _msg with no level.
    ("logfmt logrus / unparsed",
     'time="2026-08-19T21:25:40Z" level=info msg="Completed setup for nvidia-ctk-installer"',
     "nvidia-gpu-operator", None,
     'time="2026-08-19T21:25:40Z" level=info msg="Completed setup for nvidia-ctk-installer"'),
    ("python logging / unparsed",
     '2026-08-19 21:26:22,164 INFO success: doca_config_watcher entered RUNNING state',
     "nvidia-network-operator", None,
     '2026-08-19 21:26:22,164 INFO success: doca_config_watcher entered RUNNING state'),
]


def _run_vrl(program, event, tmp_path):
    """Execute a VRL program over one event via the vector CLI."""
    prog_path = tmp_path / "program.vrl"
    prog_path.write_text(program)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event) + "\n")

    result = subprocess.run(
        [_VECTOR_BIN, "vrl", "-p", str(prog_path), "-i", str(event_path), "-o"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"vrl failed: {result.stderr}"
    # The CLI renders timestamps as VRL literals (t'...'), which are not valid JSON.
    return json.loads(re.sub(r"t'([^']*)'", r'"\1"', result.stdout.strip()), strict=False)


# Two NFD labels, which are dropped, and two that stay.
_NODE_LABELS = {
    "feature.node.kubernetes.io/cpu-cpuid.AVX512VPOPCNTDQ": "true",
    "feature.node.kubernetes.io/system-os_release.VERSION_ID.major": "22",
    "crusoe.ai/nodepool.name": "l40s-nodepool",
    "nvidia.com/gpu.product": "NVIDIA-L40S",
}


def _run_operator_pipeline(line, namespace, tmp_path):
    """Run parse_operator_logs + enrich_logs over one container line the way
    vector will."""
    r = VectorConfigReloader()
    program = "\n".join([
        str(r.parse_operator_logs_transform_source),
        str(r.enrich_logs_transform_source).replace("${CHART_VERSION}", "0.0.0"),
        ".",
    ])
    event = {
        "message": line,
        # kubernetes_logs sets this from the CRI prefix; enrich_logs falls back to it.
        "timestamp": "2026-08-31T14:41:44Z",
        "kubernetes": {
            "pod_namespace": namespace,
            "pod_name": "operator-pod",
            "container_name": "operator",
            "node_labels": dict(_NODE_LABELS),
        },
        # The rest of what kubernetes_logs stamps on the event.
        "file": "/var/log/pods/nvidia-gpu-operator_operator-pod_uid/operator/0.log",
        "source_type": "kubernetes_logs",
        "stream": "stderr",
    }
    return _run_vrl(program, event, tmp_path)


@pytest.mark.skipif(not os.path.exists(_VECTOR_BIN), reason="vector binary not available")
@pytest.mark.parametrize(
    "label,line,namespace,want_level,want_msg",
    OPERATOR_LOG_SAMPLES,
    ids=[s[0] for s in OPERATOR_LOG_SAMPLES],
)
def test_operator_log_lines_from_real_cluster(label, line, namespace, want_level,
                                              want_msg, tmp_path):
    """Run parse_operator_logs + enrich_logs the way vector will, over lines
    captured from a live GPU cluster."""
    out = _run_operator_pipeline(line, namespace, tmp_path)

    assert out["_msg"] == want_msg
    if want_level is None:
        assert "level" not in out, "unstructured line should carry no level"
    else:
        assert out["level"] == want_level
    # log_source is <operator>/<container>, the namespace stripped of its
    # optional nvidia- prefix so both install layouts converge.
    assert out["log_source"] == f"{namespace.removeprefix('nvidia-')}/operator"
    # Every event needs a timestamp; the raw line ships verbatim under payload.
    assert out["_time"]
    assert out["payload"]["message"] == line
    # NFD's hardware labels are dropped; the rest of node_labels stays.
    node_labels = out["payload"]["kubernetes"]["node_labels"]
    assert not [k for k in node_labels if k.startswith("feature.node.kubernetes.io/")]
    assert node_labels == {
        "crusoe.ai/nodepool.name": "l40s-nodepool",
        "nvidia.com/gpu.product": "NVIDIA-L40S",
    }


# (label, raw container line, namespace, fields expected under payload)
OPERATOR_PAYLOAD_FIELD_SAMPLES = [
    # json: every key becomes a payload field, nested ones included.
    ("zap json / gpu-operator controller",
     '{"level":"info","ts":1788452153.1446955,"msg":"Syncing system state",'
     '"controller":"nvidia-driver-controller","object":{"name":"a100-non-ib"},'
     '"namespace":"","name":"a100-non-ib","reconcileID":"6910dbaa"}',
     "nvidia-gpu-operator",
     {"controller": "nvidia-driver-controller", "object": {"name": "a100-non-ib"},
      "name": "a100-non-ib", "reconcileID": "6910dbaa", "ts": 1788452153.1446955}),
    # klog structured: the key="value" tail after the quoted message is logfmt.
    ("klog structured with fields / nv-ipam-controller",
     'I0903 16:19:49.242232       1 node.go:44] "Notification on Node" controller="node" '
     'controllerGroup="" controllerKind="Node" '
     'Node="np-76b88e75-2.us-southcentral1-a.compute.internal" reconcileID="084d3b4e"',
     "nvidia-network-operator",
     {"controller": "node", "controllerGroup": "", "controllerKind": "Node",
      "Node": "np-76b88e75-2.us-southcentral1-a.compute.internal",
      "reconcileID": "084d3b4e"}),
    # zap console: the trailing {...} object holds the fields.
    ("zap console / network-operator",
     '2026-08-31T16:15:18Z\tINFO\tfetching DOCA driver image tags\t'
     '{"repo": "nvcr.io", "count": 3}',
     "nvidia-network-operator",
     {"repo": "nvcr.io", "count": 3}),
]


@pytest.mark.skipif(not os.path.exists(_VECTOR_BIN), reason="vector binary not available")
@pytest.mark.parametrize(
    "label,line,namespace,want_fields",
    OPERATOR_PAYLOAD_FIELD_SAMPLES,
    ids=[s[0] for s in OPERATOR_PAYLOAD_FIELD_SAMPLES],
)
def test_operator_log_fields_are_queryable_under_payload(label, line, namespace,
                                                         want_fields, tmp_path):
    """Each format's fields are lifted to payload.<field>."""
    out = _run_operator_pipeline(line, namespace, tmp_path)

    payload = out["payload"]
    for key, value in want_fields.items():
        assert payload[key] == value, key
    # Vector's own metadata and the raw line are still intact.
    assert payload["message"] == line
    assert payload["source_type"] == "kubernetes_logs"
    assert payload["timestamp"] == "2026-08-31T14:41:44Z"
    assert payload["kubernetes"]["pod_namespace"] == namespace


@pytest.mark.skipif(not os.path.exists(_VECTOR_BIN), reason="vector binary not available")
def test_operator_log_unparsed_line_adds_no_payload_fields(tmp_path):
    """A line in none of the three formats is not force-parsed into fields."""
    line = 'time="2026-08-19T21:25:40Z" level=info msg="Completed setup"'
    out = _run_operator_pipeline(line, "nvidia-gpu-operator", tmp_path)

    assert set(out["payload"]) == {
        "message", "timestamp", "kubernetes", "file", "source_type", "stream",
    }


@pytest.mark.skipif(not os.path.exists(_VECTOR_BIN), reason="vector binary not available")
def test_operator_log_fields_never_clobber_vector_metadata(tmp_path):
    """A log field named like one of vector's own keys loses to vector's value:
    payload.file stays the log file path, payload.timestamp the CRI timestamp."""
    line = ('{"level":"info","msg":"reconciled","file":"driver.go:88",'
            '"timestamp":"1999-01-01T00:00:00Z","stream":"in-band"}')
    out = _run_operator_pipeline(line, "nvidia-gpu-operator", tmp_path)

    payload = out["payload"]
    assert payload["file"].startswith("/var/log/pods/")
    assert payload["timestamp"] == "2026-08-31T14:41:44Z"
    assert payload["stream"] == "stderr"
    assert out["_msg"] == "reconciled"
