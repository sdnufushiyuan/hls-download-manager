from datetime import date
from pathlib import Path
import tempfile
import json
import os
import ssl
import unittest
from unittest.mock import Mock, patch
import numpy as np
import rasterio
from rasterio.transform import Affine
from shapely.geometry import box
import hls_auto_daily as auto
import download_hls_optram as h
import run_hls_archive as archive
import requests
from urllib3.exceptions import MaxRetryError, SSLError as UrllibSSLError


class AutoDailyTests(unittest.TestCase):
    def wrapped_ssl(self, inner):
        return requests.exceptions.SSLError(MaxRetryError(
            None, 'https://example.org/?token=must-not-be-logged', reason=UrllibSSLError(inner)))

    def test_tls_diagnostics_walk_wrapped_errors_without_disclosing_messages(self):
        error = self.wrapped_ssl(ssl.SSLEOFError(8, 'signed-url-and-token-secret'))
        error._hls_qa_stage = 'qa-tiff-transfer'
        detail = auto.safe_error(error)
        self.assertEqual(detail['category'], 'tls-eof')
        self.assertEqual(detail['stage'], 'qa-tiff-transfer')
        self.assertTrue(auto.qa_transient(error))
        self.assertNotIn('secret', json.dumps(detail))
        self.assertNotIn('token', json.dumps(detail))
        self.assertNotIn('example.org', json.dumps(detail))
        error._hls_qa_stage = 'https://example.org/private'
        self.assertNotIn('stage', auto.safe_error(error))

    def test_wrapped_certificate_failure_is_terminal_without_reset(self):
        error = self.wrapped_ssl(ssl.SSLCertVerificationError(1, 'private certificate message'))
        tile = h.Tile(0,0,0,30,1,1)
        session = Mock()
        with patch.object(auto, '_download_chunk_once', side_effect=error) as transfer, \
                patch.object(auto, 'retry_wait') as wait:
            with self.assertRaises(requests.exceptions.SSLError):
                auto.download_chunk(None,tile,Path('/qa.tif'),'EPSG:32614',session,{},lambda:None)
        self.assertEqual(transfer.call_count,1)
        wait.assert_not_called();session.close.assert_not_called()
        self.assertEqual(auto.safe_error(error)['category'],'tls-certificate')

    def test_local_io_and_http_denial_cannot_inherit_tls_retry(self):
        eof = ssl.SSLEOFError(8, 'private')
        disk = OSError(28, 'disk full');disk.__context__ = eof
        self.assertFalse(auto.qa_transient(disk))
        self.assertNotIn('category',auto.tls_detail(disk))
        response = requests.Response();response.status_code = 403
        denied = requests.exceptions.HTTPError(response=response);denied.__context__ = eof
        self.assertFalse(auto.qa_transient(denied))

    def test_native_tls_eof_gets_bounded_retry_and_ee_socket_reset(self):
        tile = h.Tile(0,0,0,30,1,1)
        error = ssl.SSLEOFError(8, 'private');error._hls_qa_stage = 'ee-get-download-url'
        connection = Mock();transport = Mock(connections={'host': connection})
        credentials = transport.credentials
        from ee import _state
        with patch.object(auto, '_download_chunk_once', side_effect=[error,None]) as transfer, \
                patch.object(auto, 'retry_wait'), \
                patch.object(_state,'get_state',return_value=Mock(http_transport=transport)):
            auto.download_chunk(None,tile,Path('/qa.tif'),'EPSG:32614',Mock(),{},lambda:None,Mock())
        self.assertEqual(transfer.call_count,2);connection.close.assert_called_once()
        self.assertEqual(transport.connections,{})
        self.assertIs(transport.credentials,credentials);transport.close.assert_not_called()

    def test_native_protocol_eof_is_retried_but_unknown_ssl_is_not(self):
        error=ssl.SSLError(1,'private protocol message')
        error.reason='UNEXPECTED_EOF_WHILE_READING'
        self.assertTrue(auto.qa_transient(error))
        self.assertEqual(auto.safe_error(error)['tls_reason'],'UNEXPECTED_EOF_WHILE_READING')
        unknown=ssl.SSLError(1,'private unknown message')
        self.assertFalse(auto.qa_transient(unknown))

    def test_eof_midstream_cleans_partial_and_retries_same_pixel_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tile=h.Tile(0,0,0,30,1,1);source=root/'source.tif'
            with rasterio.open(source,'w',driver='GTiff',width=1,height=1,count=1,dtype='uint8',
                    crs='EPSG:32614',transform=Affine(*tile.transform)) as ds:
                ds.write(np.array([[96]],dtype='uint8'),1)
            payload=source.read_bytes();error=self.wrapped_ssl(ssl.SSLEOFError(8,'private'))
            def interrupted(*args):
                yield payload[:20]
                raise error
            first=Mock(status_code=200);first.iter_content.side_effect=interrupted
            second=Mock(status_code=200);second.iter_content.return_value=[payload]
            contexts=[]
            for response in [first,second]:
                ctx=Mock();ctx.__enter__=Mock(return_value=response);ctx.__exit__=Mock(return_value=False);contexts.append(ctx)
            session=Mock();session.get.side_effect=contexts
            output=root/'qa.tif';reports=Mock()
            with patch.object(h,'get_download_url',return_value='https://example.org/?token=secret'), \
                    patch.object(auto,'retry_wait'):
                auto.download_chunk(None,tile,output,'EPSG:32614',session,{'fixed':'contract'},lambda:None,reports)
            session.close.assert_called_once()
            self.assertEqual(session.get.call_count,2)
            self.assertEqual(reports.call_args.kwargs['stage'],'qa-tiff-transfer')
            self.assertEqual(reports.call_args.kwargs['category'],'tls-eof')
            self.assertTrue(reports.call_args.kwargs['connection_reset'])
            self.assertTrue(auto.cached(output,{'fixed':'contract'}))
            self.assertFalse(list(root.glob('*.part')))
            with rasterio.open(output) as ds:np.testing.assert_array_equal(ds.read(1),[[96]])

    def test_tls_retry_exhaustion_is_finite_and_keeps_safe_context(self):
        error=self.wrapped_ssl(ssl.SSLEOFError(8,'private'));error._hls_qa_stage='qa-tiff-transfer'
        with patch.object(auto,'_download_chunk_once',side_effect=error) as transfer, \
                patch.object(auto,'retry_wait') as wait:
            with self.assertRaises(requests.exceptions.SSLError):
                auto.download_chunk(None,h.Tile(0,0,0,30,1,1),Path('/qa.tif'),'EPSG:32614',Mock(),{},lambda:None,Mock())
        self.assertEqual(transfer.call_count,4);self.assertEqual(wait.call_count,3)

    def test_proxy_is_explicit_for_both_schemes_even_with_no_proxy_environment(self):
        with patch.dict(os.environ,{'NO_PROXY':'*','HTTPS_PROXY':'http://example.org:9'}):
            with auto.qa_session('http://127.0.0.1:8118') as session:
                self.assertFalse(session.trust_env)
                for url in ['https://earthengine.googleapis.com/x','http://example.org/x']:
                    merged=session.merge_environment_settings(url,{},False,True,None)
                    self.assertEqual(merged['proxies'][url.split(':')[0]],'http://127.0.0.1:8118')

    def test_transient_download_retries_current_chunk_without_abandoning_region(self):
        tile=h.Tile(0,0,0,30,1,1);report=Mock()
        error=auto.QAHTTPError(502,'server-or-proxy-error')
        with patch.object(auto,'_download_chunk_once',side_effect=[error,None]) as transfer, \
                patch.object(auto,'retry_wait') as wait:
            auto.download_chunk(None,tile,Path('/example/qa.tif'),'EPSG:32614',None,{},lambda:None,report)
        self.assertEqual(transfer.call_count,2)
        self.assertEqual(transfer.call_args_list[0].args[2],transfer.call_args_list[1].args[2])
        self.assertEqual(report.call_args.kwargs['http_status'],502)
        wait.assert_called_once()

    def test_terminal_http_request_error_is_not_blindly_retried(self):
        tile=h.Tile(0,0,0,30,1,1)
        with patch.object(auto,'_download_chunk_once',side_effect=auto.QAHTTPError(400,'invalid-request')) as transfer, \
                patch.object(auto,'retry_wait') as wait:
            with self.assertRaises(auto.QAHTTPError):
                auto.download_chunk(None,tile,Path('/example/qa.tif'),'EPSG:32614',None,{},lambda:None)
        self.assertEqual(transfer.call_count,1);wait.assert_not_called()

    def test_error_diagnostics_keep_status_and_quota_but_never_signed_url(self):
        response=Mock(status_code=429,headers={'Retry-After':'120'})
        response.iter_content.return_value=[json.dumps({'error':{'status':'RESOURCE_EXHAUSTED',
            'message':'quota error https://example.org/?token=private-value'}}).encode()]
        with self.assertRaises(auto.QAHTTPError) as raised:auto.check_response(response)
        detail=auto.safe_error(raised.exception)
        self.assertEqual(detail['http_status'],429)
        self.assertEqual(detail['category'],'quota-or-rate-limit')
        self.assertNotIn('private-value',json.dumps(detail));self.assertEqual(raised.exception.retry_after,120)

    def test_explicit_last_month_exclusion_preserves_original_and_other_regions(self):
        sept=archive.Month(date(2026,9,1),date(2026,10,1),5)
        october=archive.Month(date(2026,10,1),date(2026,10,10),10)
        ris=archive.Region('RIS','/r.shp','EPSG:32614',1,(sept,october))
        txs=archive.Region('TXS','/t.shp','EPSG:32614',2,(sept,october))
        inventory=archive.Inventory(date(2026,9,1),date(2026,10,10),(ris,txs))
        scoped=auto.m.scope_inventory(inventory,['RIS/2026-10-01__2026-10-10'])
        self.assertEqual(scoped.regions[0].months,(sept,))
        self.assertEqual(scoped.regions[1],txs)
        self.assertEqual(inventory.regions[0].months,(sept,october))
        for keys in [['RIS/2026-10-01__2026-10-09'],['BAD/2026-10-01__2026-10-10'],
                     ['RIS/2026-09-01__2026-10-01','RIS/2026-10-01__2026-10-10']]:
            with self.assertRaises(ValueError):auto.m.scope_inventory(inventory,keys)

    def test_exclusion_needs_auditable_reason(self):
        self.assertEqual(auto.exclusion_keys({}),[])
        with self.assertRaises(ValueError):auto.exclusion_keys({'excluded_months':[{'month':'RIS/x'}]})
        self.assertEqual(auto.exclusion_keys({'excluded_months':[{'month':'RIS/x','reason':'user omitted'}]}),['RIS/x'])

    def test_readiness_and_qa_use_identical_authorized_scope(self):
        sept=archive.Month(date(2026,9,1),date(2026,10,1),5)
        october=archive.Month(date(2026,10,1),date(2026,10,10),10)
        region=archive.Region('RIS','/r.shp','EPSG:32614',1,(sept,october))
        inventory=archive.Inventory(date(2026,9,1),date(2026,10,10),(region,))
        config={'excluded_months':[{'month':'RIS/2026-10-01__2026-10-10','reason':'user request'}]}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'inventory.json';source.write_text('{}')
            paths=dict(inventory=source,state_root=root/'state',source_root=root/'archive')
            with patch.object(auto.a,'read_inventory',return_value=inventory), \
                    patch.object(auto,'region_ready',return_value=(True,None)) as gate, \
                    patch.object(auto,'prepare_history',side_effect=ValueError('stop before network')) as prepare:
                auto.run_cycle(config,paths,lambda:None,lambda *args,**kw:None)
            self.assertEqual(gate.call_args.args[1].months,(sept,))
            self.assertEqual(prepare.call_args.args[0].months,(sept,))

    def test_completion_requires_regular_block_and_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);directory=root/'L30'/'example';directory.mkdir(parents=True)
            scene={'id':'asset','sensor':'L30','index':'example'}
            records={'asset':{'tiles':[{'file':'block.tif'}]}}
            (directory/'block.tif').write_bytes(b'tiff')
            self.assertFalse(auto.m.committed_blocks_exist(root,[scene],records))
            (directory/'block.json').write_text('{}')
            self.assertTrue(auto.m.committed_blocks_exist(root,[scene],records))
            (directory/'block.json').unlink()
            (directory/'unrelated.json').write_text('{}')
            self.assertFalse(auto.m.committed_blocks_exist(root,[scene],records))

    def test_ee_parameter_mutation_cannot_corrupt_chunk_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.tif';tile=h.Tile(0,0,0,30,1,1)
            with rasterio.open(source,'w',driver='GTiff',width=1,height=1,count=1,dtype='uint8',
                    crs='EPSG:32614',transform=Affine(*tile.transform)) as ds:
                ds.write(np.array([[96]],dtype='uint8'),1)
            response=Mock(status_code=200);response.iter_content.return_value=[source.read_bytes()]
            context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
            session=Mock();session.get.return_value=context
            def mutate(image,params):
                params['image']=object();return 'https://example.org/download'
            output=root/'qa.tif'
            with patch.object(h,'get_download_url',side_effect=mutate):
                auto.download_chunk(object(),tile,output,'EPSG:32614',session,{'asset':'example'},lambda:None)
            self.assertTrue(auto.cached(output,{'asset':'example'}))
            self.assertNotIn('image',auto.m.read_json(output.with_suffix('.json'))['export'])

    def test_region_gate_checks_only_target_region_not_other_downloads(self):
        ris = archive.Region('RIS', '/roi/r.shp', 'EPSG:32614', 1, ())
        txs = archive.Region('TXS', '/roi/t.shp', 'EPSG:32614', 2, ())
        inventory = archive.Inventory(date(2020,1,1), date(2020,2,1), (ris, txs))
        with patch.object(auto.m, 'network_complete', return_value=(True, None)) as gate:
            self.assertEqual(auto.region_ready(inventory, txs, Path('/archive'), lambda: None), (True, None))
        self.assertEqual(gate.call_args.args[0].regions, (txs,))

    def test_independent_region_failure_does_not_starve_ready_region(self):
        regions = [Mock(code='RIS', area_m2=1), Mock(code='TXS', area_m2=2), Mock(code='REM', area_m2=3)]
        inventory = Mock(regions=regions)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'inventory.json'; source.write_text('{}')
            paths = dict(inventory=source, source_root=root/'archive', state_root=root/'state',
                         output_root=root/'out', water_root=root/'water')
            calls, stages = [], []
            def gate(inv, region, *args):
                calls.append(region.code)
                if region.code == 'RIS': raise ValueError('source count differs')
                return (region.code == 'TXS', 'pending')
            with patch.object(auto.a, 'read_inventory', return_value=inventory), \
                    patch.object(auto, 'region_ready', side_effect=gate), \
                    patch.object(auto, 'prepare_history', side_effect=ValueError('raw QA missing')) as prepare:
                result = auto.run_cycle({'nas_source':'//nas/data'}, paths, lambda: None,
                                        lambda phase, **values: stages.append((phase,values)))
            self.assertEqual(calls, ['RIS','TXS','REM'])
            self.assertEqual([values['region'] for phase,values in stages if phase=='checking-region-download'],calls)
            self.assertEqual(prepare.call_args.args[0].code, 'TXS')
            self.assertEqual(result['RIS']['phase'], 'blocked')
            self.assertEqual(result['REM']['phase'], 'waiting-for-region-download')

    def test_raw_qa_assembly_retains_water_land_cloud_and_masks_outside_roi(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tile = h.Tile(0,0,0,60,3,2)
            path = root/'chunk.tif'
            values = np.array([[96,64,66],[255,96,64]], dtype='uint8')
            with rasterio.open(path,'w',driver='GTiff',width=3,height=2,count=1,dtype='uint8',
                    nodata=255,crs='EPSG:32614',transform=Affine(*tile.transform)) as ds:
                ds.write(values,1)
            output = root/'day.tif'
            grid = (3,2,Affine(*tile.transform),0,0)
            auto.assemble_day(output, [(tile,path)], box(0,0,60,60), 'EPSG:32614', grid, {'a':1}, lambda: None)
            with rasterio.open(output) as ds:
                np.testing.assert_array_equal(ds.read(1), [[96,64,255],[255,96,255]])
            self.assertTrue(auto.cached(output, {'a':1}))
            with self.assertRaises(ValueError): auto.cached(output, {'a':2})
            with path.open('ab') as stream: stream.write(b'changed')
            # Input chunk is immutable evidence; output cache has its own checksum.
            with output.open('ab') as stream: stream.write(b'changed')
            with self.assertRaises(ValueError): auto.cached(output, {'a':1})

    def test_truncated_or_wrong_grid_qa_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'bad.tif'
            with rasterio.open(path,'w',driver='GTiff',width=1,height=1,count=1,dtype='uint8',
                    crs='EPSG:32614',transform=Affine(30,0,0,0,-30,30)) as ds:
                ds.write(np.array([[96]],dtype='uint8'),1)
            with self.assertRaises(ValueError):
                auto.inspect_qa(path,2,1,Affine(30,0,0,0,-30,30),'EPSG:32614')

    def test_mask_cannot_be_reused_for_another_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'water.tif';path.write_bytes(b'mask')
            auto.m.atomic_json(path.with_suffix('.json'), {'policy':auto.water.POLICY,
                'sha256':h.file_hash(path),'complete_history_sha256':'original'})
            auto.verify_mask(path,'original')
            with self.assertRaises(ValueError): auto.verify_mask(path,'changed')

    def test_original_archive_and_formal_output_must_be_separate(self):
        config = dict(inventory='/home/user/inventory.json', source_root='/nasdata/archive',
                      output_root='/nasdata/archive/daily',state_root='/home/user/state',
                      qa_root='/nasdata/qa',water_root='/nasdata/water',nas_source='//nas/data')
        with self.assertRaises(ValueError): auto.validate_config(config)


if __name__=='__main__': unittest.main()
