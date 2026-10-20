"""ETA safety and journal deduplication; no network, SSH, credentials or NAS."""
from datetime import datetime, timezone
from contextlib import redirect_stdout
import io
import json
import unittest
from unittest.mock import patch

from hls_progress_rates import BlockActivity, ProgressRates
from watch_hls_progress import render


def snapshot(done=100, total=1000):
    return {"completed_scenes": done, "total_scenes": total, "archive_complete": False,
            "service": {"active": "active", "invocation_id": "run"},
            "proxy": {"active": "active", "invocation_id": "proxy", "route": "linux_mihomo"},
            "nas": {"mounted": True}, "nodes": {n: {"phase": "downloading"} for n in ("node1", "node2", "master")},
            "failed_jobs": [], "warnings": []}


class RateTests(unittest.TestCase):
    def test_warmup_and_eta_use_candidates_not_tiles(self):
        rates = ProgressRates()
        self.assertEqual(rates.update(snapshot(), now=0)["status"], "sampling")
        self.assertIsNone(rates.update(snapshot(102), now=60)["remaining_seconds"])
        s = snapshot(106);s["download_activity"] = {"committed_blocks": 999999}
        wall = datetime(2026, 10, 19, tzinfo=timezone.utc)
        value = rates.update(s, now=120, wall_now=wall)
        self.assertEqual(value["status"], "ready")
        self.assertEqual(value["candidates_per_minute"], 3)
        self.assertEqual(value["candidates_per_day"], 4320)
        self.assertEqual(value["remaining_seconds"], 894 * 20)
        self.assertTrue(value["estimated_finish_utc"].startswith("2026-10-19T04:58:00"))

    def test_zero_progress_never_means_finished_or_infinite_date(self):
        rates = ProgressRates();rates.update(snapshot(), now=0)
        value = rates.update(snapshot(), now=120)
        self.assertEqual(value["candidates_per_day"], 0)
        self.assertEqual(value["status"], "waiting_progress")
        self.assertIsNone(value["remaining_seconds"])
        self.assertIsNone(value["estimated_finish_utc"])

    def test_regression_restart_route_total_and_time_gaps_reset_samples(self):
        for change in ("regression", "invocation", "proxy", "total", "gap", "clock"):
            with self.subTest(change=change):
                rates = ProgressRates();rates.update(snapshot(), now=0)
                rates.update(snapshot(106), now=120)
                s = snapshot(107);now=130
                if change == "regression":s["completed_scenes"] = 99
                elif change == "invocation":s["service"]["invocation_id"] = "new"
                elif change == "proxy":s["proxy"]["invocation_id"] = "new"
                elif change == "total":s["total_scenes"] = 2000
                elif change == "gap":now = 2000
                else:now = 110
                value = rates.update(s, now=now)
                self.assertEqual(value["status"], "sampling")
                self.assertIsNone(value["remaining_seconds"])

    def test_waits_and_failures_suspend_eta(self):
        for condition in ("stopped", "nas", "failed", "all_retry", "warning", "proxy"):
            with self.subTest(condition=condition):
                rates=ProgressRates();rates.update(snapshot(), now=0);s=snapshot(106)
                if condition == "stopped":s["service"]["active"]="inactive"
                elif condition == "nas":s["nas"]["mounted"]=False
                elif condition == "failed":s["failed_jobs"]=["MES/month"]
                elif condition == "all_retry":
                    for n in s["nodes"].values():n["phase"]="retrying"
                elif condition == "warning":s["warnings"]=["record unreadable"]
                else:s["proxy"]["active"]="activating"
                value=rates.update(s, now=120)
                self.assertNotEqual(value["status"], "ready")
                self.assertIsNone(value["remaining_seconds"])

    def test_single_node_retry_keeps_real_wall_time_in_average(self):
        rates=ProgressRates();rates.update(snapshot(), now=0);s=snapshot(106)
        s["nodes"]["node1"]["phase"]="retrying"
        self.assertEqual(rates.update(s, now=120)["status"], "ready")
        for n in s["nodes"].values():n["phase"]="retrying"
        self.assertEqual(rates.update(s, now=180)["status"], "paused")
        s["nodes"]["node1"]["phase"]="downloading";s["completed_scenes"]=112
        self.assertEqual(rates.update(s, now=240)["candidates_per_minute"], 3)

    def test_rolling_window_drops_old_fast_burst(self):
        rates=ProgressRates(window=300)
        for t,done in [(0,100),(120,160),(240,166),(360,172),(480,178)]:
            value=rates.update(snapshot(done), now=t)
        self.assertEqual(value["window_seconds"], 240)
        self.assertEqual(value["candidates_per_minute"], 3)

    def test_full_inventory_requires_final_archive_confirmation(self):
        rates=ProgressRates()
        self.assertEqual(rates.update(snapshot(1000), now=0)["status"], "verifying")
        s=snapshot(1000);s["archive_complete"]=True
        self.assertEqual(rates.update(s, now=1)["remaining_seconds"], 0)

    def test_bad_inventory_counters_do_not_produce_eta(self):
        for done,total in [(True,1000),(1001,1000),(-1,1000),(1,0)]:
            self.assertEqual(ProgressRates().update(snapshot(done,total),now=0)["status"],"unavailable")

    def test_rate_renderer_exposes_units_and_sampling_limits(self):
        s=snapshot();s["download_activity"]={"blocks_per_minute":90,"window_seconds":300}
        rates=ProgressRates();s["throughput"]=rates.update(s,now=0)
        text=render(s,width=140)
        self.assertIn("90.0 块/分钟",text);self.assertIn("采样中",text)
        s["completed_scenes"]=106;s["throughput"]=rates.update(s,now=120)
        text=render(s,width=140)
        self.assertIn("景/小时",text);self.assertIn("预计剩余：约",text);self.assertIn("预计完成",text)

    def test_region_status_renderer_handles_idle_nodes_and_ready_region(self):
        s=snapshot();s['nodes']={'master':{'job':None}}
        s['regions']=[{'code':'RIS','completed_scenes':10,'total_scenes':10,
                       'completed_jobs':1,'total_jobs':1,'ready_for_processing':True}]
        self.assertIn('已完成，可后续处理',render(s,width=140))
        s['regions'][0]['ready_for_processing']=False
        s['nodes']['master']['job']='RIS/2020-01-01__2020-02-01'
        self.assertIn('下载/核验中',render(s,width=140))

    def test_existing_json_command_includes_new_fields_without_continuous_loop(self):
        import watch_hls_progress as watch
        output=io.StringIO()
        with patch.object(watch,"ProgressCollector") as collector, patch.object(watch,"BlockActivity") as activity:
            collector.return_value.snapshot.return_value=snapshot()
            activity.return_value.snapshot.return_value={"blocks_per_minute":90,"window_seconds":300}
            with redirect_stdout(output):self.assertEqual(watch.main(["--json"]),0)
        value=json.loads(output.getvalue())
        self.assertEqual(value["download_activity"]["blocks_per_minute"],90)
        self.assertEqual(value["throughput"]["status"],"sampling")
        collector.return_value.snapshot.assert_called_once()


def event(cursor, stamp, message="  1/24 r000512_c000512.tif 已下载；有效像元 123", invocation="run"):
    return json.dumps({"__CURSOR":cursor,"__REALTIME_TIMESTAMP":str(int(stamp*1e6)),
                       "MESSAGE":message,"_SYSTEMD_USER_INVOCATION_ID":invocation})


class BlockTests(unittest.TestCase):
    def test_recent_commits_only_not_cache_retry_or_other_invocation(self):
        raw="\n".join([event("a",900),event("b",999),event("c",600),
            event("old",990,invocation="previous"),event("cache",995,"  1/24 r000512_c000512.tif 已校验，复用"),
            event("retry",996,"网络暂时中断（HTTPError）"),"bad-json"])
        meter=BlockActivity(runner=lambda args:raw);value=meter.snapshot(snapshot(),now=1000)
        self.assertEqual(value["committed_blocks"],2)
        self.assertEqual(value["blocks_per_minute"],0.4)

    def test_cursor_replay_and_rotation_do_not_double_count(self):
        responses=iter([event("a",900),event("a",900)+"\n"+event("b",1001)])
        calls=[]
        def run(args):calls.append(args);return next(responses)
        meter=BlockActivity(runner=run);meter.snapshot(snapshot(),now=1000)
        value=meter.snapshot(snapshot(),now=1005)
        self.assertEqual(value["committed_blocks"],2)
        self.assertIn("--after-cursor=a",calls[1])

    def test_journal_error_is_unavailable_and_can_recover_without_duplicates(self):
        calls=[event("a",900),RuntimeError("secret should never render"),event("a",900)+"\n"+event("b",1010)]
        def run(args):
            item=calls.pop(0)
            if isinstance(item,Exception):raise item
            return item
        meter=BlockActivity(runner=run);meter.snapshot(snapshot(),now=1000)
        self.assertIsNone(meter.snapshot(snapshot(),now=1005)["blocks_per_minute"])
        self.assertEqual(meter.snapshot(snapshot(),now=1015)["committed_blocks"],2)

    def test_route_change_resets_history_and_waits_for_new_sample(self):
        raw=[event("a",990),event("a",990)+"\n"+event("b",1020),event("b",1020)]
        meter=BlockActivity(runner=lambda args:raw.pop(0));meter.snapshot(snapshot(),now=1000)
        s=snapshot();s["proxy"]["invocation_id"]="next"
        value=meter.snapshot(s,now=1010)
        self.assertEqual(value["status"],"sampling")
        self.assertEqual(value["committed_blocks"],0)
        value=meter.snapshot(s,now=1040)
        self.assertEqual(value["committed_blocks"],1)
        self.assertEqual(value["blocks_per_minute"],2)

    def test_expired_events_drop_out(self):
        raw=[event("a",900),""]
        meter=BlockActivity(runner=lambda args:raw.pop(0));meter.snapshot(snapshot(),now=1000)
        self.assertEqual(meter.snapshot(snapshot(),now=1201)["committed_blocks"],0)

    def test_stopped_service_makes_no_journal_call(self):
        def forbidden(args):raise AssertionError("must not query")
        s=snapshot();s["service"]["active"]="inactive"
        self.assertEqual(BlockActivity(runner=forbidden).snapshot(s,now=1000)["status"],"unavailable")


if __name__ == "__main__":
    unittest.main()
