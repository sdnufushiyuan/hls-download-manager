#!/usr/bin/env python3
"""Stream completed HLS blocks into daily regional GeoTIFFs without resampling.

Default: region + UTC date + HLS product, crossing WRS/MGRS boundaries.
Legacy track-day mode retains source WRS/MGRS grouping and metadata lookups.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack
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
REGION_POLICY = 'daily-region-first-valid-whole-pixel-v1'
FUSION_POLICY = 'daily-region-l30-first-s30-fill-v1'
TIME_NODATA = 4294967295
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


def regional_identity(scene):
    match=INDEX_RE.fullmatch(scene['index'])
    date=day(scene)
    if not match or match[2]!=date or scene['sensor'] not in hls.SENSORS:
        raise MosaicError('Invalid HLS acquisition identity')
    return scene['sensor'],date,'REGION'


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


def compose_window(inputs, geometry, tile, recipe_hash, crs, aerosol, validated, check=lambda: None, *, trace=False):
    """One bounded six-band window; pick all bands from the same source pixel."""
    data = np.full((len(hls.BANDS), tile.height, tile.width), hls.NODATA, dtype='float32')
    codes=np.zeros((tile.height,tile.width),dtype='uint8') if trace else None
    times=np.full((tile.height,tile.width),TIME_NODATA,dtype='uint32') if trace else None
    if not inputs:
        return (data,0,codes,times) if trace else (data,0)
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
        if trace:
            codes[take]=1 if record['sensor']=='L30' else 2
            times[take]=(record['time_ms']//1000)%86400
        occupied |= take
    result=(data,int(occupied.sum()))
    return (*result,codes,times) if trace else result


def write_group(output, key, scenes, donors, records, footprints, tiles, manifest, month_root,
                metadata, check=lambda: None, *, roi=None, policy=POLICY,water_mask=None):
    sensor, date, spatial_id = key
    fused=sensor=='FUSED'
    geometry = roi if roi is not None else unary_union([footprints[s['id']] for s in scenes])
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
    allowed = {obs:unary_union([footprints[s['id']] for s in scenes if (s['sensor'],s['time_ms'])==obs])
               for obs in {(s['sensor'],s['time_ms']) for s in scenes}}
    priority=lambda s:((0 if s['sensor']=='L30' else 1) if fused else 0,s['time_ms'],s['id'])
    for scene in sorted(donors, key=priority):
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
            by_tile[(tile.row, tile.col)].append((path, record, allowed[(scene['sensor'],scene['time_ms'])]))
        if row['blocks']:
            source_rows.append(row)
    contract = {'policy':policy, 'group':list(key), 'recipe_hash':manifest['recipe_hash'],
        'target_assets':[s['id'] for s in sorted(scenes,key=lambda s:s['id'])],
        'source_blocks':source_rows, 'catalog':{s['id']:metadata[s['id']] for s in donors},
        'geometry_sha256':hls.hash_object(mapping(geometry)),
        'grid':{'crs':manifest['recipe']['crs'], 'transform':list(transform)[:6], 'width':width,'height':height},
        'bands':list(hls.BANDS),'nodata':hls.NODATA,
        'overlap_rule':'earliest UTC time, then asset ID; first valid complete six-band pixel',
        'dedup_recovery':'existing blocks from the same product and exact acquisition times, clipped to target footprints'}
    if roi is not None:
        contract['extent_rule']='fixed full regional ROI bounding grid; unobserved/invalid pixels remain NoData'
    if fused:
        contract['overlap_rule']='valid L30 first, then same-UTC-day valid S30; within product earliest time/asset ID; whole six-band pixel'
        contract['physical_bands']={'L30':{'red':'B4','nir':'B5','swir2':'B7'},
                                    'S30':{'red':'B4','nir':'B8A','swir2':'B12'}}
        contract['fusion']='No averaging, interpolation or extra bandpass adjustment; existing strict QA/NoData requirements apply to both products'
    if water_mask is not None:
        water_mask=Path(water_mask);no_symlink(water_mask)
        water_receipt=read_json(water_mask.with_suffix('.json'))
        if (water_receipt.get('policy')!='consecutive-valid-date-water-all-dates-v1' or
                water_receipt.get('consecutive_valid_dates')!=2 or
                water_receipt.get('sha256')!=hls.file_hash(water_mask)):
            raise MosaicError('Water-history mask is unverified or uses a different rule')
        with rasterio.open(water_mask) as mask:
            if ((mask.width,mask.height,mask.transform,mask.crs)!=(width,height,transform,rasterio.crs.CRS.from_user_input(manifest['recipe']['crs']))
                    or mask.count!=1 or mask.dtypes[0]!='uint8'):
                raise MosaicError('Water-history mask must match the complete regional grid')
        contract['water_exclusion']={'policy':water_receipt['policy'],'mask_sha256':water_receipt['sha256'],
            'receipt_sha256':hls.file_hash(water_mask.with_suffix('.json')),'scope':'all dates and products'}
    fingerprint = hls.hash_object(contract)
    output = Path(output)
    no_symlink(output)
    receipt = output.with_suffix('.json')
    trace_paths={'source':output.with_name(output.stem+'_source.tif'),
                 'time':output.with_name(output.stem+'_time_utc.tif')} if fused else {}
    for path in trace_paths.values():no_symlink(path)
    if output.exists():
        saved = read_json(receipt)
        if saved.get('fingerprint') != fingerprint or saved.get('sha256') != hls.file_hash(output):
            raise MosaicError('Existing daily output differs; refusing overwrite: '+output.name)
        if fused:
            maps=saved.get('provenance_maps',{})
            if set(maps)!=set(trace_paths) or any(
                    maps[k].get('path')!=str(p) or not p.is_file() or maps[k].get('sha256')!=hls.file_hash(p)
                    for k,p in trace_paths.items()):
                raise MosaicError('Fused provenance maps are missing or changed')
        return {**saved, 'reused':True}
    if receipt.exists():
        raise MosaicError('Daily receipt exists without its TIFF; refusing silent repair')
    if any(path.exists() for path in trace_paths.values()):
        raise MosaicError('Provenance map exists without its verified fused TIFF')
    output.parent.mkdir(parents=True, exist_ok=True)
    no_symlink(output)
    temporary = output.with_name(output.name+'.'+uuid.uuid4().hex+'.part')
    trace_temps={k:temporary.with_name(temporary.name+'.'+k) for k in trace_paths}
    trace_hashes=[]
    pixel_sources={'L30':0,'S30':0}
    valid_pixels, validated = 0, set()
    expected_hashes = []
    recipe = manifest['recipe']
    try:
        with rasterio.Env(GDAL_CACHEMAX=64*1024*1024, GDAL_NUM_THREADS='1'):
            with rasterio.open(temporary,'w',driver='GTiff',width=width,height=height,count=6,
                    dtype='float32',crs=recipe['crs'],transform=transform,nodata=hls.NODATA,
                    tiled=True,blockxsize=256,blockysize=256,compress='DEFLATE',predictor=3,
                    BIGTIFF='IF_SAFER',NUM_THREADS='1') as destination, ExitStack() as resources:
                trace_outputs={}
                exclusion=resources.enter_context(rasterio.open(water_mask)) if water_mask is not None else None
                for kind,path in trace_temps.items():
                    layer=resources.enter_context(rasterio.open(path,'w',driver='GTiff',width=width,height=height,
                        count=1,dtype='uint8' if kind=='source' else 'uint32',nodata=0 if kind=='source' else TIME_NODATA,
                        crs=recipe['crs'],transform=transform,tiled=True,blockxsize=256,blockysize=256,
                        compress='DEFLATE',BIGTIFF='IF_SAFER',NUM_THREADS='1'))
                    layer.set_band_description(1,'source_product' if kind=='source' else 'observation_seconds_utc')
                    layer.update_tags(fingerprint=fingerprint,date_utc=date,
                        meaning='0=NoData,1=L30,2=S30' if kind=='source' else 'seconds since UTC midnight; 4294967295=NoData')
                    trace_outputs[kind]=layer
                for index, name in enumerate(hls.BANDS,1):
                    destination.set_band_description(index,name)
                destination.update_tags(mosaic_policy=policy,fingerprint=fingerprint,product=sensor,
                    date_utc=date,spatial_id=spatial_id,recipe_hash=manifest['recipe_hash'],
                    resampling='none',temporal_averaging='none',aerosol=recipe['aerosol'])
                for row in range(min_row,last_row,tile_size):
                    for col in range(min_col,last_col,tile_size):
                        check()
                        tile = hls.Tile(row,col,origin_left+col*30,origin_top-row*30,
                                        min(tile_size,last_col-col),min(tile_size,last_row-row))
                        result=compose_window(by_tile[(row,col)],geometry,tile,
                            manifest['recipe_hash'],recipe['crs'],recipe['aerosol'],validated,check,trace=fused)
                        values,count=result[:2]
                        window = Window(col-min_col,row-min_row,tile.width,tile.height)
                        if exclusion is not None:
                            codes_to_remove=exclusion.read(1,window=window)
                            if not np.all((codes_to_remove==0)|(codes_to_remove==1)):
                                raise MosaicError('Water-history mask contains invalid codes')
                            remove=codes_to_remove==1;values[:,remove]=hls.NODATA
                            count=int(np.count_nonzero(np.all(values!=hls.NODATA,axis=0)))
                            if fused:result[2][remove]=0;result[3][remove]=TIME_NODATA
                        destination.write(values,window=window)
                        if fused:
                            codes,times=result[2:]
                            trace_outputs['source'].write(codes,1,window=window)
                            trace_outputs['time'].write(times,1,window=window)
                            trace_hashes.append((window,hashlib.sha256(codes.tobytes()).hexdigest(),
                                                  hashlib.sha256(times.tobytes()).hexdigest()))
                            pixel_sources['L30']+=int(np.count_nonzero(codes==1))
                            pixel_sources['S30']+=int(np.count_nonzero(codes==2))
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
            if fused:
                with rasterio.open(trace_temps['source']) as codes,rasterio.open(trace_temps['time']) as times:
                    for window,code_hash,time_hash in trace_hashes:
                        check()
                        if (hashlib.sha256(codes.read(1,window=window).tobytes()).hexdigest()!=code_hash or
                                hashlib.sha256(times.read(1,window=window).tobytes()).hexdigest()!=time_hash):
                            raise MosaicError('Fused provenance pixel verification failed')
        check()
        if water_mask is not None and (
                hls.file_hash(water_mask)!=contract['water_exclusion']['mask_sha256'] or
                hls.file_hash(water_mask.with_suffix('.json'))!=contract['water_exclusion']['receipt_sha256']):
            raise MosaicError('Water-history mask changed during processing')
        digest = hls.file_hash(temporary)
        saved = {'fingerprint':fingerprint,'sha256':digest,'contract':contract,
                 'valid_pixels':valid_pixels,'verified_utc':datetime.now(timezone.utc).isoformat(),
                 'rasterio_version':rasterio.__version__,'gdal_version':rasterio.__gdal_version__,
                 'bytes':temporary.stat().st_size,'path':str(output)}
        if fused:
            saved['source_pixels']=pixel_sources
            saved['provenance_maps']={k:{'path':str(trace_paths[k]),'sha256':hls.file_hash(p)} for k,p in trace_temps.items()}
        # A crash between these two commits leaves a TIFF without a receipt;
        # the next run stops rather than treating it as a verified deliverable.
        os.replace(temporary,output)
        for kind,path in trace_temps.items():os.replace(path,trace_paths[kind])
        atomic_json(receipt,saved)
        return {**saved,'reused':False}
    finally:
        temporary.unlink(missing_ok=True)
        for path in trace_temps.values():path.unlink(missing_ok=True)


def process_month(directory, region, roi, payload, metadata_cache, output_root,
                  expected_count=None, check=lambda:None, emit=print, *, grouping='track-day',fuse_products=False,
                  layout='region-product',water_mask=None):
    if grouping not in ('region-day','track-day'):
        raise MosaicError('Unknown daily grouping')
    regional=grouping=='region-day'
    if water_mask is not None and not regional:raise MosaicError('Water-history mask requires region-day grouping')
    if layout not in ('region-product','product-region'):raise MosaicError('Unknown output layout')
    if fuse_products and not regional:raise MosaicError('Product fusion requires region-day grouping')
    manifest, scenes, records, footprints, tiles = load_month(directory,roi,payload,expected_count)
    if regional:
        # Frozen indices/timestamps are sufficient for date+product grouping.
        # No WRS lookup, EE initialization or network request is needed.
        metadata={scene['id']:{'system:time_start':scene['time_ms'],
                  'MGRS_TILE_ID':INDEX_RE.fullmatch(scene['index'])[1],
                  'origin':'frozen archive manifest and HLS index'} for scene in scenes}
    else:
        metadata = metadata_cache.load(scenes)
    groups, observations = defaultdict(list), defaultdict(list)
    for scene in scenes:
        key=regional_identity(scene) if regional else identity(scene,metadata[scene['id']])
        groups[key].append(scene)
        observations[(scene['sensor'],scene['time_ms'])].append(scene)
        if fuse_products:groups[('FUSED',day(scene),'REGION')].append(scene)
    results = []
    for key, members in sorted(groups.items()):
        check()
        donors = {s['id']:s for member in members
                  for s in observations[(member['sensor'],member['time_ms'])]
                  if regional or key[0]=='S30' or identity(s,metadata[s['id']])==key}
        sensor,date,spatial_id = key
        suffix='' if regional else '_'+spatial_id
        parts=(region,sensor) if layout=='region-product' else (sensor,region)
        output = Path(output_root).joinpath(*parts,date[:4],f'{region}_{sensor}_{date}{suffix}.tif')
        result = write_group(output,key,members,list(donors.values()),records,footprints,
                             tiles,manifest,Path(directory),metadata,check,
                             roi=roi if regional else None,
                             policy=FUSION_POLICY if sensor=='FUSED' else REGION_POLICY if regional else POLICY,
                             water_mask=water_mask)
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
    result.add_argument('--grouping',choices=('region-day','track-day'),default='region-day',
                        help='Default: one fixed-extent regional TIFF per UTC day and product; legacy track-day is opt-in')
    result.add_argument('--fuse-products',action='store_true',help='Also create L30-first, same-day S30-fill regional TIFFs plus pixel source/time maps')
    result.add_argument('--layout',choices=('region-product','product-region'),default='region-product',
                        help='Use product-region for sibling L30/S30/FUSED folders, each containing region/year')
    result.add_argument('--require-network-complete',action='store_true',
                        help='Wait for the entire frozen inventory before processing any region/month')
    result.add_argument('--water-mask-root',type=Path,help='Require verified REGION/water_history.tif masks before processing')
    result.add_argument('--proxy',default='http://127.0.0.1:8118')
    result.add_argument('--nas-source',required=True,help='Expected CIFS source; verify before reading/writing NAS')
    result.add_argument('--reserve-gib',type=float,default=256)
    result.add_argument('--interval',type=int,default=300)
    result.add_argument('--watch',action='store_true',help='Poll newly completed months; keep verified month receipts')
    result.add_argument('--run',action='store_true',help='Default is an inventory-only plan with no writes/network')
    return result


def committed_blocks_exist(directory,scenes,records,check=lambda:None):
    """One NAS directory listing per scene, avoiding repeated ancestor stats.

    load_month has already checked scene directories/filenames and parent links.
    Required TIFFs and sidecars must still be regular files, never symlinks.
    SHA and raster contents remain checked when composing the actual output.
    """
    for scene in scenes:
        check()
        expected={name for entry in records[scene['id']]['tiles']
                  for name in (entry['file'],str(Path(entry['file']).with_suffix('.json')))}
        if not expected:continue
        scene_dir=Path(directory)/scene['sensor']/scene['index']
        try:
            with os.scandir(scene_dir) as entries:
                present={entry.name for entry in entries
                         if entry.name in expected and entry.is_file(follow_symlinks=False)}
        except FileNotFoundError:return False
        if present!=expected:return False
    return True


def network_complete(inventory,source,geometry_cache,check=lambda:None):
    """Hints can delay work, never authorize it; verify committed scene metadata last."""
    jobs=archive.fair_jobs(inventory,str(source))
    for job in jobs:
        check()
        if archive.processed_scene_hint(job)!=job.scene_count:return False,job.name
    for region in inventory.regions:
        if region.code not in geometry_cache:
            roi,_,payload=hls.load_roi(region.roi,region.crs);geometry_cache[region.code]=(roi,payload)
        roi,payload=geometry_cache[region.code]
        for month in region.months:
            if not month.scene_count:continue
            check();directory=Path(source)/region.code/f'{month.start}__{month.end}'
            try:
                _,scenes,records,_,_=load_month(directory,roi,payload,month.scene_count)
                if not committed_blocks_exist(directory,scenes,records,check):return False,str(directory)
            except (MosaicError,FileNotFoundError,KeyError,ValueError):return False,str(directory)
    return True,None


def main(argv=None):
    args=parser().parse_args(argv)
    if args.water_mask_root and args.grouping!='region-day':raise SystemExit('Water masks require region-day')
    if args.fuse_products and args.grouping!='region-day':raise SystemExit('Fusion requires --grouping region-day')
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
    policy=FUSION_POLICY if args.fuse_products else REGION_POLICY if args.grouping=='region-day' else POLICY
    regions=sorted((r for r in inventory.regions if r.code in args.regions),key=lambda r:(r.area_m2,r.code))
    jobs=[(region,month) for region in regions for month in region.months if month.scene_count and
          (args.month is None or f'{month.start}__{month.end}'==args.month)]
    print(json.dumps({'policy':policy,'source_root':str(source),'output_root':str(output),
                      'region_order':[r.code for r in regions],'months':len(jobs),
                      'grouping':args.grouping,'fuse_products':args.fuse_products,'layout':args.layout,
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
    network_verified=False
    try:
        with cluster.output_lock(str(output),args.state_root):
            while True:
                check()
                import subprocess
                mount=subprocess.run(['findmnt','--noheadings','--output','SOURCE,FSTYPE','--target','/nasdata'],
                                     capture_output=True,text=True,check=True,timeout=10).stdout
                if not any(row.split()==[args.nas_source,'cifs'] for row in mount.splitlines()):
                    raise MosaicError('NAS mount source mismatch')
                if args.require_network_complete and not network_verified:
                    network_verified,waiting=network_complete(inventory,source,geometry_cache,check)
                    if not network_verified:
                        atomic_json(args.state_root/'status.json',{'phase':'waiting-for-entire-network',
                            'waiting_for':waiting,'output_root':str(output),
                            'updated_utc':datetime.now(timezone.utc).isoformat()})
                        print('等待整个网络下载完成；不生成日图：'+str(waiting),flush=True)
                        if not args.watch:return 1
                        stopped.wait(args.interval);continue
                masks={r.code:args.water_mask_root/r.code/'water_history.tif' for r in regions} if args.water_mask_root else {}
                missing=[r for r,p in masks.items() if not p.is_file() or not p.with_suffix('.json').is_file()]
                if missing:
                    atomic_json(args.state_root/'status.json',{'phase':'waiting-for-original-Fmask-history',
                        'regions':missing,'output_root':str(output)})
                    print('等待完整原始Fmask历史掩膜：'+','.join(missing),flush=True)
                    if not args.watch:return 1
                    stopped.wait(args.interval);continue
                mask_hashes={r:hls.file_hash(p) for r,p in masks.items()}
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
                        if (saved.get('policy')!=policy or saved.get('output_root')!=str(output) or
                                saved.get('layout','region-product')!=args.layout or
                                saved.get('water_mask_sha256')!=mask_hashes.get(region.code) or
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
                                             lambda value:print(value,flush=True),grouping=args.grouping,
                                             fuse_products=args.fuse_products,layout=args.layout,water_mask=masks.get(region.code))
                        result.update(policy=policy,output_root=str(output),layout=args.layout,water_mask_sha256=mask_hashes.get(region.code))
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
                    summary={'policy':policy,'updated_utc':datetime.now(timezone.utc).isoformat(),
                             'output_root':str(output),'total_months':len(jobs),'completed_months':len(completed),
                             'groups':sum(row['groups'] for row in completed),
                             'pending_months':len(jobs)-len(completed)-len(failed),
                             'blocked_months':failed,'active_region':region.code,'active_month':name}
                    atomic_json(args.state_root/'status.json',summary)
                summary={'policy':policy,'updated_utc':datetime.now(timezone.utc).isoformat(),
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
