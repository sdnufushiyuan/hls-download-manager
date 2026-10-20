"""Meaningful progress boundaries; no real journal, network or NAS is required."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hls_progress_state as progress
from hls_progress_state import ProgressCollector, REGION_CODES


BOUNDARY = "2013-04-11__2013-05-01"
HASH = "a" * 64


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        run = self.root / "runs" / "archive"
        run.mkdir(parents=True)
        inventory = {"schema_version": 1, "regions": [{"code": code, "months": [
            {"start": "2013-04-11", "end_exclusive": "2013-05-01", "scene_count": 2}
        ]} for code in REGION_CODES]}
        self.write(run / "inventory.json", inventory)
        self.collector = ProgressCollector(self.root)
        self.collector.output_root = self.root / "output"
        for month in self.collector.months.values():
            month.path = self.collector.output_root / month.code / BOUNDARY
        self.active = "active"
        self.invocation = "current"
        self.proxy_active = "active"
        self.proxy_substate = "running"
        self.proxy_restarts = 2
        self.proxy_invocation = "proxy-current"
        self.journal = []
        self.mounted = True
        self.command_patch = patch("hls_progress_state._run_command", side_effect=self.command)
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)
        self.nas_patch = patch.object(self.collector, "_nas", side_effect=self.nas)
        self.nas_patch.start()
        self.addCleanup(self.nas_patch.stop)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def command(self, argv):
        if argv[0] == "systemctl":
            if argv[3] == progress.PROXY_SERVICE:
                return (f"ActiveState={self.proxy_active}\nSubState={self.proxy_substate}\n"
                        f"NRestarts={self.proxy_restarts}\nInvocationID={self.proxy_invocation}\n")
            return (f"ActiveState={self.active}\nSubState=running\nResult=success\n"
                    f"InvocationID={self.invocation}\nActiveEnterTimestamp=today\n")
        if argv[0] == "journalctl":
            result = "\n".join(json.dumps(record) for record in self.journal)
            self.journal = []
            return result
        raise AssertionError("unexpected command")

    def nas(self, warnings):
        if not self.mounted:
            warnings.append("NAS挂载不可用；显示缓存。")
        return {"mounted": self.mounted, "free_gib": 9000 if self.mounted else None}

    def event(self, message, invocation="current"):
        self.journal.append({"MESSAGE": message, "_SYSTEMD_INVOCATION_ID": invocation,
                             "__CURSOR": "cursor" + str(len(self.journal))})

    def start(self, node="node1", code="MES", invocation="current"):
        self.event(f"开始 {node} {code}/{BOUNDARY}，库存候选 2", invocation)

    def end(self, node="node1", code="MES", status=0):
        self.event(f"结束 {node} {code}/{BOUNDARY}，退出码 {status}，已结束 1/6")

    def manifest(self, code="MES", count=2, recipe=HASH):
        month = self.collector.months[code + "/" + BOUNDARY]
        scenes = [{"id": f"NASA/HLS/HLSL30/v002/SCENE{number}", "index": f"SCENE{number}",
                   "sensor": "L30"} for number in range(1, count + 1)]
        self.write(month.path / "run_manifest.json", {"recipe_hash": recipe, "scenes": scenes})
        return month, scenes

    def completed(self, month, scene, recipe=HASH, tiles=None):
        if tiles is None:
            tiles = [{"file": "r000000_c000000.tif", "valid_pixels": 0}]
        self.write(month.path / scene["sensor"] / scene["index"] / "scene.json",
                   {**scene, "recipe_hash": recipe, "tiles": tiles})

    def test_empty_progress_does_not_create_output(self):
        value = self.collector.snapshot()
        self.assertEqual((value["completed_scenes"], value["total_scenes"]), (0, 12))
        self.assertEqual((value["completed_jobs"], value["total_jobs"]), (0, 6))
        self.assertFalse(self.collector.output_root.exists())

    def test_completion_requires_identity_recipe_and_tiles(self):
        month, scenes = self.manifest()
        self.completed(month, scenes[0], tiles=[])  # No footprint intersection is valid.
        self.completed(month, scenes[1], recipe="wrong")
        value = self.collector.snapshot()
        self.assertEqual(value["completed_scenes"], 1)
        self.assertEqual(value["completed_jobs"], 0)

    def test_zero_clear_pixels_are_still_a_completed_scene(self):
        month, scenes = self.manifest()
        for scene in scenes:
            self.completed(month, scene)
        value = self.collector.snapshot()
        self.assertEqual(value["completed_scenes"], 2)
        self.assertEqual(value["completed_jobs"], 1)

    def test_region_ready_requires_successful_month_exit_and_available_nas(self):
        month, scenes = self.manifest()
        for scene in scenes:
            self.completed(month, scene)
        def ready():
            return next(row for row in self.collector.snapshot()['regions'] if row['code']=='MES')['ready_for_processing']
        self.assertFalse(ready())  # File hints alone cannot announce readiness.
        self.end()
        self.assertTrue(ready())
        self.start()
        self.assertFalse(ready())
        self.end(status=1)
        self.assertFalse(ready())
        self.end()
        self.assertTrue(ready())
        self.mounted=False
        self.assertFalse(ready())

    def test_selection_skip_counts_are_separate_from_processed_candidates(self):
        month, scenes = self.manifest()
        for scene in scenes:
            self.completed(month, scene, tiles=[])
        self.write(month.path / "download_selection_summary.json", {"recipe_hash": HASH,
            "processed_scenes": 2, "skipped_no_clear_scenes": 1,
            "skipped_overlap_scenes": 1, "skipped_empty_tiles": 4})
        value = self.collector.snapshot()
        self.assertEqual(value["completed_scenes"], 2)
        self.assertEqual(value["skipped_no_clear_scenes"], 1)
        self.assertEqual(value["skipped_overlap_scenes"], 1)
        self.assertEqual(value["skipped_empty_tiles"], 4)
        self.write(month.path / "download_selection_summary.json", {"recipe_hash": "wrong",
            "processed_scenes": 2, "skipped_no_clear_scenes": 0,
            "skipped_overlap_scenes": 0, "skipped_empty_tiles": 0})
        value = self.collector.snapshot()
        self.assertEqual(value["skipped_empty_tiles"], 4)
        self.assertTrue(any("筛选统计" in w for w in value["warnings"]))

    def test_explicit_success_avoids_per_scene_reads(self):
        self.manifest()
        self.start()
        self.end()
        with patch.object(self.collector, "_scene_complete", side_effect=AssertionError("must not stat scenes")):
            value = self.collector.snapshot()
        self.assertEqual(value["completed_scenes"], 2)
        self.assertEqual(value["completed_jobs"], 1)

    def test_new_end_event_confirms_already_cached_month(self):
        self.manifest()
        self.start()
        self.assertEqual(self.collector.snapshot()["completed_scenes"], 0)
        self.end()
        value = self.collector.snapshot()
        self.assertEqual(value["completed_scenes"], 2)
        self.assertEqual(value["completed_jobs"], 1)

    def test_current_tiles_require_matching_final_metadata(self):
        month, scenes = self.manifest()
        path = month.path / "L30" / "SCENE1"
        self.write(path / "r000000_c000000.json", {"recipe_hash": HASH, "scene_id": scenes[0]["id"]})
        self.write(path / "r000000_c000512.json", {"recipe_hash": "wrong", "scene_id": scenes[0]["id"]})
        self.write(path / "r000512_c000000.json.part", {"recipe_hash": HASH, "scene_id": scenes[0]["id"]})
        self.start()
        value = self.collector.snapshot()["nodes"]["node1"]
        self.assertEqual(value["current_scene"], "SCENE1")
        self.assertEqual(value["tile_count_current"], 1)
        self.assertEqual(value["completed_scenes"], 0)

    def test_node_attribution_ignores_interleaved_unlabelled_tiles(self):
        self.manifest("MES")
        self.manifest("SDR")
        self.start("node2", "SDR", invocation="old")
        self.start("node1", "MES")
        self.start("node2", "SDR")
        self.event("[1/2] L30 SCENE1")
        self.event("  9/100 r000000_c000000.tif 已下载；有效像元 10")
        value = self.collector.snapshot()
        self.assertTrue(value["nodes"]["node1"]["job"].startswith("MES/"))
        self.assertTrue(value["nodes"]["node2"]["job"].startswith("SDR/"))
        self.assertEqual(value["nodes"]["node2"]["tile_count_current"], 0)

    def test_restart_does_not_keep_old_active_node(self):
        self.manifest()
        self.start()
        self.assertEqual(self.collector.snapshot()["nodes"]["node1"]["phase"], "downloading")
        self.invocation = "next"
        self.start(invocation="old")
        value = self.collector.snapshot()
        self.assertIsNone(value["nodes"]["node1"]["job"])
        self.assertEqual(value["nodes"]["node1"]["phase"], "waiting")

    def test_failed_service_never_reports_background_download(self):
        self.manifest()
        self.start()
        self.end(status=1)
        self.active = "failed"
        value = self.collector.snapshot()
        self.assertEqual(value["failed_jobs"], ["MES/" + BOUNDARY])
        self.assertEqual(value["nodes"]["node1"]["phase"], "stopped")
        self.assertEqual(value["completed_jobs"], 0)

    def test_detached_nas_is_not_scanned_or_counted_as_zero(self):
        month, scenes = self.manifest()
        self.completed(month, scenes[0])
        self.collector.snapshot()
        self.mounted = False
        with patch.object(self.collector, "_discover", side_effect=AssertionError("must not read detached mount")):
            value = self.collector.snapshot()
        self.assertEqual(value["completed_scenes"], 1)
        self.assertFalse(value["nas"]["mounted"])
        self.assertTrue(value["warnings"])

    def test_repeated_poll_checks_only_first_unfinished_scene(self):
        month, scenes = self.manifest()
        self.completed(month, scenes[0])
        self.start()
        self.collector.snapshot()
        with patch.object(self.collector, "_scene_complete", wraps=self.collector._scene_complete) as check:
            self.collector.snapshot()
        self.assertEqual(check.call_count, 1)
        self.assertEqual(check.call_args.args[1]["index"], "SCENE2")

    def test_inventory_difference_is_a_reference_warning(self):
        self.manifest(count=3)
        self.start()
        first = self.collector.snapshot()
        second = self.collector.snapshot()
        self.assertEqual(first["total_scenes"], 12)
        self.assertTrue(any("库存参考" in warning for warning in second["warnings"]))

    def test_command_failures_are_warnings_without_raw_output(self):
        with patch("hls_progress_state._run_command", side_effect=RuntimeError("sensitive raw error")):
            value = self.collector.snapshot()
        self.assertEqual(value["service"]["active"], "unknown")
        self.assertEqual(value["nodes"]["node1"]["phase"], "unknown")
        self.assertTrue(value["warnings"])
        self.assertNotIn("sensitive", json.dumps(value))

    def test_full_completion_needs_final_success_and_live_nas(self):
        for code in REGION_CODES:
            self.manifest(code)
            self.end(code=code)
        self.active = "inactive"
        value = self.collector.snapshot()
        self.assertEqual(value["completed_jobs"], 6)
        self.assertFalse(value["archive_complete"])
        self.event("结束：完成 6；失败 0；未启动 0。")
        self.assertTrue(self.collector.snapshot()["archive_complete"])
        self.mounted = False
        self.assertFalse(self.collector.snapshot()["archive_complete"])

    def test_no_new_journal_events_are_an_empty_successful_poll(self):
        empty = SimpleNamespace(returncode=1, stdout="", stderr="")
        with patch("hls_progress_state.subprocess.run", return_value=empty):
            # Use the unpatched function because setUp replaces the public helper.
            helper = self.command_patch.temp_original
            self.assertEqual(helper(["journalctl", "--after-cursor=cursor"]), "")
        denied = SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        with patch("hls_progress_state.subprocess.run", return_value=denied):
            with self.assertRaises(RuntimeError):
                helper(["journalctl", "--after-cursor=cursor"])

    def test_failed_job_recovers_last_completion_after_previous_poll(self):
        month, scenes = self.manifest()
        self.start()
        self.collector.snapshot()
        self.completed(month, scenes[0])
        self.end(status=1)
        value = self.collector.snapshot()
        self.assertEqual(value["completed_scenes"], 1)
        self.assertEqual(value["completed_jobs"], 0)

    def test_old_failed_invocation_is_not_a_current_failure(self):
        self.manifest()
        self.event(f"结束 node1 MES/{BOUNDARY}，退出码 1，已结束 1/6", invocation="old")
        self.start()
        value = self.collector.snapshot()
        self.assertEqual(value["failed_jobs"], [])
        self.assertEqual(value["nodes"]["node1"]["phase"], "downloading")

    def test_scene_read_permission_error_is_visible_and_conservative(self):
        self.manifest()
        self.start()
        original = progress._read_json
        def read(path, limit):
            if path.name == "scene.json":
                raise PermissionError("hidden raw details")
            return original(path, limit)
        with patch("hls_progress_state._read_json", side_effect=read):
            value = self.collector.snapshot()
        self.assertEqual(value["completed_scenes"], 0)
        self.assertTrue(any("完成记录暂不可读" in warning for warning in value["warnings"]))
        self.assertNotIn("hidden raw details", json.dumps(value))

    def test_network_wait_keeps_job_then_resumes_download(self):
        self.manifest()
        self.start()
        self.event(f"网络恢复等待 node1 MES/{BOUNDARY}，重试 1/4，等待 300 秒。")
        self.journal[-1]["__REALTIME_TIMESTAMP"] = str(int(progress.datetime.now(progress.timezone.utc).timestamp() * 1000000) - 20_000_000)
        waiting = self.collector.snapshot()
        node = waiting["nodes"]["node1"]
        self.assertEqual(node["phase"], "retrying")
        self.assertEqual(node["job"], "MES/" + BOUNDARY)
        self.assertEqual(node["retry"]["attempt"], 1)
        self.assertEqual(node["retry"]["total_retries"], 4)
        self.assertEqual(node["retry"]["wait_seconds"], 300)
        self.assertTrue(275 <= node["retry"]["remaining_seconds"] <= 280)
        self.assertEqual(waiting["failed_jobs"], [])
        self.event(f"网络恢复重试 node1 MES/{BOUNDARY}，重试 1/4。")
        resumed = self.collector.snapshot()["nodes"]["node1"]
        self.assertEqual(resumed["phase"], "downloading")
        self.assertEqual(resumed["retry"]["remaining_seconds"], 0)

    def test_old_network_wait_does_not_claim_current_node(self):
        self.manifest()
        self.event(f"网络恢复等待 node1 MES/{BOUNDARY}，重试 1/4，等待 300 秒。", invocation="old")
        value = self.collector.snapshot()["nodes"]["node1"]
        self.assertEqual(value["phase"], "waiting")
        self.assertIsNone(value["job"])
        self.assertIsNone(value["retry"])

    def test_final_network_failure_clears_wait_and_remains_a_failure(self):
        self.manifest()
        self.start()
        self.event(f"网络恢复等待 node1 MES/{BOUNDARY}，重试 4/4，等待 300 秒。")
        self.collector.snapshot()
        self.end(status=75)
        self.active = "failed"
        value = self.collector.snapshot()
        self.assertEqual(value["failed_jobs"], ["MES/" + BOUNDARY])
        self.assertEqual(value["nodes"]["node1"]["phase"], "stopped")
        self.assertIsNone(value["nodes"]["node1"]["retry"])
        from watch_hls_progress import render
        output = render(value)
        self.assertIn("任务失败", output)
        self.assertNotIn("等待网络恢复，自动重试", output)

    def test_service_auto_restart_is_shown_as_recovery_not_permanent_failure(self):
        from watch_hls_progress import render
        snapshot = self.collector.snapshot()
        snapshot["service"].update(active="activating", substate="auto-restart", result="exit-code")
        output = render(snapshot)
        self.assertIn("等待服务自动重启，随后继续断点下载", output)
        self.assertNotIn("任务失败，请查看日志", output)

    def test_proxy_snapshot_is_local_and_warning_does_not_leak_errors(self):
        self.proxy_active = "activating"
        self.proxy_substate = "auto-restart"
        value = self.collector.snapshot()
        self.assertEqual(value["proxy"], {"active": "activating", "substate": "auto-restart", "restarts": 2,
                                        "invocation_id": "proxy-current"})
        from watch_hls_progress import render
        self.assertIn("下载代理恢复中", render(value))
        def command(argv):
            if argv[0] == "systemctl" and argv[3] == progress.PROXY_SERVICE:
                raise RuntimeError("private error details must not appear")
            return self.command(argv)
        with patch("hls_progress_state._run_command", side_effect=command):
            unknown = self.collector.snapshot()
        self.assertEqual(unknown["proxy"]["active"], "unknown")
        self.assertTrue(any("代理服务状态读取失败" in warning for warning in unknown["warnings"]))
        self.assertNotIn("private error", json.dumps(unknown))

    def test_frontend_shows_wait_seconds_and_bounded_retry_number(self):
        self.manifest()
        self.start()
        self.event(f"网络恢复等待 node1 MES/{BOUNDARY}，重试 2/4，等待 300 秒。")
        from watch_hls_progress import render
        output = render(self.collector.snapshot())
        self.assertIn("等待网络恢复，自动重试", output)
        self.assertIn("恢复重试 2/4", output)
        self.assertIn("本次等待 300 秒", output)

    def test_mihomo_route_is_read_only_and_shows_independent_linux_transport(self):
        from watch_hls_progress import render
        route_path = self.collector.program_root / 'runs/hls_ssh_route.json'
        self.write(route_path, {'route': 'linux_mihomo'})
        before = route_path.read_bytes()
        snapshot = self.collector.snapshot()
        self.assertEqual(snapshot['proxy']['route'], 'linux_mihomo')
        rendered = render(snapshot)
        self.assertIn('Mihomo', rendered)
        self.assertIn('不依赖 Windows', rendered)
        self.assertNotIn('新加坡 TLS/SSH 2222 直连', rendered)
        self.assertEqual(route_path.read_bytes(), before)

    def test_proxy_route_is_read_only_and_completed_active_job_shows_verifying(self):
        month, scenes = self.manifest()
        for scene in scenes:
            self.completed(month, scene, tiles=[])
        self.start()
        self.write(self.collector.program_root / 'runs/hls_ssh_route.json', {'route': 'windows_ssh_relay'})
        snapshot = self.collector.snapshot()
        self.assertEqual(snapshot['proxy']['route'], 'windows_ssh_relay')
        self.assertEqual(snapshot['nodes']['node1']['phase'], 'verifying')
        from watch_hls_progress import render
        self.assertIn('Windows需保持在线', render(snapshot))

    def test_fractional_network_retry_delay_matches_archive_log(self):
        self.manifest()
        self.start()
        self.event(f"网络恢复等待 node1 MES/{BOUNDARY}，重试 1/4，等待 1.5 秒。")
        node = self.collector.snapshot()["nodes"]["node1"]
        self.assertEqual(node["phase"], "retrying")
        self.assertEqual(node["retry"]["wait_seconds"], 1.5)
        self.assertTrue(0 <= node["retry"]["remaining_seconds"] <= 1.5)

    def test_master_worker_has_independent_progress_and_retry_lifecycle(self):
        self.manifest("MES")
        self.manifest("SDR")
        self.manifest("REM")
        self.start("node1", "MES")
        self.start("node2", "SDR")
        self.start("master", "REM")
        self.event(f"网络恢复等待 master REM/{BOUNDARY}，重试 1/4，等待 300 秒。")
        waiting = self.collector.snapshot()
        self.assertEqual(waiting["nodes"]["master"]["phase"], "retrying")
        self.assertEqual(waiting["nodes"]["master"]["job"], "REM/" + BOUNDARY)
        self.assertEqual(waiting["nodes"]["node1"]["job"], "MES/" + BOUNDARY)
        self.assertEqual(waiting["nodes"]["node2"]["job"], "SDR/" + BOUNDARY)
        self.assertEqual(waiting["failed_jobs"], [])
        from watch_hls_progress import render
        self.assertIn("master（管理节点）", render(waiting))
        self.event(f"网络恢复重试 master REM/{BOUNDARY}，重试 1/4。")
        self.assertEqual(self.collector.snapshot()["nodes"]["master"]["phase"], "downloading")
        self.end("master", "REM")
        finished = self.collector.snapshot()
        self.assertIsNone(finished["nodes"]["master"]["retry"])
        self.assertEqual(finished["completed_scenes"], 2)
        self.assertEqual(finished["total_scenes"], 12)
        self.assertEqual(finished["completed_jobs"], 1)

    def test_only_fixed_worker_names_can_claim_jobs(self):
        self.manifest()
        for unknown in ("node3", "master-other", "master2"):
            self.start(unknown, "MES")
            self.event(f"网络恢复等待 {unknown} MES/{BOUNDARY}，重试 1/4，等待 300 秒。")
        value = self.collector.snapshot()
        self.assertEqual(set(value["nodes"]), {"node1", "node2", "master"})
        self.assertTrue(all(record["job"] is None for record in value["nodes"].values()))


if __name__ == "__main__":
    unittest.main()
