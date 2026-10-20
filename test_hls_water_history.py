from pathlib import Path
import tempfile
import unittest
import numpy as np
import rasterio
from rasterio.transform import from_origin
from hls_water_history import build,apply_mask
from download_hls_optram import file_hash


class WaterHistoryTests(unittest.TestCase):
    def test_cloud_skips_land_breaks_and_two_sensors_on_one_day_count_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);rows=[]
            qa=[[96,96,96,96,96],[66,64,64,255,66],[96,96,64,64,255],
                [255,255,64,96,66],[64,64,64,96,64]]
            profile=dict(driver='GTiff',width=5,height=1,count=1,dtype='uint8',nodata=255,
                         crs='EPSG:32614',transform=from_origin(0,30,30,30))
            for i,values in enumerate(qa,1):
                path=root/f'L30_{i}.tif'
                with rasterio.open(path,'w',**profile) as ds:ds.write(np.array([values],dtype='uint8'),1)
                rows.append(dict(date=f'2020010{i}',product='L30',path=str(path),sha256=file_hash(path)))
            path=root/'S30_1.tif'
            with rasterio.open(path,'w',**profile) as ds:ds.write(np.array([[255,255,96,64,255]],dtype='uint8'),1)
            rows.append(dict(date='20200101',product='S30',path=str(path),sha256=file_hash(path)))
            mask=root/'water.tif';receipt=build({'raw_unfiltered_fmask':True,'observations':rows},mask)
            self.assertEqual(receipt['excluded_pixels'],2)
            with rasterio.open(mask) as ds:np.testing.assert_array_equal(ds.read(1),[[1,0,0,1,0]])
            main=root/'main.tif';prof=dict(profile,count=6,dtype='float32',nodata=-9999)
            a=np.arange(30,dtype='float32').reshape(6,1,5)
            with rasterio.open(main,'w',**prof) as ds:ds.write(a)
            target=root/'filtered.tif';apply_mask(main,mask,target)
            with rasterio.open(target) as ds:filtered=ds.read()
            self.assertTrue(np.all(filtered[:,:,0]==-9999));self.assertTrue(np.all(filtered[:,:,3]==-9999))
            np.testing.assert_array_equal(filtered[:,:,[1,2,4]],a[:,:,[1,2,4]])
            with self.assertRaises(ValueError):apply_mask(main,mask,target)
            with self.assertRaises(ValueError):build({'observations':rows},root/'bad.tif')


if __name__=='__main__':unittest.main()
