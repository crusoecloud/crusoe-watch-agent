#!/usr/bin/env python3
"""
NVIDIA GPU Driver Log Collector

This application runs as a DaemonSet and collects nvidia-bug-report logs from
NVIDIA GPU driver pods running in the nvidia-gpu-operator namespace.

Features:
- Discovers NVIDIA GPU driver pods on the same node
- Executes nvidia-bug-report.sh in the driver pod
- Downloads the generated log file to local storage
- Supports periodic collection or one-time execution
"""

import os
import sys
import time
import logging
import tarfile
import tempfile
from pathlib import Path
from typing import Optional, List
from datetime import datetime

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream

# Configuration from environment variables
NODE_NAME = os.environ.get("NODE_NAME")
LOG_OUTPUT_DIR = os.environ.get("LOG_OUTPUT_DIR", "/logs")
NVIDIA_NAMESPACE = os.environ.get("NVIDIA_NAMESPACE", "nvidia-gpu-operator")
NVIDIA_DRIVER_POD_PREFIX = os.environ.get("NVIDIA_DRIVER_POD_PREFIX", "nvidia-gpu-driver")
COLLECTION_INTERVAL = int(os.environ.get("COLLECTION_INTERVAL", "3600"))  # 1 hour default
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() == "true"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

# Logging setup
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s %(levelname)s: %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)


class NvidiaLogCollector:
    """Collects nvidia-bug-report logs from NVIDIA GPU driver pods."""

    def __init__(self):
        """Initialize the log collector."""
        self.node_name = NODE_NAME
        if not self.node_name:
            raise RuntimeError("NODE_NAME environment variable not set")

        self.nvidia_namespace = NVIDIA_NAMESPACE
        self.driver_pod_prefix = NVIDIA_DRIVER_POD_PREFIX
        self.output_dir = Path(LOG_OUTPUT_DIR)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Initialize Kubernetes client
        try:
            config.load_incluster_config()
            LOG.info("Loaded in-cluster Kubernetes configuration")
        except config.ConfigException:
            config.load_kube_config()
            LOG.info("Loaded kubeconfig from local environment")

        self.k8s_api = client.CoreV1Api()
        LOG.info(f"Initialized log collector for node: {self.node_name}")

    def find_nvidia_driver_pod(self) -> Optional[client.V1Pod]:
        """
        Find the NVIDIA GPU driver pod running on this node.

        Returns:
            V1Pod object if found, None otherwise
        """
        try:
            # List pods in nvidia-gpu-operator namespace on this node
            pods = self.k8s_api.list_namespaced_pod(
                namespace=self.nvidia_namespace,
                field_selector=f"spec.nodeName={self.node_name}"
            )

            # Find the driver pod
            for pod in pods.items:
                if pod.metadata.name.startswith(self.driver_pod_prefix):
                    if pod.status.phase == "Running":
                        LOG.info(f"Found NVIDIA driver pod: {pod.metadata.name}")
                        return pod
                    else:
                        LOG.warning(
                            f"Found NVIDIA driver pod {pod.metadata.name} but it's not Running (status: {pod.status.phase})"
                        )

            LOG.warning(
                f"No running NVIDIA driver pod found on node {self.node_name} in namespace {self.nvidia_namespace}"
            )
            return None

        except ApiException as e:
            LOG.error(f"Error finding NVIDIA driver pod: {e}")
            return None

    def execute_nvidia_bug_report(self, pod: client.V1Pod) -> Optional[str]:
        """
        Execute nvidia-bug-report.sh in the driver pod.

        Args:
            pod: The NVIDIA driver pod

        Returns:
            Path to the generated log file within the pod, or None on error
        """
        pod_name = pod.metadata.name
        container_name = self._get_driver_container_name(pod)

        if not container_name:
            LOG.error(f"Could not find suitable container in pod {pod_name}")
            return None

        LOG.info(f"Executing nvidia-bug-report.sh in pod {pod_name}, container {container_name}")

        try:
            # Generate unique filename with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_filename = f"nvidia-bug-report-{self.node_name}-{timestamp}.log.gz"
            log_path = f"/tmp/{log_filename}"

            # Execute nvidia-bug-report.sh
            exec_command = [
                "/bin/bash",
                "-c",
                f"nvidia-bug-report.sh --output-file {log_path} && echo {log_path}"
            ]

            LOG.info(f"Running command: {' '.join(exec_command)}")

            resp = stream(
                self.k8s_api.connect_get_namespaced_pod_exec,
                pod_name,
                self.nvidia_namespace,
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

            if log_path in output or "Bug report generated" in output:
                LOG.info(f"nvidia-bug-report.sh completed successfully, log file: {log_path}")
                return log_path
            else:
                LOG.error(f"nvidia-bug-report.sh execution may have failed. Output: {output}")
                return None

        except ApiException as e:
            LOG.error(f"Error executing nvidia-bug-report.sh: {e}")
            return None
        except Exception as e:
            LOG.error(f"Unexpected error during nvidia-bug-report execution: {e}")
            return None

    def download_log_file(self, pod: client.V1Pod, remote_path: str) -> Optional[Path]:
        """
        Download the log file from the driver pod using tar.

        Args:
            pod: The NVIDIA driver pod
            remote_path: Path to the log file in the pod

        Returns:
            Path to the downloaded file, or None on error
        """
        pod_name = pod.metadata.name
        container_name = self._get_driver_container_name(pod)

        if not container_name:
            LOG.error(f"Could not find suitable container in pod {pod_name}")
            return None

        LOG.info(f"Downloading {remote_path} from pod {pod_name}")

        try:
            # Create tar of the file
            exec_command = ["tar", "cf", "-", remote_path]

            resp = stream(
                self.k8s_api.connect_get_namespaced_pod_exec,
                pod_name,
                self.nvidia_namespace,
                container=container_name,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False
            )

            # Read tar data
            tar_data = b""
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    tar_data += resp.read_stdout().encode('latin-1')
                if resp.peek_stderr():
                    stderr = resp.read_stderr()
                    if stderr:
                        LOG.warning(f"tar stderr: {stderr}")

            resp.close()

            if not tar_data:
                LOG.error(f"No data received when downloading {remote_path}")
                return None

            # Extract tar to output directory
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar") as tmp_tar:
                tmp_tar.write(tar_data)
                tmp_tar_path = tmp_tar.name

            try:
                with tarfile.open(tmp_tar_path, 'r') as tar:
                    # Extract only the specific file
                    member = tar.getmember(remote_path.lstrip('/'))
                    member.name = os.path.basename(remote_path)
                    tar.extract(member, path=self.output_dir)

                output_file = self.output_dir / os.path.basename(remote_path)
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
        except Exception as e:
            LOG.error(f"Unexpected error downloading log file: {e}")
            return None

    def cleanup_remote_log(self, pod: client.V1Pod, remote_path: str) -> bool:
        """
        Clean up the temporary log file from the driver pod.

        Args:
            pod: The NVIDIA driver pod
            remote_path: Path to the log file to remove

        Returns:
            True if cleanup successful, False otherwise
        """
        pod_name = pod.metadata.name
        container_name = self._get_driver_container_name(pod)

        if not container_name:
            return False

        LOG.info(f"Cleaning up {remote_path} from pod {pod_name}")

        try:
            exec_command = ["rm", "-f", remote_path]

            resp = stream(
                self.k8s_api.connect_get_namespaced_pod_exec,
                pod_name,
                self.nvidia_namespace,
                container=container_name,
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False
            )

            LOG.info(f"Successfully cleaned up {remote_path}")
            return True

        except Exception as e:
            LOG.warning(f"Error cleaning up remote log file (non-critical): {e}")
            return False

    def collect_logs(self) -> bool:
        """
        Main log collection workflow.

        Returns:
            True if logs collected successfully, False otherwise
        """
        LOG.info("Starting log collection cycle")

        # Find the NVIDIA driver pod
        driver_pod = self.find_nvidia_driver_pod()
        if not driver_pod:
            LOG.error("Cannot collect logs: NVIDIA driver pod not found")
            return False

        # Execute nvidia-bug-report.sh
        remote_log_path = self.execute_nvidia_bug_report(driver_pod)
        if not remote_log_path:
            LOG.error("Failed to generate nvidia-bug-report")
            return False

        # Download the log file
        local_log_path = self.download_log_file(driver_pod, remote_log_path)
        if not local_log_path:
            LOG.error("Failed to download nvidia-bug-report")
            return False

        # Cleanup remote log file
        self.cleanup_remote_log(driver_pod, remote_log_path)

        LOG.info(f"Log collection completed successfully: {local_log_path}")
        return True

    def _get_driver_container_name(self, pod: client.V1Pod) -> Optional[str]:
        """
        Get the name of the driver container in the pod.

        Args:
            pod: The NVIDIA driver pod

        Returns:
            Container name, or None if not found
        """
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
        LOG.info(f"NVIDIA Log Collector started on node: {self.node_name}")
        LOG.info(f"Output directory: {self.output_dir}")
        LOG.info(f"Run once mode: {RUN_ONCE}")
        LOG.info(f"Collection interval: {COLLECTION_INTERVAL}s")

        if RUN_ONCE:
            # Run once and exit
            success = self.collect_logs()
            sys.exit(0 if success else 1)
        else:
            # Run continuously
            while True:
                try:
                    self.collect_logs()
                except Exception as e:
                    LOG.error(f"Error during log collection: {e}", exc_info=True)

                LOG.info(f"Sleeping for {COLLECTION_INTERVAL} seconds until next collection")
                time.sleep(COLLECTION_INTERVAL)


def main():
    """Entry point."""
    try:
        collector = NvidiaLogCollector()
        collector.run()
    except KeyboardInterrupt:
        LOG.info("Received interrupt signal, shutting down")
        sys.exit(0)
    except Exception as e:
        LOG.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()