import unittest,threading,time,tempfile
from pathlib import Path
from unittest.mock import MagicMock,patch
import download_hls_optram as h

class HigherPoolChecks(unittest.TestCase):
    def test_four_six_eight_bounded_threads_serial_signing_and_session_isolation(self):
        for workers in [4,6,8]:
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as folder:
                barrier=threading.Barrier(workers);lock=threading.Lock()
                active=peak=sign_active=sign_peak=0;sessions=[]
                image=MagicMock()
                def sign(_):
                    nonlocal sign_active,sign_peak
                    with lock:sign_active+=1;sign_peak=max(sign_peak,sign_active)
                    time.sleep(.002)
                    with lock:sign_active-=1
                    return 'https://example.invalid/bounded'
                image.getDownloadURL.side_effect=sign
                def new_session():
                    s=MagicMock();s.proxies={};s.owners=set();sessions.append(s);return s
                def download(image,tile,path,crs,session,*args,url_lock=None,cancel_event=None,**kwargs):
                    nonlocal active,peak
                    session.owners.add(threading.get_ident())
                    h.get_download_url(image,{},url_lock,cancel_event)
                    with lock:active+=1;peak=max(peak,active)
                    try:barrier.wait(5)
                    finally:
                        with lock:active-=1
                    return {'valid_pixels':tile.col},False
                tiles=[h.Tile(0,i,300000+i*120,3300000,4,3) for i in range(workers*3)]
                with patch('requests.Session',side_effect=new_session),patch.object(h,'download_tile',side_effect=download):
                    with h.TileDownloadPool(workers,'http://127.0.0.1:8118') as pool:
                        result=pool.download_scene(image,tiles,Path(folder),'EPSG:32650',h.MountGuard(folder),'recipe','low',{'sensor':'L30','index':'synthetic','id':'synthetic'},lambda _:None)
                self.assertEqual(peak,workers);self.assertEqual(sign_peak,1)
                self.assertEqual(len(sessions),workers)
                self.assertTrue(all(len(s.owners)==1 and s.close.call_count==1 for s in sessions))
                self.assertEqual([x['file'] for x in result],[x.name for x in tiles])

    def test_eight_workers_fatal_error_cancels_remaining_without_more_submission(self):
        barrier=threading.Barrier(8);calls=[];lock=threading.Lock()
        def work(image,tile,*args,cancel_event=None,**kwargs):
            with lock:calls.append(tile.col)
            barrier.wait(5)
            if tile.col==0:raise h.DownloadError('synthetic NAS failure')
            if tile.col==1:raise h.TransientDownloadError('synthetic transport failure')
            self.assertTrue(cancel_event.wait(5));h.check_download_cancelled(cancel_event)
        tiles=[h.Tile(0,i,300000+i*120,3300000,4,3) for i in range(24)]
        with tempfile.TemporaryDirectory() as folder,patch.object(h,'download_tile',side_effect=work):
            with self.assertRaisesRegex(h.DownloadError,'synthetic NAS failure'):
                with h.TileDownloadPool(8,'none') as pool:
                    pool.download_scene(MagicMock(),tiles,Path(folder),'EPSG:32650',h.MountGuard(folder),'recipe','low',{'sensor':'L30','index':'synthetic','id':'synthetic'},lambda _:None)
        self.assertEqual(sorted(calls),list(range(8)))

if __name__=='__main__':unittest.main()
