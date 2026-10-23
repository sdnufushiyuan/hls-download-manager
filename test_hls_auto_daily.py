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
    def test_adjacent_groups_cover_only_missing_windows_and_short_edges(self):
        tiles = [h.Tile(r,c,300+c*30,600-r*30,w,ht)
                 for r,ht in [(0,768),(768,77)] for c,w in [(0,768),(768,33)]]
        groups = list(auto.qa_transfer_groups(tiles))
        self.assertEqual(len(groups),1)
        batch,members = groups[0]
        self.assertEqual((batch.width,batch.height),(801,845))
        self.assertEqual(members,tiles)
        groups = list(auto.qa_transfer_groups([tiles[0],tiles[3]]))
        self.assertTrue(all(len(members)==1 for _,members in groups))
        bad = h.Tile(0,768,23400,600,33,768)
        with self.assertRaises(ValueError):list(auto.qa_transfer_groups([tiles[0],bad]))
        with self.assertRaises(ValueError):list(auto.qa_transfer_groups([tiles[0],tiles[0]]))

    def test_batch_limit_and_fresh_region_request_counts(self):
        for width,height,expected in [(2001,2001,9),(2860,3584,20)]:
            tiles=[h.Tile(r,c,300+c*30,600-r*30,min(768,width-c),min(768,height-r))
                for r in range(0,height,768) for c in range(0,width,768)]
            self.assertEqual(len(tiles),expected)
            groups=list(auto.qa_transfer_groups(tiles));self.assertEqual(len(groups),1)
            batch,members=groups[0]
            self.assertEqual((batch.width,batch.height),(width,height))
            self.assertEqual(sum(t.width*t.height for t in members),width*height)
        tiles=[h.Tile(r,c,c*30,600-r*30,768,768) for r in range(0,4608,768) for c in range(0,4608,768)]
        groups=list(auto.qa_transfer_groups(tiles))
        self.assertEqual(len(groups),4)
        self.assertTrue(all(batch.width<=4096 and batch.height<=4096 for batch,_ in groups))
        self.assertEqual(sum(len(members) for _,members in groups),len(tiles))

    def test_batch_pixels_daily_output_and_original_contracts_equal_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tiles=[h.Tile(r,c,c*30,120-r*30,w,ht)
                for r,ht in [(0,3),(3,1)] for c,w in [(0,3),(3,2)]]
            calls=[]
            def transfer(image,tile,path,crs,session,contract,check,report=None):
                calls.append(tile)
                rows=np.arange(tile.row,tile.row+tile.height)[:,None]
                cols=np.arange(tile.col,tile.col+tile.width)[None,:]
                values=np.array([64,96,66,255],dtype='uint8')[(rows+cols)%4]
                path.parent.mkdir(parents=True,exist_ok=True)
                with rasterio.open(path,'w',driver='GTiff',width=tile.width,height=tile.height,
                        count=1,dtype='uint8',crs=crs,transform=Affine(*tile.transform),nodata=255,
                        compress='deflate',tiled=True,blockxsize=256,blockysize=256) as ds:ds.write(values,1)
                auto.m.atomic_json(path.with_suffix('.json'),{'contract':contract,'sha256':h.file_hash(path)})
            with patch.object(auto,'download_chunk',side_effect=transfer):
                serial=auto.download_qa_day(None,tiles,root/'serial','S30','20200101','EPSG:32614',None,
                    {'fixed':'contract'},'test',lambda:None,Mock())
                self.assertEqual(len(calls),4);calls.clear()
                batched=auto.download_qa_day(None,tiles,root/'batch','S30','20200101','EPSG:32614',None,
                    {'fixed':'contract'},'test',lambda:None,Mock(),True)
                self.assertEqual(len(calls),1)
            for (t,left),(_,right) in zip(serial,batched):
                self.assertEqual(h.file_hash(left),h.file_hash(right))
                one=auto.m.read_json(left.with_suffix('.json'));two=auto.m.read_json(right.with_suffix('.json'))
                self.assertEqual(one['contract'],two['contract'])
                self.assertTrue(auto.cached(right,one['contract']))
                self.assertEqual(two['derived_from']['method'],'exact-grid-window-v1')
            grid=(5,4,Affine(30,0,0,0,-30,120),0,0)
            for name,chunks in [('serial',serial),('batch',batched)]:
                auto.assemble_day(root/(name+'.tif'),chunks,box(0,0,150,120),'EPSG:32614',grid,{},lambda:None)
            self.assertEqual(h.file_hash(root/'serial.tif'),h.file_hash(root/'batch.tif'))
            # A restart reuses every committed window, with no network calls or rewrites.
            hashes={p:h.file_hash(p) for _,p in batched}
            mtimes={p:p.stat().st_mtime_ns for p in hashes}
            with patch.object(auto,'download_chunk') as transfer:
                auto.download_qa_day(None,tiles,root/'batch','S30','20200101','EPSG:32614',None,
                    {'fixed':'contract'},'test',lambda:None,Mock(),True)
            transfer.assert_not_called()
            self.assertEqual(hashes,{p:h.file_hash(p) for p in hashes})
            self.assertEqual(mtimes,{p:p.stat().st_mtime_ns for p in hashes})

    def test_partial_cache_excluded_from_new_transfers_and_bad_cache_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tiles=[h.Tile(0,c,c*30,30,1,1) for c in range(3)]
            chunk=root/'chunks/L30/20200101'/tiles[0].name;chunk.parent.mkdir(parents=True)
            with rasterio.open(chunk,'w',driver='GTiff',width=1,height=1,count=1,dtype='uint8',
                    crs='EPSG:32614',transform=Affine(*tiles[0].transform)) as ds:
                ds.write(np.array([[96]],dtype='uint8'),1)
            contract={'date':'20200101','tile':tiles[0].name,'ee_version':'test'}
            auto.m.atomic_json(chunk.with_suffix('.json'),{'contract':contract,'sha256':h.file_hash(chunk)})
            before=(h.file_hash(chunk),chunk.stat().st_mtime_ns)
            with patch.object(auto,'download_chunk',side_effect=InterruptedError()) as transfer:
                with self.assertRaises(InterruptedError):
                    auto.download_qa_day(None,tiles,root,'L30','20200101','EPSG:32614',None,
                        {'date':'20200101'},'test',lambda:None,Mock(),True)
            batch=transfer.call_args.args[1]
            self.assertEqual((batch.col,batch.width),(1,2))
            self.assertEqual(before,(h.file_hash(chunk),chunk.stat().st_mtime_ns))
            chunk.write_bytes(b'corrupted')
            with patch.object(auto,'download_chunk') as transfer:
                with self.assertRaises(ValueError):
                    auto.download_qa_day(None,tiles,root,'L30','20200101','EPSG:32614',None,
                        {'date':'20200101'},'test',lambda:None,Mock(),True)
            transfer.assert_not_called()

    def test_interrupted_split_reuses_completed_batch_without_new_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tiles=[h.Tile(r,c,c*30,60-r*30,1,1) for r in range(2) for c in range(2)]
            def transfer(image,tile,path,crs,session,contract,check,report=None):
                path.parent.mkdir(parents=True,exist_ok=True)
                with rasterio.open(path,'w',driver='GTiff',width=2,height=2,count=1,dtype='uint8',
                        crs=crs,transform=Affine(*tile.transform)) as ds:
                    ds.write(np.array([[96,64],[255,66]],dtype='uint8'),1)
                auto.m.atomic_json(path.with_suffix('.json'),{'contract':contract,'sha256':h.file_hash(path)})
            split=auto.split_qa_batch
            def interrupted(*args):
                visits=[0]
                def check():
                    visits[0]+=1
                    if visits[0]==3:raise InterruptedError()
                return split(*args[:-1],check)
            with patch.object(auto,'download_chunk',side_effect=transfer), \
                    patch.object(auto,'split_qa_batch',side_effect=interrupted):
                with self.assertRaises(InterruptedError):
                    auto.download_qa_day(None,tiles,root,'L30','20200101','EPSG:32614',None,{},'test',lambda:None,Mock(),True)
            existing=root/'chunks/L30/20200101'/tiles[0].name
            before=(h.file_hash(existing),existing.stat().st_mtime_ns)
            self.assertEqual(len(list((root/'chunks').rglob('*.tif'))),1)
            with patch.object(auto,'download_chunk') as transfer:
                chunks=auto.download_qa_day(None,tiles,root,'L30','20200101','EPSG:32614',None,{},'test',lambda:None,Mock(),True)
            transfer.assert_not_called()
            self.assertEqual(len(chunks),4)
            self.assertEqual(before,(h.file_hash(existing),existing.stat().st_mtime_ns))
            self.assertFalse(list(root.rglob('*.part')))

    def test_tiff_429_reuses_url_and_keeps_retry_after_and_finite_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tile=h.Tile(0,0,0,30,1,1);source=root/'source.tif'
            with rasterio.open(source,'w',driver='GTiff',width=1,height=1,count=1,dtype='uint8',
                    crs='EPSG:32614',transform=Affine(*tile.transform)) as ds:
                ds.write(np.array([[96]],dtype='uint8'),1)
            first=Mock(status_code=429,headers={'Retry-After':'42'})
            first.iter_content.return_value=[b'{"error":{"status":"RESOURCE_EXHAUSTED"}}']
            second=Mock(status_code=200);second.iter_content.return_value=[source.read_bytes()]
            contexts=[]
            for response in [first,second]:
                ctx=Mock();ctx.__enter__=Mock(return_value=response);ctx.__exit__=Mock(return_value=False);contexts.append(ctx)
            session=Mock();session.get.side_effect=contexts
            with patch.object(h,'get_download_url',return_value='https://example.org/?token=private') as urls, \
                    patch.object(auto,'retry_wait') as wait:
                auto.download_chunk(None,tile,root/'qa.tif','EPSG:32614',session,{},lambda:None,Mock())
            urls.assert_called_once();wait.assert_called_once()
            self.assertEqual(wait.call_args.args[0],42)
            self.assertEqual(session.get.call_args_list[0],session.get.call_args_list[1])
            self.assertNotIn('private',(root/'qa.json').read_text())

    def test_batch_opt_in_does_not_change_other_region_or_configuration(self):
        regions=[Mock(code=code,area_m2=n) for n,code in enumerate(['SMALL','MEDIUM','LARGE'])]
        inventory=Mock(regions=regions)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'inventory.json';source.write_text('{}')
            paths=dict(inventory=source,source_root=root/'archive',state_root=root/'state')
            config={'nas_source':'//nas/data'};before=json.dumps(config,sort_keys=True)
            with patch.object(auto.a,'read_inventory',return_value=inventory), \
                    patch.object(auto,'region_ready',return_value=(True,None)), \
                    patch.object(auto,'prepare_history',side_effect=ValueError('stop before network')) as prepare:
                auto.run_cycle(config,paths,lambda:None,Mock(),qa_batch_regions=['SMALL','MEDIUM'])
            self.assertEqual([c.kwargs['batch_transfers'] for c in prepare.call_args_list],[True,True,False])
            self.assertEqual(json.dumps(config,sort_keys=True),before)

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
