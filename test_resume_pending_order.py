"""Offline ordering checks: completion hints never remove or bypass jobs."""
import io,json,tempfile,unittest
from pathlib import Path
from datetime import date
from contextlib import nullcontext,redirect_stdout
from unittest.mock import patch,Mock
import run_hls_archive as a
import run_hls_cluster as c
from test_run_hls_archive import fixture

class ResumeOrderChecks(unittest.TestCase):
    def jobs(self,folder,count):
        return [a.Job('MES',date(2020,i+1,1),date(2020,i+2,1),str(Path(folder)/str(i)),10) for i in range(count)]
    def write(self,job,value):
        p=Path(job.out);p.mkdir(exist_ok=True)
        (p/'download_selection_summary.json').write_text(json.dumps(value))
    def test_stable_partition_keeps_every_identical_job_and_original_queue(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs=self.jobs(folder,5);original=list(jobs)
            for i in [0,3]:self.write(jobs[i],{'policy':'clear-land-exact-time-unique-pixels-v1','processed_scenes':10})
            self.write(jobs[1],{'policy':'clear-land-exact-time-unique-pixels-v1','processed_scenes':9})
            self.write(jobs[2],{'policy':'unknown','processed_scenes':10})
            result=a.prioritize_pending_jobs(jobs)
            self.assertEqual(result,[jobs[i] for i in [1,2,4,0,3]])
            self.assertEqual(jobs,original)
            self.assertCountEqual([id(x) for x in result],[id(x) for x in jobs])
    def test_malformed_oversized_and_wrong_type_hints_remain_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs=self.jobs(folder,5)
            for job in jobs:Path(job.out).mkdir()
            hints=['{',' '*5000,json.dumps({'policy':'clear-land-exact-time-unique-pixels-v1','processed_scenes':True}),json.dumps({'policy':'clear-land-exact-time-unique-pixels-v1','processed_scenes':11}),'[]']
            for job,text in zip(jobs,hints):(Path(job.out)/'download_selection_summary.json').write_text(text)
            self.assertEqual(a.prioritize_pending_jobs(jobs),jobs)
    def test_hint_alone_never_bypasses_runner_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs=self.jobs(folder,2)
            self.write(jobs[0],{'policy':'clear-land-exact-time-unique-pixels-v1','processed_scenes':10})
            called=[]
            result=a.run_jobs(a.prioritize_pending_jobs(jobs),['master'],lambda job:called.append(job.out) or (1 if job.out==jobs[0].out else 0),emit=lambda _:None)
            self.assertEqual(called,[jobs[1].out,jobs[0].out])
            self.assertEqual(result[jobs[0].name],1)
    def test_legacy_month_markers_are_deferred_without_being_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs=self.jobs(folder,2)
            scenes=[{'sensor':'L30','index':'scene%d'%i} for i in range(10)]
            directory=Path(jobs[0].out);directory.mkdir()
            (directory/'run_manifest.json').write_text(json.dumps({'scenes':scenes}))
            for scene in scenes:
                marker=directory/'L30'/scene['index']/'scene.json';marker.parent.mkdir(parents=True)
                marker.write_text('not trusted as validation')
            self.assertEqual(a.prioritize_pending_jobs(jobs),[jobs[1],jobs[0]])
            (directory/'L30/scene9/scene.json').unlink()
            self.assertEqual(a.prioritize_pending_jobs(jobs),jobs)
    def test_legacy_unsafe_scene_path_and_wrong_count_are_not_completion_hints(self):
        with tempfile.TemporaryDirectory() as folder:
            jobs=self.jobs(folder,2)
            directory=Path(jobs[0].out);directory.mkdir()
            manifest=directory/'run_manifest.json'
            manifest.write_text(json.dumps({'scenes':[{'sensor':'L30','index':'../escape'}]*10}))
            self.assertEqual(a.prioritize_pending_jobs(jobs),jobs)
            manifest.write_text(json.dumps({'scenes':[{'sensor':'L30','index':'valid'}]*9}))
            self.assertEqual(a.prioritize_pending_jobs(jobs),jobs)
    def test_plan_never_reads_resume_hints_or_connects(self):
        with patch('sys.stdin',io.StringIO(json.dumps(fixture()))),patch.object(a,'prioritize_pending_jobs') as priority,patch.object(a,'ArchiveRunner') as runner,redirect_stdout(io.StringIO()):
            self.assertEqual(a.main(['--inventory','-','--out','/nasdata/test','--resume-pending-first','--plan']),0)
        priority.assert_not_called();runner.assert_not_called()
    def test_opt_in_runtime_order_is_used_for_same_complete_job_set(self):
        observed=[]
        def reorder(jobs):return list(reversed(jobs))
        def run(jobs,*args,**kwargs):observed.extend(jobs);return {job.name:0 for job in jobs}
        inventory=a.parse_inventory(fixture());original=a.fair_jobs(inventory,'/nasdata/test')
        with patch('sys.stdin',io.StringIO(json.dumps(fixture()))),patch.object(c,'require_master'),patch.object(c,'output_lock',return_value=nullcontext()),patch.object(a,'ArchiveRunner',return_value=Mock()),patch.object(a,'preflight'),patch.object(a.signal,'signal'),patch.object(a,'prioritize_pending_jobs',side_effect=reorder),patch.object(a,'run_jobs',side_effect=run),redirect_stdout(io.StringIO()):
            self.assertEqual(a.main(['--inventory','-','--out','/nasdata/test','--resume-pending-first','--run']),0)
        self.assertEqual(observed,list(reversed(original)))

if __name__=='__main__':unittest.main()
