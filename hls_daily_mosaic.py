#!/usr/bin/env python3
"""Stream completed HLS blocks into daily track/tile GeoTIFFs without resampling.

L30: UTC date + source Landsat WRS path/row. S30: UTC date + MGRS tile.
Only catalog metadata is fetched; raster data comes from the existing archive.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import threading
import uuid

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.windows import Window
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

import download_hls_optram as hls
import run_hls_archive as archive
import run_hls_cluster as cluster

POLICY = 'daily-track-first-valid-whole-pixel-v1'
MONTH_RE = re.compile(r'\d{4}-\d{2}-\d{2}__\d{4}-\d{2}-\d{2}')
INDEX_RE = re.compile(r'T(\d{2}[A-Z]{3})_(\d{8})T\d{6}')
PRODUCT_RE = re.compile(r'LC0[89]_L1[A-Z0-9]{2}_(\d{3})(\d{3})_(\d{8})_\d{8}_\d{2}_(?:T1|T2|RT)')


class MosaicError(RuntimeError):
    pass


class MonthPending(MosaicError):
    pass


class CatalogUnavailable(MosaicError):
    pass


def read_json(path, limit=8 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink():
        raise MosaicError('Refusing a symlink metadata file')
    with path.open('r', encoding='utf-8-sig') as stream:
        text = stream.read(limit + 1)
    if len(text) > limit:
        raise MosaicError('Metadata exceeds the bounded size limit')
    return json.loads(text)


def no_symlink(path):
    for item in (Path(path), *Path(path).parents):
        if item.is_symlink():
            raise MosaicError('Refusing a symlink in an input/output path')


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def day(scene):
    value = scene.get('time_ms')
    if type(value) is not int:
        raise MosaicError('Scene timestamp is missing')
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime('%Y%m%d')


def identity(scene, metadata):
    """Never interpret an HLS MGRS identifier as a Landsat WRS identifier."""
    match = INDEX_RE.fullmatch(scene['index'])
    date = day(scene)
    if not match or match[2] != date or metadata.get('system:time_start') != scene['time_ms']:
        raise MosaicError('Catalog/index/manifest acquisition dates disagree')
    if scene['sensor'] == 'S30':
        tile = metadata.get('MGRS_TILE_ID')
        if tile != match[1]:
            raise MosaicError('S30 MGRS metadata disagrees with its HLS index')
        return 'S30', date, 'T' + tile
    if scene['sensor'] != 'L30':
        raise MosaicError('Unknown HLS product')
    product = metadata.get('LANDSAT_PRODUCT_ID')
    if not isinstance(product, str):
        raise MosaicError('L30 source Landsat product metadata is missing')
    sources = PRODUCT_RE.findall(product)
    if (not sources or PRODUCT_RE.sub('',product).strip(' ,;\t\r\n') or
            any(d != date or not 1 <= int(p) <= 233 or not 1 <= int(r) <= 248
                          for p, r, d in sources)):
        raise MosaicError('Invalid source Landsat date or WRS path/row')
    keys = {(p, r) for p, r, _ in sources}
    if len(keys) != 1:
        raise MosaicError('One HLS asset contains multiple WRS path/rows; strict grouping needs source disambiguation')
    path, row = next(iter(keys))
    return 'L30', date, f'P{path}_R{row}'


class MetadataCache:
    """Initialize lazily: hls.initialize_ee calls ee.Initialize(project=project).

    At most 32 immutable asset identities per metadata RPC, cached permanently.
    Serial batches bound response size; no per-pixel requests or raster export.
    """
    def __init__(self, root, project=None, proxy='http://127.0.0.1:8118'):
        self.root, self.project, self.proxy = Path(root), project, proxy
        self.ee = None

    def fetch_batch(self, batch):
        query = {str(i): self.ee.Image(scene['id']).toDictionary(
            ['LANDSAT_PRODUCT_ID', 'MGRS_TILE_ID', 'PRODUCT_URI', 'system:time_start'])
            for i, (scene, _) in enumerate(batch)}
        try:
            return self.ee.Dictionary(query).getInfo()
        except Exception as error:
            raise CatalogUnavailable('Catalog metadata request failed (' + type(error).__name__ + ')') from None

    def load(self, scenes):
        result, missing = {}, []
        for scene in scenes:
            key = hashlib.sha256(scene['id'].encode()).hexdigest()
            path = self.root / (key + '.json')
            if path.exists():
                saved = read_json(path, 64 * 1024)
                if saved.get('asset_id') != scene['id']:
                    raise MosaicError('Catalog cache asset mismatch')
                result[scene['id']] = saved['properties']
            else:
                missing.append((scene, path))
        if missing and self.ee is None:
            project, _ = hls.resolve_project(self.project)
            if not project:
                raise MosaicError('EE_PROJECT or --project is required for uncached catalog metadata')
            try:
                self.ee = hls.initialize_ee(project, self.proxy)
                self.ee.data.setDeadline(60000)
            except Exception as error:
                raise CatalogUnavailable('Catalog initialization failed ('+type(error).__name__+')') from None
        for offset in range(0, len(missing), 32):
            batch = missing[offset:offset + 32]
            # A bounded dictionary of source identity properties, never raster arrays.
            values = self.fetch_batch(batch)
            for i, (scene, path) in enumerate(batch):
                properties = values[str(i)]
                if not isinstance(properties,dict) or properties.get('system:time_start')!=scene['time_ms']:
                    raise MosaicError('Catalog acquisition timestamp mismatch')
                # Cache the exact response even when WRS provenance is ambiguous;
                # identity() later blocks output without repeatedly querying EE.
                atomic_json(path, {'asset_id': scene['id'], 'properties': properties,
                    'checked_utc': datetime.now(timezone.utc).isoformat(),
                    'ee_version': self.ee.__version__})
                result[scene['id']] = properties
        return result


def load_month(directory, roi, payload, expected_count=None):
    directory = Path(directory)
    no_symlink(directory)
    try:
        manifest = read_json(directory / 'run_manifest.json')
    except FileNotFoundError:
        raise MonthPending('Month has not started') from None
    recipe = manifest['recipe']
    if (manifest.get('recipe_hash') != hls.hash_object(recipe) or
            recipe.get('roi_sha256') != hls.hash_object(payload) or
            recipe.get('bands') != list(hls.BANDS) or recipe.get('resolution_m') != 30 or
            recipe.get('nodata') != hls.NODATA):
        raise MosaicError('Archive recipe/ROI/bands/grid mismatch')
    scenes = manifest['scenes']
    if not isinstance(scenes, list) or len(scenes) > 100000:
        raise MosaicError('Invalid month scene list')
    if expected_count is not None and len(scenes) != expected_count:
        raise MosaicError('Month count differs from frozen inventory; refusing a partial/sample month')
    tiles = {tile.name: tile for tile in hls.make_tiles(roi, recipe['tile_size'])}
    records, relevant, footprints, seen = {}, [], {}, set()
    start = datetime.fromisoformat(recipe['start']).date()
    end = datetime.fromisoformat(recipe['end_exclusive']).date()
    for scene in scenes:
        sensor, index = scene['sensor'], scene['index']
        if (sensor not in hls.SENSORS or not INDEX_RE.fullmatch(index) or
                scene['id'] != hls.SENSORS[sensor][0] + '/' + index or scene['id'] in seen or
                not start <= datetime.fromtimestamp(scene['time_ms']/1000, timezone.utc).date() < end):
            raise MosaicError('Invalid, duplicate or out-of-month asset')
        seen.add(scene['id'])
        scene_dir = directory / sensor / index
        if scene_dir.is_symlink() or scene_dir.parent.is_symlink():
            raise MosaicError('Refusing a symlink scene directory')
        try:
            record = read_json(scene_dir / 'scene.json')
        except FileNotFoundError:
            raise MonthPending('Month still has unfinished scene records') from None
        if (record.get('recipe_hash') != manifest['recipe_hash'] or
                any(record.get(key) != scene[key] for key in ('id', 'sensor', 'index', 'time_ms')) or
                not isinstance(record.get('tiles'), list)):
            raise MosaicError('Scene completion record identity mismatch')
        names = [entry['file'] for entry in record['tiles']]
        if len(set(names)) != len(names) or any(name not in tiles for name in names):
            raise MosaicError('Invalid or duplicate committed block')
        records[scene['id']] = record
        footprint = shape(scene['projected_footprint'])
        if not footprint.is_valid:
            raise MosaicError('Invalid projected scene footprint')
        footprint = roi.intersection(footprint)
        if not footprint.is_empty and footprint.area > 0:
            relevant.append(scene)
            footprints[scene['id']] = footprint
    return manifest, relevant, records, footprints, tiles


def compose_window(inputs, geometry, tile, recipe_hash, crs, aerosol, validated, check=lambda: None):
    """One bounded six-band window; pick all bands from the same source pixel."""
    data = np.full((len(hls.BANDS), tile.height, tile.width), hls.NODATA, dtype='float32')
    occupied = np.zeros((tile.height, tile.width), dtype=bool)
    inside = geometry_mask([mapping(geometry)], out_shape=occupied.shape,
                           transform=Affine(*tile.transform), invert=True, all_touched=False)
    for path, record, allowed_geometry in inputs:
        check()
        if path not in validated:
            stats = hls.cached_tile(path, tile, crs, recipe_hash, aerosol, record['id'],
                                    selection_hash=record.get('selection_hash'))
            if stats is None:
                raise MosaicError('Source block failed checksum/grid/mask validation: ' + path.name)
            validated.add(path)
        with rasterio.open(path) as source:
            values = source.read()
        allowed = geometry_mask([mapping(allowed_geometry)],out_shape=occupied.shape,
                                transform=Affine(*tile.transform),invert=True,all_touched=False)
        take = inside & allowed & ~occupied & np.all(values != hls.NODATA, axis=0)
        data[:, take] = values[:, take]
        occupied |= take
    return data, int(occupied.sum())


def write_group(output, key, scenes, donors, records, footprints, tiles, manifest, month_root,
                metadata, check=lambda: None):
    sensor, date, spatial_id = key
    geometry = unary_union([footprints[s['id']] for s in scenes])
    selected = [tile for tile in tiles.values() if geometry.intersection(box(*tile.bounds)).area > 0]
    if not selected:
        return None
    tile_size = manifest['recipe']['tile_size']
    min_row, min_col = min(t.row for t in selected), min(t.col for t in selected)
    last_row, last_col = max(t.row+t.height for t in selected), max(t.col+t.width for t in selected)
    anchor = min(tiles.values(), key=lambda t: (t.row, t.col))
    origin_left, origin_top = anchor.left-anchor.col*30, anchor.top+anchor.row*30
    transform = Affine(30, 0, origin_left+min_col*30, 0, -30, origin_top-min_row*30)
    width, height = last_col-min_col, last_row-min_row
    source_rows, by_tile = [], defaultdict(list)
    allowed = {stamp:unary_union([footprints[s['id']] for s in scenes if s['time_ms']==stamp])
               for stamp in {s['time_ms'] for s in scenes}}
    for scene in sorted(donors, key=lambda s: (s['time_ms'], s['id'])):
        record = records[scene['id']]
        row = {'asset_id':scene['id'], 'time_ms':scene['time_ms'],
               'scene_record_sha256':hls.hash_object(record), 'blocks':[]}
        for entry in record['tiles']:
            tile = tiles[entry['file']]
            if geometry.intersection(box(*tile.bounds)).area <= 0:
                continue
            path = Path(month_root)/scene['sensor']/scene['index']/tile.name
            sidecar = path.with_suffix('.json')
            if path.is_symlink() or sidecar.is_symlink():
                raise MosaicError('Refusing a symlink source block')
            saved = read_json(sidecar, 128*1024)
            if saved.get('scene_id') != scene['id'] or saved.get('recipe_hash') != manifest['recipe_hash']:
                raise MosaicError('Block sidecar identity mismatch')
            row['blocks'].append({'file':str(path.relative_to(month_root)), 'sha256':saved['sha256']})
            by_tile[(tile.row, tile.col)].append((path, record, allowed[scene['time_ms']]))
        if row['blocks']:
            source_rows.append(row)
    contract = {'policy':POLICY, 'group':list(key), 'recipe_hash':manifest['recipe_hash'],
        'target_assets':[s['id'] for s in sorted(scenes,key=lambda s:s['id'])],
        'source_blocks':source_rows, 'catalog':{s['id']:metadata[s['id']] for s in donors},
        'geometry_sha256':hls.hash_object(mapping(geometry)),
        'grid':{'crs':manifest['recipe']['crs'], 'transform':list(transform)[:6], 'width':width,'height':height},
        'bands':list(hls.BANDS),'nodata':hls.NODATA,
        'overlap_rule':'earliest UTC time, then asset ID; first valid complete six-band pixel',
        'dedup_recovery':'existing blocks from the same product and exact acquisition times, clipped to target footprints'}
    fingerprint = hls.hash_object(contract)
    output = Path(output)
    no_symlink(output)
    receipt = output.with_suffix('.json')
    if output.exists():
        saved = read_json(receipt)
        if saved.get('fingerprint') != fingerprint or saved.get('sha256') != hls.file_hash(output):
            raise MosaicError('Existing daily output differs; refusing overwrite: '+output.name)
        return {**saved, 'reused':True}
    if receipt.exists():
        raise MosaicError('Daily receipt exists without its TIFF; refusing silent repair')
    output.parent.mkdir(parents=True, exist_ok=True)
    no_symlink(output)
    temporary = output.with_name(output.name+'.'+uuid.uuid4().hex+'.part')
    valid_pixels, validated = 0, set()
    expected_hashes = []
    recipe = manifest['recipe']
    try:
        with rasterio.Env(GDAL_CACHEMAX=64*1024*1024, GDAL_NUM_THREADS='1'):
            with rasterio.open(temporary,'w',driver='GTiff',width=width,height=height,count=6,
                    dtype='float32',crs=recipe['crs'],transform=transform,nodata=hls.NODATA,
                    tiled=True,blockxsize=256,blockysize=256,compress='DEFLATE',predictor=3,
                    BIGTIFF='IF_SAFER',NUM_THREADS='1') as destination:
                for index, name in enumerate(hls.BANDS,1):
                    destination.set_band_description(index,name)
                destination.update_tags(mosaic_policy=POLICY,fingerprint=fingerprint,product=sensor,
                    date_utc=date,spatial_id=spatial_id,recipe_hash=manifest['recipe_hash'],
                    resampling='none',temporal_averaging='none',aerosol=recipe['aerosol'])
                for row in range(min_row,last_row,tile_size):
                    for col in range(min_col,last_col,tile_size):
                        check()
                        tile = hls.Tile(row,col,origin_left+col*30,origin_top-row*30,
                                        min(tile_size,last_col-col),min(tile_size,last_row-row))
                        values, count = compose_window(by_tile[(row,col)],geometry,tile,
                            manifest['recipe_hash'],recipe['crs'],recipe['aerosol'],validated,check)
                        window = Window(col-min_col,row-min_row,tile.width,tile.height)
                        destination.write(values,window=window)
                        expected_hashes.append((window,hashlib.sha256(values.tobytes()).hexdigest()))
                        valid_pixels += count
            # Read the entire output in bounded windows; exact bytes include NoData.
            with rasterio.open(temporary) as result:
                if (result.count != 6 or tuple(result.descriptions) != hls.BANDS or
                        result.nodata != hls.NODATA or result.transform != transform or
                        result.crs != rasterio.crs.CRS.from_user_input(recipe['crs'])):
                    raise MosaicError('Daily TIFF grid/band verification failed')
                for window, digest in expected_hashes:
                    check()
                    if hashlib.sha256(result.read(window=window).tobytes()).hexdigest()!=digest:
                        raise MosaicError('Daily TIFF pixel verification failed')
        check()
        digest = hls.file_hash(temporary)
        saved = {'fingerprint':fingerprint,'sha256':digest,'contract':contract,
                 'valid_pixels':valid_pixels,'verified_utc':datetime.now(timezone.utc).isoformat(),
                 'rasterio_version':rasterio.__version__,'gdal_version':rasterio.__gdal_version__,
                 'bytes':temporary.stat().st_size,'path':str(output)}
        # A crash between these two commits leaves a TIFF without a receipt;
        # the next run stops rather than treating it as a verified deliverable.
        os.replace(temporary,output)
        atomic_json(receipt,saved)
        return {**saved,'reused':False}
    finally:
        temporary.unlink(missing_ok=True)


def process_month(directory, region, roi, payload, metadata_cache, output_root,
                  expected_count=None, check=lambda:None, emit=print):
    manifest, scenes, records, footprints, tiles = load_month(directory,roi,payload,expected_count)
    metadata = metadata_cache.load(scenes)
    groups, observations = defaultdict(list), defaultdict(list)
    for scene in scenes:
        groups[identity(scene,metadata[scene['id']])].append(scene)
        observations[(scene['sensor'],scene['time_ms'])].append(scene)
    results = []
    for key, members in sorted(groups.items()):
        check()
        donors = {s['id']:s for member in members
                  for s in observations[(member['sensor'],member['time_ms'])]
                  if key[0]=='S30' or identity(s,metadata[s['id']])==key}
        sensor,date,spatial_id = key
        output = Path(output_root)/region/sensor/date[:4]/f'{region}_{sensor}_{date}_{spatial_id}.tif'
        result = write_group(output,key,members,list(donors.values()),records,footprints,
                             tiles,manifest,Path(directory),metadata,check)
        if result:
            results.append(result)
            emit(f"日景{'复用' if result['reused'] else '完成'} {output.name}；有效像元 {result['valid_pixels']:,}")
    return {'month':Path(directory).name,'groups':len(results),
            'outputs':[{'path':r['path'],'sha256':r['sha256'],'valid_pixels':r['valid_pixels']} for r in results],
            'manifest_sha256':hls.file_hash(Path(directory)/'run_manifest.json'),
            'verified_utc':datetime.now(timezone.utc).isoformat()}


def parser():
    result=argparse.ArgumentParser(description=__doc__)
    result.add_argument('--inventory',required=True,type=Path)
    result.add_argument('--source-root',required=True,type=Path)
    result.add_argument('--output-root',required=True,type=Path)
    result.add_argument('--state-root',required=True,type=Path)
    result.add_argument('--regions',nargs='+',choices=archive.REGION_CODES,default=list(archive.REGION_CODES))
    result.add_argument('--month',help='Optional YYYY-MM-DD__YYYY-MM-DD subset for a first sample')
    result.add_argument('--project')
    result.add_argument('--proxy',default='http://127.0.0.1:8118')
    result.add_argument('--nas-source',required=True,help='Expected CIFS source; verify before reading/writing NAS')
    result.add_argument('--reserve-gib',type=float,default=256)
    result.add_argument('--interval',type=int,default=300)
    result.add_argument('--watch',action='store_true',help='Poll newly completed months; keep verified month receipts')
    result.add_argument('--run',action='store_true',help='Default is an inventory-only plan with no writes/network')
    return result


def main(argv=None):
    args=parser().parse_args(argv)
    if (args.month and not MONTH_RE.fullmatch(args.month)) or not 30<=args.interval<=86400:
        raise SystemExit('Invalid month or polling interval')
    if not math.isfinite(args.reserve_gib) or args.reserve_gib<1:
        raise SystemExit('Reserve must be a positive finite GiB value')
    no_symlink(args.source_root);no_symlink(args.output_root);no_symlink(args.state_root)
    source,output=args.source_root.resolve(),args.output_root.resolve()
    if (source==output or source in output.parents or output in source.parents or
            not source.is_relative_to('/nasdata') or not output.is_relative_to('/nasdata') or
            output==Path('/nasdata')):
        raise SystemExit('Use separate non-nested archive/daily directories below /nasdata')
    if args.state_root.resolve()==source or source in args.state_root.resolve().parents:
        raise SystemExit('State/catalog cache must be outside the source archive')
    inventory=archive.read_inventory(str(args.inventory))
    regions=sorted((r for r in inventory.regions if r.code in args.regions),key=lambda r:(r.area_m2,r.code))
    jobs=[(region,month) for region in regions for month in region.months if month.scene_count and
          (args.month is None or f'{month.start}__{month.end}'==args.month)]
    print(json.dumps({'policy':POLICY,'source_root':str(source),'output_root':str(output),
                      'region_order':[r.code for r in regions],'months':len(jobs),
                      'grouping':'L30 UTC date+WRS path/row; S30 UTC date+MGRS; separate products',
                      'mode':'run' if args.run else 'plan'},ensure_ascii=False),flush=True)
    if not args.run:
        return 0
    cluster.require_master()
    args.state_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    stopped=threading.Event()
    storage=hls.MountGuard(output,'/nasdata',args.nas_source)
    original_mount=[None]
    def check():
        if stopped.is_set():
            raise InterruptedError('Daily mosaic worker stopped')
        mount_id=storage.check()
        if original_mount[0] is None:
            original_mount[0]=mount_id
        if mount_id!=original_mount[0]:
            raise MosaicError('NAS mount changed during processing')
        if shutil.disk_usage('/nasdata').free < args.reserve_gib*1024**3:
            raise MosaicError('NAS free space fell below reserve')
    previous={sig:signal.signal(sig,lambda *_:stopped.set()) for sig in (signal.SIGTERM,signal.SIGINT)}
    cache=MetadataCache(args.state_root/'catalog',args.project,args.proxy)
    geometry_cache={}
    try:
        with cluster.output_lock(str(output),args.state_root):
            while True:
                check()
                import subprocess
                mount=subprocess.run(['findmnt','--noheadings','--output','SOURCE,FSTYPE','--target','/nasdata'],
                                     capture_output=True,text=True,check=True,timeout=10).stdout
                if not any(row.split()==[args.nas_source,'cifs'] for row in mount.splitlines()):
                    raise MosaicError('NAS mount source mismatch')
                pending,completed,failed=0,[],[]
                last_region=None
                for region,month in jobs:
                    check()
                    if last_region and last_region!=region.code and (pending or failed):
                        break  # Keep downstream processing focused on the smallest unfinished region.
                    last_region=region.code
                    if shutil.disk_usage('/nasdata').free < args.reserve_gib*1024**3:
                        raise MosaicError('NAS free space fell below reserve')
                    name=f'{month.start}__{month.end}'
                    directory=source/region.code/name
                    receipt=args.state_root/'months'/region.code/(name+'.json')
                    if receipt.exists():
                        saved=read_json(receipt)
                        if (saved.get('policy')!=POLICY or saved.get('output_root')!=str(output) or
                                saved.get('manifest_sha256')!=hls.file_hash(directory/'run_manifest.json')):
                            raise MosaicError('Completed month receipt conflicts with source/output')
                        completed.append({'region':region.code,**saved})
                        continue
                    try:
                        if region.code not in geometry_cache:
                            roi,crs,payload=hls.load_roi(region.roi,region.crs)
                            geometry_cache[region.code]=(roi,payload)
                        roi,payload=geometry_cache[region.code]
                        result=process_month(directory,region.code,roi,payload,cache,output,month.scene_count,check,
                                             lambda value:print(value,flush=True))
                        result.update(policy=POLICY,output_root=str(output))
                        atomic_json(receipt,result)
                        completed.append({'region':region.code,**result})
                    except MonthPending:
                        pending+=1
                    except (MosaicError,ValueError,KeyError,OSError) as error:
                        failed.append({'region':region.code,'month':name,'error_type':type(error).__name__,
                                       'reason':str(error) if isinstance(error,MosaicError) else type(error).__name__})
                        print(f'日景暂未交付 {region.code}/{name}：{failed[-1]["reason"]}',flush=True)
                        if isinstance(error,CatalogUnavailable):
                            break  # One failed metadata batch per poll; no request storm.
                    summary={'policy':POLICY,'updated_utc':datetime.now(timezone.utc).isoformat(),
                             'output_root':str(output),'total_months':len(jobs),'completed_months':len(completed),
                             'groups':sum(row['groups'] for row in completed),
                             'pending_months':len(jobs)-len(completed)-len(failed),
                             'blocked_months':failed,'active_region':region.code,'active_month':name}
                    atomic_json(args.state_root/'status.json',summary)
                summary={'policy':POLICY,'updated_utc':datetime.now(timezone.utc).isoformat(),
                         'output_root':str(output),'total_months':len(jobs),'completed_months':len(completed),
                         'groups':sum(row['groups'] for row in completed),
                         'pending_months':len(jobs)-len(completed)-len(failed),
                         'blocked_months':failed,'active_region':last_region if pending or failed else None,
                         'regions':{r.code:{'completed_months':sum(row['region']==r.code for row in completed),
                             'total_months':sum(item.code==r.code for item,_ in jobs)} for r in regions}}
                atomic_json(args.state_root/'status.json',summary)
                print(f'日景扫描：完成 {len(completed)}/{len(jobs)} 月，TIFF {summary["groups"]}，待交付 {summary["pending_months"]}，阻塞 {len(failed)}。',flush=True)
                if not args.watch:
                    return 1 if failed or pending else 0
                stopped.wait(args.interval)
    except InterruptedError:
        return 0
    except (MosaicError,OSError,ValueError,RuntimeError) as error:
        print('日景处理停止：'+type(error).__name__+(': '+str(error) if isinstance(error,MosaicError) else ''),file=sys.stderr)
        return 1
    finally:
        for sig,handler in previous.items():
            signal.signal(sig,handler)


if __name__=='__main__':
    raise SystemExit(main())
