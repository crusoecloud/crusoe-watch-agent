#!/usr/bin/env python3
"""
GPU Driver Log Collector - Unified K8s and VM

This application collects GPU bug reports in both Kubernetes and VM environments.
Supports both NVIDIA and AMD GPUs.

Features:
- Auto-detects K8s vs VM environment
- Auto-detects or uses configured GPU type (NVIDIA or AMD)
- K8s: Discovers GPU driver pods and executes bug reports (GPU Operator or bundled mode)
- VM: Executes bug report scripts locally
- API-driven or scheduled collection modes
- Manages disk space by keeping only recent logs
"""

import os
import sys
import time
import logging
import tarfile
import tempfile
import base64
import requests
import subprocess
import socket
import json
import shlex
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from datetime import datetime, timezone

# Environment detection - determines K8s vs VM mode
ENVIRONMENT = "kubernetes" if os.getenv("KUBERNETES_SERVICE_HOST") else "vm"

# Conditional imports for K8s
if ENVIRONMENT == "kubernetes":
    from kubernetes import client, config
    from kubernetes.client.rest import ApiException
    from kubernetes.stream import stream

# Configuration from environment variables
GPU_TYPE = os.environ.get("GPU_TYPE", "nvidia").lower()  # nvidia or amd
LOG_OUTPUT_DIR = os.environ.get("LOG_OUTPUT_DIR", "/logs")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
MAX_LOGS_TO_KEEP = int(os.environ.get("MAX_LOGS_TO_KEEP", "1"))

# API configuration
API_BASE_URL = os.environ.get("API_BASE_URL", os.environ.get("LOG_COLLECTOR_API_BASE_URL", "https://cms-monitoring.crusoecloud.com"))
API_POLL_INTERVAL = int(os.environ.get("API_POLL_INTERVAL", "60"))
API_ENABLED = os.environ.get("API_ENABLED", "false").lower() == "true"
COLLECTION_TIMEOUT = int(os.environ.get("COLLECTION_TIMEOUT", "300"))
COLLECTION_INTERVAL = int(os.environ.get("COLLECTION_INTERVAL", "3600"))
CRUSOE_AUTH_TOKEN = os.environ.get("CRUSOE_MONITORING_TOKEN") or os.environ.get("CRUSOE_AUTH_TOKEN")

# K8s-specific configuration (only used when ENVIRONMENT == "kubernetes")
NODE_NAME = os.environ.get("NODE_NAME")
DRIVER_NAMESPACE = os.environ.get("DRIVER_NAMESPACE", "nvidia-gpu-operator" if GPU_TYPE == "nvidia" else "amd-gpu-operator")
DRIVER_POD_PREFIX = os.environ.get("DRIVER_POD_PREFIX", f"{GPU_TYPE}-gpu-driver")
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() == "true"


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging - efficient and no parsing needed."""

    def format(self, record):
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace('+00:00', 'Z'),
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


# Logging setup
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    handlers=[handler]
)
LOG = logging.getLogger(__name__)


class LogCollector:
    """
    Unified GPU log collector supporting both Kubernetes and VM environments.
    Supports both NVIDIA and AMD GPUs.
    """

    def __init__(self):
        """Initialize the log collector for K8s or VM environment."""
        self.environment = ENVIRONMENT
        self.gpu_type = GPU_TYPE
        self.output_dir = Path(LOG_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Environment-specific initialization
        if self.environment == "kubernetes":
            self._init_kubernetes()
        else:
            self._init_vm()

        # Warn if API mode is enabled but token is missing
        if API_ENABLED and not CRUSOE_AUTH_TOKEN:
            LOG.warning("API_ENABLED is true but auth token is not set. API calls may fail authentication.")

    def _init_kubernetes(self):
        """Initialize Kubernetes-specific configuration."""
        self.node_name = NODE_NAME
        if not self.node_name:
            raise RuntimeError("NODE_NAME environment variable not set in Kubernetes mode")

        self.driver_namespace = DRIVER_NAMESPACE
        self.driver_pod_prefix = DRIVER_POD_PREFIX

        # Get VM_ID from environment or read from DMI
        self.vm_id = os.environ.get("VM_ID")
        if not self.vm_id:
            self.vm_id = self._read_vm_id_from_dmi()

        # Initialize Kubernetes client
        try:
            config.load_incluster_config()
            LOG.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            config.load_kube_config()
            LOG.info("Loaded kubeconfig from local environment")

        self.k8s_api = client.CoreV1Api()
        LOG.info(f"Initialized {self.gpu_type.upper()} log collector for K8s node: {self.node_name}")
        if self.vm_id:
            LOG.info(f"VM ID: {self.vm_id}")

    def _init_vm(self):
        """Initialize VM-specific configuration."""
        self.node_name = socket.gethostname()
        self.hostname = self.node_name

        # Get VM_ID from environment or read from DMI
        self.vm_id = os.environ.get("VM_ID")
        if not self.vm_id:
            self.vm_id = self._read_vm_id_from_dmi()

        LOG.info(f"Initialized {self.gpu_type.upper()} log collector for VM: {self.hostname}")
        if self.vm_id:
            LOG.info(f"VM ID: {self.vm_id}")

    def _read_vm_id_from_dmi(self) -> Optional[str]:
        """
        Read VM ID from DMI.

        In K8s: Reads from /host/sys/class/dmi/id/product_uuid
        In VM: Uses dmidecode command

        Returns:
            VM ID string if available, None otherwise
        """
        if self.environment == "kubernetes":
            # K8s: Read from mounted host filesystem
            dmi_path = Path("/host/sys/class/dmi/id/product_uuid")
            try:
                if dmi_path.exists():
                    vm_id = dmi_path.read_text().strip()
                    LOG.info(f"Read VM ID from DMI: {vm_id}")
                    return vm_id
                else:
                    LOG.warning(f"DMI file not found: {dmi_path}")
                    return None
            except Exception as e:
                LOG.warning(f"Failed to read VM ID from DMI: {e}")
                return None
        else:
            # VM: Use dmidecode command
            try:
                result = subprocess.run(
                    ["dmidecode", "-s", "system-uuid"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    vm_id = result.stdout.strip()
                    LOG.info(f"Read VM ID from dmidecode: {vm_id}")
                    return vm_id
            except Exception as e:
                LOG.error(f"Failed to read VM ID from dmidecode: {e}")
            return None

    def _get_auth_headers(self) -> Dict[str, str]:
        """
        Get authentication headers for API requests.

        Returns:
            Dictionary with Authorization header if token is available
        """
        headers = {}
        if CRUSOE_AUTH_TOKEN:
            headers['Authorization'] = f'Bearer {CRUSOE_AUTH_TOKEN}'
        return headers

    def check_for_tasks(self) -> Optional[Dict[str, Any]]:
        """
        Poll the API to check if there are any log collection tasks.

        Returns:
            Dictionary with task details including event_id, or None if no tasks
        """
        if not self.vm_id:
            LOG.warning("VM_ID not set, cannot check for tasks")
            return None

        try:
            url = f"{API_BASE_URL}/agent/check-tasks"
            params = {"vm_id": self.vm_id}
            headers = self._get_auth_headers()

            LOG.debug(f"Polling API: {url} with params: {params}")
            response = requests.get(url, params=params, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                if data.get("status") == "success" and data.get("event_id"):
                    LOG.info(f"Received task with event_id: {data['event_id']}")
                    return data
                else:
                    LOG.debug(f"No tasks available: {data}")
                    return None
            elif response.status_code == 404:
                LOG.debug("No tasks found for this VM")
                return None
            else:
                LOG.warning(f"Unexpected API response: {response.status_code} - {response.text}")
                return None

        except requests.exceptions.Timeout:
            LOG.warning("API request timed out")
            return None
        except requests.exceptions.RequestException as e:
            LOG.error(f"API request failed: {e}")
            return None
        except Exception as e:
            LOG.error(f"Unexpected error checking for tasks: {e}")
            return None

    def report_result(self, event_id: str, status: str, log_file: Optional[Path] = None, message: str = "") -> bool:
        """
        Report collection result to the API - combines upload and status in a single call.

        Args:
            event_id: Event ID for this collection
            status: Status string ('success' or 'failed')
            log_file: Optional path to log file (for success case)
            message: Optional message with details (for failed case or additional info)

        Returns:
            True if report successful, False otherwise
        """
        try:
            url = f"{API_BASE_URL}/agent/upload-logs"
            headers = self._get_auth_headers()

            if log_file and status == "success":
                # Success case - upload file with success status
                LOG.info(f"Uploading log file {log_file.name} with success status for event {event_id}")

                with open(log_file, 'rb') as f:
                    files = {'file': (log_file.name, f, 'application/gzip')}
                    data = {
                        'vm_id': self.vm_id,
                        'event_id': event_id,
                        'node_name': self.node_name,
                        'status': status,
                        'message': message if message else 'Logs collected and uploaded successfully'
                    }

                    response = requests.post(url, files=files, data=data, headers=headers, timeout=60)
            else:
                # Failed case - send status only
                LOG.info(f"Sending {status} status for event {event_id}: {message}")
                data = {
                    'vm_id': self.vm_id,
                    'event_id': event_id,
                    'status': status,
                    'message': message,
                    'node_name': self.node_name
                }

                response = requests.post(url, json=data, headers=headers, timeout=10)

            if response.status_code == 200:
                LOG.info(f"Successfully reported {status} result for event {event_id}")
                return True
            else:
                LOG.error(f"Failed to report result: {response.status_code} - {response.text}")
                return False

        except requests.exceptions.Timeout:
            LOG.error("Report request timed out")
            return False
        except requests.exceptions.RequestException as e:
            LOG.error(f"Report request failed: {e}")
            return False
        except Exception as e:
            LOG.error(f"Unexpected error during report: {e}")
            return False

    def _get_node_instance_type(self) -> Optional[str]:
        """
        Get the instance type from Kubernetes node labels (K8s only).

        Returns:
            Instance type (e.g., "a100-80gb.1x", "gb200-320gb.1x") or None
        """
        if self.environment != "kubernetes":
            return None

        try:
            node = self.k8s_api.read_node(self.node_name)
            return node.metadata.labels.get('node.kubernetes.io/instance-type')
        except ApiException as e:
            LOG.error(f"Error reading node labels: {e}")
            return None

    def _is_bundled_driver_mode(self) -> bool:
        """
        Determine if this node uses bundled driver mode (K8s only).

        Returns:
            True if node should use bundled drivers (execute locally),
            False for GPU Operator path (exec into driver pod)
        """
        if self.environment == "vm":
            # VMs always use bundled/local mode
            return True

        # AMD always uses bundled driver mode in K8s
        if self.gpu_type == "amd":
            LOG.info(f"Node {self.node_name} uses AMD, using bundled driver mode")
            return True

        # GB200 nodes use bundled NVIDIA drivers
        instance_type = self._get_node_instance_type()
        if instance_type and 'gb200' in instance_type.lower():
            LOG.info(f"Node {self.node_name} is GB200 (instance-type={instance_type}), using bundled driver mode")
            return True

        # All other GPU types use GPU Operator
        LOG.info(f"Node {self.node_name} instance-type={instance_type}, using GPU Operator mode")
        return False

    def find_nvidia_driver_pod(self):
        """
        Find the NVIDIA GPU driver pod running on this node (K8s only).

        Primary: Uses label selector (app.kubernetes.io/component=nvidia-driver) which is
        set by the official NVIDIA GPU Operator.
        Fallback: Uses pod name prefix for custom/legacy deployments.

        Returns:
            V1Pod object if found, None otherwise
        """
        if self.environment != "kubernetes":
            return None

        try:
            # Primary: Find by label selector (reliable, set by GPU Operator)
            pods = self.k8s_api.list_namespaced_pod(
                namespace=self.driver_namespace,
                field_selector=f"spec.nodeName={self.node_name}",
                label_selector="app.kubernetes.io/component=nvidia-driver"
            )

            for pod in pods.items:
                if pod.status.phase == "Running":
                    LOG.info(f"Found NVIDIA driver pod by label selector: {pod.metadata.name}")
                    return pod
                else:
                    LOG.warning(
                        f"Found NVIDIA driver pod {pod.metadata.name} by label but it's not Running (status: {pod.status.phase})"
                    )

            # Fallback: Find by pod name prefix (for custom/legacy deployments)
            LOG.info(f"No pod found with label selector, trying name prefix fallback: '{self.driver_pod_prefix}'")
            pods = self.k8s_api.list_namespaced_pod(
                namespace=self.driver_namespace,
                field_selector=f"spec.nodeName={self.node_name}"
            )

            for pod in pods.items:
                if pod.metadata.name.startswith(self.driver_pod_prefix):
                    if pod.status.phase == "Running":
                        LOG.info(f"Found NVIDIA driver pod by name prefix: {pod.metadata.name}")
                        return pod
                    else:
                        LOG.warning(
                            f"Found NVIDIA driver pod {pod.metadata.name} but it's not Running (status: {pod.status.phase})"
                        )

            LOG.warning(
                f"No running NVIDIA driver pod found on node {self.node_name} in namespace {self.driver_namespace}"
            )
            return None

        except ApiException as e:
            LOG.error(f"Error finding NVIDIA driver pod: {e}")
            return None

    def execute_nvidia_bug_report(self, pod, event_id: Optional[str] = None) -> Optional[str]:
        """
        Execute nvidia-bug-report.sh in the driver pod (K8s only).

        Args:
            pod: The NVIDIA driver pod
            event_id: Optional event ID to include in filename

        Returns:
            Path to the generated log file within the pod, or None on error
        """
        if self.environment != "kubernetes":
            return None

        pod_name = pod.metadata.name
        container_name = self._get_driver_container_name(pod)

        if not container_name:
            LOG.error(f"Could not find suitable container in pod {pod_name}")
            return None

        LOG.info(f"Executing nvidia-bug-report.sh in pod {pod_name}, container {container_name}")

        try:
            # Generate unique filename with timestamp and optional event_id
            # Note: nvidia-bug-report.sh automatically adds .gz extension
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if event_id:
                log_filename_base = f"nvidia-bug-report-{self.node_name}-{event_id}-{timestamp}.log"
            else:
                log_filename_base = f"nvidia-bug-report-{self.node_name}-{timestamp}.log"
            log_path_base = f"/tmp/{log_filename_base}"

            # The actual file will have .gz appended by nvidia-bug-report.sh
            actual_log_path = f"{log_path_base}.gz"

            # Execute nvidia-bug-report.sh (it will add .gz automatically)
            exec_command = [
                "/bin/bash",
                "-c",
                f"nvidia-bug-report.sh --output-file {shlex.quote(log_path_base)} && echo {shlex.quote(actual_log_path)}"
            ]

            LOG.info(f"Running command: {' '.join(exec_command)}")

            resp = stream(
                self.k8s_api.connect_get_namespaced_pod_exec,
                pod_name,
                self.driver_namespace,
                container=container_name,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False
            )

            output = ""
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    output += resp.read_stdout()
                if resp.peek_stderr():
                    stderr = resp.read_stderr()
                    if stderr:
                        LOG.info(f"nvidia-bug-report stderr: {stderr}")

            resp.close()

            if actual_log_path in output or "Bug report generated" in output:
                LOG.info(f"nvidia-bug-report.sh completed successfully, log file: {actual_log_path}")
                return actual_log_path
            else:
                LOG.error(f"nvidia-bug-report.sh execution may have failed. Output: {output}")
                return None

        except ApiException as e:
            LOG.error(f"Error executing nvidia-bug-report.sh: {e}")
            return None
        except Exception as e:
            LOG.error(f"Unexpected error during nvidia-bug-report execution: {e}")
            return None

    def execute_bug_report_local(self, event_id: Optional[str] = None) -> Optional[Path]:
        """
        Execute bug report script locally (bundled in container).
        Works for both NVIDIA and AMD GPUs, in both K8s (bundled mode) and VM environments.

        Args:
            event_id: Optional event ID to include in filename

        Returns:
            Path to generated log file, or None on error
        """
        LOG.info(f"Executing {self.gpu_type}-bug-report.sh locally (bundled driver mode)")

        # Determine script path based on GPU type
        script_path = Path(f"/usr/bin/{self.gpu_type}-bug-report.sh")
        if not script_path.exists():
            LOG.error(f"Bug report script not found at {script_path}")
            return None

        try:
            # Generate unique filename with timestamp and optional event_id
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if event_id:
                log_filename_base = f"{self.gpu_type}-bug-report-{self.node_name}-{event_id}-{timestamp}.log"
            else:
                log_filename_base = f"{self.gpu_type}-bug-report-{self.node_name}-{timestamp}.log"

            # Output directly to /logs
            log_path_base = Path(self.output_dir) / log_filename_base
            actual_log_path = Path(f"{log_path_base}.gz")

            # NVIDIA workaround: temporarily rename mst to prevent
            # nvidia-bug-report.sh from hanging on 'mst gpu add'
            # This is primarily needed in VM environments
            mst_path = Path("/usr/bin/mst")
            mst_backup_path = Path("/usr/bin/mstbkup")
            mst_was_renamed = False

            try:
                if self.gpu_type == "nvidia" and mst_path.exists():
                    LOG.debug("Temporarily renaming /usr/bin/mst to prevent hang")
                    mst_path.rename(mst_backup_path)
                    mst_was_renamed = True

                # Execute bug report script bundled in the container
                cmd = [str(script_path), "--output-file", str(log_path_base)]

                LOG.info(f"Running command: {' '.join(cmd)}")

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=COLLECTION_TIMEOUT
                )

                if result.returncode == 0 and actual_log_path.exists():
                    LOG.info(f"Bug report completed successfully: {actual_log_path}")
                    return actual_log_path
                else:
                    LOG.error(f"Bug report failed with return code {result.returncode}")
                    LOG.error(f"stdout: {result.stdout}")
                    LOG.error(f"stderr: {result.stderr}")
                    return None

            finally:
                # Restore mst binary if it was renamed
                if mst_was_renamed and mst_backup_path.exists():
                    LOG.debug("Restoring /usr/bin/mst from backup")
                    mst_backup_path.rename(mst_path)

        except subprocess.TimeoutExpired:
            LOG.error(f"Bug report timed out after {COLLECTION_TIMEOUT}s")
            return None
        except Exception as e:
            LOG.error(f"Error executing bug report locally: {e}")
            return None

    def download_log_file(self, pod, remote_path: str) -> Optional[Path]:
        """
        Download the log file from the driver pod using tar (K8s only).

        Args:
            pod: The NVIDIA driver pod
            remote_path: Path to the log file in the pod

        Returns:
            Path to the downloaded file, or None on error
        """
        if self.environment != "kubernetes":
            return None

        pod_name = pod.metadata.name
        container_name = self._get_driver_container_name(pod)

        if not container_name:
            LOG.error(f"Could not find suitable container in pod {pod_name}")
            return None

        LOG.info(f"Downloading {remote_path} from pod {pod_name}")

        try:
            # First verify the file exists
            LOG.debug(f"Verifying file exists: {remote_path}")
            check_command = ["test", "-f", remote_path, "&&", "echo", "EXISTS"]

            resp = stream(
                self.k8s_api.connect_get_namespaced_pod_exec,
                pod_name,
                self.driver_namespace,
                container=container_name,
                command=["/bin/sh", "-c", " ".join(check_command)],
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False
            )

            if "EXISTS" not in resp:
                LOG.error(f"File does not exist: {remote_path}")
                # List files in /tmp to help debug
                list_resp = stream(
                    self.k8s_api.connect_get_namespaced_pod_exec,
                    pod_name,
                    self.driver_namespace,
                    container=container_name,
                    command=["ls", "-lh", "/tmp/"],
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False
                )
                LOG.debug(f"Files in /tmp:\n{list_resp}")
                return None

            # Use base64 to avoid binary encoding issues
            # Create tar, pipe to base64, then decode on our side
            remote_dir = os.path.dirname(remote_path)
            remote_file = os.path.basename(remote_path)
            exec_command = [
                "/bin/sh", "-c",
                f"tar -C {remote_dir} -cf - {remote_file} | base64"
            ]

            LOG.debug(f"Running command: {exec_command[2]}")

            resp = stream(
                self.k8s_api.connect_get_namespaced_pod_exec,
                pod_name,
                self.driver_namespace,
                container=container_name,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False
            )

            # Read base64-encoded data (this is text, not binary)
            if resp:
                LOG.debug(f"Received base64 data, decoding...")
                try:
                    tar_data = base64.b64decode(resp)
                except Exception as e:
                    LOG.error(f"Failed to decode base64 data: {e}")
                    return None
            else:
                LOG.error(f"No data received when downloading {remote_path}")
                return None

            LOG.debug(f"Decoded {len(tar_data)} bytes of tar data")

            # Extract tar to output directory
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tmp_tar:
                tmp_tar.write(tar_data)
                tmp_tar_path = tmp_tar.name

            try:
                with tarfile.open(tmp_tar_path, 'r') as tar:
                    # List members for debugging
                    members = tar.getmembers()
                    LOG.debug(f"Tar contains {len(members)} members: {[m.name for m in members]}")

                    # Extract the file (it should just be the filename without path now)
                    member = tar.getmember(remote_file)
                    tar.extract(member, path=self.output_dir)

                output_file = self.output_dir / remote_file
                LOG.info(f"Successfully downloaded log to {output_file}")
                return output_file

            finally:
                os.unlink(tmp_tar_path)

        except ApiException as e:
            LOG.error(f"Kubernetes API error downloading log file: {e}")
            return None
        except tarfile.TarError as e:
            LOG.error(f"Error extracting tar file: {e}")
            return None
        except KeyError as e:
            LOG.error(f"File not found in tar archive: {e}")
            return None
        except Exception as e:
            LOG.error(f"Unexpected error downloading log file: {e}")
            return None

    def cleanup_remote_log(self, pod, remote_path: str) -> bool:
        """
        Clean up the temporary log file from the driver pod (K8s only).

        Args:
            pod: The NVIDIA driver pod
            remote_path: Path to the log file to remove

        Returns:
            True if cleanup successful, False otherwise
        """
        if self.environment != "kubernetes":
            return False

        pod_name = pod.metadata.name
        container_name = self._get_driver_container_name(pod)

        if not container_name:
            return False

        LOG.info(f"Cleaning up {remote_path} from pod {pod_name}")

        try:
            # Delete the file and verify it's gone
            exec_command = [
                "/bin/sh", "-c",
                f"rm -f {remote_path} && if [ -f {remote_path} ]; then echo 'STILL_EXISTS'; else echo 'DELETED'; fi"
            ]

            resp = stream(
                self.k8s_api.connect_get_namespaced_pod_exec,
                pod_name,
                self.driver_namespace,
                container=container_name,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False
            )

            if "DELETED" in resp:
                LOG.info(f"Successfully cleaned up {remote_path}")
                return True
            elif "STILL_EXISTS" in resp:
                LOG.error(f"File still exists after deletion attempt: {remote_path}")
                return False
            else:
                LOG.warning(f"Unexpected cleanup response: {resp}")
                return False

        except Exception as e:
            LOG.warning(f"Error cleaning up remote log file (non-critical): {e}")
            return False

    def _cleanup_logs_by_pattern(self, pattern: str, log_type: str) -> None:
        """
        Helper method to clean up log files matching a pattern.

        Args:
            pattern: Glob pattern to match log files
            log_type: Description of log type for logging (e.g., "compressed", "unzipped")
        """
        log_files = sorted(
            self.output_dir.glob(pattern),
            key=lambda p: p.stat().st_mtime,
            reverse=True  # Newest first
        )

        if not log_files:
            LOG.debug(f"No existing {log_type} log files found to clean up")
            return

        total_files = len(log_files)
        files_to_keep = log_files[:MAX_LOGS_TO_KEEP]
        files_to_delete = log_files[MAX_LOGS_TO_KEEP:]

        if files_to_delete:
            LOG.info(f"Found {total_files} {log_type} log files, keeping {len(files_to_keep)}, removing {len(files_to_delete)} old logs")

            for log_file in files_to_delete:
                try:
                    file_size = log_file.stat().st_size
                    log_file.unlink()
                    LOG.info(f"Deleted old {log_type} log: {log_file.name} ({file_size / (1024*1024):.2f} MB)")
                except Exception as e:
                    LOG.warning(f"Failed to delete {log_file.name}: {e}")
        else:
            LOG.debug(f"No {log_type} log cleanup needed, only {total_files} files found (max: {MAX_LOGS_TO_KEEP})")

    def cleanup_old_logs(self) -> None:
        """
        Clean up old log files to prevent disk space issues.
        Keeps only the most recent MAX_LOGS_TO_KEEP files.
        Cleans up both compressed (.log.gz) and unzipped (.log) files in the output directory.
        """
        try:
            self._cleanup_logs_by_pattern(f"{self.gpu_type}-bug-report-*.log.gz", "compressed")
            self._cleanup_logs_by_pattern(f"{self.gpu_type}-bug-report-*.log", "unzipped")
        except Exception as e:
            LOG.warning(f"Error during log cleanup (non-critical): {e}")

    def collect_logs(self, event_id: Optional[str] = None) -> Tuple[Optional[Path], str]:
        """
        Main log collection workflow for both K8s and VM environments.

        Args:
            event_id: Optional event ID to include in filename and for API tracking

        Returns:
            Tuple of (path to collected log file, error message).
            On success: (Path, "")
            On failure: (None, "specific error message")
        """
        LOG.info(f"Starting log collection cycle ({self.environment} mode, node {self.node_name}){f' for event {event_id}' if event_id else ''}")

        # Clean up old logs to prevent disk space issues
        self.cleanup_old_logs()

        local_log_path = None

        if self._is_bundled_driver_mode():
            # Execute bug report locally (bundled in container)
            # This path is used by: VMs, AMD in K8s, GB200 in K8s
            LOG.info("Using bundled driver mode")
            local_log_path = self.execute_bug_report_local(event_id)

            if not local_log_path:
                error_msg = "Failed to execute bug report locally (bundled driver mode)"
                LOG.error(error_msg)
                return None, error_msg
        else:
            # Execute via GPU Operator driver pod (NVIDIA in K8s only, non-GB200)
            LOG.info("Using GPU Operator mode (driver pod)")
            driver_pod = self.find_nvidia_driver_pod()
            if not driver_pod:
                error_msg = f"NVIDIA driver pod not found on node {self.node_name} in namespace {self.driver_namespace}"
                LOG.error(f"Cannot collect logs: {error_msg}")
                return None, error_msg

            remote_log_path = self.execute_nvidia_bug_report(driver_pod, event_id)
            if not remote_log_path:
                error_msg = f"Failed to execute nvidia-bug-report.sh in driver pod {driver_pod.metadata.name}"
                LOG.error(error_msg)
                return None, error_msg

            local_log_path = self.download_log_file(driver_pod, remote_log_path)
            if not local_log_path:
                error_msg = f"Failed to download log file from driver pod {driver_pod.metadata.name}"
                LOG.error(error_msg)
                return None, error_msg

            # Clean up remote log file
            self.cleanup_remote_log(driver_pod, remote_log_path)

        LOG.info(f"Log collection completed successfully: {local_log_path} ({local_log_path.stat().st_size / (1024*1024):.2f} MB)")
        return local_log_path, ""

    def collect_logs_with_timeout(self, event_id: str) -> Tuple[bool, Optional[Path], str]:
        """
        Collect logs with timeout handling.

        Args:
            event_id: Event ID for this collection task

        Returns:
            Tuple of (success, log_path, error_message)
        """
        import threading

        result = {"log_path": None, "error": None, "completed": False}

        def collection_target():
            try:
                log_path, error_msg = self.collect_logs(event_id)
                result["log_path"] = log_path
                if error_msg:
                    result["error"] = error_msg
                result["completed"] = True
            except Exception as e:
                result["error"] = str(e)
                result["completed"] = True

        thread = threading.Thread(target=collection_target, daemon=True)
        thread.start()
        thread.join(timeout=COLLECTION_TIMEOUT)

        if not result["completed"]:
            # Timeout occurred
            error_msg = f"Collection timeout after {COLLECTION_TIMEOUT} seconds"
            LOG.error(error_msg)
            return False, None, error_msg

        if result["error"]:
            return False, None, result["error"]

        if result["log_path"]:
            return True, result["log_path"], ""
        else:
            return False, None, "Collection failed with unknown error"

    def _get_driver_container_name(self, pod) -> Optional[str]:
        """
        Get the name of the driver container in the pod (K8s only).

        Args:
            pod: The NVIDIA driver pod

        Returns:
            Container name, or None if not found
        """
        if self.environment != "kubernetes":
            return None

        # Try to find a container with 'driver' in the name
        for container in pod.spec.containers:
            if "driver" in container.name.lower():
                return container.name

        # If not found, use the first container
        if pod.spec.containers:
            return pod.spec.containers[0].name

        return None

    def run(self):
        """Main execution loop."""
        LOG.info(f"{self.gpu_type.upper()} Log Collector started")
        LOG.info(f"Environment: {self.environment}")
        LOG.info(f"Node/Host: {self.node_name}")
        LOG.info(f"VM ID: {self.vm_id}")
        LOG.info(f"Output directory: {self.output_dir}")
        LOG.info(f"API-driven mode: {API_ENABLED}")

        if self.environment == "kubernetes":
            LOG.info(f"Run once mode: {RUN_ONCE}")
            if RUN_ONCE:
                # Run once and exit (K8s only)
                log_path, error_msg = self.collect_logs()
                if error_msg:
                    LOG.error(f"Collection failed: {error_msg}")
                sys.exit(0 if log_path else 1)

        if API_ENABLED:
            # API-driven mode: poll for tasks and collect logs on-demand
            self._run_api_mode()
        else:
            # Scheduled mode: collect logs at regular intervals
            self._run_scheduled_mode()

    def _run_api_mode(self):
        """Run in API-driven mode - poll for tasks and collect on-demand."""
        LOG.info(f"Running in API-driven mode")
        LOG.info(f"API base URL: {API_BASE_URL}")
        LOG.info(f"Polling interval: {API_POLL_INTERVAL}s")
        LOG.info(f"Collection timeout: {COLLECTION_TIMEOUT}s")

        if not self.vm_id:
            LOG.error("VM_ID not set - cannot run in API-driven mode")
            sys.exit(1)

        while True:
            try:
                # Check if there's a task to process
                task = self.check_for_tasks()

                if task and task.get("event_id"):
                    event_id = task["event_id"]
                    LOG.info(f"Processing collection task for event: {event_id}")

                    # Collect logs with timeout
                    success, log_path, error_msg = self.collect_logs_with_timeout(event_id)

                    if success and log_path:
                        # Report success and upload logs in a single call
                        LOG.info(f"Reporting success and uploading logs for event {event_id}")
                        upload_success = self.report_result(event_id, "success", log_file=log_path)

                        if not upload_success:
                            # Upload failed - try to report failure status without file
                            LOG.warning(f"Upload failed for event {event_id}, reporting failure status")
                            self.report_result(event_id, "failed", message="Log collection succeeded but upload failed")
                    else:
                        # Report failure with error message
                        LOG.error(f"Collection failed for event {event_id}: {error_msg}")
                        self.report_result(event_id, "failed", message=error_msg)

                else:
                    # No tasks available
                    LOG.debug("No collection tasks available")

            except Exception as e:
                LOG.error(f"Error in API polling loop: {e}", exc_info=True)

            # Wait before polling again
            time.sleep(API_POLL_INTERVAL)

    def _run_scheduled_mode(self):
        """Run in scheduled mode - collect logs at regular intervals."""
        LOG.info(f"Running in scheduled mode")
        LOG.info(f"Collection interval: {COLLECTION_INTERVAL}s")

        while True:
            try:
                log_path, error_msg = self.collect_logs()
                if error_msg:
                    LOG.error(f"Collection failed: {error_msg}")
            except Exception as e:
                LOG.error(f"Error during log collection: {e}", exc_info=True)

            LOG.info(f"Sleeping for {COLLECTION_INTERVAL} seconds until next collection")
            time.sleep(COLLECTION_INTERVAL)


def main():
    """Entry point."""
    try:
        collector = LogCollector()
        collector.run()
    except KeyboardInterrupt:
        LOG.info("Received interrupt signal, shutting down")
        sys.exit(0)
    except Exception as e:
        LOG.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
