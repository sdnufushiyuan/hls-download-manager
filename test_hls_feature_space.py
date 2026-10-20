from pathlib import Path
import tempfile
import unittest
import numpy as np
import rasterio
from rasterio.transform import from_origin
from download_hls_optram import BANDS,NODATA,file_hash
from hls_feature_space import process


class FeatureSpaceTests(unittest.TestCase):
    def test_physical_qc_refuses_negative_reflectance_then_refuses_stale_review_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);inputs=root/'inputs';inputs.mkdir()
            arrays={}
            for p in ('L30','S30'):
                a=np.zeros((6,2,3),dtype='float32');a[0]=.1;a[1]=.3;a[2]=.2 if p=='L30' else .25
                if p=='L30':a[0,0,0]=-.01;a[2,0,1]=.002
                a[3]=(a[1]-a[0])/(a[1]+a[0]);a[4]=(1-a[2])**2/(2*a[2]);a[5]=64
                path=inputs/(p+'.tif')
                with rasterio.open(path,'w',driver='GTiff',width=3,height=2,count=6,dtype='float32',
                        nodata=NODATA,crs='EPSG:32614',transform=from_origin(0,60,30,30)) as ds:
                    ds.write(a);ds.descriptions=BANDS
                arrays[p]=a
            output=root/'analysis'
            result=process(inputs/'L30.tif',inputs/'S30.tif',output,'TEST','20200101')
            self.assertEqual(result['datasets']['L30']['valid_pixels'],5)
            self.assertEqual(result['datasets']['L30']['STR_gt_15_retained'],1)
            with rasterio.open(output/'FUSED/TEST/2020/TEST_FUSED_20200101_QC.tif') as ds:
                fused=ds.read()
            np.testing.assert_array_equal(fused[:,0,0],arrays['S30'][:,0,0])
            np.testing.assert_array_equal(fused[:,0,1],arrays['L30'][:,0,1])
            review={'input_sha256':{p:file_hash(inputs/(p+'.tif')) for p in arrays},
                    'reject':[{'product':'L30','row':0,'col':1,'reason':'reviewed spectral issue','evidence':'external QA record'}]}
            reviewed=root/'reviewed'
            checked=process(inputs/'L30.tif',inputs/'S30.tif',reviewed,'TEST','20200101',review)
            self.assertEqual(checked['source_pixels']['S30'],2)
            self.assertEqual(process(inputs/'L30.tif',inputs/'S30.tif',reviewed,'TEST','20200101',review),checked)
            with self.assertRaises(ValueError):process(inputs/'L30.tif',inputs/'S30.tif',output,'TEST','20200101',review)
            review['input_sha256']['L30']='stale'
            with self.assertRaises(ValueError):process(inputs/'L30.tif',inputs/'S30.tif',root/'stale','TEST','20200101',review)
            self.assertFalse((root/'stale').exists())


if __name__=='__main__':unittest.main()
