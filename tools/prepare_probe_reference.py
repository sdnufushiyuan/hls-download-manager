"""Create the optimizer's trusted 128-pixel reference from a chosen HLS grid."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import download_hls_optram as hls


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--grid-tile', type=Path, required=True)
    parser.add_argument('--proxy', default='http://127.0.0.1:8118')
    args=parser.parse_args()
    settings=json.loads(args.probe.read_text())
    target=Path(settings['reference'])
    if target.exists():parser.error('Reference already exists; refusing to replace it.')
    import rasterio
    from rasterio.io import MemoryFile
    import requests
    with rasterio.open(args.grid_tile) as dataset:
        crs,transform=dataset.crs.to_string(),list(dataset.transform)[:6]
    _,_,payload=hls.load_roi(settings['roi'],crs)
    project,_=hls.resolve_project()
    if not project:parser.error('Set EE_PROJECT before preparing the reference.')
    ee=hls.initialize_ee(project,args.proxy)
    region=ee.FeatureCollection([ee.Feature(ee.Geometry(payload['features'][0]['geometry'],proj='EPSG:4326',geodesic=False))])
    image=hls.build_image(ee,ee.Image(settings['scene_id']),settings['sensor'],region,
                          settings.get('reflectance_scale',1.0),settings.get('aerosol','low'))
    url=image.getDownloadURL({'crs':crs,'crs_transform':transform,'dimensions':[128,128],'format':'GEO_TIFF'})
    with requests.Session() as session:
        session.trust_env=False
        response=session.get(url,proxies={'http':args.proxy,'https':args.proxy},timeout=(15,60),stream=True)
        response.raise_for_status()
        data=bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data)>1024*1024:raise ValueError('Reference response exceeded bound')
        response.close()
    with MemoryFile(bytes(data)) as memory:
        with memory.open() as dataset:
            if dataset.count!=6 or dataset.width!=128 or dataset.height!=128 or dataset.crs.to_string()!=crs or list(dataset.transform)[:6]!=transform:
                raise ValueError('Unexpected reference grid')
    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with target.open('xb') as stream:stream.write(data)
    target.chmod(0o600)
    print('Reference created; no production tile was modified.')


if __name__ == '__main__':
    try:main()
    except Exception as error:
        print('Reference preparation failed: '+type(error).__name__,file=sys.stderr)
        raise SystemExit(1)
