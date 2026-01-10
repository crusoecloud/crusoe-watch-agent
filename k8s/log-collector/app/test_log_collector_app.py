#!/usr/bin/env python3
"""
Unit tests for NVIDIA Log Collector
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
import tempfile
import os
from pathlib import Path

# Set environment variables before importing the module
os.environ['NODE_NAME'] = 'test-node'
os.environ['LOG_OUTPUT_DIR'] = tempfile.mkdtemp()

from log_collector_app import NvidiaLogCollector


class TestNvidiaLogCollector(unittest.TestCase):
    """Test cases for NvidiaLogCollector class."""

    def setUp(self):
        """Set up test fixtures."""
        self.output_dir = tempfile.mkdtemp()
        os.environ['LOG_OUTPUT_DIR'] = self.output_dir
        os.environ['NODE_NAME'] = 'test-node-1'

    @patch('log_collector_app.config.load_incluster_config')
    @patch('log_collector_app.client.CoreV1Api')
    def test_initialization(self, mock_core_api, mock_load_config):
        """Test collector initialization."""
        collector = NvidiaLogCollector()

        self.assertEqual(collector.node_name, 'test-node-1')
        self.assertEqual(collector.nvidia_namespace, 'nvidia-gpu-operator')
        self.assertTrue(collector.output_dir.exists())
        mock_load_config.assert_called_once()

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


class TestEnvironmentVariables(unittest.TestCase):
    """Test environment variable handling."""

    def test_missing_node_name(self):
        """Test that missing NODE_NAME raises error."""
        # Remove NODE_NAME temporarily
        original_node_name = os.environ.get('NODE_NAME')
        if 'NODE_NAME' in os.environ:
            del os.environ['NODE_NAME']

        with patch('log_collector_app.config.load_incluster_config'):
            with self.assertRaises(RuntimeError):
                NvidiaLogCollector()

        # Restore NODE_NAME
        if original_node_name:
            os.environ['NODE_NAME'] = original_node_name

    def test_custom_nvidia_namespace(self):
        """Test custom NVIDIA namespace."""
        os.environ['NVIDIA_NAMESPACE'] = 'custom-gpu-namespace'
        os.environ['NODE_NAME'] = 'test-node'

        with patch('log_collector_app.config.load_incluster_config'):
            with patch('log_collector_app.client.CoreV1Api'):
                collector = NvidiaLogCollector()
                self.assertEqual(collector.nvidia_namespace, 'custom-gpu-namespace')

        # Cleanup
        os.environ['NVIDIA_NAMESPACE'] = 'nvidia-gpu-operator'


if __name__ == '__main__':
    unittest.main()