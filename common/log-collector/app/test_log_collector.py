#!/usr/bin/env python3
"""
Unit tests for Unified Log Collector (K8s and VM)
"""

import unittest
from unittest.mock import Mock, patch, MagicMock, mock_open
import tempfile
import os
from pathlib import Path

# Set minimal required environment variables before importing the module
# Set KUBERNETES_SERVICE_HOST to ensure K8s mode is detected
os.environ['KUBERNETES_SERVICE_HOST'] = 'kubernetes.default.svc'
os.environ['NODE_NAME'] = 'test-node'
os.environ['LOG_OUTPUT_DIR'] = tempfile.mkdtemp()

from log_collector import LogCollector


class TestLogCollector(unittest.TestCase):
    """Test cases for LogCollector class."""

    def setUp(self):
        """Set up test fixtures."""
        self.output_dir = tempfile.mkdtemp()
        # Store original environment variables
        self.original_env = {
            'NODE_NAME': os.environ.get('NODE_NAME'),
            'LOG_OUTPUT_DIR': os.environ.get('LOG_OUTPUT_DIR'),
            'GPU_TYPE': os.environ.get('GPU_TYPE'),
            'DRIVER_NAMESPACE': os.environ.get('DRIVER_NAMESPACE'),
            'VM_ID': os.environ.get('VM_ID'),
            'API_ENABLED': os.environ.get('API_ENABLED'),
        }

        # Set test environment
        os.environ['NODE_NAME'] = 'test-node'
        os.environ['LOG_OUTPUT_DIR'] = self.output_dir
        os.environ['GPU_TYPE'] = 'nvidia'
        os.environ['DRIVER_NAMESPACE'] = 'nvidia-gpu-operator'

    def tearDown(self):
        """Clean up after tests."""
        # Restore original environment variables
        for key, value in self.original_env.items():
            if value is None:
                if key in os.environ:
                    del os.environ[key]
            else:
                os.environ[key] = value

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_initialization(self, mock_core_api, mock_load_config):
        """Test collector initialization."""
        collector = LogCollector()

        self.assertEqual(collector.node_name, 'test-node')
        self.assertEqual(collector.gpu_type, 'nvidia')
        self.assertEqual(collector.driver_namespace, 'nvidia-gpu-operator')
        self.assertTrue(collector.output_dir.exists())
        mock_load_config.assert_called_once()

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_initialization_with_vm_id(self, mock_core_api, mock_load_config):
        """Test collector initialization with VM_ID."""
        os.environ['VM_ID'] = 'test-vm-123'
        collector = LogCollector()

        self.assertEqual(collector.vm_id, 'test-vm-123')

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_find_nvidia_driver_pod_by_label(self, mock_core_api, mock_load_config):
        """Test finding NVIDIA driver pod by label selector (primary method)."""
        collector = LogCollector()

        # Mock pod found by label selector
        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-driver-daemonset-abc123'
        mock_pod.status.phase = 'Running'

        mock_pod_list = Mock()
        mock_pod_list.items = [mock_pod]

        collector.k8s_api.list_namespaced_pod = Mock(return_value=mock_pod_list)

        result = collector.find_nvidia_driver_pod()

        self.assertIsNotNone(result)
        self.assertEqual(result.metadata.name, 'nvidia-driver-daemonset-abc123')
        # Verify only one call was made (found by label, no fallback needed)
        self.assertEqual(collector.k8s_api.list_namespaced_pod.call_count, 1)
        # Verify label selector was used
        call_kwargs = collector.k8s_api.list_namespaced_pod.call_args[1]
        self.assertEqual(call_kwargs['label_selector'], 'app.kubernetes.io/component=nvidia-driver')

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_find_nvidia_driver_pod_not_found(self, mock_core_api, mock_load_config):
        """Test when NVIDIA driver pod is not found by label or prefix."""
        collector = LogCollector()

        mock_pod_list = Mock()
        mock_pod_list.items = []

        # Both label selector and prefix fallback return empty
        collector.k8s_api.list_namespaced_pod = Mock(return_value=mock_pod_list)

        result = collector.find_nvidia_driver_pod()

        self.assertIsNone(result)
        # Verify list_namespaced_pod was called twice (label lookup + prefix fallback)
        self.assertEqual(collector.k8s_api.list_namespaced_pod.call_count, 2)

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_find_nvidia_driver_pod_prefix_fallback(self, mock_core_api, mock_load_config):
        """Test finding NVIDIA driver pod via name prefix fallback."""
        collector = LogCollector()

        # First call (label selector) returns empty
        mock_empty_list = Mock()
        mock_empty_list.items = []

        # Second call (prefix fallback) returns the pod
        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-ubuntu22.04-abc123'
        mock_pod.status.phase = 'Running'

        mock_pod_list_with_pod = Mock()
        mock_pod_list_with_pod.items = [mock_pod]

        collector.k8s_api.list_namespaced_pod = Mock(
            side_effect=[mock_empty_list, mock_pod_list_with_pod]
        )

        result = collector.find_nvidia_driver_pod()

        self.assertIsNotNone(result)
        self.assertEqual(result.metadata.name, 'nvidia-gpu-driver-ubuntu22.04-abc123')
        # Verify list_namespaced_pod was called twice
        self.assertEqual(collector.k8s_api.list_namespaced_pod.call_count, 2)
        # Verify first call used label selector
        first_call_kwargs = collector.k8s_api.list_namespaced_pod.call_args_list[0][1]
        self.assertEqual(first_call_kwargs['label_selector'], 'app.kubernetes.io/component=nvidia-driver')

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_get_driver_container_name(self, mock_core_api, mock_load_config):
        """Test getting driver container name."""
        collector = LogCollector()

        # Mock pod with multiple containers
        mock_container1 = Mock()
        mock_container1.name = 'toolkit'

        mock_container2 = Mock()
        mock_container2.name = 'nvidia-driver-ctr'

        mock_pod = Mock()
        mock_pod.spec.containers = [mock_container1, mock_container2]

        result = collector._get_driver_container_name(mock_pod)

        self.assertEqual(result, 'nvidia-driver-ctr')

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_get_driver_container_name_fallback(self, mock_core_api, mock_load_config):
        """Test getting driver container name with fallback."""
        collector = LogCollector()

        # Mock pod with no 'driver' in container names
        mock_container = Mock()
        mock_container.name = 'main-container'

        mock_pod = Mock()
        mock_pod.spec.containers = [mock_container]

        result = collector._get_driver_container_name(mock_pod)

        self.assertEqual(result, 'main-container')

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_cleanup_old_logs(self, mock_core_api, mock_load_config):
        """Test cleanup of old log files."""
        collector = LogCollector()

        # Create some fake log files
        for i in range(10):
            log_file = collector.output_dir / f"nvidia-bug-report-node-{i}.log.gz"
            log_file.touch()

        # Run cleanup (should keep only 1 newest based on MAX_LOGS_TO_KEEP=1)
        collector.cleanup_old_logs()

        # Count remaining files
        remaining_files = list(collector.output_dir.glob("nvidia-bug-report-*.log.gz"))
        self.assertEqual(len(remaining_files), 1)


class TestAPIMode(unittest.TestCase):
    """Test API-driven mode functionality."""

    def setUp(self):
        """Set up test fixtures for API tests."""
        self.output_dir = tempfile.mkdtemp()
        os.environ['NODE_NAME'] = 'test-node'
        os.environ['LOG_OUTPUT_DIR'] = self.output_dir
        os.environ['VM_ID'] = 'test-vm-123'
        os.environ['API_ENABLED'] = 'true'
        os.environ['API_BASE_URL'] = 'https://test-api.com'

    def tearDown(self):
        """Clean up after tests."""
        for key in ['NODE_NAME', 'LOG_OUTPUT_DIR', 'VM_ID', 'API_ENABLED', 'API_BASE_URL']:
            if key in os.environ:
                del os.environ[key]

    @patch('log_collector.requests.get')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_check_for_tasks_success(self, mock_core_api, mock_load_config, mock_get):
        """Test checking for tasks successfully."""
        collector = LogCollector()

        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'status': 'success',
            'event_id': 'evt-12345'
        }
        mock_get.return_value = mock_response

        result = collector.check_for_tasks()

        self.assertIsNotNone(result)
        self.assertEqual(result['event_id'], 'evt-12345')
        mock_get.assert_called_once()

    @patch('log_collector.requests.get')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_check_for_tasks_no_tasks(self, mock_core_api, mock_load_config, mock_get):
        """Test checking for tasks when none available."""
        collector = LogCollector()

        # Mock API response with no tasks
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = collector.check_for_tasks()

        self.assertIsNone(result)

    @patch('log_collector.requests.post')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_report_result_success(self, mock_core_api, mock_load_config, mock_post):
        """Test reporting successful collection result with file upload."""
        collector = LogCollector()

        # Create a fake log file
        log_file = Path(self.output_dir) / "test-log.log.gz"
        log_file.write_text("test log content")

        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = collector.report_result('evt-123', 'success', log_file=log_file)

        self.assertTrue(result)
        mock_post.assert_called_once()
        # Verify it was called with files (multipart)
        call_kwargs = mock_post.call_args[1]
        self.assertIn('files', call_kwargs)
        self.assertIn('data', call_kwargs)

    @patch('log_collector.requests.post')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_report_result_failure(self, mock_core_api, mock_load_config, mock_post):
        """Test reporting failed collection result."""
        collector = LogCollector()

        # Mock successful API response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        result = collector.report_result('evt-123', 'failed', message='Collection timeout')

        self.assertTrue(result)
        mock_post.assert_called_once()
        # Verify it was called with json (not multipart)
        call_kwargs = mock_post.call_args[1]
        self.assertIn('json', call_kwargs)
        self.assertNotIn('files', call_kwargs)


class TestEnvironmentVariables(unittest.TestCase):
    """Test environment variable handling."""

    def setUp(self):
        """Store original environment variables."""
        self.original_node_name = os.environ.get('NODE_NAME')
        self.original_namespace = os.environ.get('DRIVER_NAMESPACE')

    def tearDown(self):
        """Restore original environment variables."""
        if self.original_node_name:
            os.environ['NODE_NAME'] = self.original_node_name
        elif 'NODE_NAME' in os.environ:
            del os.environ['NODE_NAME']

        if self.original_namespace:
            os.environ['DRIVER_NAMESPACE'] = self.original_namespace
        elif 'DRIVER_NAMESPACE' in os.environ:
            del os.environ['DRIVER_NAMESPACE']

    def test_missing_node_name(self):
        """Test that missing NODE_NAME raises error in K8s mode."""
        # Remove NODE_NAME
        if 'NODE_NAME' in os.environ:
            del os.environ['NODE_NAME']

        # Ensure KUBERNETES_SERVICE_HOST is set for K8s mode
        os.environ['KUBERNETES_SERVICE_HOST'] = 'kubernetes.default.svc'

        with patch('log_collector.config.load_incluster_config'):
            with patch('log_collector.client.CoreV1Api'):
                # Patch the module-level NODE_NAME constant to be None
                with patch('log_collector.NODE_NAME', None):
                    with self.assertRaises(RuntimeError):
                        LogCollector()

    def test_custom_driver_namespace(self):
        """Test custom driver namespace."""
        os.environ['NODE_NAME'] = 'test-node'
        os.environ['GPU_TYPE'] = 'nvidia'

        with patch('log_collector.config.load_incluster_config'):
            with patch('log_collector.client.CoreV1Api'):
                # Patch the module-level constant since it's evaluated at import time
                with patch('log_collector.DRIVER_NAMESPACE', 'custom-gpu-namespace'):
                    collector = LogCollector()
                    self.assertEqual(collector.driver_namespace, 'custom-gpu-namespace')


class TestCollectionWorkflow(unittest.TestCase):
    """Test the complete collection workflow."""

    def setUp(self):
        """Set up test environment."""
        self.output_dir = tempfile.mkdtemp()
        os.environ['NODE_NAME'] = 'test-node'
        os.environ['LOG_OUTPUT_DIR'] = self.output_dir

    def tearDown(self):
        """Clean up test environment."""
        if 'NODE_NAME' in os.environ:
            del os.environ['NODE_NAME']
        if 'LOG_OUTPUT_DIR' in os.environ:
            del os.environ['LOG_OUTPUT_DIR']

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_collect_logs_without_event_id(self, mock_core_api, mock_load_config):
        """Test log collection without event_id (scheduled mode)."""
        collector = LogCollector()

        # Mock GPU Operator mode (not bundled)
        collector._is_bundled_driver_mode = Mock(return_value=False)

        # Mock finding driver pod
        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-test'
        collector.find_nvidia_driver_pod = Mock(return_value=mock_pod)

        # Mock execute nvidia-bug-report
        collector.execute_nvidia_bug_report = Mock(return_value='/tmp/test-log.log.gz')

        # Mock download
        test_log = Path(self.output_dir) / 'test-log.log.gz'
        test_log.write_text('test')
        collector.download_log_file = Mock(return_value=test_log)

        # Mock cleanup
        collector.cleanup_remote_log = Mock(return_value=True)
        collector.cleanup_old_logs = Mock()

        log_path, error_msg = collector.collect_logs()

        self.assertIsNotNone(log_path)
        self.assertEqual(log_path, test_log)
        self.assertEqual(error_msg, "")
        # Verify execute was called with event_id=None
        collector.execute_nvidia_bug_report.assert_called_once_with(mock_pod, None)

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_collect_logs_with_event_id(self, mock_core_api, mock_load_config):
        """Test log collection with event_id (API mode)."""
        collector = LogCollector()

        # Mock GPU Operator mode (not bundled)
        collector._is_bundled_driver_mode = Mock(return_value=False)

        # Mock finding driver pod
        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-test'
        collector.find_nvidia_driver_pod = Mock(return_value=mock_pod)

        # Mock execute nvidia-bug-report
        collector.execute_nvidia_bug_report = Mock(return_value='/tmp/test-log-evt123.log.gz')

        # Mock download
        test_log = Path(self.output_dir) / 'test-log-evt123.log.gz'
        test_log.write_text('test')
        collector.download_log_file = Mock(return_value=test_log)

        # Mock cleanup
        collector.cleanup_remote_log = Mock(return_value=True)
        collector.cleanup_old_logs = Mock()

        log_path, error_msg = collector.collect_logs(event_id='evt-123')

        self.assertIsNotNone(log_path)
        self.assertEqual(log_path, test_log)
        self.assertEqual(error_msg, "")
        # Verify execute was called with event_id
        collector.execute_nvidia_bug_report.assert_called_once_with(mock_pod, 'evt-123')

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_collect_logs_no_driver_pod(self, mock_core_api, mock_load_config):
        """Test log collection when driver pod not found returns specific error."""
        collector = LogCollector()
        collector._is_bundled_driver_mode = Mock(return_value=False)
        collector.find_nvidia_driver_pod = Mock(return_value=None)
        collector.cleanup_old_logs = Mock()

        log_path, error_msg = collector.collect_logs()

        self.assertIsNone(log_path)
        self.assertIn("NVIDIA driver pod not found", error_msg)
        self.assertIn(collector.node_name, error_msg)

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_collect_logs_execute_fails(self, mock_core_api, mock_load_config):
        """Test log collection when nvidia-bug-report execution fails."""
        collector = LogCollector()
        collector._is_bundled_driver_mode = Mock(return_value=False)

        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-test'
        collector.find_nvidia_driver_pod = Mock(return_value=mock_pod)
        collector.execute_nvidia_bug_report = Mock(return_value=None)
        collector.cleanup_old_logs = Mock()

        log_path, error_msg = collector.collect_logs()

        self.assertIsNone(log_path)
        self.assertIn("Failed to execute nvidia-bug-report.sh", error_msg)
        self.assertIn(mock_pod.metadata.name, error_msg)

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_collect_logs_download_fails(self, mock_core_api, mock_load_config):
        """Test log collection when download fails."""
        collector = LogCollector()
        collector._is_bundled_driver_mode = Mock(return_value=False)

        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-test'
        collector.find_nvidia_driver_pod = Mock(return_value=mock_pod)
        collector.execute_nvidia_bug_report = Mock(return_value='/tmp/test-log.log.gz')
        collector.download_log_file = Mock(return_value=None)
        collector.cleanup_old_logs = Mock()

        log_path, error_msg = collector.collect_logs()

        self.assertIsNone(log_path)
        self.assertIn("Failed to download log file", error_msg)
        self.assertIn(mock_pod.metadata.name, error_msg)


if __name__ == '__main__':
    unittest.main()