from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import hls_system_monitor as monitor


class MonitorTests(unittest.TestCase):
    def test_archive_view_uses_small_summaries_and_never_scene_or_block_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'download_selection_summary.json').write_text(json.dumps({
                'policy':'clear-land-exact-time-unique-pixels-v1','processed_scenes':2}))
            from unittest.mock import Mock
            collector=SimpleNamespace(_configuration_warnings=[],_job_status={},_active_nodes={'master':'RIS/month'},
                _retries={},_current_failed=set(),months={'RIS/month':SimpleNamespace(path=root,code='RIS',expected=3)},
                _service=Mock(return_value={'active':'active'}),_proxy=Mock(return_value={}),
                _journal=Mock(),_nas=Mock(return_value={'mounted':True}),
                _recover=Mock(side_effect=AssertionError('must not scan scenes')))
            result=monitor.archive_snapshot(collector)
            self.assertEqual(result['completed_scenes'],2)
            self.assertEqual(result['nodes']['master']['job'],'RIS/month')
            self.assertFalse(result['archive_complete'])
            self.assertEqual(result['count_mode'],'download-summary-hints')
            collector._recover.assert_not_called()

    def test_complete_month_hint_shows_reuse_not_fresh_download_and_keeps_safety_states(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'download_selection_summary.json').write_text(json.dumps({
                'policy':'clear-land-exact-time-unique-pixels-v1','processed_scenes':3}))
            collector=SimpleNamespace(_configuration_warnings=[],_job_status={},_active_nodes={'master':'RIS/month'},
                _retries={},_current_failed=set(),months={'RIS/month':SimpleNamespace(path=root,code='RIS',expected=3)},
                _service=Mock(return_value={'active':'active'}),_proxy=Mock(return_value={}),
                _journal=Mock(),_nas=Mock(return_value={'mounted':True}))
            result=monitor.archive_snapshot(collector)
            self.assertEqual(result['nodes']['master']['phase'],'verifying')
            self.assertFalse(result['archive_complete'])
            self.assertIn('断点复核/复用','\n'.join(monitor.overview({'archive':result})))
            collector._retries={'master':{'waiting':True}}
            self.assertEqual(monitor.archive_snapshot(collector)['nodes']['master']['phase'],'retrying')
            collector._service.return_value={'active':'failed'}
            self.assertEqual(monitor.archive_snapshot(collector)['nodes']['master']['phase'],'stopped')

    def test_credentials_are_redacted_but_other_users_commands_remain_readable(self):
        command='python work.py --password "two words" --token=fake-token https://user:fake-pass@example.org/?access_token=fake-access'
        result=monitor.scrub(command)
        self.assertIn('python work.py',result)
        for value in ['two words','fake-token','fake-pass','fake-access']:
            self.assertNotIn(value,result)
        self.assertNotIn('fake-bearer',monitor.scrub('Authorization: Bearer fake-bearer'))
        self.assertNotIn('secret words',monitor.scrub("PASSWORD='secret words' python task.py"))

    @unittest.skipUnless(__import__('sys').platform.startswith('linux'),'Linux proc sampling')
    def test_cpu_is_sampled_and_pid_reuse_cannot_inherit_another_process_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'net').mkdir();(root/'42').mkdir()
            (root/'uptime').write_text('1000 1000')
            (root/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 400 kB\n')
            (root/'net/dev').write_text('header\nheader\n eth0: 100 0 0 0 0 0 0 0 200 0 0 0 0 0 0 0\n')
            (root/'42/cmdline').write_bytes(b'python\0worker.py\0--token=fake-value\0')
            def stat(cpu,start):
                fields=['S']+['0']*21;fields[11]=str(cpu);fields[12]='0';fields[19]=str(start);fields[21]='10'
                (root/'42/stat').write_text('42 (python (worker)) '+' '.join(fields))
            (root/'stat').write_text('cpu 100 0 0 800 100 0 0 0\n');stat(100,10)
            sampler=monitor.HostSampler(root);sampler.hz=100
            with patch.object(monitor.time,'monotonic',side_effect=[0,2,4]):
                initial=sampler.sample();self.assertIsNone(initial['cpu'])
                (root/'stat').write_text('cpu 200 0 0 850 150 0 0 0\n');stat(200,10)
                next_view=sampler.sample()
                self.assertAlmostEqual(next_view['cpu'],50)
                self.assertAlmostEqual(next_view['processes'][0]['cpu'],50)
                self.assertNotIn('fake-value',next_view['processes'][0]['command'])
                stat(900,20);self.assertIsNone(sampler.sample()['processes'][0]['cpu'])

    def test_all_users_and_remote_hosts_are_searchable(self):
        data={'hosts':{'master':{'processes':[dict(pid=1,user='alice',command='python first.py',cpu=1,rss=1024,age=300)]},
                       'node2':{'processes':[dict(pid=9,user='bob',command='Rscript analysis.R',cpu=80,rss=4096,age=600)]}}}
        self.assertEqual(len(monitor.process_rows(data)),2)
        self.assertEqual(monitor.process_rows(data,'bob')[0]['host'],'node2')
        self.assertEqual(monitor.process_rows(data,'analysis.R')[0]['user'],'bob')
        self.assertIn('bob','\n'.join(monitor.view_lines(data,4)))

    def test_eta_requires_observed_progress_and_resets_on_counter_rollback(self):
        rate=monitor.Rate()
        self.assertIsNone(rate.sample('RIS',100,200,now=0))
        self.assertIsNone(rate.sample('RIS',101,200,now=30))
        result=rate.sample('RIS',110,200,now=120)
        self.assertAlmostEqual(result['per_minute'],5)
        self.assertAlmostEqual(result['remaining_seconds'],1080)
        self.assertIsNone(rate.sample('RIS',1,200,now=130))

    def test_service_inventory_only_issues_read_only_list_commands(self):
        with patch.object(monitor,'run',return_value=json.dumps([{'unit':'task.service','active':'active'}])) as run:
            result=monitor.list_services()
        self.assertEqual(len(result),2)
        self.assertTrue(all('list-units' in call.args[0] for call in run.call_args_list))
        self.assertFalse(any('start' in call.args[0] for call in run.call_args_list))

    def test_qa_and_completed_daily_outputs_are_distinct_in_plain_view(self):
        value={'qa':{'phase':'original-qa','region':'RIS'},
               'regions':{'RIS':{'completed_days':228,'total_days':1816,'phase':'original-qa'}},
               'daily_counts':{'L30':0,'S30':0,'FUSED':0}}
        text='\n'.join(monitor.overview(value))
        self.assertIn('228/1816',text);self.assertIn('L30 0张',text)
        self.assertIn('旧试算图不计入',text)

    def test_scope_banner_uses_explicit_region_exclusions_instead_of_hardcoded_RIS(self):
        excluded={'month':'REM/2026-10-01__2026-10-10','reason':'explicit user scope'}
        value={'qa':{'phase':'watching','regions':{'REM':{'authorized_scope_exclusions':[excluded]}}},
               'regions':{'REM':{'excluded_months':[excluded]}}}
        text='\n'.join(monitor.overview(value))
        self.assertEqual(text.count(excluded['month']),1)
        self.assertNotIn('RIS后处理已排除',text)
        self.assertIn('旧试算图不计入',text)

    def test_only_configured_project_node_aliases_are_probed(self):
        config=dict(program_root='/opt/hls',archive_run_root='/opt/hls/run',archive_output_root='/nasdata/archive',
                    auto_state_root='/opt/hls/auto',daily_output_root='/nasdata/daily',nas_source='//NAS_HOST/data',hosts=['node1'])
        monitor.validate(config)
        with self.assertRaises(ValueError):monitor.validate(dict(config,hosts=['-oProxyCommand=bad']))


if __name__=='__main__':unittest.main()
