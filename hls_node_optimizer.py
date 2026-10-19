"""Select a consistently faster HLS route; Mihomo handles rapid failover.

Credentials and signed URLs never enter state or logs. The probe reads a small
fixed HLS tile into memory; production data and downloader services are untouched.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

GROUP = 'HLS-PREFERRED'
ROUTE = 'HLS-ROUTE'
INTERVAL = 300
COOLDOWN = 600
IMPROVEMENT = 0.25


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.chmod(0o600)
    os.replace(temporary, path)


def ranked(samples):
    """Require two valid downloads; 429/auth/content failures do not win."""
    result = []
    for name, rows in samples.items():
        valid = [row for row in rows if row.get('ok') is True
                 and type(row.get('seconds')) in (int, float)
                 and math.isfinite(row['seconds']) and row['seconds'] > 0]
        if len(rows) != 2 or len(valid) != 2:
            continue
        result.append({'name': name, 'seconds': statistics.median(r['seconds'] for r in valid),
                       'round_seconds': [r['seconds'] for r in valid]})
    return sorted(result, key=lambda row: (row['seconds'], row['name']))


def choose(current, ranking, state, now, *, unavailable=False):
    """Two successive cycles and a 25% improvement in both rounds, with dwell."""
    updated = dict(state)
    if not ranking:
        updated.update(challenger=None, wins=0)
        return current, updated, 'no_verified_candidate'
    best = ranking[0]
    old = next((row for row in ranking if row['name'] == current), None)
    if old is None and unavailable:
        updated.update(challenger=None, wins=0, last_switch=now)
        return best['name'], updated, 'verified_failover'
    if best['name'] == current or old is None:
        updated.update(challenger=None, wins=0)
        return current, updated, 'keep_current_or_native_failover'
    improves = all(a <= b * (1 - IMPROVEMENT)
                   for a, b in zip(best['round_seconds'], old['round_seconds']))
    if not improves:
        updated.update(challenger=None, wins=0)
        return current, updated, 'improvement_below_threshold'
    recent = 0 <= now - state.get('last_cycle', 0) <= INTERVAL * 2
    wins = state.get('wins', 0) + 1 if recent and state.get('challenger') == best['name'] else 1
    updated.update(challenger=best['name'], wins=wins)
    if wins < 2:
        return current, updated, 'confirm_next_cycle'
    if now - state.get('last_switch', 0) < COOLDOWN:
        return current, updated, 'minimum_dwell'
    updated.update(challenger=None, wins=0, last_switch=now)
    return best['name'], updated, 'consistently_faster'


def candidates(names, delays, current, cursor, limit=5):
    """Keep the incumbent, test low latency routes, and rotate one challenger."""
    if current not in names or not names:
        raise ValueError('invalid current route')
    selected = [current]
    order = sorted((name for name in names if delays.get(name, 0) > 0), key=lambda n: delays[n])
    for name in order[:limit - 2]:
        if name not in selected:
            selected.append(name)
    # The rotating slot prevents a high-latency but high-throughput route from
    # being permanently excluded. Failed rotation probes never become winners.
    for offset in range(len(names)):
        name = names[(cursor + offset) % len(names)]
        if name not in selected:
            selected.append(name)
            return selected, (cursor + offset + 1) % len(names)
    return selected, cursor


class Controller:
    def __init__(self, config):
        import requests
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers['Authorization'] = 'Bearer ' + config['api_secret']
        self.base = config['api_url']
        if self.base not in ('http://127.0.0.1:19091', 'http://127.0.0.1:19092'):
            raise ValueError('controller must be the owned loopback endpoint')

    def get(self, path):
        response = self.session.get(self.base + path, timeout=(2, 8))
        response.raise_for_status()
        return response.json()

    def select(self, node):
        response = self.session.put(self.base + '/proxies/' + GROUP, json={'name': node}, timeout=(2, 8))
        response.raise_for_status()
        if self.get('/proxies/' + GROUP).get('now') != node:
            raise RuntimeError('selection not confirmed')


class Probe:
    def __init__(self, root, proxy, settings):
        self.root, self.proxy, self.settings = root, proxy, settings

    def prepare(self):
        import rasterio
        sys.path.insert(0, str(self.root))
        import download_hls_optram as hls
        project, _ = hls.resolve_project()
        with rasterio.open(self.settings['reference']) as dataset:
            self.transform, self.crs = list(dataset.transform)[:6], dataset.crs.to_string()
            expected = dataset.read(window=((0, 128), (0, 128)))
            self.expected_hash = hashlib.sha256(expected.tobytes()).hexdigest()
        _, _, payload = hls.load_roi(self.settings['roi'], self.crs)
        ee = hls.initialize_ee(project, self.proxy)
        ee.data.setDeadline(30000)
        region = ee.FeatureCollection([ee.Feature(ee.Geometry(payload['features'][0]['geometry'],
                                    proj='EPSG:4326', geodesic=False))])
        image = hls.build_image(ee, ee.Image(self.settings['scene_id']),
                                self.settings['sensor'], region,
                                self.settings.get('reflectance_scale', 1.0), self.settings.get('aerosol', 'low'))
        self.url = image.getDownloadURL({'crs': self.crs, 'crs_transform': self.transform,
                                         'dimensions': [128, 128], 'format': 'GEO_TIFF'})

    def run(self, port):
        import requests
        from rasterio.io import MemoryFile
        start = time.monotonic()
        row = {'ok': False}
        try:
            with requests.Session() as session:
                session.trust_env = False
                proxy = 'http://127.0.0.1:' + str(port)
                session.proxies = {'http': proxy, 'https': proxy}
                with session.get(self.url, stream=True, timeout=(5, 8), verify=True) as response:
                    row['http_status'] = response.status_code
                    response.raise_for_status()
                    payload = bytearray()
                    for chunk in response.iter_content(65536):
                        payload.extend(chunk)
                        if len(payload) > 1024 * 1024 or time.monotonic() - start > 15:
                            raise TimeoutError('bounded probe')
            transferred = time.monotonic() - start
            with MemoryFile(bytes(payload)) as memory:
                with memory.open() as dataset:
                    if (dataset.count != 6 or dataset.width != 128 or dataset.height != 128 or
                            dataset.crs.to_string() != self.crs or list(dataset.transform)[:6] != self.transform or
                            hashlib.sha256(dataset.read().tobytes()).hexdigest() != self.expected_hash):
                        raise ValueError('probe content mismatch')
            row.update(ok=True, bytes=len(payload), seconds=round(transferred, 4), pixel_hash_verified=True)
        except Exception as error:
            row.update(error_type=type(error).__name__, seconds=round(time.monotonic() - start, 4))
        return row


def cycle(config, state, *, dry_run=False):
    import subprocess
    controller = Controller(config)
    all_proxies = controller.get('/proxies')['proxies']
    current = all_proxies[GROUP]['now']
    names = [node['name'] for node in config['nodes']]
    if set(all_proxies[GROUP]['all']) != set(names):
        raise RuntimeError('candidate set changed')
    delays = {}
    for name in names:
        history = all_proxies[name].get('history', [])
        if history:
            try:
                latest = history[-1]
                age = datetime.now(timezone.utc).timestamp() - datetime.fromisoformat(latest['time'].replace('Z', '+00:00')).timestamp()
                if 0 <= age <= 600:
                    delays[name] = latest.get('delay', 0)
            except (KeyError, ValueError, TypeError):
                pass
    # A running core also supports other tasks; no EE probes while this archive
    # is stopped or complete. Native public HTTPS failover remains available.
    active = subprocess.run(['systemctl','--user','is-active','hls-archive.service'],
                            capture_output=True, text=True, timeout=5).stdout.strip()
    if active != 'active':
        return {**state, 'status': 'archive_not_active', 'selected': current,
                'effective': all_proxies[ROUTE].get('now'), 'updated_at': datetime.now(timezone.utc).isoformat()}
    selected, cursor = candidates(names, delays, current, state.get('cursor', 0))
    probe = Probe(Path(config['program_root']), config['download_proxy'], config['probe'])
    probe.prepare()
    ports = {node['name']: node['port'] for node in config['nodes']}
    samples = {name: [] for name in selected}
    # Reverse the second pass to reduce order bias; at most two extra transfers.
    for order in (selected, list(reversed(selected))):
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda name: probe.run(ports[name]), order))
        for name, row in zip(order, rows):
            samples[name].append(row)
    ranking = ranked(samples)
    now = time.time()
    quota_or_auth = any(row.get('http_status') in (401, 403, 429) for row in samples[current])
    unavailable = all_proxies[current].get('alive') is False and not quota_or_auth
    target, updated, reason = choose(current, ranking, state, now, unavailable=unavailable)
    switched = target != current
    if switched and not dry_run:
        controller.select(target)
    return {**updated, 'updated_at': datetime.now(timezone.utc).isoformat(),
            'last_cycle': now, 'cursor': cursor, 'status': 'dry_run' if dry_run else 'running',
            'selected': current if dry_run else target, 'proposed': target,
            'effective': controller.get('/proxies/' + ROUTE).get('now'),
            'reason': reason, 'ranking': ranking, 'samples': samples,
            'candidate_count': len(names), 'probe_bytes': sum(row.get('bytes', 0) for rows in samples.values() for row in rows),
            'performance_interval_seconds': INTERVAL, 'native_failover_interval_seconds': 30}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    config = json.loads(args.config.read_text())
    state_path = Path(config['state_file'])
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError:
        state = {}
    if not isinstance(state, dict):
        raise ValueError('state must be an object')
    import fcntl
    lock = os.open(str(state_path)+'.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    while True:
        started = time.monotonic()
        try:
            new_state = cycle(config, state, dry_run=args.dry_run)
            if not args.dry_run:
                atomic_json(state_path, new_state)
                state = new_state
            print(json.dumps(new_state, ensure_ascii=True), flush=True)
        except Exception as error:
            # Keep old selection and native fallback on quota/auth/transport or
            # controller trouble. Never log raw exception text or signed URLs.
            state = {**state, 'status': 'performance_probe_unavailable', 'error_type': type(error).__name__,
                     'updated_at': datetime.now(timezone.utc).isoformat(), 'challenger': None, 'wins': 0}
            if not args.dry_run:
                atomic_json(state_path, state)
            print(json.dumps({key: state.get(key) for key in ('status','error_type','updated_at') }), flush=True)
        if args.once or args.dry_run:
            return 0 if state.get('status') != 'performance_probe_unavailable' else 1
        time.sleep(max(1, INTERVAL - (time.monotonic() - started)))


if __name__ == '__main__':
    raise SystemExit(main())
