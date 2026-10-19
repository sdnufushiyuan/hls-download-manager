"""Offline archive scheduling checks; no EE, node jobs or NAS writes."""
import argparse
from contextlib import contextmanager, nullcontext, redirect_stdout, redirect_stderr
import copy
from datetime import date
import io
import json
import shlex
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import run_hls_archive as archive
import run_hls_cluster as cluster


def fixture():
    boundaries = cluster.split_months(date(2020, 1, 15), date(2020, 4, 8), "/nasdata/test")
    crs = {"SDR": "EPSG:32650", "REM": "EPSG:32630", "MES": "EPSG:5070",
           "RIS": "EPSG:32614", "OZN": "EPSG:32755", "TXS": "EPSG:32614"}
    return {"schema_version": 1, "start": "2020-01-15", "end_exclusive": "2020-04-08",
            "regions": [{"code": code, "roi": f"/home/researcher/论文 数据/ROI/{code}/原始 面.shp",
                         "crs": crs[code], "area_m2": 944 if code == "MES" else 1,
                         "months": [{"start": shard.start.isoformat(),
                                     "end_exclusive": shard.end.isoformat(), "scene_count": 10}
                                    for shard in boundaries]} for code in archive.REGION_CODES]}


class ArchiveTests(unittest.TestCase):
    def args(self, extra=()):
        return archive.parser().parse_args(["--inventory", "-", "--out", "/nasdata/论文 数据/HLS 全历史", *extra])

    @contextmanager
    def retry_clock(self, on_sleep=lambda: None):
        clock, delays = [0.0], []
        def sleep(seconds):
            delays.append(seconds)
            clock[0] += seconds
            on_sleep()
        with patch.object(archive.time, "monotonic", side_effect=lambda: clock[0]), \
                patch.object(archive.time, "sleep", side_effect=sleep):
            yield delays

    def test_independent_regions_outputs_crs_and_empty_months(self):
        payload = fixture()
        payload["regions"][0]["months"][0]["scene_count"] = 0
        inventory = archive.parse_inventory(payload)
        jobs = archive.fair_jobs(inventory, self.args().out)
        self.assertEqual(len(jobs), 23)
        self.assertEqual(len({job.out for job in jobs}), len(jobs))
        self.assertFalse(any(job.code == "SDR" and job.start == date(2020, 1, 15) for job in jobs))
        for job in jobs:
            self.assertIn(f"/{job.code}/{job.start.isoformat()}__{job.end.isoformat()}", job.out)
        args = archive.region_arguments(self.args(), inventory)
        self.assertEqual(args["MES"].crs, "EPSG:5070")
        self.assertEqual(args["OZN"].crs, "EPSG:32755")

    def test_tile_workers_pass_through_independent_region_jobs(self):
        inventory = archive.parse_inventory(fixture())
        args = archive.region_arguments(self.args(["--workers", "8"]), inventory)
        runner = archive.ArchiveRunner(args)
        job = archive.replace(archive.fair_jobs(inventory, self.args().out)[0], node="node1")
        remote = shlex.split(runner.command_for(job)[-1])
        self.assertEqual(remote[remote.index("--workers") + 1], "8")
        for value in ("0", "9"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                archive.region_arguments(self.args(["--workers", value]), inventory)

    def test_region_rounds_prioritize_mes_without_starving_small_regions(self):
        jobs = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)
        self.assertEqual(jobs[0].code, "MES")
        for offset in range(0, 24, 6):
            self.assertEqual({job.code for job in jobs[offset:offset+6]}, set(archive.REGION_CODES))
        for code in archive.REGION_CODES:
            selected = [job for job in jobs if job.code == code]
            self.assertTrue(all(a.end == b.start for a, b in zip(selected, selected[1:])))

    def test_dynamic_queue_shares_mes_between_nodes_with_one_process_each(self):
        jobs = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)
        lock, two_mes_nodes = threading.Lock(), threading.Event()
        active, maxima, mes_nodes, seen = {}, {}, set(), []
        max_total = [0]
        def runner(job):
            with lock:
                active[job.node] = active.get(job.node, 0) + 1
                maxima[job.node] = max(maxima.get(job.node, 0), active[job.node])
                max_total[0] = max(max_total[0], sum(active.values()))
                seen.append(job.name)
                if job.code == "MES":
                    mes_nodes.add(job.node)
                    if len(mes_nodes) == 2:
                        two_mes_nodes.set()
            try:
                if job.code == "MES" and not two_mes_nodes.wait(3):
                    raise AssertionError("MES remained on only one node")
                return 0
            finally:
                with lock:
                    active[job.node] -= 1
        results = archive.run_jobs(jobs, cluster.DEFAULT_NODES, runner, lambda _: None)
        self.assertEqual(len(results), len(jobs))
        self.assertEqual(set(seen), {job.name for job in jobs})
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(mes_nodes, set(cluster.DEFAULT_NODES))
        self.assertEqual(maxima, {"node1": 1, "node2": 1})
        self.assertEqual(max_total[0], 2)

    def test_invalid_manifest_regions_dates_geometry_and_counts(self):
        mutations = [lambda p: p.update(schema_version=True),
                     lambda p: p.update(start="20200115"),
                     lambda p: p.update(end_exclusive="2020-01-15"),
                     lambda p: p["regions"].pop(),
                     lambda p: p["regions"][0].update(code="../outside"),
                     lambda p: p["regions"][0].update(code="MES"),
                     lambda p: p["regions"][0].update(roi="relative.shp"),
                     lambda p: p["regions"][0].update(roi="/home/a/../b.shp"),
                     lambda p: p["regions"][0].update(crs="EPSG:4326"),
                     lambda p: p["regions"][0].update(crs="EPSG:2277"),
                     lambda p: p["regions"][0].update(area_m2=float("nan")),
                     lambda p: p["regions"][0]["months"].pop(),
                     lambda p: p["regions"][0]["months"][0].update(end_exclusive="2020-02-02"),
                     lambda p: p["regions"][0]["months"][0].update(scene_count=-1),
                     lambda p: p["regions"][0]["months"][0].update(scene_count=True)]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                payload = fixture()
                mutate(payload)
                with self.assertRaises(ValueError):
                    archive.parse_inventory(payload)

    def test_three_workers_share_distinct_mes_months_including_master(self):
        jobs = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)
        lock, all_mes = threading.Lock(), threading.Event()
        mes_nodes, active, maxima, seen = set(), {}, {}, []
        def runner(job):
            with lock:
                active[job.node] = active.get(job.node, 0) + 1
                maxima[job.node] = max(maxima.get(job.node, 0), active[job.node])
                seen.append(job.out)
                if job.code == "MES":
                    mes_nodes.add(job.node)
                    if mes_nodes == set(cluster.NODES):
                        all_mes.set()
            try:
                if job.code == "MES" and not all_mes.wait(3):
                    raise AssertionError("Not all three workers shared MES months")
                return 0
            finally:
                with lock:
                    active[job.node] -= 1
        results = archive.run_jobs(jobs, cluster.NODES, runner, lambda _: None)
        self.assertEqual(len(results), len(jobs))
        self.assertEqual(mes_nodes, set(cluster.NODES))
        self.assertEqual(maxima, {"node1": 1, "node2": 1, "master": 1})
        self.assertEqual(len(seen), len(set(seen)))

    def test_duplicate_json_keys_and_nan_are_rejected(self):
        for text in ('{"schema_version":1,"schema_version":1}', '{"schema_version":NaN}'):
            with patch.object(sys, "stdin", io.StringIO(text)), self.assertRaises(ValueError):
                archive.read_inventory("-")

    def test_ignored_metadata_cannot_override_nodes_commands_or_out(self):
        payload = fixture()
        payload.update(nodes=["evil-host"], command="touch injected", out="/tmp/injected")
        payload["regions"][0].update(proxy="http://evil", options=["--out", "/tmp"])
        inventory = archive.parse_inventory(payload)
        args = archive.region_arguments(self.args(), inventory)
        runner = archive.ArchiveRunner(args)
        job = archive.fair_jobs(inventory, self.args().out)[0]
        job = archive.replace(job, node="node1")
        command = runner.command_for(job)
        self.assertEqual(command[-2], "node1")
        remote = shlex.split(command[-1])
        self.assertEqual(remote[remote.index("--out")+1], job.out)
        self.assertNotIn("--proxy", remote)
        with self.assertRaises(ValueError):
            runner.command_for(archive.replace(job, node="evil-host"))
        with self.assertRaises(ValueError):
            runner.command_for(archive.replace(job, out="/tmp/outside"))

    def test_job_commands_keep_region_args_independent_and_quote_paths(self):
        payload = fixture()
        payload["regions"][0]["roi"] = "/home/researcher/ROI/单引号 ' ; $(touch injected).shp"
        inventory = archive.parse_inventory(payload)
        args = archive.region_arguments(self.args(), inventory)
        runner = archive.ArchiveRunner(args)
        initial = copy.copy(vars(runner.args))
        for job in archive.fair_jobs(inventory, self.args().out):
            job = archive.replace(job, node="node2")
            remote = shlex.split(runner.command_for(job, True)[-1])
            self.assertEqual(remote[remote.index("--roi")+1], args[job.code].roi)
            self.assertEqual(remote[remote.index("--crs")+1], args[job.code].crs)
            self.assertEqual(remote[-1], "--dry-run")
        self.assertEqual(vars(runner.args), initial)

    def test_only_real_download_ssh_255_maps_to_temporary_network_exit(self):
        inventory = archive.parse_inventory(fixture())
        runner = archive.ArchiveRunner(archive.region_arguments(self.args(), inventory))
        job = archive.replace(archive.fair_jobs(inventory, self.args().out)[0], node="node1")
        with patch.object(cluster.SSHRunner, "__call__", return_value=255) as parent_call:
            self.assertEqual(runner(job), 75)
            parent_call.assert_called_with(job, False)
            self.assertEqual(runner(job, dry_run=True), 255)
            parent_call.assert_called_with(job, True)
            for status in (0, 1, 2, 75, 130):
                parent_call.return_value = status
                self.assertEqual(runner(job), status)

    def test_management_node_has_local_command_and_no_ssh_exit_mapping(self):
        inventory = archive.parse_inventory(fixture())
        runner = archive.ArchiveRunner(archive.region_arguments(self.args(), inventory))
        job = archive.replace(archive.fair_jobs(inventory, self.args().out)[0], node="master")
        command = runner.command_for(job)
        self.assertEqual(command[0], cluster.WRAPPER)
        self.assertEqual(command[command.index("--out") + 1], job.out)
        with patch.object(cluster.SSHRunner, "__call__", return_value=255):
            self.assertEqual(runner(job), 255)

    def test_inventory_ceiling_is_derived_and_lower_override_is_rejected(self):
        payload = fixture()
        payload["regions"][2]["months"][0]["scene_count"] = 5000
        inventory = archive.parse_inventory(payload)
        args = archive.region_arguments(self.args(), inventory)
        self.assertEqual(args["MES"].max_scenes, 5510)
        with self.assertRaises(ValueError):
            archive.region_arguments(self.args(["--max-scenes", "4999"]), inventory)

    def test_plan_has_no_ee_node_or_filesystem_side_effects(self):
        with patch.object(sys, "stdin", io.StringIO(json.dumps(fixture()))), \
                patch.dict(sys.modules, {"ee": None}), \
                patch.object(cluster, "output_lock", side_effect=AssertionError("lock created")), \
                patch.object(cluster.subprocess, "Popen", side_effect=AssertionError("SSH launched")), \
                patch.object(cluster, "require_master", side_effect=AssertionError("host checked")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(archive.main(["--inventory", "-", "--out", self.args().out]), 0)

    def test_preflight_each_region_node_once_then_failure_stops_real_dispatch(self):
        inventory = archive.parse_inventory(fixture())
        jobs = archive.fair_jobs(inventory, self.args().out)
        observed = []
        def runner(job, dry_run=False):
            observed.append((job.code, job.node, dry_run))
            return 0
        archive.preflight(jobs, cluster.DEFAULT_NODES, runner, lambda _: None)
        self.assertEqual(len(observed), 12)
        self.assertEqual(len(set(observed)), 12)
        self.assertTrue(all(dry_run for _, _, dry_run in observed))
        failed = Mock(side_effect=[0, 1])
        with patch.object(sys, "stdin", io.StringIO(json.dumps(fixture()))), \
                patch.object(cluster, "require_master"), \
                patch.object(cluster, "output_lock", return_value=nullcontext()), \
                patch.object(archive, "ArchiveRunner", return_value=failed), \
                patch.object(archive.signal, "signal"), \
                patch.object(archive, "run_jobs", side_effect=AssertionError("real jobs started")), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(archive.main(["--inventory", "-", "--out", self.args().out, "--run"]), 1)
        self.assertTrue(all(call.kwargs == {"dry_run": True} for call in failed.call_args_list))
        failed.cancel.assert_called_once()

    def test_failure_stops_new_queue_claims_but_other_active_job_finishes(self):
        jobs = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)
        other_started, failure_recorded = threading.Event(), threading.Event()
        def runner(job):
            if job.node == "node1":
                if not other_started.wait(3):
                    raise AssertionError("second node did not start")
                return 7
            other_started.set()
            if not failure_recorded.wait(3):
                raise AssertionError("failure was not recorded")
            return 0
        def emit(value):
            if value.startswith("结束 node1"):
                failure_recorded.set()
        results = archive.run_jobs(jobs, cluster.DEFAULT_NODES, runner, emit)
        self.assertEqual(len(results), 2)
        self.assertEqual(sorted(results.values()), [0, 7])

    def test_network_retry_reuses_same_job_node_and_only_reports_final_result(self):
        job = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)[0]
        messages, attempts, checks = [], [], []
        def runner(selected):
            self.assertFalse(any(message.startswith("结束 ") for message in messages))
            attempts.append(selected)
            return 75 if len(attempts) == 1 else 0
        with self.retry_clock() as delays:
            results = archive.run_jobs([job], ("node1",), runner, messages.append,
                                       lambda: checks.append(True), network_retry_delay=1)
        self.assertEqual(results, {job.name: 0})
        self.assertEqual(len(attempts), 2)
        self.assertIs(attempts[0], attempts[1])
        self.assertEqual(attempts[0].node, "node1")
        self.assertEqual(attempts[0].out, job.out)
        self.assertIn(f"网络恢复等待 node1 {job.name}，重试 1/4，等待 1 秒。", messages)
        self.assertIn(f"网络恢复重试 node1 {job.name}，重试 1/4。", messages)
        self.assertEqual(sum(message.startswith("结束 ") for message in messages), 1)
        self.assertTrue(all(0 < seconds <= 0.2 for seconds in delays))
        self.assertAlmostEqual(sum(delays), 1)
        self.assertGreaterEqual(len(checks), len(delays))

    def test_non_network_failures_never_retry_or_claim_next_job(self):
        jobs = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)[:2]
        for status in (1, 7, 255):
            with self.subTest(status=status):
                runner = Mock(return_value=status)
                messages = []
                with patch.object(archive.time, "sleep", side_effect=AssertionError("Unexpected wait")):
                    results = archive.run_jobs(jobs, ("node1",), runner, messages.append)
                self.assertEqual(results, {jobs[0].name: status})
                runner.assert_called_once()
                self.assertFalse(any(message.startswith("网络恢复") for message in messages))

    def test_network_retry_budget_is_finite_and_zero_disables_retries(self):
        jobs = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)[:2]
        for retries in (0, 4):
            with self.subTest(retries=retries):
                runner = Mock(return_value=75)
                messages = []
                with self.retry_clock() as delays:
                    results = archive.run_jobs(jobs, ("node1",), runner, messages.append,
                                               network_job_retries=retries)
                self.assertEqual(results, {jobs[0].name: 75})
                self.assertEqual(runner.call_count, retries + 1)
                self.assertTrue(all(call.args[0] is runner.call_args.args[0]
                                    for call in runner.call_args_list))
                self.assertAlmostEqual(sum(delays), retries * 300)
                self.assertEqual(sum(message.startswith("网络恢复等待 ") for message in messages), retries)
                self.assertEqual(sum(message.startswith("网络恢复重试 ") for message in messages), retries)
                self.assertEqual(sum(message.startswith("结束 ") for message in messages), 1)

    def test_cancel_during_network_wait_never_retries_or_reports_completion(self):
        job = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)[0]
        cancelled, messages = threading.Event(), []
        runner = Mock(return_value=75)
        def check_cancelled():
            if cancelled.is_set():
                raise cluster.Cancelled(15)
        with self.retry_clock(cancelled.set) as delays:
            with self.assertRaises(cluster.Cancelled):
                archive.run_jobs([job], ("node1",), runner, messages.append, check_cancelled)
        runner.assert_called_once()
        self.assertEqual(delays, [0.2])
        self.assertTrue(any(message.startswith("网络恢复等待 ") for message in messages))
        self.assertFalse(any(message.startswith(("网络恢复重试 ", "结束 ")) for message in messages))

    def test_peer_permanent_failure_stops_a_waiting_network_retry(self):
        jobs = archive.fair_jobs(archive.parse_inventory(fixture()), self.args().out)[:3]
        waiting, failure_recorded = threading.Event(), threading.Event()
        calls, messages = [], []
        def runner(job):
            calls.append(job)
            if job.node == "node1":
                return 75
            if not waiting.wait(3):
                raise AssertionError("The network recovery wait never started")
            return 1
        def emit(message):
            messages.append(message)
            if message.startswith("网络恢复等待 node1 "):
                waiting.set()
                if not failure_recorded.wait(3):
                    raise AssertionError("The permanent failure was not recorded")
            if message.startswith("结束 node2 "):
                failure_recorded.set()
        with patch.object(archive.time, "sleep", side_effect=AssertionError("Unexpected retry wait")):
            results = archive.run_jobs(jobs, cluster.DEFAULT_NODES, runner, emit)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sorted(results.values()), [1, 75])
        self.assertFalse(any(message.startswith("网络恢复重试 ") for message in messages))

    def test_invalid_network_parser_and_programmatic_policy_never_start(self):
        invalid = [("--network-job-retries", value) for value in ("-1", "9", "1.5", "nan")]
        invalid += [("--network-retry-delay", value) for value in ("0", "3601", "nan", "inf", "invalid")]
        for flag, value in invalid:
            with self.subTest(flag=flag, value=value), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as failure:
                    self.args([flag, value])
                self.assertEqual(failure.exception.code, 2)
        for retries, delay in ((True, 1), (-1, 1), (9, 1), (4, True),
                               (4, 0), (4, float("nan")), (4, 3601)):
            runner = Mock()
            with self.subTest(retries=retries, delay=delay), self.assertRaises(ValueError):
                archive.run_jobs([], ("node1",), runner,
                                 network_job_retries=retries, network_retry_delay=delay)
            runner.assert_not_called()
        self.assertEqual(self.args(["--network-job-retries", "8"]).network_job_retries, 8)
        self.assertEqual(self.args(["--network-retry-delay", "3600"]).network_retry_delay, 3600)

    def test_main_reports_and_applies_cli_network_policy(self):
        output = io.StringIO()
        runner = Mock(side_effect=[75] + [0] * 24)
        with patch.object(sys, "stdin", io.StringIO(json.dumps(fixture()))), \
                patch.object(cluster, "require_master"), \
                patch.object(cluster, "output_lock", return_value=nullcontext()), \
                patch.object(archive, "ArchiveRunner", return_value=runner), \
                patch.object(archive, "preflight"), \
                patch.object(archive.signal, "signal"), \
                self.retry_clock() as delays, redirect_stdout(output):
            self.assertEqual(archive.main(["--inventory", "-", "--out", self.args().out, "--run",
                                           "--nodes", "node1", "--network-job-retries", "1",
                                           "--network-retry-delay", "1.5"]), 0)
        summary, _ = json.JSONDecoder().raw_decode(output.getvalue())
        self.assertEqual(summary["network_retry_policy"],
                         {"exit_code": 75, "mapped_download_ssh_exit_code": 255,
                          "retries": 1, "max_attempts": 2,
                          "delay_seconds": 1.5, "scope": "same_node_same_month"})
        self.assertEqual(runner.call_count, 25)
        self.assertAlmostEqual(sum(delays), 1.5)
        runner.cancel.assert_called_once()

    def test_network_exhaustion_requests_service_retry_but_permanent_error_stops(self):
        for status, expected in ((75, 75), (1, 1)):
            with self.subTest(status=status), \
                    patch.object(sys, "stdin", io.StringIO(json.dumps(fixture()))), \
                    patch.object(cluster, "require_master"), \
                    patch.object(cluster, "output_lock", return_value=nullcontext()), \
                    patch.object(archive, "ArchiveRunner", return_value=Mock(return_value=status)), \
                    patch.object(archive, "preflight"), \
                    patch.object(archive.signal, "signal"), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(archive.main([
                    "--inventory", "-", "--out", self.args().out, "--run",
                    "--nodes", "node1", "--network-job-retries", "0",
                ]), expected)

    def test_invalid_nodes_and_out_root_never_start(self):
        with self.assertRaises(ValueError):
            archive.run_jobs([], ("unknown",), lambda _: 0)
        with self.assertRaises(ValueError):
            archive.region_arguments(self.args(["--out", "/tmp/outside"]),
                                     archive.parse_inventory(fixture()))
        with self.assertRaises(ValueError):
            archive.region_arguments(self.args(["--nodes", "node1", "node1"]),
                                     archive.parse_inventory(fixture()))


if __name__ == "__main__":
    unittest.main()
