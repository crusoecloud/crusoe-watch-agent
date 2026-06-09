#!/usr/bin/env python3
"""
Unit tests for Unified Log Collector (K8s and VM)
"""

import unittest
from unittest.mock import Mock, patch, MagicMock, mock_open
import tempfile
import os
import base64
import io
import itertools
import tarfile
from pathlib import Path

# Set minimal required environment variables before importing the module
# Set KUBERNETES_SERVICE_HOST to ensure K8s mode is detected
os.environ['KUBERNETES_SERVICE_HOST'] = 'kubernetes.default.svc'
os.environ['NODE_NAME'] = 'test-node'
os.environ['LOG_OUTPUT_DIR'] = tempfile.mkdtemp()

import log_collector
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
        collector.execute_nvidia_bug_report = Mock(return_value=('/tmp/test-log.log.gz', None, None))

        # Mock download
        test_log = Path(self.output_dir) / 'test-log.log.gz'
        test_log.write_text('test')
        collector.download_log_file = Mock(return_value=(test_log, None, None))

        # Mock cleanup
        collector.cleanup_remote_log = Mock(return_value=True)
        collector.cleanup_old_logs = Mock()

        log_path, error_code, error_msg = collector.collect_logs()

        self.assertIsNotNone(log_path)
        self.assertEqual(log_path, test_log)
        self.assertIsNone(error_code)
        self.assertIsNone(error_msg)
        # Verify execute was called with event_id=None
        collector.execute_nvidia_bug_report.assert_called_once_with(mock_pod, None)
        collector.cleanup_remote_log.assert_called_once_with(mock_pod, '/tmp/test-log.log.gz')

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
        collector.execute_nvidia_bug_report = Mock(return_value=('/tmp/test-log-evt123.log.gz', None, None))

        # Mock download
        test_log = Path(self.output_dir) / 'test-log-evt123.log.gz'
        test_log.write_text('test')
        collector.download_log_file = Mock(return_value=(test_log, None, None))

        # Mock cleanup
        collector.cleanup_remote_log = Mock(return_value=True)
        collector.cleanup_old_logs = Mock()

        log_path, error_code, error_msg = collector.collect_logs(event_id='evt-123')

        self.assertIsNotNone(log_path)
        self.assertEqual(log_path, test_log)
        self.assertIsNone(error_code)
        self.assertIsNone(error_msg)
        # Verify execute was called with event_id
        collector.execute_nvidia_bug_report.assert_called_once_with(mock_pod, 'evt-123')
        collector.cleanup_remote_log.assert_called_once_with(mock_pod, '/tmp/test-log-evt123.log.gz')

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_collect_logs_no_driver_pod(self, mock_core_api, mock_load_config):
        """Test log collection when driver pod not found returns specific error."""
        collector = LogCollector()
        collector._is_bundled_driver_mode = Mock(return_value=False)
        collector.find_nvidia_driver_pod = Mock(return_value=None)
        collector.cleanup_old_logs = Mock()

        log_path, error_code, error_msg = collector.collect_logs()

        self.assertIsNone(log_path)
        self.assertEqual(error_code, 'CWA-BR-5004')
        self.assertIn("NVIDIA driver pod not found", error_msg)

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_collect_logs_execute_fails(self, mock_core_api, mock_load_config):
        """Test log collection when nvidia-bug-report execution fails."""
        collector = LogCollector()
        collector._is_bundled_driver_mode = Mock(return_value=False)

        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-test'
        collector.find_nvidia_driver_pod = Mock(return_value=mock_pod)
        collector.execute_nvidia_bug_report = Mock(return_value=(None, 'CWA-BR-5005', 'Error executing bug report script'))
        collector.cleanup_old_logs = Mock()

        log_path, error_code, error_msg = collector.collect_logs()

        self.assertIsNone(log_path)
        self.assertEqual(error_code, 'CWA-BR-5005')
        self.assertIn("Error executing bug report script", error_msg)

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_collect_logs_download_fails(self, mock_core_api, mock_load_config):
        """Test log collection when download fails."""
        collector = LogCollector()
        collector._is_bundled_driver_mode = Mock(return_value=False)

        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-test'
        collector.find_nvidia_driver_pod = Mock(return_value=mock_pod)
        collector.execute_nvidia_bug_report = Mock(return_value=('/tmp/test-log.log.gz', None, None))
        collector.download_log_file = Mock(return_value=(None, 'CWA-BR-5007', 'Unexpected error downloading bug report'))
        collector.cleanup_remote_log = Mock(return_value=True)
        collector.cleanup_old_logs = Mock()

        log_path, error_code, error_msg = collector.collect_logs()

        self.assertIsNone(log_path)
        self.assertEqual(error_code, 'CWA-BR-5007')
        self.assertIn("Unexpected error downloading bug report", error_msg)
        collector.cleanup_remote_log.assert_called_once_with(mock_pod, '/tmp/test-log.log.gz')


class FakeExecResponse:
    def __init__(self, stdout='', stderr=''):
        self.stdout = stdout
        self.stderr = stderr
        self.open = True

    def is_open(self):
        return self.open

    def update(self, timeout=1):
        self.open = False

    def peek_stdout(self):
        return bool(self.stdout)

    def read_stdout(self):
        output = self.stdout
        self.stdout = ''
        return output

    def peek_stderr(self):
        return bool(self.stderr)

    def read_stderr(self):
        output = self.stderr
        self.stderr = ''
        return output

    def close(self):
        self.open = False


class ChunkedFakeExecResponse:
    def __init__(self, stdout_chunks=None, stderr_chunks=None):
        self.stdout_chunks = list(stdout_chunks or [])
        self.stderr_chunks = list(stderr_chunks or [])
        self.current_stdout = ''
        self.current_stderr = ''
        self.open = True

    def is_open(self):
        return self.open

    def update(self, timeout=1):
        if self.stdout_chunks:
            self.current_stdout = self.stdout_chunks.pop(0)
        else:
            self.current_stdout = ''

        if self.stderr_chunks:
            self.current_stderr = self.stderr_chunks.pop(0)
        else:
            self.current_stderr = ''

        if not self.stdout_chunks and not self.stderr_chunks and not self.current_stdout and not self.current_stderr:
            self.open = False

    def peek_stdout(self):
        return bool(self.current_stdout)

    def read_stdout(self):
        output = self.current_stdout
        self.current_stdout = ''
        return output

    def peek_stderr(self):
        return bool(self.current_stderr)

    def read_stderr(self):
        output = self.current_stderr
        self.current_stderr = ''
        return output

    def close(self):
        self.open = False


class HangingFakeExecResponse:
    """Mimics an exec stream that never closes (a hung command / half-open websocket)."""
    def __init__(self):
        self.closed = False

    def is_open(self):
        return not self.closed

    def update(self, timeout=1):
        pass  # never yields data, never closes

    def peek_stdout(self):
        return False

    def peek_stderr(self):
        return False

    def close(self):
        self.closed = True


class TestDownloadLogFile(unittest.TestCase):
    def setUp(self):
        self.output_dir = tempfile.mkdtemp()
        os.environ['NODE_NAME'] = 'test-node'
        os.environ['LOG_OUTPUT_DIR'] = self.output_dir

    def tearDown(self):
        if 'NODE_NAME' in os.environ:
            del os.environ['NODE_NAME']
        if 'LOG_OUTPUT_DIR' in os.environ:
            del os.environ['LOG_OUTPUT_DIR']

    def _make_pod(self):
        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-test'
        mock_container = Mock()
        mock_container.name = 'nvidia-driver-ctr'
        mock_pod.spec.containers = [mock_container]
        return mock_pod

    def _make_encoded_tar(self, remote_file, file_data):
        tar_buffer = io.BytesIO()
        with tarfile.open(fileobj=tar_buffer, mode='w') as tar:
            tar_info = tarfile.TarInfo(remote_file)
            tar_info.size = len(file_data)
            tar.addfile(tar_info, io.BytesIO(file_data))

        return base64.b64encode(tar_buffer.getvalue()).decode()

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_ignores_stderr_when_decoding_base64(self, mock_core_api, mock_load_config, mock_stream):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        remote_file = 'nvidia-bug-report-test-node.log.gz'
        file_data = b'test log data'
        encoded_tar = self._make_encoded_tar(remote_file, file_data)
        mock_pod = self._make_pod()

        mock_stream.side_effect = [
            'EXISTS',
            FakeExecResponse(stdout=encoded_tar, stderr='tar: removing leading / from member names\n')
        ]

        log_path, error_code, error_msg = collector.download_log_file(mock_pod, f'/tmp/{remote_file}')

        self.assertEqual(log_path, Path(self.output_dir) / remote_file)
        self.assertIsNone(error_code)
        self.assertIsNone(error_msg)
        self.assertEqual(log_path.read_bytes(), file_data)

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_reassembles_multichunk_stdout(self, mock_core_api, mock_load_config, mock_stream):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        remote_file = 'nvidia-bug-report-test-node.log.gz'
        file_data = b'test log data' * 1024
        encoded_tar = self._make_encoded_tar(remote_file, file_data)
        chunks = [encoded_tar[i:i + 97] for i in range(0, len(encoded_tar), 97)]

        mock_stream.side_effect = [
            'EXISTS',
            ChunkedFakeExecResponse(stdout_chunks=chunks)
        ]

        log_path, error_code, error_msg = collector.download_log_file(self._make_pod(), f'/tmp/{remote_file}')

        self.assertEqual(log_path, Path(self.output_dir) / remote_file)
        self.assertIsNone(error_code)
        self.assertIsNone(error_msg)
        self.assertEqual(log_path.read_bytes(), file_data)

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_uses_preload_content_false_for_download(self, mock_core_api, mock_load_config, mock_stream):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        remote_file = 'nvidia-bug-report-test-node.log.gz'
        encoded_tar = self._make_encoded_tar(remote_file, b'test log data')

        mock_stream.side_effect = [
            'EXISTS',
            FakeExecResponse(stdout=encoded_tar)
        ]

        collector.download_log_file(self._make_pod(), f'/tmp/{remote_file}')

        self.assertEqual(mock_stream.call_args_list[1][1]['_preload_content'], False)

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_handles_empty_poll_without_stopping(self, mock_core_api, mock_load_config, mock_stream):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        remote_file = 'nvidia-bug-report-test-node.log.gz'
        file_data = b'test log data after empty poll'
        encoded_tar = self._make_encoded_tar(remote_file, file_data)

        mock_stream.side_effect = [
            'EXISTS',
            ChunkedFakeExecResponse(stdout_chunks=['', encoded_tar[:50], encoded_tar[50:]])
        ]

        log_path, error_code, error_msg = collector.download_log_file(self._make_pod(), f'/tmp/{remote_file}')

        self.assertEqual(log_path, Path(self.output_dir) / remote_file)
        self.assertIsNone(error_code)
        self.assertIsNone(error_msg)
        self.assertEqual(log_path.read_bytes(), file_data)

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_interleaved_stderr_does_not_corrupt_base64(self, mock_core_api, mock_load_config, mock_stream):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        remote_file = 'nvidia-bug-report-test-node.log.gz'
        file_data = b'test log data with stderr'
        encoded_tar = self._make_encoded_tar(remote_file, file_data)
        stdout_chunks = [encoded_tar[:50], encoded_tar[50:]]
        stderr_chunks = ['tar warning\n', 'tar more warning\n']

        mock_stream.side_effect = [
            'EXISTS',
            ChunkedFakeExecResponse(stdout_chunks=stdout_chunks, stderr_chunks=stderr_chunks)
        ]

        log_path, error_code, error_msg = collector.download_log_file(self._make_pod(), f'/tmp/{remote_file}')

        self.assertEqual(log_path, Path(self.output_dir) / remote_file)
        self.assertIsNone(error_code)
        self.assertIsNone(error_msg)
        self.assertEqual(log_path.read_bytes(), file_data)

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_read_exec_stream_collects_stdout_and_stderr_chunks(self, mock_core_api, mock_load_config):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        resp = ChunkedFakeExecResponse(
            stdout_chunks=['out1', '', 'out2'],
            stderr_chunks=['err1', 'err2']
        )

        stdout_output, stderr_output = collector._read_exec_stream(resp)

        self.assertEqual(stdout_output, 'out1out2')
        self.assertEqual(stderr_output, 'err1err2')

    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_read_exec_stream_times_out_on_hung_exec(self, mock_core_api, mock_load_config):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        resp = HangingFakeExecResponse()

        # Patch monotonic so the deadline (first call) is immediately exceeded by the
        # loop's checks. chain+repeat keeps the fake stable no matter how many times
        # monotonic() is called, so adding e.g. elapsed-time logging later won't break it.
        monotonic_values = itertools.chain([1000.0], itertools.repeat(1100.0))
        with patch.object(log_collector.time, 'monotonic', side_effect=monotonic_values):
            with self.assertRaises(TimeoutError):
                collector._read_exec_stream(resp, timeout=5)

        # The hung stream must be closed so the websocket/thread can be released.
        self.assertTrue(resp.closed)

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_timeout_returns_timed_out_code(self, mock_core_api, mock_load_config, mock_stream):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        # File-exists check succeeds, then the download read hangs.
        mock_stream.side_effect = [
            'EXISTS',
            HangingFakeExecResponse()
        ]

        # Drive monotonic past the deadline so _read_exec_stream raises TimeoutError.
        monotonic_values = itertools.chain([1000.0], itertools.repeat(1000.0 + log_collector.COLLECTION_TIMEOUT + 1))
        with patch.object(log_collector.time, 'monotonic', side_effect=monotonic_values):
            log_path, error_code, error_msg = collector.download_log_file(self._make_pod(), '/tmp/test.log.gz')

        # A hung exec must surface as a timeout, not a generic internal error.
        self.assertIsNone(log_path)
        self.assertEqual(error_code, 'CWA-BR-5008')

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_reports_invalid_base64_stdout(self, mock_core_api, mock_load_config, mock_stream):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        mock_pod = Mock()
        mock_pod.metadata.name = 'nvidia-gpu-driver-test'
        mock_container = Mock()
        mock_container.name = 'nvidia-driver-ctr'
        mock_pod.spec.containers = [mock_container]

        mock_stream.side_effect = [
            'EXISTS',
            FakeExecResponse(stdout='not base64!', stderr='tar failed\n')
        ]

        log_path, error_code, error_msg = collector.download_log_file(mock_pod, '/tmp/test.log.gz')

        self.assertIsNone(log_path)
        self.assertEqual(error_code, 'CWA-BR-5007')
        self.assertIn("Unexpected error downloading bug report", error_msg)

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_empty_stdout_returns_error(self, mock_core_api, mock_load_config, mock_stream):
        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        mock_stream.side_effect = [
            'EXISTS',
            FakeExecResponse(stdout='', stderr='tar: produced no output\n')
        ]

        log_path, error_code, error_msg = collector.download_log_file(self._make_pod(), '/tmp/test.log.gz')

        self.assertIsNone(log_path)
        self.assertEqual(error_code, 'CWA-BR-5007')
        self.assertIn("Unexpected error downloading bug report", error_msg)

    @patch('log_collector.stream')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_download_log_file_non_tar_payload_returns_error(self, mock_core_api, mock_load_config, mock_stream):
        # Valid base64 that decodes to non-tar bytes: mirrors a clean-boundary truncation
        non_tar = base64.b64encode(b'valid base64 but not a tar archive').decode()

        with patch.object(log_collector, 'LOG_OUTPUT_DIR', self.output_dir):
            collector = LogCollector()

        mock_stream.side_effect = [
            'EXISTS',
            FakeExecResponse(stdout=non_tar)
        ]

        log_path, error_code, error_msg = collector.download_log_file(self._make_pod(), '/tmp/test.log.gz')

        self.assertIsNone(log_path)
        self.assertEqual(error_code, 'CWA-BR-5007')
        self.assertIn("Unexpected error downloading bug report", error_msg)


class TestProxyConfig(unittest.TestCase):
    """Test proxy URL resolution."""

    def test_proxy_disabled_returns_api_base_url(self):
        """When PROXY_ENABLED is false, _get_cms_base_url returns API_BASE_URL."""
        with patch.object(log_collector, 'PROXY_ENABLED', False):
            with patch.object(log_collector, 'PROXY_URL', 'proxy.internal'):
                with patch.object(log_collector, 'API_BASE_URL', 'https://cms.example.com'):
                    self.assertEqual(log_collector._get_cms_base_url(), 'https://cms.example.com')

    def test_proxy_enabled_returns_proxy_base_url(self):
        """When PROXY_ENABLED is true, _get_cms_base_url returns http://PROXY_URL:PROXY_PORT."""
        with patch.object(log_collector, 'PROXY_ENABLED', True):
            with patch.object(log_collector, 'PROXY_URL', 'proxy.internal'):
                with patch.object(log_collector, 'PROXY_PORT', '3128'):
                    self.assertEqual(log_collector._get_cms_base_url(), 'http://proxy.internal:3128')

    def test_proxy_enabled_but_no_url_falls_back_to_api_base(self):
        """When PROXY_ENABLED is true but PROXY_URL is empty, falls back to API_BASE_URL."""
        with patch.object(log_collector, 'PROXY_ENABLED', True):
            with patch.object(log_collector, 'PROXY_URL', ''):
                with patch.object(log_collector, 'API_BASE_URL', 'https://cms.example.com'):
                    self.assertEqual(log_collector._get_cms_base_url(), 'https://cms.example.com')

    @patch('log_collector.requests.get')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_check_for_tasks_uses_proxy_url(self, mock_core_api, mock_load_config, mock_get):
        """check_for_tasks hits the proxy base URL when proxy is enabled."""
        os.environ['NODE_NAME'] = 'test-node'
        os.environ['VM_ID'] = 'test-vm-123'
        collector = LogCollector()

        mock_response = Mock()
        mock_response.status_code = 404
        mock_get.return_value = mock_response

        with patch.object(log_collector, 'PROXY_ENABLED', True):
            with patch.object(log_collector, 'PROXY_URL', 'proxy.internal'):
                with patch.object(log_collector, 'PROXY_PORT', '3128'):
                    collector.check_for_tasks()

        call_args = mock_get.call_args
        self.assertTrue(call_args[0][0].startswith('http://proxy.internal:3128'))

    @patch('log_collector.requests.post')
    @patch('log_collector.config.load_incluster_config')
    @patch('log_collector.client.CoreV1Api')
    def test_report_result_uses_proxy_url(self, mock_core_api, mock_load_config, mock_post):
        """report_result hits the proxy base URL when proxy is enabled."""
        os.environ['NODE_NAME'] = 'test-node'
        os.environ['VM_ID'] = 'test-vm-123'
        collector = LogCollector()

        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        with patch.object(log_collector, 'PROXY_ENABLED', True):
            with patch.object(log_collector, 'PROXY_URL', 'proxy.internal'):
                with patch.object(log_collector, 'PROXY_PORT', '3128'):
                    collector.report_result('evt-123', 'failed', message='test')

        call_args = mock_post.call_args
        self.assertTrue(call_args[0][0].startswith('http://proxy.internal:3128'))


if __name__ == '__main__':
    unittest.main()