"""Watch completed regions, cache original daily QA, then deliver filtered mosaics.

No changes to downloads or frozen inventory. One QA transfer at a time, explicit
30 m transforms, same-grid local processing, independent per-region failures.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import uuid

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from rasterio.windows import Window
import requests
from shapely.geometry import mapping

import download_hls_optram as h
import hls_daily_mosaic as m
import hls_water_history as water
import run_hls_archive as a

POLICY = 'completed-region-original-qa-daily-v1'


def now():
    return datetime.now(timezone.utc).isoformat()


def validate_config(config):
    names = ('inventory', 'source_root', 'output_root', 'state_root', 'qa_root', 'water_root')
    paths = {key: Path(config[key]) for key in names}
    for path in paths.values():
        if not path.is_absolute():
            raise ValueError('Use absolute paths')
        m.no_symlink(path)
    roots = [paths[key] for key in ('source_root', 'output_root', 'qa_root', 'water_root')]
    for root in roots:
        if root == Path('/nasdata') or not root.is_relative_to('/nasdata'):
            raise ValueError('Data roots must be separate directories below /nasdata')
    for i, left in enumerate(roots):
        for right in roots[i + 1:]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError('Data roots may not overlap')
        if paths['state_root'] == left or left in paths['state_root'].parents:
            raise ValueError('State must be outside the data roots')
    if type(config.get('interval', 300)) is not int or not 60 <= config.get('interval', 300) <= 86400:
        raise ValueError('Polling interval must be 60..86400 seconds')
    if not config.get('nas_source'):
        raise ValueError('Verified NAS source required')
    return paths


def region_ready(inventory, region, source, check):
    subset = a.Inventory(inventory.start, inventory.end, (region,))
    return m.network_complete(subset, source, {}, check)


def collect_region(region, source):
    roi, crs, payload = h.load_roi(region.roi, region.crs)
    scenes, sources = [], []
    for month in region.months:
        if not month.scene_count:
            continue
        path = source / region.code / f'{month.start}__{month.end}'
        manifest, relevant, _, _, _ = m.load_month(path, roi, payload, month.scene_count)
        scenes.extend(relevant)
        sources.append({'path': str(path / 'run_manifest.json'), 'sha256': h.file_hash(path / 'run_manifest.json')})
    if not scenes or len({s['id'] for s in scenes}) != len(scenes):
        raise ValueError('Empty or duplicate original asset history')
    tiles = h.make_tiles(roi, 768)
    anchor = min(tiles, key=lambda t: (t.row, t.col))
    min_row, min_col = min(t.row for t in tiles), min(t.col for t in tiles)
    width = max(t.col + t.width for t in tiles) - min_col
    height = max(t.row + t.height for t in tiles) - min_row
    transform = Affine(30, 0, anchor.left - anchor.col * 30 + min_col * 30,
                       0, -30, anchor.top + anchor.row * 30 - min_row * 30)
    return roi, crs, tiles, scenes, sources, (width, height, transform, min_row, min_col)


def original_daily_qa(ee, scenes):
    """Preserve original QA codes; valid same-day water/land conflicts -> 255.

    Earliest clear raw QA takes priority over cloudy QA. No water/land exclusion
    is applied. Cross-product conflicts are handled by the local history builder.
    """
    images = [ee.Image(s['id']).select('Fmask') for s in
              sorted(scenes, key=lambda s: (s['time_ms'], s['id']), reverse=True)]
    collection = ee.ImageCollection.fromImages(images)
    def valid(q):
        return q.neq(255).And(q.bitwiseAnd(30).eq(0)).And(q.rightShift(6).eq(1))
    clear = collection.map(lambda q: q.updateMask(valid(q))).mosaic()
    wet = collection.map(lambda q: valid(q).And(q.bitwiseAnd(32).neq(0))).max().unmask(0, False)
    land = collection.map(lambda q: valid(q).And(q.bitwiseAnd(32).eq(0))).max().unmask(0, False)
    return clear.unmask(collection.mosaic()).updateMask(wet.And(land).Not()).unmask(255, False).toUint8()


def inspect_qa(path, width, height, transform, crs):
    with rasterio.open(path) as ds:
        if (ds.count != 1 or ds.dtypes[0] != 'uint8' or ds.width != width or ds.height != height or
                ds.transform != transform or ds.crs != rasterio.crs.CRS.from_user_input(crs)):
            raise ValueError('Raw QA exact grid/type mismatch')
        for _, win in ds.block_windows(1):
            values = ds.read(1, window=win)
            if values.dtype != np.uint8:
                raise ValueError('Invalid Fmask encoding')


def cached(path, contract):
    receipt = path.with_suffix('.json')
    if not path.exists() and not receipt.exists():
        return False
    if not path.exists() or not receipt.exists():
        raise ValueError('Uncommitted QA/mask output requires inspection: ' + str(path))
    saved = m.read_json(receipt)
    if saved.get('contract') != contract or saved.get('sha256') != h.file_hash(path):
        raise ValueError('Cached QA source or checksum changed: ' + str(path))
    return True


def download_chunk(image, tile, path, crs, session, contract, check):
    if cached(path, contract):
        inspect_qa(path, tile.width, tile.height, Affine(*tile.transform), crs)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    parameters = {'crs': str(crs), 'crs_transform': tile.transform,
                  'dimensions': [tile.width, tile.height], 'format': 'GEO_TIFF'}
    try:
        check()
        # EE's getDownloadURL mutates its params dict by adding an Image object.
        # Preserve the serializable export provenance separately.
        url = h.get_download_url(image, dict(parameters))
        with session.get(url, stream=True, timeout=(30, 180)) as response:
            response.raise_for_status()
            size = 0
            with temporary.open('xb') as stream:
                for chunk in response.iter_content(256 * 1024):
                    check()
                    size += len(chunk)
                    if size > 4 * 1024 * 1024:
                        raise ValueError('Unexpected oversized QA chunk')
                    stream.write(chunk)
        inspect_qa(temporary, tile.width, tile.height, Affine(*tile.transform), crs)
        with rasterio.open(temporary) as ds:
            values = ds.read(1)
        # Keep a compact exact local copy; provider mask/fill is explicitly 255.
        with rasterio.open(temporary, 'w', driver='GTiff', width=tile.width, height=tile.height,
                count=1, dtype='uint8', crs=crs, transform=Affine(*tile.transform), nodata=255,
                compress='deflate', tiled=True, blockxsize=256, blockysize=256) as ds:
            ds.write(values, 1)
        check()
        os.replace(temporary, path)
        m.atomic_json(path.with_suffix('.json'), {'contract': contract, 'sha256': h.file_hash(path),
                      'export': parameters, 'downloaded_utc': now()})
    finally:
        temporary.unlink(missing_ok=True)


def assemble_day(path, chunks, roi, crs, grid, contract, check):
    width, height, transform, min_row, min_col = grid
    if cached(path, contract):
        inspect_qa(path, width, height, transform, crs)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with rasterio.open(temporary, 'w', driver='GTiff', width=width, height=height,
                count=1, dtype='uint8', crs=crs, transform=transform, nodata=255,
                compress='deflate', tiled=True, blockxsize=256, blockysize=256, BIGTIFF='IF_SAFER') as dst:
            dst.set_band_description(1, 'original_Fmask')
            dst.update_tags(policy=POLICY, resampling='nearest', same_day='valid consensus; conflicts 255')
            for _, win in dst.block_windows(1):
                dst.write(np.full((int(win.height), int(win.width)), 255, dtype='uint8'), 1, window=win)
            for tile, chunk in chunks:
                check()
                with rasterio.open(chunk) as src:
                    values = src.read(1)
                inside = geometry_mask([mapping(roi)], out_shape=values.shape,
                                       transform=Affine(*tile.transform), invert=True)
                values[~inside] = 255
                dst.write(values, 1, window=Window(tile.col-min_col, tile.row-min_row, tile.width, tile.height))
        inspect_qa(temporary, width, height, transform, crs)
        # Verify every written source window after closing the compressed output.
        with rasterio.open(temporary) as dst:
            for tile, chunk in chunks:
                check()
                with rasterio.open(chunk) as src:
                    expected = src.read(1)
                inside = geometry_mask([mapping(roi)], out_shape=expected.shape,
                                       transform=Affine(*tile.transform), invert=True)
                expected[~inside] = 255
                actual = dst.read(1, window=Window(tile.col-min_col, tile.row-min_row, tile.width, tile.height))
                if not np.array_equal(expected, actual):
                    raise ValueError('Daily original QA readback mismatch')
        check()
        os.replace(temporary, path)
        m.atomic_json(path.with_suffix('.json'), {'contract': contract, 'sha256': h.file_hash(path),
                      'created_utc': now(), 'raw_unfiltered_fmask': True})
    finally:
        temporary.unlink(missing_ok=True)


def verify_mask(path, history_sha):
    receipt = m.read_json(path.with_suffix('.json'))
    if (receipt.get('policy') != water.POLICY or receipt.get('sha256') != h.file_hash(path) or
            receipt.get('complete_history_sha256') != history_sha):
        raise ValueError('Static mask is not bound to the complete region history')


def prepare_history(region, config, paths, check, progress):
    roi, crs, tiles, scenes, sources, grid = collect_region(region, paths['source_root'])
    contract = {'policy': POLICY, 'region': region.code, 'sources': sources,
                'inventory_sha256': h.file_hash(paths['inventory']), 'crs': str(crs),
                'grid': [grid[0], grid[1], *list(grid[2])[:6]], 'roi_sha256': h.hash_object(mapping(roi))}
    grouped = defaultdict(list)
    for scene in scenes:
        grouped[(m.day(scene), scene['sensor'])].append(scene)
    root = paths['qa_root'] / region.code
    history_path = paths['state_root'] / region.code / 'original_qa_history.json'
    observations = []
    ee = None
    proxy = config.get('proxy', 'http://127.0.0.1:8118')
    with requests.Session() as session:
        session.trust_env = False
        if proxy:
            session.proxies.update(http=proxy, https=proxy)
        for number, ((date, product), members) in enumerate(sorted(grouped.items()), 1):
            check()
            day_contract = {**contract, 'assets': sorted(s['id'] for s in members), 'date': date, 'product': product}
            path = root / product / date[:4] / f'{region.code}_{product}_{date}_Fmask.tif'
            if not cached(path, day_contract):
                if ee is None:
                    project, _ = h.resolve_project(config.get('project'))
                    if not project:
                        raise ValueError('Existing EE project required')
                    ee = h.initialize_ee(project, proxy)
                    ee.data.setDeadline(60000)
                image = original_daily_qa(ee, members)
                chunks = []
                for tile in tiles:
                    chunk = root / 'chunks' / product / date / tile.name
                    download_chunk(image, tile, chunk, crs, session,
                                   {**day_contract, 'tile': tile.name, 'ee_version': ee.__version__}, check)
                    chunks.append((tile, chunk))
                assemble_day(path, chunks, roi, crs, grid, day_contract, check)
            observations.append({'date': date, 'product': product, 'path': str(path), 'sha256': h.file_hash(path)})
            progress('original-qa', region=region.code, completed_days=number, total_days=len(grouped))
    for source in sources:
        if h.file_hash(Path(source['path'])) != source['sha256']:
            raise ValueError('Source scene manifest changed during QA acquisition')
    manifest = {'raw_unfiltered_fmask': True, 'complete_region_history': contract,
                'observations': observations, 'same_product_day': 'valid raw QA first; water/land conflict skipped'}
    if history_path.exists() and m.read_json(history_path) != manifest:
        raise ValueError('Complete QA history changed; choose new state')
    m.atomic_json(history_path, manifest)
    history_sha = h.file_hash(history_path)
    mask = paths['water_root'] / region.code / 'water_history.tif'
    if mask.exists() or mask.with_suffix('.json').exists():
        verify_mask(mask, history_sha)
    else:
        progress('building-static-water-mask', region=region.code, total_days=len(grouped))
        receipt = water.build(manifest, mask, check)
        receipt['complete_history_sha256'] = history_sha
        m.atomic_json(mask.with_suffix('.json'), receipt)
        verify_mask(mask, history_sha)
    return mask, history_sha


def run_cycle(config, paths, check, progress):
    inventory_sha = h.file_hash(paths['inventory'])
    inventory = a.read_inventory(str(paths['inventory']))
    results = {}
    for region in sorted(inventory.regions, key=lambda r: (r.area_m2, r.code)):
        check()
        try:
            completion = paths['state_root'] / region.code / 'completed.json'
            if completion.exists():
                saved = m.read_json(completion)
                if (saved.get('inventory_sha256') != inventory_sha or saved.get('config_sha256') != h.hash_object(config)
                        or saved.get('policy') != POLICY):
                    raise ValueError('Completed region configuration changed; use new state')
                for source in saved['source_manifests']:
                    if h.file_hash(Path(source['path'])) != source['sha256']:
                        raise ValueError('Completed source manifest changed')
                verify_mask(Path(saved['mask']), saved['result']['history_sha256'])
                results[region.code] = saved['result']
                continue
            progress('checking-region-download', region=region.code, regions=results)
            ready, waiting = region_ready(inventory, region, paths['source_root'], check)
            if not ready:
                results[region.code] = {'phase': 'waiting-for-region-download', 'waiting_for': waiting}
                continue
            progress('region-download-verified', region=region.code)
            mask, history_sha = prepare_history(region, config, paths, check, progress)
            if h.file_hash(paths['inventory']) != inventory_sha:
                raise ValueError('Frozen inventory changed')
            state = paths['state_root'] / region.code / 'daily'
            progress('generating-daily-mosaics', region=region.code)
            args = ['--inventory', str(paths['inventory']), '--source-root', str(paths['source_root']),
                    '--output-root', str(paths['output_root']), '--state-root', str(state),
                    '--nas-source', config['nas_source'], '--regions', region.code,
                    '--layout', 'product-region', '--grouping', 'region-day', '--fuse-products',
                    '--water-mask-root', str(paths['water_root']), '--run']
            code = subprocess.run([sys.executable, str(Path(__file__).with_name('hls_daily_mosaic.py')), *args],
                                  check=False).returncode
            check()
            if code:
                raise ValueError('Daily generator did not complete; inspect regional daily status')
            summary = m.read_json(state / 'status.json')
            if (summary.get('completed_months') != summary.get('total_months') or
                    summary.get('blocked_months') or summary.get('pending_months') != 0):
                raise ValueError('No fully committed regional daily completion receipt')
            results[region.code] = {'phase': 'completed', 'history_sha256': history_sha,
                                    'mask_sha256': h.file_hash(mask), 'daily_state': str(state)}
            history = m.read_json(paths['state_root'] / region.code / 'original_qa_history.json')
            m.atomic_json(completion, {'policy': POLICY, 'config_sha256': h.hash_object(config),
                'inventory_sha256': inventory_sha, 'source_manifests': history['complete_region_history']['sources'],
                'mask': str(mask), 'result': results[region.code], 'completed_utc': now()})
        except InterruptedError:
            raise
        except Exception as error:
            # URLs, authentication headers and provider exceptions are not logged.
            results[region.code] = {'phase': 'blocked', 'error_type': type(error).__name__}
            if isinstance(error, (ValueError, m.MosaicError)):
                results[region.code]['reason'] = str(error)
        progress('region-scan', regions=results)
    progress('completed' if all(row['phase'] == 'completed' for row in results.values()) else 'watching', regions=results)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    config = m.read_json(args.config)
    paths = validate_config(config)
    if not args.run:
        print(json.dumps({'mode': 'plan', 'policy': POLICY, 'trigger': 'each complete verified region',
                          'output_root': str(paths['output_root'])})); return 0
    import fcntl
    paths['state_root'].mkdir(parents=True, exist_ok=True, mode=0o700)
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    guard = h.MountGuard(paths['output_root'], '/nasdata', config['nas_source'])
    mount = [None]
    def check():
        if stopped.is_set(): raise InterruptedError()
        current = guard.check()
        if mount[0] is None: mount[0] = current
        if mount[0] != current: raise ValueError('NAS mount changed')
        if shutil.disk_usage('/nasdata').free < 256 * 1024**3: raise ValueError('NAS reserve below 256 GiB')
    def progress(phase, **values):
        m.atomic_json(paths['state_root'] / 'status.json', {'phase': phase, 'updated_utc': now(), **values})
        print(json.dumps({'phase': phase, **values}, ensure_ascii=False), flush=True)
    with (paths['state_root'] / 'supervisor.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stopped.is_set():
            try:
                check()
                results = run_cycle(config, paths, check, progress)
                if all(row['phase'] == 'completed' for row in results.values()): return 0
            except InterruptedError:
                return 0
            except Exception as error:
                progress('blocked', error_type=type(error).__name__)
            if args.once: return 0
            stopped.wait(config.get('interval', 300))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
