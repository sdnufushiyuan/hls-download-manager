"""Region priority, validation barriers and cancellation without EE or NAS."""
from contextlib import nullcontext, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import run_hls_archive as a
import run_hls_cluster as c
from test_run_hls_archive import fixture


class RegionPriorityTests(unittest.TestCase):
    def test_remaining_work_reorders_regions_but_keeps_every_month(self):
        inv = a.parse_inventory(fixture())
        jobs = a.fair_jobs(inv, '/nasdata/test')
        def hint(job):
            return job.scene_count if job.code == 'MES' else 5 if job.code == 'TXS' else 0
        with patch.object(a, 'processed_scene_hint', side_effect=hint):
            ordered, ranking = a.smallest_region_jobs(inv, jobs, read_hints=True, pending_first=True)
        self.assertEqual([row['code'] for row in ranking][:2], ['MES', 'TXS'])
        self.assertEqual(ranking[0]['remaining_candidate_hint'], 0)
        self.assertCountEqual([id(job) for job in ordered], [id(job) for job in jobs])
        self.assertEqual([job.code for job in ordered[:4]], ['MES'] * 4)
        self.assertEqual([job.code for job in ordered[4:8]], ['TXS'] * 4)
        # A stale 'complete' hint still runs the full original month job.
        seen = []
        result = a.run_region_jobs(ordered, ['master'], lambda job: seen.append(job.name) or 0, emit=lambda _: None)
        self.assertEqual(len(result), len(jobs))
        self.assertEqual(seen, [job.name for job in ordered])

    def test_partial_hint_is_bounded_and_used_only_to_estimate_order(self):
        with tempfile.TemporaryDirectory() as folder:
            inv = a.parse_inventory(fixture())
            job = replace(a.fair_jobs(inv, '/nasdata/test')[0], out=folder)
            path = Path(folder) / 'download_selection_summary.json'
            for value, expected in [(4, 4), (10, 10), (True, 0), (-1, 0), (11, 0)]:
                path.write_text(json.dumps({'policy':'clear-land-exact-time-unique-pixels-v1',
                                            'processed_scenes':value}))
                self.assertEqual(a.processed_scene_hint(job), expected)
            path.write_text(' ' * 4097)
            self.assertEqual(a.processed_scene_hint(job), 0)

    def test_plan_reads_no_output_hints_and_orders_whole_regions(self):
        with patch('sys.stdin', io.StringIO(json.dumps(fixture()))), \
             patch.object(a, 'processed_scene_hint', side_effect=AssertionError('NAS access')), \
             patch.object(a, 'ArchiveRunner') as runner, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(a.main(['--inventory','-','--out','/nasdata/test',
                                     '--schedule','smallest-first','--plan']), 0)
        runner.assert_not_called()
        summary, _ = json.JSONDecoder().raw_decode(output.getvalue())
        self.assertEqual(summary['schedule'], 'smallest-first')
        self.assertEqual(summary['region_priority_inventory_only'][-1]['code'], 'MES')
        self.assertEqual(len(summary['jobs']), 24)
        self.assertEqual(len({row['region'] for row in summary['jobs'][:4]}), 1)

    def test_barrier_waits_for_last_month_while_other_nodes_are_idle(self):
        jobs = a.fair_jobs(a.parse_inventory(fixture()), '/nasdata/test')
        first = [job for job in jobs if job.code == 'RIS'][:3]
        second = [job for job in jobs if job.code == 'MES'][:3]
        release, two_done, next_started = threading.Event(), threading.Event(), threading.Event()
        lock, done, results = threading.Lock(), [], {}
        def runner(job):
            if job.code == 'MES':
                next_started.set()
                return 0
            if job.name == first[0].name:
                if not release.wait(5):
                    return 1
            with lock:
                done.append(job.name)
                if len(done) == 2:
                    two_done.set()
            return 0
        def run():
            results.update(a.run_region_jobs(first + second, c.NODES, runner, emit=lambda _: None))
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(two_done.wait(3))
            self.assertFalse(next_started.wait(0.05))
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 6)
        self.assertTrue(next_started.is_set())

    def test_failure_never_announces_ready_or_starts_next_region(self):
        jobs = a.fair_jobs(a.parse_inventory(fixture()), '/nasdata/test')
        jobs = [next(j for j in jobs if j.code == code) for code in ('RIS', 'MES')]
        for status in (1, 75):
            seen, messages = [], []
            result = a.run_region_jobs(jobs, ['master'], lambda job: seen.append(job.code) or status,
                                       messages.append, network_job_retries=0)
            self.assertEqual(seen, ['RIS'])
            self.assertEqual(list(result.values()), [status])
            self.assertFalse(any(message.startswith('区域完成 ') for message in messages))

    def test_runtime_uses_barrier_and_keeps_pending_first_inside_region(self):
        observed = []
        def run(jobs, *args, **kwargs):
            observed.extend(jobs)
            return {job.name: 0 for job in jobs}
        with patch('sys.stdin', io.StringIO(json.dumps(fixture()))), \
             patch.object(c, 'require_master'), patch.object(c, 'output_lock', return_value=nullcontext()), \
             patch.object(a, 'ArchiveRunner', return_value=Mock()), patch.object(a, 'preflight'), \
             patch.object(a.signal, 'signal'), patch.object(a, 'processed_scene_hint',
                 side_effect=lambda job: job.scene_count if job.start.day == 15 else 0), \
             patch.object(a, 'prioritize_pending_jobs', side_effect=AssertionError('global partition')), \
             patch.object(a, 'run_region_jobs', side_effect=run) as barrier, redirect_stdout(io.StringIO()):
            self.assertEqual(a.main(['--inventory','-','--out','/nasdata/test','--schedule','smallest-first',
                                     '--resume-pending-first','--run']), 0)
        barrier.assert_called_once()
        self.assertEqual(len(observed), 24)
        for offset in range(0, 24, 4):
            group = observed[offset:offset+4]
            self.assertEqual(len({job.code for job in group}), 1)
            self.assertEqual(group[-1].start.day, 15)


if __name__ == '__main__':
    unittest.main()
