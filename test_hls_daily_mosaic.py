"""Real small rasters verify grouping, complete-pixel overlaps and safe resume."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import numpy as np
import rasterio
from rasterio.transform import Affine
from shapely.geometry import box, mapping

import download_hls_optram as h
import hls_daily_mosaic as m


class MosaicTests(unittest.TestCase):
    def test_network_gate_cannot_treat_completion_hints_as_verification(self):
        inventory=Mock()
        region=Mock();region.code='RIS';region.months=[Mock(scene_count=1,start='2020-01-01',end='2020-02-01')]
        inventory.regions=[region]
        job=Mock(scene_count=1,name='RIS/month')
        with patch.object(m.archive,'fair_jobs',return_value=[job]),patch.object(m.archive,'processed_scene_hint',return_value=0):
            self.assertFalse(m.network_complete(inventory,Path('/archive'),{})[0])
        cache={'RIS':(None,None)}
        with patch.object(m.archive,'fair_jobs',return_value=[job]),patch.object(m.archive,'processed_scene_hint',return_value=1),patch.object(m,'load_month',side_effect=m.MonthPending('incomplete')):
            self.assertFalse(m.network_complete(inventory,Path('/archive'),cache)[0])

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.month=self.root/'2013-04-01__2013-05-01';self.month.mkdir()
        self.roi=box(0,0,3840,1920)
        self.payload={'geometry':'synthetic fixture'}
        self.tiles=h.make_tiles(self.roi,64)
        self.recipe={'roi_sha256':h.hash_object(self.payload),'bands':list(h.BANDS),'resolution_m':30,
                     'nodata':h.NODATA,'crs':'EPSG:32614','tile_size':64,'aerosol':'low',
                     'start':'2013-04-01','end_exclusive':'2013-05-01'}
        self.fingerprint=h.hash_object(self.recipe)
        self.scenes=[];self.metadata={}

    def add(self,tile_id='14UNA',sensor='L30',stamp='20130428T173005',pathrow='032025',
            footprint=None,blocks=None,value=.1):
        time_ms=int(datetime.strptime(stamp,'%Y%m%dT%H%M%S').replace(tzinfo=timezone.utc).timestamp()*1000)
        index='T'+tile_id+'_'+stamp
        scene={'index':index,'sensor':sensor,'id':h.SENSORS[sensor][0]+'/'+index,
               'time_ms':time_ms,'projected_footprint':mapping(footprint if footprint is not None else self.roi)}
        directory=self.month/sensor/index;directory.mkdir(parents=True)
        entries=[]
        for number in blocks or []:
            tile=self.tiles[number]
            data=np.full((6,tile.height,tile.width),h.NODATA,dtype='float32')
            data[0]=value;data[1]=value+.1;data[2]=value+.2
            data[3]=(data[1]-data[0])/(data[1]+data[0]);data[4]=(1-data[2])**2/(2*data[2]);data[5]=64
            # One invalid pixel confirms NoData holes survive the merge.
            data[:,0,0]=h.NODATA
            path=directory/tile.name
            with rasterio.open(path,'w',driver='GTiff',width=tile.width,height=tile.height,count=6,
                    dtype='float32',nodata=h.NODATA,crs='EPSG:32614',transform=Affine(*tile.transform)) as dst:
                dst.write(data)
                dst.update_tags(recipe_hash=self.fingerprint,scene_id=scene['id'],aerosol='low')
                for i,name in enumerate(h.BANDS,1):dst.set_band_description(i,name)
            sidecar={'sha256':h.file_hash(path),'recipe_hash':self.fingerprint,'scene_id':scene['id'],
                     'transform':tile.transform,'dimensions':[tile.width,tile.height],'valid_pixels':64*64-1}
            m.atomic_json(path.with_suffix('.json'),sidecar)
            entries.append({'file':tile.name,'valid_pixels':64*64-1})
        record={**scene,'recipe_hash':self.fingerprint,'tiles':entries}
        m.atomic_json(directory/'scene.json',record)
        self.scenes.append(scene)
        self.metadata[scene['id']]={'system:time_start':time_ms,**(
            {'LANDSAT_PRODUCT_ID':f'LC08_L1TP_{pathrow}_{stamp[:8]}_20200912_02_T1'} if sensor=='L30' else
            {'MGRS_TILE_ID':tile_id,'PRODUCT_URI':'synthetic'})}
        self.save_manifest()
        return scene

    def save_manifest(self):
        m.atomic_json(self.month/'run_manifest.json',{'recipe':self.recipe,'recipe_hash':self.fingerprint,'scenes':self.scenes})

    def process(self,grouping='track-day',fuse_products=False,layout='region-product',water_mask=None):
        cache=Mock();cache.load.side_effect=lambda scenes:{s['id']:self.metadata[s['id']] for s in scenes}
        if grouping=='region-day':cache.load.side_effect=AssertionError('Regional mode must not query EE metadata')
        return m.process_month(self.month,'RIS',self.roi,self.payload,cache,self.root/'daily',len(self.scenes),
                               emit=lambda _:None,grouping=grouping,fuse_products=fuse_products,layout=layout,water_mask=water_mask)

    def test_static_water_mask_removes_both_products_and_fused_provenance(self):
        self.add(blocks=[0]);self.add(sensor='S30',blocks=[0,1],value=.2)
        mask=self.root/'water_history.tif';values=np.zeros((64,128),dtype='uint8');values[1,1]=1
        with rasterio.open(mask,'w',driver='GTiff',width=128,height=64,count=1,dtype='uint8',
                crs='EPSG:32614',transform=Affine(30,0,0,0,-30,1920)) as ds:ds.write(values,1)
        m.atomic_json(mask.with_suffix('.json'),{'policy':'consecutive-valid-date-water-all-dates-v1',
            'consecutive_valid_dates':2,'sha256':h.file_hash(mask)})
        result=self.process('region-day',True,'product-region',mask)
        for row in result['outputs']:
            with rasterio.open(row['path']) as ds:self.assertTrue(np.all(ds.read()[:,1,1]==h.NODATA))
            if '/FUSED/' in row['path'].replace('\\','/'):
                receipt=m.read_json(Path(row['path']).with_suffix('.json'))
                with rasterio.open(receipt['provenance_maps']['source']['path']) as ds:self.assertEqual(ds.read(1)[1,1],0)
                with rasterio.open(receipt['provenance_maps']['time']['path']) as ds:self.assertEqual(ds.read(1)[1,1],m.TIME_NODATA)

    def test_product_first_folders_keep_same_day_fusion_and_verified_reuse(self):
        self.add(blocks=[0])
        self.add(sensor='S30',blocks=[1],value=.2)
        result=self.process('region-day',True,'product-region')
        self.assertEqual(result['groups'],3)
        for row in result['outputs']:
            path=Path(row['path'])
            self.assertIn(path.relative_to(self.root/'daily').parts[0],('L30','S30','FUSED'))
            self.assertEqual(path.relative_to(self.root/'daily').parts[1:3],('RIS','2013'))
        self.assertEqual(self.process('region-day',True,'product-region')['groups'],3)
        with self.assertRaises(m.MosaicError):self.process('region-day',True,'unknown')

    def test_regional_day_crosses_wrs_and_mgrs_and_does_not_query_ee(self):
        self.add(blocks=[0],footprint=box(0,0,1920,1920))
        second=self.add('14UNV',blocks=[1],pathrow='031026',footprint=box(1920,0,3840,1920),value=.2)
        self.metadata[second['id']]={}  # Source WRS is irrelevant to regional day grouping.
        result=self.process('region-day')
        self.assertEqual(result['groups'],1)
        path=Path(result['outputs'][0]['path'])
        self.assertEqual(path.name,'RIS_L30_20130428.tif')
        with rasterio.open(path) as src:
            self.assertEqual((src.width,src.height),(128,64))
            self.assertAlmostEqual(float(src.read(1)[1,1]),.1,6)
            self.assertAlmostEqual(float(src.read(1)[1,65]),.2,6)

    def test_regional_grid_is_identical_across_dates_and_products_with_holes(self):
        self.add(blocks=[0],footprint=box(0,0,1920,1920))
        self.add(sensor='S30',blocks=[1],footprint=box(1920,0,3840,1920),value=.2)
        self.add(stamp='20130429T173005',blocks=[1],footprint=box(1920,0,3840,1920))
        result=self.process('region-day');self.assertEqual(result['groups'],3)
        grids=[]
        for row in result['outputs']:
            with rasterio.open(row['path']) as src:
                grids.append((src.width,src.height,src.transform,src.crs))
                self.assertEqual((src.width,src.height),(128,64))
                if Path(row['path']).name=='RIS_L30_20130428.tif':
                    self.assertTrue(np.all(src.read()[:,:,64:]==h.NODATA))
        self.assertEqual(len(set(grids)),1)

    def test_fusion_prioritizes_l30_even_when_s30_is_earlier_and_fills_holes(self):
        self.add(blocks=[0],value=.1)
        self.add(sensor='S30',stamp='20130428T170000',blocks=[0,1],value=.3)
        result=self.process('region-day',True)
        self.assertEqual(result['groups'],3)  # Keep both original product day images as well.
        row=next(v for v in result['outputs'] if '/FUSED/' in v['path'].replace('\\','/'))
        saved=m.read_json(Path(row['path']).with_suffix('.json'))
        with rasterio.open(row['path']) as src:
            values=src.read()
            self.assertEqual(src.descriptions,h.BANDS)
            self.assertAlmostEqual(float(values[0,1,1]),.1,6)
            self.assertAlmostEqual(float(values[0,1,65]),.3,6)
            self.assertAlmostEqual(float(values[4,1,65]),float((1-.5)**2/(2*.5)),6)
        with rasterio.open(saved['provenance_maps']['source']['path']) as src:
            codes=src.read(1)
            self.assertEqual((int(codes[1,1]),int(codes[1,65]),int(codes[0,0])),(1,2,0))
        with rasterio.open(saved['provenance_maps']['time']['path']) as src:
            times=src.read(1)
            self.assertEqual(int(times[1,1]),17*3600+30*60+5)
            self.assertEqual(int(times[1,65]),17*3600)
            self.assertEqual(int(times[0,0]),m.TIME_NODATA)
        self.assertEqual(saved['source_pixels'],{'L30':4095,'S30':4095})
        self.assertEqual(self.process('region-day',True)['groups'],3)

    def test_fusion_does_not_fill_from_another_day_and_handles_s30_only(self):
        self.add(blocks=[0])
        self.add(sensor='S30',stamp='20130429T170000',blocks=[1],value=.3)
        result=self.process('region-day',True)
        fused=[v for v in result['outputs'] if '/FUSED/' in v['path'].replace('\\','/')]
        self.assertEqual(len(fused),2)
        for row in fused:
            saved=m.read_json(Path(row['path']).with_suffix('.json'))
            if '20130428' in row['path']:
                self.assertEqual(saved['source_pixels'],{'L30':4095,'S30':0})
            else:self.assertEqual(saved['source_pixels'],{'L30':0,'S30':4095})

    def test_fusion_requires_valid_source_qa_and_detects_changed_provenance(self):
        scene=self.add(sensor='S30',blocks=[0])
        result=self.process('region-day',True)
        fused=next(v for v in result['outputs'] if '/FUSED/' in v['path'].replace('\\','/'))
        saved=m.read_json(Path(fused['path']).with_suffix('.json'))
        source=Path(saved['provenance_maps']['source']['path'])
        source.write_bytes(source.read_bytes()+b'tamper')
        with self.assertRaises(m.MosaicError):self.process('region-day',True)

    def test_invalid_s30_qa_is_never_used_for_fusion(self):
        scene=self.add(sensor='S30',blocks=[0])
        block=self.month/'S30'/scene['index']/self.tiles[0].name
        with rasterio.open(block,'r+') as src:
            data=src.read();data[5,1,1]=66;src.write(data)
        saved=m.read_json(block.with_suffix('.json'));saved['sha256']=h.file_hash(block)
        m.atomic_json(block.with_suffix('.json'),saved)
        with self.assertRaises(m.MosaicError):self.process('region-day',True)
        self.assertFalse(list((self.root/'daily').rglob('*.tif')))

    def test_same_wrs_day_merges_two_hls_tiles_and_preserves_values(self):
        self.add(blocks=[0],footprint=box(0,0,1920,1920))
        self.add('14UNV',blocks=[1],footprint=box(1920,0,3840,1920),value=.2)
        result=self.process();self.assertEqual(result['groups'],1)
        path=Path(result['outputs'][0]['path'])
        self.assertEqual(path.name,'RIS_L30_20130428_P032_R025.tif')
        with rasterio.open(path) as src:
            self.assertEqual(src.driver,'GTiff');self.assertEqual((src.width,src.height),(128,64))
            self.assertEqual(src.descriptions,h.BANDS);self.assertEqual(src.nodata,h.NODATA)
            self.assertEqual(src.tags()['resampling'],'none')
            self.assertAlmostEqual(float(src.read(1)[1,1]),.1,6)
            self.assertAlmostEqual(float(src.read(1)[1,65]),.2,6)
            self.assertTrue(np.all(src.read()[:,0,0]==h.NODATA))
        before=h.file_hash(path)
        self.assertEqual(self.process()['outputs'][0]['sha256'],before)
        self.assertEqual(h.file_hash(path),before)

    def test_different_wrs_same_date_and_different_dates_stay_separate(self):
        self.add(blocks=[0])
        self.add('14UNV',pathrow='031025',blocks=[1])
        self.add(stamp='20130429T173005',blocks=[0])
        result=self.process()
        self.assertEqual(result['groups'],3)
        names={Path(row['path']).name for row in result['outputs']}
        self.assertIn('RIS_L30_20130428_P031_R025.tif',names)
        self.assertIn('RIS_L30_20130429_P032_R025.tif',names)
        # Equal timestamps must not let a different WRS path/row donate pixels.
        self.assertTrue(all(row['valid_pixels']==4095 for row in result['outputs']))

    def test_s30_mosaic_recovers_same_time_donor_inside_target_tile_only(self):
        target=self.add(sensor='S30',footprint=box(0,0,1920,1920),blocks=[])
        self.add('14UNV',sensor='S30',blocks=[0,1],value=.2)
        result=self.process();self.assertEqual(result['groups'],2)
        path=next(Path(row['path']) for row in result['outputs'] if 'T14UNA' in row['path'])
        with rasterio.open(path) as src:
            self.assertEqual(src.width,64)
            self.assertAlmostEqual(float(src.read(1)[1,1]),.2,6)
        record=m.read_json(path.with_suffix('.json'))
        self.assertEqual(record['contract']['target_assets'],[target['id']])
        self.assertIn('14UNV',record['contract']['source_blocks'][0]['asset_id'])

    def test_different_product_or_time_cannot_fill_a_missing_observation(self):
        self.add(sensor='S30',blocks=[],footprint=box(0,0,1920,1920))
        self.add('14UNV',sensor='S30',stamp='20130428T180000',blocks=[0])
        self.add(sensor='L30',blocks=[0])
        result=self.process()
        target=next(row for row in result['outputs'] if '_S30_' in row['path'] and 'T14UNA' in row['path'])
        self.assertEqual(target['valid_pixels'],0)

    def test_same_day_same_s30_tile_uses_first_valid_whole_pixel(self):
        early=self.add(sensor='S30',blocks=[0],value=.1)
        later=self.add(sensor='S30',stamp='20130428T180000',blocks=[0,1],value=.3)
        result=self.process();self.assertEqual(result['groups'],1)
        with rasterio.open(result['outputs'][0]['path']) as src:
            values=src.read()
            self.assertAlmostEqual(float(values[0,1,1]),.1,6)
            self.assertAlmostEqual(float(values[0,1,65]),.3,6)
            self.assertAlmostEqual(float(values[3,1,65]),float((.4-.3)/(.4+.3)),6)

    def test_catalog_date_wrs_or_mgrs_errors_are_not_guessed(self):
        scene=self.add(blocks=[0])
        meta=self.metadata[scene['id']]
        for product in ['LC08_L1TP_032025_20130427_20200912_02_T1',
                        'LC08_L1TP_032025_20130428_20200912_02_T1; LC08_L1TP_032026_20130428_20200912_02_T1',
                        'unparsed_source '+meta['LANDSAT_PRODUCT_ID']]:
            with self.assertRaises(m.MosaicError):m.identity(scene,dict(meta,LANDSAT_PRODUCT_ID=product))
        with self.assertRaises(m.MosaicError):m.identity(scene,dict(meta,**{'system:time_start':0}))
        with self.assertRaises(m.MosaicError):m.identity(dict(scene,sensor='S30'),{'MGRS_TILE_ID':'14UNV','system:time_start':scene['time_ms']})

    def test_donor_is_clipped_to_target_footprint_at_its_exact_time(self):
        self.add(sensor='S30',blocks=[0],footprint=box(0,0,1920,1920),value=.1)
        self.add(sensor='S30',stamp='20130428T180000',blocks=[1],footprint=box(1920,0,3840,1920),value=.3)
        self.add('14UNV',sensor='S30',blocks=[1],value=.8)
        result=self.process()
        path=next(row['path'] for row in result['outputs'] if 'T14UNA' in row['path'])
        with rasterio.open(path) as src:
            self.assertAlmostEqual(float(src.read(1)[1,65]),.3,6)

    def test_source_grid_mismatch_is_rejected_even_with_updated_checksum(self):
        scene=self.add(blocks=[0])
        block=self.month/scene['sensor']/scene['index']/self.tiles[0].name
        with rasterio.open(block,'r+') as dataset:
            dataset.transform=Affine(31,0,0,0,-30,1920)
        saved=m.read_json(block.with_suffix('.json'));saved['sha256']=h.file_hash(block)
        m.atomic_json(block.with_suffix('.json'),saved)
        with self.assertRaises(m.MosaicError):self.process()

    def test_incomplete_month_creates_no_daily_tiff(self):
        scene=self.add(blocks=[0])
        (self.month/scene['sensor']/scene['index']/'scene.json').unlink()
        with self.assertRaises(m.MonthPending):self.process()
        self.assertFalse((self.root/'daily').exists())

    def test_corrupt_source_and_corrupt_existing_output_are_not_overwritten(self):
        scene=self.add(blocks=[0])
        block=self.month/scene['sensor']/scene['index']/self.tiles[0].name
        original=block.read_bytes();block.write_bytes(original+b'corruption')
        with self.assertRaises(m.MosaicError):self.process()
        self.assertEqual(block.read_bytes(),original+b'corruption')
        block.write_bytes(original)
        result=self.process();path=Path(result['outputs'][0]['path'])
        path.write_bytes(path.read_bytes()+b'corruption');before=path.read_bytes()
        with self.assertRaises(m.MosaicError):self.process()
        self.assertEqual(path.read_bytes(),before)

    def test_sample_count_mismatch_is_not_announced_complete(self):
        self.add(blocks=[0])
        with self.assertRaises(m.MosaicError):m.load_month(self.month,self.roi,self.payload,2)


if __name__=='__main__':unittest.main()
