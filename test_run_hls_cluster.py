"""Offline checks; no SSH, EE, credential access or NAS writes."""
from contextlib import nullcontext, redirect_stdout, redirect_stderr
from datetime import date
import io
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import run_hls_cluster as cluster


class ClusterTests(unittest.TestCase):
    def args(self, extra=()):
        return cluster.parser().parse_args([
            "--roi", "/nasdata/研究区/河流 面.shp", "--start", "2024-01-15",
            "--end", "2024-04-08", "--out", "/nasdata/HLS 测试", *extra])

    def test_partial_months_are_exact_and_disjoint(self):
        shards = cluster.validate(self.args())
        self.assertEqual([(s.start.isoformat(), s.end.isoformat()) for s in shards], [
            ("2024-01-15", "2024-02-01"), ("2024-02-01", "2024-03-01"),
            ("2024-03-01", "2024-04-01"), ("2024-04-01", "2024-04-08")])
        self.assertEqual([s.node for s in shards], ["node1", "node2", "node1", "node2"])
        self.assertEqual(sum((s.end-s.start).days for s in shards),
                         (date(2024, 4, 8)-date(2024, 1, 15)).days)
        self.assertEqual(len({s.out for s in shards}), len(shards))

    def test_year_boundary_and_leap_february(self):
        shards = cluster.split_months(date(2023, 12, 31), date(2024, 3, 1), "/nasdata/test")
        self.assertEqual([(s.end-s.start).days for s in shards], [1, 31, 29])
        self.assertEqual(shards[-1].end, date(2024, 3, 1))

    def test_exact_month_end_has_no_empty_shard(self):
        shards = cluster.split_months(date(2024, 1, 1), date(2024, 2, 1), "/nasdata/test")
        self.assertEqual(len(shards), 1)
        self.assertEqual(shards[0].end, date(2024, 2, 1))

    def test_invalid_interval_nodes_and_out_are_rejected(self):
        for nodes in (("node3",), ("node1", "node1"), ()):
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                cluster.split_months(date(2024, 1, 1), date(2024, 2, 1), "/nasdata/test", nodes)
        for extra in (("--out", "/tmp/test"), ("--out", "/nasdata"),
                      ("--out", "/nasdata/test/../../tmp"), ("--roi", "relative.shp"),
                      ("--start", "2024-05-01"), ("--require-mount", "/other")):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                cluster.validate(self.args(extra))

    def test_remote_quote_roundtrip_keeps_user_strings_literal(self):
        args = self.args(["--roi", "/nasdata/河流 '面' ; $(touch injected).shp",
                          "--out", "/nasdata/HLS '结果'; $HOME", "--project", "project-id",
                          "--crs", "EPSG:32650", "--mount-source", "//nas.example.org/data"])
        shard = cluster.validate(args)[0]
        ssh = cluster.ssh_argv(args, shard)
        self.assertEqual(shlex.split(ssh[-1]), cluster.downloader_argv(args, shard))
        self.assertEqual(ssh[-2], "node1")
        self.assertEqual(shlex.split(ssh[-1])[2], args.roi)
        self.assertIn("-tt", ssh)

    def test_preflight_forwards_science_options_and_only_adds_dryrun(self):
        args = self.args(["--buffer-m", "100", "--repair-geometries", "--limit", "1",
                          "--aerosol", "climatology-low", "--reflectance-scale", "0.0001"])
        shard = cluster.validate(args)[0]
        real = cluster.downloader_argv(args, shard)
        self.assertEqual(cluster.downloader_argv(args, shard, True), real + ["--dry-run"])
        self.assertIn("--repair-geometries", real)
        self.assertEqual(real[real.index("--limit")+1], "1")
        self.assertEqual(real[real.index("--out")+1], shard.out)

    def test_plan_never_launches_or_creates_lock(self):
        argv = ["--roi", "/nasdata/not-present.shp", "--start", "2024-01-01",
                "--end", "2024-02-01", "--out", "/nasdata/not-present-output"]
        with patch.object(cluster, "output_lock", side_effect=AssertionError("lock created")), \
                patch.object(cluster.subprocess, "Popen", side_effect=AssertionError("SSH launched")), \
                patch.object(cluster, "require_master", side_effect=AssertionError("host read")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cluster.main(argv), 0)

    def test_tile_workers_are_bounded_and_forwarded(self):
        args = self.args(["--workers", "8"])
        shard = cluster.validate(args)[0]
        remote = cluster.downloader_argv(args, shard)
        self.assertEqual(remote[remote.index("--workers") + 1], "8")
        for value in ("0", "9"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cluster.validate(self.args(["--workers", value]))

    def test_master_job_runs_owning_wrapper_locally_without_ssh(self):
        args = self.args(["--nodes", "master"])
        shard = cluster.validate(args)[0]
        self.assertEqual(shard.node, "master")
        command = cluster.ssh_argv(args, shard)
        self.assertEqual(command, cluster.downloader_argv(args, shard))
        self.assertEqual(command[0], cluster.WRAPPER)

    def test_one_worker_per_node_with_two_total(self):
        shards = cluster.validate(self.args())
        both_started = threading.Event()
        lock = threading.Lock()
        active, maxima, history = {}, {}, []
        max_total = [0]
        def runner(shard):
            with lock:
                active[shard.node] = active.get(shard.node, 0) + 1
                maxima[shard.node] = max(maxima.get(shard.node, 0), active[shard.node])
                max_total[0] = max(max_total[0], sum(active.values()))
                history.append((shard.node, shard.name))
                if sum(active.values()) == 2:
                    both_started.set()
            if not both_started.wait(3):
                raise AssertionError("Both nodes did not run concurrently")
            with lock:
                active[shard.node] -= 1
            return 0
        results = cluster.run_shards(shards, runner, lambda _: None)
        self.assertEqual(len(results), 4)
        self.assertEqual(maxima, {"node1": 1, "node2": 1})
        self.assertEqual(max_total[0], 2)
        for node in cluster.NODES:
            self.assertEqual([name for n, name in history if n == node],
                             [s.name for s in shards if s.node == node])

    def test_failure_stops_later_shards(self):
        shards = cluster.split_months(date(2024, 1, 1), date(2024, 5, 1),
                                     "/nasdata/test", ("node1",))
        called = []
        def fail(shard):
            called.append(shard.name)
            return 7
        results = cluster.run_shards(shards, fail, lambda _: None)
        self.assertEqual(called, [shards[0].name])
        self.assertEqual(results, {shards[0].name: 7})

    def test_other_active_node_finishes_without_starting_next_shard(self):
        shards = cluster.validate(self.args())
        other_started, failure_recorded = threading.Event(), threading.Event()
        def runner(shard):
            if shard.node == "node1":
                if not other_started.wait(3):
                    raise AssertionError("other node did not start")
                return 7
            other_started.set()
            if not failure_recorded.wait(3):
                raise AssertionError("failure was not recorded")
            return 0
        def emit(value):
            if value.startswith("结束 node1"):
                failure_recorded.set()
        results = cluster.run_shards(shards, runner, emit)
        self.assertEqual(results, {shards[0].name: 7, shards[1].name: 0})

    def test_failed_preflight_never_dispatches_download(self):
        argv = ["--roi", "/nasdata/not-present.shp", "--start", "2024-01-01",
                "--end", "2024-03-01", "--out", "/nasdata/offline-output", "--run"]
        runner = Mock(side_effect=[0, 1])
        with patch.object(cluster, "require_master"), \
                patch.object(cluster, "output_lock", return_value=nullcontext()), \
                patch.object(cluster, "SSHRunner", return_value=runner), \
                patch.object(cluster, "run_shards", side_effect=AssertionError("real job started")), \
                patch.object(cluster.signal, "signal"), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(cluster.main(argv), 1)
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(all(call.kwargs == {"dry_run": True} for call in runner.call_args_list))
        runner.cancel.assert_called_once()

    def test_cancel_terminates_active_ssh_and_escalates_if_needed(self):
        runner = cluster.SSHRunner(self.args())
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [cluster.subprocess.TimeoutExpired("ssh", 5), 143]
        runner.active.add(child)
        runner.cancel()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertTrue(runner.cancelled)
        with patch.object(cluster.subprocess, "Popen", side_effect=AssertionError("new SSH started")):
            self.assertEqual(runner(cluster.validate(self.args())[0]), 130)

    def test_hup_during_popen_is_deferred_until_child_is_tracked(self):
        runner = cluster.SSHRunner(self.args())
        child = Mock(pid=123)
        child.poll.return_value = None
        child.wait.return_value = 143
        hup = getattr(cluster.signal, "SIGHUP", 1)
        def spawn(*_args, **_kwargs):
            runner.record_signal(hup, None)
            return child
        with patch.object(cluster.subprocess, "Popen", side_effect=spawn):
            with self.assertRaises(cluster.Cancelled) as error:
                runner(cluster.validate(self.args())[0], dry_run=True)
        self.assertEqual(error.exception.signum, hup)
        self.assertIn(child, runner.active)
        child.wait.assert_not_called()
        runner.cancel()
        child.terminate.assert_called_once()
        self.assertNotIn(child, runner.active)

    def test_signal_during_preflight_wait_preserves_live_child_for_cleanup(self):
        runner = cluster.SSHRunner(self.args())
        child = Mock(pid=124)
        child.poll.return_value = None
        def wait(timeout):
            self.assertEqual(timeout, 0.2)
            runner.record_signal(cluster.signal.SIGTERM, None)
            raise cluster.subprocess.TimeoutExpired("ssh", timeout)
        child.wait.side_effect = wait
        with patch.object(cluster.subprocess, "Popen", return_value=child):
            with self.assertRaises(cluster.Cancelled):
                runner(cluster.validate(self.args())[0], dry_run=True)
        self.assertIn(child, runner.active)
        child.wait.side_effect = None
        child.wait.return_value = 143
        runner.cancel()
        self.assertNotIn(child, runner.active)

    def test_cleanup_survives_process_exit_race_and_repeated_signal(self):
        runner = cluster.SSHRunner(self.args())
        child = Mock(pid=125)
        child.poll.return_value = None
        def terminate():
            runner.record_signal(cluster.signal.SIGINT, None)
            raise ProcessLookupError()
        child.terminate.side_effect = terminate
        child.wait.return_value = 0
        runner.active.add(child)
        runner.cancel()
        self.assertIsNone(runner.pending_signal)
        self.assertFalse(runner.active)

    def test_cleanup_timeout_is_bounded_and_keeps_unfinished_child(self):
        runner = cluster.SSHRunner(self.args())
        child = Mock(pid=126)
        child.poll.return_value = None
        child.wait.side_effect = [cluster.subprocess.TimeoutExpired("ssh", 5),
                                 cluster.subprocess.TimeoutExpired("ssh", 10)]
        runner.active.add(child)
        with self.assertRaises(cluster.CleanupError) as error:
            runner.cancel()
        self.assertEqual(error.exception.pids, (126,))
        self.assertEqual([call.kwargs for call in child.wait.call_args_list],
                         [{"timeout": 5}, {"timeout": 10}])
        self.assertIn(child, runner.active)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real flock requires Linux")
    def test_same_root_lock_conflicts_then_releases_without_unlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            with cluster.output_lock("/nasdata/offline-test", temporary):
                with self.assertRaises(RuntimeError):
                    with cluster.output_lock("/nasdata/offline-test", temporary):
                        self.fail("second holder acquired lock")
            self.assertEqual(len(list(Path(temporary).glob(".deployment-cluster-*.lock"))), 1)
            with cluster.output_lock("/nasdata/offline-test", temporary):
                pass

    @unittest.skipUnless(sys.platform.startswith("linux"), "Real flock requires Linux")
    def test_failed_cleanup_marks_lock_and_blocks_next_run_until_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(cluster.CleanupError):
                with cluster.output_lock("/nasdata/offline-failed-test", temporary):
                    raise cluster.CleanupError([126])
            lock = next(Path(temporary).glob(".deployment-cluster-*.lock"))
            self.assertIn('"cleanup_failed": true', lock.read_text())
            with self.assertRaises(RuntimeError):
                with cluster.output_lock("/nasdata/offline-failed-test", temporary):
                    self.fail("unsafe restart allowed")
            lock.write_text("")  # Simulate an operator clearing status after inspection.
            with cluster.output_lock("/nasdata/offline-failed-test", temporary):
                pass


if __name__ == "__main__":
    unittest.main()
