"""Render user systemd units. Installation is explicit; never starts downloads."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hls_node_optimizer import atomic_json


def word(value):
    value=str(value)
    if any(c in value for c in '\r\n\x00'):
        raise ValueError('Control characters are not allowed in unit configuration')
    return '"'+value.replace('%','%%').replace('\\','\\\\').replace('"','\\"')+'"'


def render(config, root):
    python=root/'.venv/bin/python'
    run=Path(config['run_root'])
    for path in [root,run,Path(config['output_root']),Path(config['mihomo_binary']),Path(config['proxy_config']),Path(config['optimizer_config'])]:
        if not path.is_absolute():raise ValueError('All deployment paths must be absolute')
    if config['output_root'] != '/nasdata' and not config['output_root'].startswith('/nasdata/'):
        raise ValueError('Archive output must be below the verified /nasdata mount')
    environment=root/'private/deployment.env'
    # WorkingDirectory is a scalar path, unlike ExecStart/EnvironmentFile.
    # Its quotes are literal on supported systemd versions.
    directory=str(root).replace('%','%%')
    common=f'WorkingDirectory={directory}\nEnvironmentFile={word(environment)}\nNice=10\nUMask=0077\n'
    archive_args=[python,'-u',root/'run_hls_archive.py','--inventory',run/'inventory.json','--out',config['output_root'],
                  '--mount-source',config['nas_source'],'--nodes',*config.get('nodes',['node1','node2','master']),
                  '--workers',str(config.get('workers',4)),'--schedule',config.get('schedule','smallest-first'),
                  '--resume-pending-first','--run']
    if config.get('schedule','smallest-first') not in ('fair','smallest-first'):raise ValueError('Unknown schedule')
    if not set(config.get('nodes',['node1','node2','master'])).issubset({'node1','node2','master'}):raise ValueError('Unknown node role')
    if type(config.get('workers',4)) is not int or not 1<=config.get('workers',4)<=8:raise ValueError('Workers must be 1–8')
    units = {
      'hls-download-proxy.service':f'''[Unit]
Description=HLS loopback Mihomo proxy
ConditionHost=master
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
{common}ExecStart={' '.join(map(word,[config['mihomo_binary'],'-d',root/'private/mihomo-runtime','-f',config['proxy_config']]))}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
''',
      'hls-archive.service':f'''[Unit]
Description=Resumable HLS regional archives
ConditionHost=master
After=hls-download-proxy.service
Wants=hls-download-proxy.service
StartLimitIntervalSec=0

[Service]
Type=simple
{common}ExecStart={' '.join(map(word,archive_args))}
Restart=on-failure
RestartPreventExitStatus=1
RestartSec=300
KillMode=mixed
TimeoutStopSec=45

[Install]
WantedBy=default.target
''',
      'hls-node-optimizer.service':f'''[Unit]
Description=Verified HLS route optimizer
ConditionHost=master
After=hls-download-proxy.service
Requires=hls-download-proxy.service
PartOf=hls-download-proxy.service

[Service]
Type=simple
{common}ExecStart={' '.join(map(word,[python,'-u',root/'hls_node_optimizer.py','--config',config['optimizer_config']]))}
Restart=always
RestartSec=30
NoNewPrivileges=yes
TimeoutStopSec=15

[Install]
WantedBy=default.target hls-download-proxy.service
'''}
    daily=config.get('daily_mosaic')
    if daily:
        daily_out=Path(daily['output_root']);daily_state=Path(daily['state_root'])
        source=Path(config['output_root'])
        if (not daily_out.is_absolute() or not daily_state.is_absolute() or
                not daily_out.is_relative_to('/nasdata') or daily_out==Path('/nasdata') or
                daily_out==source or source in daily_out.parents or daily_out in source.parents or
                daily_state==source or source in daily_state.parents):
            raise ValueError('Daily mosaics need separate absolute output/state directories')
        interval=daily.get('interval',300)
        if type(interval) is not int or not 30<=interval<=86400:raise ValueError('Daily interval must be 30–86400')
        daily_args=[python,'-u',root/'hls_daily_mosaic.py','--inventory',run/'inventory.json',
                    '--source-root',source,'--output-root',daily_out,'--state-root',daily_state,
                    '--nas-source',config['nas_source'],'--interval',str(interval),'--watch','--run']
        units['hls-daily-mosaic.service']=f'''[Unit]
Description=Verified HLS daily WRS/MGRS GeoTIFF mosaics
ConditionHost=master
After=hls-download-proxy.service
Wants=hls-download-proxy.service

[Service]
Type=simple
WorkingDirectory={directory}
EnvironmentFile={word(environment)}
ExecStart={' '.join(map(word,daily_args))}
Restart=on-failure
RestartSec=300
RestartPreventExitStatus=1
Nice=15
UMask=0077
CPUQuota=50%
MemoryHigh=256M
MemoryMax=512M
IOWeight=50
TimeoutStopSec=30

[Install]
WantedBy=default.target
'''
    return units


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('private/rendered-systemd'))
    parser.add_argument('--install',action='store_true')
    args=parser.parse_args()
    if not sys.platform.startswith('linux'):parser.error('Run service rendering/installation on Linux.')
    root=Path(__file__).resolve().parents[1]
    config=json.loads(args.config.read_text())
    units=render(config,root)
    private=root/'private';private.mkdir(mode=0o700,exist_ok=True);private.chmod(0o700)
    environment={
      'HLS_RUN_ROOT':config['run_root'],'HLS_OUTPUT_ROOT':config['output_root'],'HLS_NAS_SOURCE':config['nas_source'],
      'EE_PROJECT':config['ee_project'],'OMP_NUM_THREADS':'1','OPENBLAS_NUM_THREADS':'1',
      'MKL_NUM_THREADS':'1','NUMEXPR_NUM_THREADS':'1','GDAL_NUM_THREADS':'1'}
    payload='\n'.join(key+'='+word(value).replace('%%','%') for key,value in environment.items())+'\n'
    env_path=private/'deployment.env'
    if env_path.exists() and env_path.read_text()!=payload:parser.error('Refusing to overwrite a different deployment.env')
    env_path.write_text(payload);env_path.chmod(0o600)
    run=Path(config['run_root']);run.mkdir(parents=True,exist_ok=True)
    run_config={'service':'hls-archive.service','output_root':config['output_root']}
    if (run/'run.json').exists() and json.loads((run/'run.json').read_text())!=run_config:parser.error('Existing run metadata differs')
    atomic_json(run/'run.json',run_config)
    (root/'runs').mkdir(exist_ok=True)
    atomic_json(root/'runs/hls_ssh_route.json',{'route':'linux_mihomo'})
    args.output.mkdir(mode=0o700,parents=True,exist_ok=True)
    for name,payload in units.items():
        target=args.output/name
        if target.exists() and target.read_text()!=payload:parser.error('Use a new render directory; existing unit differs')
        target.write_text(payload)
    if args.install:
        target=Path.home()/'.config/systemd/user';target.mkdir(parents=True,exist_ok=True)
        for name,payload in units.items():
            if (target/name).exists() and (target/name).read_text()!=payload:parser.error('Existing installed unit differs: '+name)
        for name in units:shutil.copy2(args.output/name,target/name)
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        print('Units installed. No services were enabled or started.')
    else:
        print('Units rendered. Inspect them and rerun with --install when ready.')


if __name__=='__main__':main()
