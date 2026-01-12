#!/usr/bin/env python3
"""
Unit tests for NVIDIA Log Collector
"""

import unittest
from unittest.mock import Mock, patch, MagicMock, mock_open
import tempfile
import os
from pathlib import Path

# Set minimal required environment variables before importing the module
os.environ['NODE_NAME'] = 'test-node'
os.environ['LOG_OUTPUT_DIR'] = tempfile.mkdtemp()

from log_collector_app import NvidiaLogCollector


class TestNvidiaLogCollector(unittest.TestCase):
    """Test cases for NvidiaLogCollector class."""

    def setUp(self):
        """Set up test fixtures."""
        self.output_dir = tempfile.mkdtemp()
        # Store original environment variables
        self.original_env = {
            'NODE_NAME': os.environ.get('NODE_NAME'),
            'LOG_OUTPUT_DIR': os.environ.get('LOG_OUTPUT_DIR'),
            'NVIDIA_NAMESPACE': os.environ.get('NVIDIA_NAMESPACE'),
            'VM_ID': os.environ.get('VM_ID'),
            'API_ENABLED': os.environ.get('API_ENABLED'),
        }

        # Set test environment
        os.environ['NODE_NAME'] = 'test-node'
        os.environ['LOG_OUTPUT_DIR'] = self.output_dir
        os.environ['NVIDIA_NAMESPACE'] = 'nvidia-gpu-operator'

    def tearDown(self):
        """Clean up after tests."""
        # Restore original environment variables
        for key, value in self.original_env.items():
            if value is None:
                if key in os.environ:
                    del os.environ[key]
            else:
                os.environ[key] = value

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_initialization(self, mock_core_api, mock_load_config):
        """Test collector initialization."""
        collector = NvidiaLogCollector()

        self.assertEqual(collector.node_name, 'test-node')
        self.assertEqual(collector.nvidia_namespace, 'nvidia-gpu-operator')
        self.assertTrue(collector.output_dir.exists())
        mock_load_config.assert_called_once()

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_initialization_with_vm_id(self, mock_core_api, mock_load_config):
        """Test collector initialization with VM_ID."""
        os.environ['VM_ID'] = 'test-vm-123'
        collector = NvidiaLogCollector()

        self.assertEqual(collector.vm_id, 'test-vm-123')

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_find_nvidia_driver_pod_success(self, mock_core_api, mock_load_config):
        """Test finding NVIDIA driver pod."""
        collector = NvidiaLogCollector()

        # Mock pod
        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-ubuntu22.04-abc123'
        mock_pod.status.phase = 'Running'

        mock_pod_list = Mock()
        mock_pod_list.items = [mock_pod]

        collector.k8s_api.list_namespaced_pod = Mock(return_value=mock_pod_list)

        result = collector.find_nvidia_driver_pod()

        self.assertIsNotNone(result)
        self.assertEqual(result.metadata.name, 'nvidia-gpu-driver-ubuntu22.04-abc123')

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_find_nvidia_driver_pod_not_found(self, mock_core_api, mock_load_config):
        """Test when NVIDIA driver pod is not found."""
        collector = NvidiaLogCollector()

        mock_pod_list = Mock()
        mock_pod_list.items = []

        collector.k8s_api.list_namespaced_pod = Mock(return_value=mock_pod_list)

        result = collector.find_nvidia_driver_pod()

        self.assertIsNone(result)

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_get_driver_container_name(self, mock_core_api, mock_load_config):
        """Test getting driver container name."""
        collector = NvidiaLogCollector()

        # Mock pod with multiple containers
        mock_container1 = Mock()
        mock_container1.name = 'toolkit'

        mock_container2 = Mock()
        mock_container2.name = 'nvidia-driver-ctr'

        mock_pod = Mock()
        mock_pod.spec.containers = [mock_container1, mock_container2]

        result = collector._get_driver_container_name(mock_pod)

        self.assertEqual(result, 'nvidia-driver-ctr')

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_get_driver_container_name_fallback(self, mock_core_api, mock_load_config):
        """Test getting driver container name with fallback."""
        collector = NvidiaLogCollector()

        # Mock pod with no 'driver' in container names
        mock_container = Mock()
        mock_container.name = 'main-container'

        mock_pod = Mock()
        mock_pod.spec.containers = [mock_container]

        result = collector._get_driver_container_name(mock_pod)

        self.assertEqual(result, 'main-container')

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_cleanup_old_logs(self, mock_core_api, mock_load_config):
        """Test cleanup of old log files."""
        collector = NvidiaLogCollector()

        # Create some fake log files
        for i in range(10):
            log_file = collector.output_dir / f"nvidia-bug-report-node-{i}.log.gz"
            log_file.touch()

        # Run cleanup (should keep only 5 newest)
        collector.cleanup_old_logs()

        # Count remaining files
        remaining_files = list(collector.output_dir.glob("nvidia-bug-report-*.log.gz"))
        self.assertEqual(len(remaining_files), 5)


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

    @patch('log_collector_app.requests.get')
    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_check_for_tasks_success(self, mock_core_api, mock_load_config, mock_get):
        """Test checking for tasks successfully."""
        collector = NvidiaLogCollector()

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

    @patch('log_collector_app.requests.get')
    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_check_for_tasks_no_tasks(self, mock_core_api, mock_load_config, mock_get):
        """Test checking for tasks when none available."""
        collector = NvidiaLogCollector()

        # Mock API response with no tasks
        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        result = collector.check_for_tasks()

        self.assertIsNone(result)

    @patch('log_collector_app.requests.post')
    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_report_result_success(self, mock_core_api, mock_load_config, mock_post):
        """Test reporting successful collection result with file upload."""
        collector = NvidiaLogCollector()

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

    @patch('log_collector_app.requests.post')
    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_report_result_failure(self, mock_core_api, mock_load_config, mock_post):
        """Test reporting failed collection result."""
        collector = NvidiaLogCollector()

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
        self.original_namespace = os.environ.get('NVIDIA_NAMESPACE')

    def tearDown(self):
        """Restore original environment variables."""
        if self.original_node_name:
            os.environ['NODE_NAME'] = self.original_node_name
        elif 'NODE_NAME' in os.environ:
            del os.environ['NODE_NAME']

        if self.original_namespace:
            os.environ['NVIDIA_NAMESPACE'] = self.original_namespace
        elif 'NVIDIA_NAMESPACE' in os.environ:
            del os.environ['NVIDIA_NAMESPACE']

    def test_missing_node_name(self):
        """Test that missing NODE_NAME raises error."""
        # Remove NODE_NAME
        if 'NODE_NAME' in os.environ:
            del os.environ['NODE_NAME']

        with patch('log_collector_app.config.load_incluster_config'):
            with patch('log_collector_app.client.CoreV1Api'):
                with self.assertRaises(RuntimeError):
                    NvidiaLogCollector()

    def test_custom_nvidia_namespace(self):
        """Test custom NVIDIA namespace."""
        os.environ['NVIDIA_NAMESPACE'] = 'custom-gpu-namespace'
        os.environ['NODE_NAME'] = 'test-node'

        with patch('log_collector_app.config.load_incluster_config'):
            with patch('log_collector_app.client.CoreV1Api'):
                collector = NvidiaLogCollector()
                self.assertEqual(collector.nvidia_namespace, 'custom-gpu-namespace')


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

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_collect_logs_without_event_id(self, mock_core_api, mock_load_config):
        """Test log collection without event_id (scheduled mode)."""
        collector = NvidiaLogCollector()

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

        result = collector.collect_logs()

        self.assertIsNotNone(result)
        self.assertEqual(result, test_log)
        # Verify execute was called with event_id=None
        collector.execute_nvidia_bug_report.assert_called_once_with(mock_pod, None)

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_collect_logs_with_event_id(self, mock_core_api, mock_load_config):
        """Test log collection with event_id (API mode)."""
        collector = NvidiaLogCollector()

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

        result = collector.collect_logs(event_id='evt-123')

        self.assertIsNotNone(result)
        self.assertEqual(result, test_log)
        # Verify execute was called with event_id
        collector.execute_nvidia_bug_report.assert_called_once_with(mock_pod, 'evt-123')

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_collect_logs_no_driver_pod(self, mock_core_api, mock_load_config):
        """Test log collection when driver pod not found."""
        collector = NvidiaLogCollector()
        collector.find_nvidia_driver_pod = Mock(return_value=None)
        collector.cleanup_old_logs = Mock()

        result = collector.collect_logs()

        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()