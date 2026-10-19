"""Build private loopback-only Mihomo routing and optimizer configuration."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hls_node_optimizer import atomic_json


def build(nodes, initial, root, out, probe, secret):
    if not 1 <= len(nodes) <= 100 or len({n['name'] for n in nodes}) != len(nodes):
        raise ValueError('Expected 1–100 uniquely named nodes')
    if any(n.get('dialer-proxy') for n in nodes):
        raise ValueError('Nodes with external dialer dependencies are not supported')
    if any(n['name'].startswith('HLS-') for n in nodes):
        raise ValueError('HLS- is reserved for routing groups')
    if initial not in {n['name'] for n in nodes}:
        raise ValueError('Initial node is not in the supplied node set')
    names = [initial]+[n['name'] for n in nodes if n['name'] != initial]
    health = {'url':'https://earthengine.googleapis.com/','expected-status':'404','timeout':5000,'lazy':False}
    routing = {'mixed-port':8118,'allow-lan':False,'bind-address':'127.0.0.1','mode':'rule','log-level':'warning',
               'ipv6':False,'external-controller':'127.0.0.1:19091','secret':secret,
               'profile':{'store-selected':True},'proxies':nodes,
               'proxy-groups':[dict(health,name='HLS-FAST',type='url-test',proxies=names,interval=300,tolerance=200),
                   {'name':'HLS-PREFERRED','type':'select','proxies':names,'default-selected':initial},
                   dict(health,name='HLS-ROUTE',type='fallback',proxies=['HLS-PREFERRED','HLS-FAST'],interval=30)],
               'rules':['MATCH,HLS-ROUTE'],
               'listeners':[{'name':'hls-probe-'+str(i),'type':'http','listen':'127.0.0.1','port':18400+i,
                             'proxy':node['name'],'users':[]} for i,node in enumerate(nodes)]}
    optimizer = {'api_url':'http://127.0.0.1:19091','api_secret':secret,'program_root':str(root),
                 'download_proxy':'http://127.0.0.1:8118','state_file':str(out/'optimizer-state.json'),
                 'probe':probe,'nodes':[{'name':n['name'],'port':18400+i} for i,n in enumerate(nodes)]}
    return routing, optimizer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nodes', type=Path, required=True, help='Private JSON/YAML with a proxies array')
    parser.add_argument('--probe', type=Path, required=True)
    parser.add_argument('--initial', required=True)
    parser.add_argument('--out', type=Path, default=Path('private'))
    args = parser.parse_args()
    import yaml
    nodes = yaml.safe_load(args.nodes.read_text(encoding='utf-8'))['proxies']
    probe = json.loads(args.probe.read_text())
    out = args.out.resolve()
    if any((out/name).exists() for name in ['mihomo.json','optimizer.json']):
        parser.error('Existing configuration is not overwritten; use a new private output directory.')
    out.mkdir(mode=0o700,parents=True,exist_ok=True)
    out.chmod(0o700)
    os.umask(0o077)
    routing, optimizer = build(nodes,args.initial,Path(__file__).resolve().parents[1],out,probe,secrets.token_urlsafe(32))
    atomic_json(out/'mihomo.json', routing)
    atomic_json(out/'optimizer.json', optimizer)
    print('Private loopback-only configs created; validate mihomo.json with your installed Mihomo binary.')


if __name__ == '__main__':
    main()
