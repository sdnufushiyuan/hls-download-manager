import json
from pathlib import Path
import tempfile
import unittest
import sys

from tools.build_proxy_config import build
from tools.install_services import render, word
from tools.check_public_tree import issues


class ProjectToolsTests(unittest.TestCase):
    def test_proxy_builder_keeps_all_network_listeners_on_loopback(self):
        nodes=[{'name':'example-node','type':'ss','server':'proxy.example.org','port':443,'password':'fake'}]
        routing,optimizer=build(nodes,'example-node',Path('/opt/manager'),Path('/opt/manager/private'),{},'fake')
        self.assertEqual(routing['external-controller'],'127.0.0.1:19091')
        self.assertTrue(all(listener['listen']=='127.0.0.1' for listener in routing['listeners']))
        self.assertEqual(optimizer['api_secret'],'fake')
        self.assertEqual(routing['proxy-groups'][-1]['interval'],30)

    def test_unknown_or_duplicate_nodes_do_not_generate_configuration(self):
        node={'name':'one','type':'ss'}
        for nodes,initial in [([node,node],'one'),([node],'missing'),([dict(node,**{'dialer-proxy':'external'})],'one')]:
            with self.assertRaises(ValueError):build(nodes,initial,Path('/opt/x'),Path('/opt/x/private'),{},'fake')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux systemd path semantics')
    def test_renderer_uses_supplied_paths_and_keeps_explicit_start_gate(self):
        root=Path('/opt/hls manager')
        config=json.loads((Path(__file__).parent/'examples/deployment.example.json').read_text())
        units=render(config,root)
        self.assertIn('"/opt/hls manager/.venv/bin/python"',units['hls-archive.service'])
        self.assertIn('WorkingDirectory=/opt/hls manager\n',units['hls-archive.service'])
        self.assertIn('RestartPreventExitStatus=1',units['hls-archive.service'])
        self.assertTrue(all('ConditionHost=master' in unit for unit in units.values()))
        self.assertIn('--workers" "4"',units['hls-archive.service'])
        self.assertIn('--schedule" "smallest-first"',units['hls-archive.service'])
        self.assertIn('--schedule" "fair"',render(dict(config,schedule='fair'),root)['hls-archive.service'])
        with self.assertRaises(ValueError):render(dict(config,schedule='unknown'),root)
        daily=units['hls-daily-mosaic.service']
        self.assertIn('hls_daily_mosaic.py',daily)
        self.assertIn('MemoryMax=512M',daily)
        self.assertIn('CPUQuota=50%',daily)
        self.assertIn('"--grouping" "region-day"',daily)
        self.assertIn('"--fuse-products"',daily)
        with self.assertRaises(ValueError):render(dict(config,daily_mosaic=dict(config['daily_mosaic'],grouping='track-day')),root)
        with self.assertRaises(ValueError):render(dict(config,daily_mosaic={'output_root':config['output_root'],'state_root':'/opt/state'}),root)
        with self.assertRaises(ValueError):render(dict(config,output_root='/tmp/unverified'),root)

    def test_unit_values_reject_control_characters_and_escape_percent(self):
        self.assertEqual(word('/opt/100% done'), '"/opt/100%% done"')
        with self.assertRaises(ValueError):word('x\nExecStart=unexpected')

    def test_public_check_allows_only_examples_and_detects_secret_artifacts(self):
        self.assertEqual(issues(Path('examples/a.txt'),'https://proxy.example.org 127.0.0.1 sender@example.org'),[])
        self.assertIn('private/runtime directory',issues(Path('private/config.json'),'{}'))
        self.assertIn('private key',issues(Path('a.txt'),'-----BEGIN '+'OPENSSH PRIVATE KEY-----'))
        self.assertIn('GitHub token',issues(Path('a.txt'),'ghp_'+'a'*30))


if __name__=='__main__':unittest.main()
