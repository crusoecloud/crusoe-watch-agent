#!/usr/bin/env python3
"""
NVIDIA GPU Driver Log Collector for VMs

This application collects nvidia-bug-report logs from VMs by executing
nvidia-bug-report.sh locally (bundled in container via nvidia-utils).

Features:
- Polls API for on-demand log collection tasks
- Executes nvidia-bug-report.sh locally
- Uploads collected logs to monitoring backend
- Manages disk space by keeping only recent logs
"""

import os
import sys
import time
import subprocess
import socket
import logging
import json
import requests
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime

# Version for VM log collector
VERSION = "1.0.0"

# Configuration from environment variables
LOG_OUTPUT_DIR = os.environ.get("LOG_OUTPUT_DIR", "/logs")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
MAX_LOGS_TO_KEEP = int(os.environ.get("MAX_LOGS_TO_KEEP", "1"))

# API configuration
API_BASE_URL = os.environ.get("API_BASE_URL", "https://cms-monitoring.crusoecloud.com")
API_POLL_INTERVAL = int(os.environ.get("API_POLL_INTERVAL", "60"))
API_ENABLED = os.environ.get("API_ENABLED", "false").lower() == "true"
COLLECTION_TIMEOUT = int(os.environ.get("COLLECTION_TIMEOUT", "300"))
CRUSOE_MONITORING_TOKEN = os.environ.get("CRUSOE_MONITORING_TOKEN")


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging - efficient and no parsing needed."""

    def format(self, record):
        log_data = {
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
            "level": record.levelname.lower(),
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


# Logging setup with JSON formatter
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    handlers=[handler]
)
LOG = logging.getLogger(__name__)


class VmNvidiaLogCollector:
    """
    VM-specific NVIDIA log collector.

    Features:
    - VM ID detection via dmidecode
    - Hostname-based identification
    - API-driven on-demand collection
    - Local nvidia-bug-report.sh execution
    """

    def __init__(self):
        """Initialize the VM log collector."""
        # Get hostname
        self.hostname = socket.gethostname()

        # Get VM_ID from environment or read from DMI
        self.vm_id = os.environ.get("VM_ID")
        if not self.vm_id:
            self.vm_id = self._read_vm_id_from_dmi()

        # Get output directory
        self.output_dir = Path(os.environ.get("LOG_OUTPUT_DIR", LOG_OUTPUT_DIR))
        self.output_dir.mkdir(parents=True, exist_ok=True)

        LOG.info(f"Initialized log collector for VM: {self.hostname}")
        if self.vm_id:
            LOG.info(f"VM ID: {self.vm_id}")

        # Warn if API mode is enabled but token is missing
        if API_ENABLED and not CRUSOE_MONITORING_TOKEN:
            LOG.warning("API_ENABLED is true but CRUSOE_MONITORING_TOKEN is not set. API calls may fail authentication.")

    def _read_vm_id_from_dmi(self) -> Optional[str]:
        """
        Read VM ID from DMI using dmidecode.

        Returns:
            VM ID string if available, None otherwise
        """
        try:
            result = subprocess.run(
                ["dmidecode", "-s", "system-uuid"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
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
        if CRUSOE_MONITORING_TOKEN:
            headers['Authorization'] = f'Bearer {CRUSOE_MONITORING_TOKEN}'
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
                        'node_name': self.hostname,
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
                    'node_name': self.hostname
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

    def execute_nvidia_bug_report_local(self, event_id: Optional[str] = None) -> Optional[Path]:
        """
        Execute nvidia-bug-report.sh locally (bundled in container).

        Args:
            event_id: Optional event ID to include in filename

        Returns:
            Path to generated log file, or None on error
        """
        LOG.info("Executing nvidia-bug-report.sh locally (bundled driver mode)")

        if not Path("/usr/bin/nvidia-bug-report.sh").exists():
            LOG.error("nvidia-bug-report.sh not found at /usr/bin/nvidia-bug-report.sh")
            return None

        try:
            # Generate unique filename with timestamp and optional event_id
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if event_id:
                log_filename_base = f"nvidia-bug-report-{self.hostname}-{event_id}-{timestamp}.log"
            else:
                log_filename_base = f"nvidia-bug-report-{self.hostname}-{timestamp}.log"

            # Output directly to /logs
            log_path_base = self.output_dir / log_filename_base
            actual_log_path = Path(f"{log_path_base}.gz")

            # Execute nvidia-bug-report.sh bundled in the container
            cmd = ["/usr/bin/nvidia-bug-report.sh", "--output-file", str(log_path_base)]

            LOG.info(f"Running command: {' '.join(cmd)}")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=COLLECTION_TIMEOUT
            )

            if result.returncode == 0 and actual_log_path.exists():
                LOG.info(f"nvidia-bug-report.sh completed successfully: {actual_log_path}")
                return actual_log_path
            else:
                LOG.error(f"nvidia-bug-report.sh failed with return code {result.returncode}")
                LOG.error(f"stdout: {result.stdout}")
                LOG.error(f"stderr: {result.stderr}")
                return None

        except subprocess.TimeoutExpired:
            LOG.error(f"nvidia-bug-report.sh timed out after {COLLECTION_TIMEOUT}s")
            return None
        except Exception as e:
            LOG.error(f"Error executing nvidia-bug-report.sh locally: {e}")
            return None

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
            self._cleanup_logs_by_pattern("nvidia-bug-report-*.log.gz", "compressed")
            self._cleanup_logs_by_pattern("nvidia-bug-report-*.log", "unzipped")
        except Exception as e:
            LOG.warning(f"Error during log cleanup (non-critical): {e}")

    def collect_logs_with_timeout(self, event_id: str) -> tuple[bool, Optional[Path], str]:
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
                log_path = self.collect_logs(event_id)
                result["log_path"] = log_path
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
            return False, None, "Collection failed without specific error"

    def collect_logs(self, event_id: Optional[str] = None) -> Optional[Path]:
        """
        VM-specific log collection workflow

        Args:
            event_id: Optional event ID to include in filename and for API tracking

        Returns:
            Path to collected log file if successful, None otherwise
        """
        LOG.info(f"Starting log collection for VM {self.vm_id}{f' for event {event_id}' if event_id else ''}")

        # Clean up old logs to prevent disk space issues
        self.cleanup_old_logs()

        # Execute nvidia-bug-report.sh locally
        local_log_path = self.execute_nvidia_bug_report_local(event_id)

        if not local_log_path:
            LOG.error("Failed to execute nvidia-bug-report.sh locally")
            return None

        LOG.info(f"Log collection completed successfully: {local_log_path} ({local_log_path.stat().st_size / (1024*1024):.2f} MB)")
        return local_log_path

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

    def run(self):
        """Main execution loop."""
        LOG.info(f"NVIDIA Log Collector v{VERSION} started on VM: {self.hostname}")
        LOG.info(f"VM ID: {self.vm_id}")
        LOG.info(f"Output directory: {self.output_dir}")
        LOG.info(f"API-driven mode: {API_ENABLED}")

        if not API_ENABLED:
            LOG.error("API_ENABLED must be true for VM log collector")
            sys.exit(1)

        # VM log collector only supports API-driven mode
        self._run_api_mode()


def main():
    """Entry point."""
    try:
        collector = VmNvidiaLogCollector()
        collector.run()
    except KeyboardInterrupt:
        LOG.info("Received interrupt signal, shutting down")
        sys.exit(0)
    except Exception as e:
        LOG.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
