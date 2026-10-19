import subprocess
import signal
import socket
import sys
import unittest
from unittest.mock import Mock, patch

import run_hls_node as node


class NodeControllerTests(unittest.TestCase):
    def setUp(self):
        node._pending_signal = None
        self.real_reuse = node.reuse_completed_locally
        self.reuse_patch = patch.object(node, "reuse_completed_locally", return_value=None)
        self.reuse_patch.start()
        self.addCleanup(self.reuse_patch.stop)

    def tearDown(self):
        node._pending_signal = None

    def test_download_workers_override_oversubscribed_library_threads(self):
        with patch.dict(node.os.environ, {'OPENBLAS_NUM_THREADS': '80', 'OMP_NUM_THREADS': '80'}):
            environment = node.child_environment()
            self.assertEqual(environment['OPENBLAS_NUM_THREADS'], '1')
            self.assertEqual(environment['OMP_NUM_THREADS'], '1')
            self.assertEqual(environment['GDAL_NUM_THREADS'], '1')
            self.assertEqual(environment['HLS_MIN_FREE_GIB'], '256')
            self.assertEqual(node.os.environ['OPENBLAS_NUM_THREADS'], '80')

    def test_completed_month_finishes_offline_even_when_proxy_is_down(self):
        argv = ["--roi", "/nasdata/roi.shp", "--start", "2024-01-01",
                "--end", "2024-02-01", "--out", "/nasdata/cached"]
        for hostname in ("master", "node1", "node2"):
            with self.subTest(host=hostname), patch.object(node.sys, "platform", "linux"), \
                    patch.object(node.socket, "gethostname", return_value=hostname), \
                    patch.object(node.os, "access", return_value=True), \
                    patch.object(node, "lock_node", return_value=42), \
                    patch.object(node, "reuse_completed_locally", return_value=0), \
                    patch.object(node, "require_master_proxy", side_effect=AssertionError("network gate")), \
                    patch.object(node, "require_free_port", side_effect=AssertionError("network gate")), \
                    patch.object(node.subprocess, "Popen", side_effect=AssertionError("network process")), \
                    patch.object(node, "stop_child"), patch.object(node.os, "close"), \
                    patch.object(node.signal, "signal"):
                self.assertEqual(node.run(argv), 0)

    def test_offline_probe_remaining_work_and_permanent_error_status(self):
        child = Mock()
        for status, expected in ((3, None), (0, 0), (1, 1)):
            with self.subTest(status=status), patch.object(node.subprocess, "Popen", return_value=child) as spawn, \
                    patch.object(node, "monitor_children", return_value=status), \
                    patch.object(node, "stop_child") as stop:
                self.assertEqual(self.real_reuse("/python", ["--roi", "/r.shp"]), expected)
                self.assertEqual(spawn.call_args.args[0][-1], "--resume-complete-only")
                stop.assert_called_once_with(child)

    def test_signal_during_spawn_is_deferred_until_child_is_tracked(self):
        proxy = Mock()
        def interrupted_spawn(*args, **kwargs):
            node.record_interrupt(signal.SIGTERM, None)
            return proxy
        with patch.object(node.sys, "platform", "linux"), \
                patch.object(node.socket, "gethostname", return_value="node1"), \
                patch.object(node.os, "access", return_value=True), \
                patch.object(node, "lock_node", return_value=42), \
                patch.object(node, "require_free_port"), \
                patch.object(node.subprocess, "Popen", side_effect=interrupted_spawn), \
                patch.object(node, "stop_child") as stop, \
                patch.object(node.os, "close") as close, \
                patch.object(node.signal, "SIGHUP", 1, create=True), \
                patch.object(node.signal, "signal"), self.assertRaises(node.NodeInterrupted):
            node.run(["--check"])
        self.assertEqual(stop.call_args_list[-1].args, (proxy,))
        close.assert_called_once_with(42)

    def test_proxy_override_and_abbreviation_are_rejected(self):
        standard = ["--roi", "/nasdata/a.shp", "--start", "2024-01-01",
                    "--end", "2024-02-01", "--out", "/nasdata/output"]
        for flag in ("--proxy", "--proxy=none"):
            with self.subTest(flag=flag), self.assertRaises(node.NodeError):
                node.validated_download_args(standard + [flag, "none"])
        for flag in ("--prox", "--prox=none"):
            with self.subTest(flag=flag), patch("sys.stderr"), self.assertRaises(SystemExit):
                node.validated_download_args(standard + [flag, "none"])

    def test_proxy_failure_interrupts_incomplete_download(self):
        download, proxy = Mock(), Mock()
        download.poll.return_value = None
        proxy.poll.return_value = 255
        with self.assertRaisesRegex(node.NodeError, "代理已退出"):
            node.monitor_children(download, proxy)

    def test_download_exit_status_is_preserved(self):
        download, proxy = Mock(), Mock()
        download.poll.return_value = 9
        self.assertEqual(node.monitor_children(download, proxy), 9)

    def test_master_download_borrows_proxy_without_creating_or_stopping_it(self):
        argv = ["--roi", "/nasdata/roi.shp", "--start", "2024-01-01",
                "--end", "2024-02-01", "--out", "/nasdata/master-test"]
        child = Mock()
        with patch.object(node.sys, "platform", "linux"), \
                patch.object(node.socket, "gethostname", return_value="master"), \
                patch.object(node.os, "access", return_value=True), \
                patch.object(node, "lock_node", return_value=42), \
                patch.object(node, "require_master_proxy") as ready, \
                patch.object(node, "require_free_port", side_effect=AssertionError("extra proxy")), \
                patch.object(node.subprocess, "Popen", return_value=child) as spawn, \
                patch.object(node, "monitor_children", return_value=0) as monitor, \
                patch.object(node, "stop_child") as stop, \
                patch.object(node.os, "close"), patch.object(node.signal, "signal"):
            self.assertEqual(node.run(argv), 0)
        ready.assert_called_once()
        self.assertEqual(spawn.call_count, 1)
        command = spawn.call_args.args[0]
        self.assertEqual(command[-2:], ["--proxy", "http://127.0.0.1:8118"])
        monitor.assert_called_once_with(child, None, timeout=None)
        self.assertEqual([call.args[0] for call in stop.call_args_list], [child, None])

    def test_master_proxy_readiness_is_retryable_and_never_restarts_shared_service(self):
        for state in (Mock(returncode=1, stdout=""), Mock(returncode=0, stdout="inactive\n")):
            with self.subTest(state=state), \
                    patch.object(node.subprocess, "run", return_value=state) as command, \
                    patch.object(node.socket, "create_connection") as connect, \
                    self.assertRaises(node.TransientNodeError):
                node.require_master_proxy()
            self.assertIn("show", command.call_args.args[0])
            connect.assert_not_called()
        with patch.object(node.subprocess, "run", return_value=Mock(returncode=0, stdout="active\n")), \
                patch.object(node.socket, "create_connection", side_effect=OSError("unavailable")), \
                self.assertRaises(node.TransientNodeError):
            node.require_master_proxy()

    def test_transient_proxy_failure_has_retryable_exit_and_fatal_error_does_not(self):
        with patch.object(node.signal, 'SIGHUP', 1, create=True), \
                patch.object(node.signal, 'signal'), patch('sys.stderr'):
            with patch.object(node, 'run', side_effect=node.TransientNodeError('safe network failure')):
                self.assertEqual(node.main(['--check']), 75)
            with patch.object(node, 'run', side_effect=node.NodeError('active listener belongs to another task')):
                self.assertEqual(node.main(['--check']), 1)

    def test_readiness_rejects_a_listener_owned_by_another_process(self):
        proxy = Mock(pid=4321)
        proxy.poll.return_value = None
        state = Mock(stdout='LISTEN 0 128 127.0.0.1:18119 0.0.0.0:* users:(("ssh",pid=1234,fd=4))\n')
        with patch.object(node.subprocess, "run", return_value=state), \
                patch.object(node.time, "monotonic", side_effect=[0, 0, 20]), \
                patch.object(node.time, "sleep"), self.assertRaises(node.NodeError):
            node.wait_proxy(proxy, timeout=15)

    def test_cleanup_escalates_a_stuck_child(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("child", 4), 0]
        node.stop_child(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_port_probe_rejects_an_active_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with self.assertRaises(node.NodeError):
                node.require_free_port(listener.getsockname()[1])

    @unittest.skipUnless(sys.platform == "linux", "Linux TCP TIME_WAIT regression")
    def test_port_probe_allows_owned_connection_time_wait_between_tasks(self):
        # Reproduce a temporary SSH listener's accepted connection surviving
        # in TIME_WAIT after its process exits. No fixed production port used.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            client = socket.create_connection(("127.0.0.1", port), timeout=2)
            accepted, _ = listener.accept()
            accepted.close()  # Server actively closes, so this port gets TIME_WAIT.
            self.assertEqual(client.recv(1), b"")
            client.close()
        # A bare bind falsely reports this non-listening port as occupied.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bare:
            with self.assertRaises(OSError):
                bare.bind(("127.0.0.1", port))
        node.require_free_port(port)


if __name__ == "__main__":
    unittest.main()
