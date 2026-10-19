"""Freeze bounded monthly scene counts for the six-region archive profile."""
import argparse
from datetime import date
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import download_hls_optram as hls
import run_hls_archive as archive
import run_hls_cluster as cluster


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--regions', type=Path, required=True)
    parser.add_argument('--start', type=date.fromisoformat, required=True)
    parser.add_argument('--end', type=date.fromisoformat, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--proxy', default='http://127.0.0.1:8118')
    parser.add_argument('--project')
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Refusing to overwrite a frozen inventory; use a new run directory.')
    regions = json.loads(args.regions.read_text())['regions']
    if {r['code'] for r in regions} != set(archive.REGION_CODES) or len(regions) != len(archive.REGION_CODES):
        parser.error('This archive profile requires SDR/REM/MES/RIS/OZN/TXS; use the single-ROI CLI for other studies.')
    months = cluster.split_months(args.start, args.end, '/nasdata/validation')
    if len(months) > 600:
        parser.error('At most 600 months may be queried per inventory.')
    project, _ = hls.resolve_project(args.project)
    if not project:
        parser.error('Set EE_PROJECT or --project before querying metadata.')
    ee = hls.initialize_ee(project, args.proxy)
    ee.data.setDeadline(60000)
    result = []
    for spec in regions:
        roi, crs, payload = hls.load_roi(spec['roi'], spec['crs'])
        geometry = ee.FeatureCollection([ee.Feature(ee.Geometry(feature['geometry'], proj='EPSG:4326', geodesic=False))
                                         for feature in payload['features']]).geometry()
        collections = [ee.ImageCollection(asset).filterBounds(geometry) for asset, _ in hls.SENSORS.values()]
        queries = {month.name: collections[0].filterDate(month.start.isoformat(), month.end.isoformat()).size().add(
                     collections[1].filterDate(month.start.isoformat(), month.end.isoformat()).size()) for month in months}
        counts = ee.Dictionary(queries).getInfo()
        result.append({'code':spec['code'], 'roi':str(Path(spec['roi']).resolve()), 'crs':crs.to_string(),
                       'area_m2':float(roi.area), 'months':[{'start':m.start.isoformat(), 'end_exclusive':m.end.isoformat(),
                                                          'scene_count':counts[m.name]} for m in months]})
        print(spec['code'] + ': monthly metadata received', flush=True)
    inventory = {'schema_version':1,'start':args.start.isoformat(),'end_exclusive':args.end.isoformat(),'regions':result}
    archive.parse_inventory(inventory)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(inventory, stream, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
