"""Read-only Chinese terminal view of Linux hosts, all visible users and HLS.

No task control, EE requests, sudo, shell history or proxy credentials. Remote
host probes use the existing SSH aliases and print already-redacted JSON only.
"""
from __future__ import annotations
import argparse
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata

REGIONS=('RIS','TXS','REM','OZN','SDR','MES')
PHASES={'checking-region-download':'核验完整研究区','region-download-verified':'整理原始QA清单',
 'original-qa':'补取原始QA','qa-transfer-retry':'网络/限流退避重试','building-static-water-mask':'构建历史水体掩膜',
 'generating-daily-mosaics':'生成正式日图','waiting-for-region-download':'等待该区下载完整',
 'waiting-for-entire-network':'等待下载','watching':'等待下一轮检查','region-scan':'检查研究区',
 'completed':'已完成','blocked':'受阻，需查看原因'}
ACTIVE={'active':'运行','inactive':'未运行','failed':'失败','activating':'启动/重试','deactivating':'正在停止'}


def scrub(value):
    value=str(value)
    value=re.sub(r'(?i)(https?://)[^\s/@:]+:[^\s/@]+@',r'\1***@',value)
    value=re.sub(r'(?i)([?&](?:token|access_token|signature|x-goog-signature|key)=)[^&\s"\']+',r'\1***',value)
    value=re.sub(r'(?i)(\bBearer\s+)[^\s"\']+',r'\1***',value)
    value=re.sub(r'''(?ix)((?:--?)?(?:password|passwd|api[_-]?key|api[_-]?secret|refresh[_-]?token|access[_-]?token|connection[_-]?token|token|secret|authorization)\b(?:["']?\s*[:=]\s*|\s+))(?:"[^"]*"|'[^']*'|[^\s,]+)''',r'\1***',value)
    return ''.join(c if c.isprintable() else ' ' for c in value)[:8192]


def fit(value,width):
    result=[];used=0
    for char in scrub(value):
        cost=0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in 'WF' else 1
        if used+cost>max(0,width-1):return ''.join(result)+'…'
        result.append(char);used+=cost
    return ''.join(result)


def run(args,timeout=8):
    result=subprocess.run(args,capture_output=True,text=True,errors='replace',timeout=timeout)
    if result.returncode:raise RuntimeError('Read-only command unavailable')
    return result.stdout


def read_json(path,limit=8*1024*1024):
    path=Path(path)
    if path.is_symlink():raise ValueError('Symlink metadata')
    with path.open('rb') as f:raw=f.read(limit+1)
    if len(raw)>limit:raise ValueError('Oversized metadata')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('Invalid metadata')
    return value


def elapsed(seconds):
    if seconds is None:return '采样中'
    seconds=max(0,int(seconds))
    if seconds>=86400:return f'{seconds/86400:.1f}天'
    if seconds>=3600:return f'{seconds/3600:.1f}小时'
    return f'{seconds/60:.0f}分钟'


def progress_bar(done,total,width=16):
    fraction=min(1,max(0,done/total)) if total else 0
    n=int(fraction*width)
    return '['+'#'*n+'-'*(width-n)+']'+f' {fraction:5.1%}'


class HostSampler:
    def __init__(self,proc=Path('/proc')):
        self.proc=Path(proc);self.previous=None;self.users={}
        self.hz=os.sysconf('SC_CLK_TCK');self.page=os.sysconf('SC_PAGE_SIZE')

    def sample(self):
        import pwd
        now=time.monotonic();uptime=float((self.proc/'uptime').read_text().split()[0])
        cpu=list(map(int,(self.proc/'stat').read_text().splitlines()[0].split()[1:9]))
        total=sum(cpu);idle=cpu[3]+cpu[4]
        busy=None
        if self.previous and total>self.previous['total']:
            busy=100*(1-(idle-self.previous['idle'])/(total-self.previous['total']))
        memory={line.split(':')[0]:int(line.split()[1])*1024 for line in (self.proc/'meminfo').read_text().splitlines()}
        network={}
        for line in (self.proc/'net/dev').read_text().splitlines()[2:]:
            name,raw=line.split(':');fields=raw.split();name=name.strip()
            if name=='lo':continue
            rx,tx=int(fields[0]),int(fields[8]);speed=None
            if self.previous and name in self.previous['network']:
                dt=now-self.previous['at'];old=self.previous['network'][name]
                speed=[max(0,(rx-old[0])/dt),max(0,(tx-old[1])/dt)] if dt>0 else None
            network[name]={'bytes':[rx,tx],'speed':speed}
        processes=[];ticks={};seen=denied=0
        for path in self.proc.iterdir():
            if not path.name.isdigit():continue
            seen+=1
            try:
                text=(path/'stat').read_text();left=text.index('(');right=text.rindex(')')
                name=text[left+1:right];fields=text[right+1:].split()
                start=int(fields[19]);used=int(fields[11])+int(fields[12]);identity=(int(path.name),start)
                ticks[identity]=used;percent=None
                if self.previous and identity in self.previous['ticks']:
                    dt=now-self.previous['at']
                    percent=max(0,(used-self.previous['ticks'][identity])/self.hz/dt*100) if dt>0 else None
                uid=path.stat().st_uid
                if uid not in self.users:
                    try:self.users[uid]=pwd.getpwuid(uid).pw_name
                    except KeyError:self.users[uid]=str(uid)
                try:
                    with (path/'cmdline').open('rb') as stream:command=stream.read(32768).replace(b'\0',b' ').decode('utf-8','replace').strip()
                    visible=True
                except PermissionError:command='命令参数无读取权限';visible=False;denied+=1
                processes.append({'pid':int(path.name),'user':self.users[uid],'state':fields[0],
                    'cpu':percent,'rss':max(0,int(fields[21]))*self.page,'age':max(0,uptime-start/self.hz),
                    'command':scrub(command or '['+name+']'),'command_visible':visible})
            except (FileNotFoundError,ProcessLookupError,ValueError,IndexError):continue
            except PermissionError:denied+=1
        self.previous={'at':now,'total':total,'idle':idle,'ticks':ticks,
                       'network':{k:v['bytes'] for k,v in network.items()}}
        return {'host':socket.gethostname(),'timestamp':time.time(),'cpu':busy,'cores':os.cpu_count(),
            'load':list(os.getloadavg()),'memory_total':memory['MemTotal'],
            'memory_used':memory['MemTotal']-memory.get('MemAvailable',memory.get('MemFree',0)),
            'network':network,'processes':processes,'visible_pids':seen,'restricted_pids':denied}


class Rate:
    def __init__(self):self.samples=deque();self.key=None
    def sample(self,key,done,total,now=None):
        now=time.monotonic() if now is None else now
        if key!=self.key or (self.samples and done<self.samples[-1][1]):self.samples.clear()
        self.key=key;self.samples.append((now,done))
        while len(self.samples)>1 and self.samples[0][0]<now-900:self.samples.popleft()
        dt=now-self.samples[0][0];delta=done-self.samples[0][1]
        if dt<120 or delta<3:return None
        per_min=delta/dt*60
        return {'per_minute':per_min,'remaining_seconds':max(0,total-done)/per_min*60,'window_seconds':dt}


def list_services():
    rows=[]
    for scope,extra in [('系统',[]),('当前用户',['--user'])]:
        try:
            values=json.loads(run(['systemctl',*extra,'list-units','--type=service','--all','--output=json','--no-pager']))
            rows.extend({'scope':scope,**row} for row in values)
        except (OSError,RuntimeError,ValueError,subprocess.TimeoutExpired):
            rows.append({'scope':scope,'unit':'读取暂不可用','active':'unknown','sub':''})
    return rows


def validate(config):
    required=('program_root','archive_run_root','archive_output_root','auto_state_root','daily_output_root','nas_source')
    for key in required:
        if not isinstance(config.get(key),str):raise ValueError('Missing monitor configuration: '+key)
        if key!='nas_source' and not Path(config[key]).is_absolute():raise ValueError('Absolute paths required')
    for host in config.get('hosts',[]):
        if host not in ('node1','node2'):raise ValueError('Only existing node1/node2 aliases supported')


class Collector:
    def __init__(self,config):
        validate(config);self.config=config;self.lock=threading.Lock();self.stop=threading.Event()
        self.data={'hosts':{},'services':[],'qa':{},'archive':{},'events':[], 'messages':[],'regions':{},
                   'archive_service':config.get('archive_service','hls-archive.service')}
        self.threads=[];self.cursor=None;self.qa_rates={};self.event_rows=deque(maxlen=200)

    def put(self,**values):
        with self.lock:self.data.update(values)

    def snapshot(self):
        with self.lock:return json.loads(json.dumps(self.data))

    def host(self,name,value):
        with self.lock:self.data['hosts'][name]=value

    def start(self):
        workers=[self.local_loop,self.quick_loop,self.archive_loop]
        workers.extend(lambda name=host:self.remote_loop(name) for host in self.config.get('hosts',[]))
        for worker in workers:
            thread=threading.Thread(target=worker,daemon=True);thread.start();self.threads.append(thread)

    def local_loop(self):
        sampler=HostSampler()
        while not self.stop.is_set():
            try:self.host('master',sampler.sample())
            except (OSError,ValueError):self.host('master',{'error':'主机统计读取失败','timestamp':time.time()})
            self.stop.wait(5)

    def remote_loop(self,host):
        script=str(Path(__file__).resolve());python=self.config.get('python',sys.executable)
        while not self.stop.is_set():
            try:
                raw=run(['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=4','-o','ServerAliveInterval=5',
                    '-o','ServerAliveCountMax=1',host,shlex.join([python,script,'--host-probe'])],timeout=12)
                value=json.loads(raw)
                if not isinstance(value.get('processes'),list):raise ValueError('Invalid host probe')
                value['received_at']=time.time()  # Host clocks may differ; freshness uses local reception.
                self.host(host,value)
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired):
                self.host(host,{'error':'SSH暂不可用；不影响该节点后台任务','timestamp':time.time()})
            self.stop.wait(15)

    def qa_events(self):
        unit=self.config.get('auto_service','hls-auto-daily.service')
        args=['journalctl','--user','-u',unit,'--no-pager','--output=json','-n','10000',
              '--output-fields=MESSAGE,__CURSOR,__REALTIME_TIMESTAMP']
        if self.cursor:args.append('--after-cursor='+self.cursor)
        else:args.append('--since=-24h')
        try:
            raw=run(args)
            with self.lock:regions=dict(self.data['regions'])
            for line in raw.splitlines():
                try:
                    row=json.loads(line);self.cursor=row.get('__CURSOR',self.cursor)
                    value=json.loads(row.get('MESSAGE',''))
                    if not isinstance(value,dict):continue
                    stamp=float(row['__REALTIME_TIMESTAMP'])/1e6
                    for region,status in value.get('regions',{}).items():
                        if region in REGIONS:regions[region]={**regions.get(region,{}),**status}
                    region=value.get('region')
                    if region in REGIONS:regions[region]={**regions.get(region,{}),**value}
                    if value.get('phase') not in ('original-qa','checking-region-download'):
                        message=PHASES.get(value.get('phase'),value.get('phase',''))
                        if value.get('http_status'):message+=f" HTTP {value['http_status']}"
                        if value.get('retry'):message+=f"；重试 {value['retry']}/{value.get('retry_limit','?')}，等待{value.get('wait_seconds','?')}秒"
                        self.event_rows.append(datetime.fromtimestamp(stamp).astimezone().strftime('%H:%M:%S')+' '+str(region or '')+' '+message)
                except (ValueError,TypeError,KeyError):continue
            self.put(regions=regions,events=list(self.event_rows))
        except (OSError,RuntimeError,subprocess.TimeoutExpired):self.cursor=None

    def quick_loop(self):
        last_services=last_counts=0
        while not self.stop.is_set():
            try:
                state=read_json(Path(self.config['auto_state_root'])/'status.json',1024*1024)
                self.put(qa=state)
                self.qa_events()
                snapshot=self.snapshot();rates={}
                for region,row in snapshot['regions'].items():
                    if type(row.get('completed_days')) is int and type(row.get('total_days')) is int:
                        rate=self.qa_rates.setdefault(region,Rate()).sample((region,row['total_days']),row['completed_days'],row['total_days'])
                        if row.get('phase')=='original-qa':rates[region]=rate
                self.put(qa_rates=rates)
                if time.monotonic()-last_services>15:
                    self.put(services=list_services());last_services=time.monotonic()
                if time.monotonic()-last_counts>60:
                    counts=defaultdict(int);regional=defaultdict(int)
                    for path in Path(self.config['auto_state_root']).glob('*/daily/months/*/*.json'):
                        month=read_json(path)
                        for output in month.get('outputs',[]):
                            match=re.fullmatch(r'[A-Z]+_(L30|S30|FUSED)_\d{8}\.tif',Path(output['path']).name)
                            if match:
                                counts[match[1]]+=1;regional[Path(output['path']).name.split('_')[0]]+=1
                    mounted=run(['findmnt','--noheadings','--output','SOURCE,FSTYPE','--target','/nasdata']).split()
                    nas_ok=mounted==[self.config['nas_source'],'cifs']
                    free=shutil.disk_usage('/nasdata').free if nas_ok else None
                    self.put(daily_counts=dict(counts),region_daily_counts=dict(regional),nas={'mounted':nas_ok,'free':free})
                    optimizer=self.config.get('optimizer_state')
                    if optimizer:
                        opt=read_json(optimizer)
                        self.put(route={'selected':opt.get('selected'),'updated_at':opt.get('updated_at')})
                    last_counts=time.monotonic()
            except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as error:
                self.put(messages=['部分任务统计暂不可读：'+type(error).__name__])
            self.stop.wait(5)

    def archive_loop(self):
        try:
            import hls_progress_state as state
            from hls_progress_rates import BlockActivity,ProgressRates
            state.SERVICE=self.config.get('archive_service','hls-archive.service')
            state.OUTPUT_ROOT=Path(self.config['archive_output_root']);state.NAS_SOURCE=self.config['nas_source']
            os.environ['HLS_RUN_ROOT']=self.config['archive_run_root']
            collector=state.ProgressCollector(Path(self.config['program_root']))
            rates=ProgressRates();activity=BlockActivity()
            while not self.stop.is_set():
                snapshot=collector.snapshot();snapshot['throughput']=rates.update(snapshot)
                snapshot['download_activity']=activity.snapshot(snapshot)
                self.put(archive=snapshot)
                self.stop.wait(30)
        except Exception as error:self.put(archive={'loading_error':type(error).__name__})


def process_rows(data,query='',sort='cpu'):
    rows=[]
    for host,view in data.get('hosts',{}).items():
        for proc in view.get('processes',[]):
            row={'host':host,**proc}
            if not query or query.lower() in (' '.join([host,str(row['pid']),row['user'],row['command']])).lower():rows.append(row)
    return sorted(rows,key=lambda r:r.get(sort) or 0,reverse=True)


def overview(data):
    lines=['主机资源（CPU为采样期间使用率；进程CPU的100%表示占满一个核心）']
    for host in ['master','node1','node2']:
        row=data.get('hosts',{}).get(host,{})
        if not row:lines.append(f'  {host:<7} 正在读取…');continue
        if row.get('error'):lines.append(f"  {host:<7} {row['error']}");continue
        cpu='采样中' if row.get('cpu') is None else f"{row['cpu']:.1f}%"
        age=max(0,int(time.time()-row.get('received_at',row.get('timestamp',time.time()))))
        lines.append(f"  {host:<7} CPU {cpu:>7} / {row.get('cores','?')}核  内存 {row['memory_used']/2**30:.1f}/{row['memory_total']/2**30:.1f} GiB  进程 {len(row['processes'])}  更新{age}秒前")
    lines.extend(['','后台任务'])
    units={row.get('unit'):row for row in data.get('services',[]) if row.get('scope')=='当前用户'}
    for label,unit in [('日图及QA','hls-auto-daily.service'),('下载归档',data.get('archive_service','hls-archive.service')),
                       ('下载代理','hls-download-proxy.service'),('节点择优','hls-node-optimizer.service')]:
        if label=='下载归档':
            state=data.get('archive',{}).get('service',{}).get('active') or units.get(unit,{}).get('active')
        else:state=units.get(unit,{}).get('active')
        lines.append(f"  {label:<6} [{ACTIVE.get(state,'读取中')}]")
    lines.append('  当前代理节点：'+str(data.get('route',{}).get('selected') or '状态载入中'))
    archive=data.get('archive',{})
    lines.extend(['','原始下载（候选景包含已复用/跳过；不等于正式日图数量）'])
    if 'completed_scenes' in archive:
        lines.append(f"  月任务 {archive.get('completed_jobs',0)}/{archive.get('total_jobs',0)}；候选 {archive['completed_scenes']:,}/{archive.get('total_scenes',0):,}")
        blocks=archive.get('download_activity',{}).get('blocks_per_minute')
        lines.append('  新落盘速度：'+(f'{blocks:.1f} 块/分钟' if blocks is not None else '测速采样中'))
        rate=archive.get('throughput',{})
        lines.append('  全部下载预计剩余：'+(elapsed(rate.get('remaining_seconds')) if rate.get('status')=='ready' else rate.get('reason','测速采样中')))
        for node,row in archive.get('nodes',{}).items():
            phase={'downloading':'下载中','verifying':'核验中','retrying':'等待重试','waiting':'等待分配','stopped':'已停止','preflight':'启动核验'}.get(row.get('phase'),'读取中')
            lines.append(f"  {node:<7} {phase}  {row.get('job') or '暂无月份'}  {row.get('completed_scenes',0)}/{row.get('total_scenes',0)}景")
    else:lines.append('  下载统计在后台载入，耗时不会卡住面板。')
    qa=data.get('qa',{});phase=qa.get('phase')
    lines.extend(['','后处理（必须先补齐全时段QA，再统一过滤水体并生成正式日图）'])
    lines.append('  当前：'+str(qa.get('region',''))+' '+PHASES.get(phase,phase or '读取中'))
    if qa.get('http_status'):lines.append(f"  HTTP {qa['http_status']}；重试 {qa.get('retry','?')}/{qa.get('retry_limit','?')}，本次等待{qa.get('wait_seconds','?')}秒")
    lines.append('  研究区   QA已完成/应有       正式日图   处理阶段')
    counts=data.get('region_daily_counts',{})
    for region in REGIONS:
        row=data.get('regions',{}).get(region,{})
        done,total=row.get('completed_days'),row.get('total_days')
        amount=f'{done}/{total}' if type(done) is int and total else '尚未建立清单'
        stage=PHASES.get(row.get('phase'),row.get('phase','等待下载/核验'))
        if row.get('phase')=='blocked' and qa.get('region')!=region:stage='上次受阻，待重试'
        delivered=counts.get(region,0) if 'daily_counts' in data else '读取中'
        lines.append(f"  {region:<7} {amount:<18} {delivered!s:<10} {stage}")
    current=qa.get('region');row=data.get('regions',{}).get(current,{})
    if row.get('total_days'):
        lines.append('  '+str(current)+' QA '+progress_bar(row.get('completed_days',0),row['total_days']))
        rate=data.get('qa_rates',{}).get(current)
        lines.append('  QA速度/剩余：'+(f"{rate['per_minute']:.1f}组/分钟；约{elapsed(rate['remaining_seconds'])}（仅QA阶段）" if rate else '采样中或正在退避；持续观察至少2分钟'))
    daily=data.get('daily_counts',{})
    lines.append('  完成月已交付日图：'+ '  '.join(f'{p} {daily.get(p,0)}张' for p in ['L30','S30','FUSED']))
    lines.append('  QA一组=同日期同产品；当前月正在写入的日图待该月核验后计入。')
    lines.append('  RIS后处理已排除2026年10月分片；旧试算图不计入正式结果。')
    nas=data.get('nas',{})
    lines.append('  NAS：'+(f"已挂载；剩余 {nas['free']/2**40:.2f} TiB" if nas.get('mounted') else '状态载入中/挂载待确认'))
    lines.extend(['','当前最占CPU的命令（各主机、所有可见用户）'])
    for row in process_rows(data)[:5]:
        cpu='—' if row['cpu'] is None else f"{row['cpu']:.1f}%"
        lines.append(f"  {row['host']} {row['user']} PID{row['pid']} {cpu}  {row['command']}")
    lines.extend('提示：'+message for message in data.get('messages',[]))
    lines.extend('下载提示：'+message for message in archive.get('warnings',[])[:2])
    return lines


def view_lines(data,tab,query='',sort='cpu'):
    if tab==1:return overview(data)
    if tab==2:
        rows=process_rows(data,query,sort)
        lines=[f'所有可见用户进程：{len(rows)} 条；按 {"CPU" if sort=="cpu" else "内存"} 排序；/搜索 用户名、PID、主机或命令',
               '主机    用户          PID     CPU%    内存MiB    已运行      当前命令（常见凭据参数已脱敏）']
        for row in rows:
            cpu='—' if row['cpu'] is None else f"{row['cpu']:.1f}"
            lines.append(f"{row['host']:<7} {row['user'][:12]:<12} {row['pid']:>7} {cpu:>7} {row['rss']/2**20:>10.1f} {elapsed(row['age']):>9}  {row['command']}")
        return lines
    if tab==3:
        rows=[row for row in data.get('services',[]) if query.lower() in json.dumps(row,ensure_ascii=False).lower()]
        rows.sort(key=lambda row:(row.get('active')!='failed',row.get('active')!='active',row.get('unit','')))
        return ['master系统及当前用户服务；其他用户的任务可在进程页按用户名搜索','范围     状态        服务名 / 说明']+[
            f"{row['scope']:<8} {ACTIVE.get(row.get('active'),row.get('active','?')):<8} {row.get('unit')}  {row.get('description','')}" for row in rows]
    if tab==4:
        groups={}
        for row in process_rows(data,query):
            key=(row['host'],row['user']);group=groups.setdefault(key,{'count':0,'cpu':0,'rss':0})
            group['count']+=1;group['cpu']+=row.get('cpu') or 0;group['rss']+=row['rss']
        return ['按主机/用户汇总所有可见进程；CPU可超过100%（使用多个核心）','主机    用户                进程数    CPU合计%    RSS合计GiB']+[
            f"{host:<7} {user:<20} {row['count']:>6} {row['cpu']:>11.1f} {row['rss']/2**30:>13.2f}" for (host,user),row in sorted(groups.items(),key=lambda pair:pair[1]['cpu'],reverse=True)]
    return ['最近后处理事件（新事件在前；不会显示签名下载URL）']+list(reversed(data.get('events',[])))


def screen(collector):
    if os.environ.get('TERM') in (None,'','dumb'):os.environ['TERM']='xterm-256color'
    import curses
    def ui(stdscr):
        try:curses.curs_set(0)
        except curses.error:pass
        color=0
        if curses.has_colors():
            try:
                curses.start_color();curses.use_default_colors();curses.init_pair(1,curses.COLOR_CYAN,-1);color=curses.color_pair(1)
            except curses.error:pass
        stdscr.timeout(200);tab=1;offset=0;query='';entering=False;sort='cpu';last=0;paused=False;cached={};detail=None
        while True:
            if not paused:cached=collector.snapshot()
            height,width=stdscr.getmaxyx();body=max(1,height-4)
            lines=view_lines(cached,tab,query,sort)
            if detail:
                row=next((r for r in process_rows(cached) if (r['host'],r['pid'])==detail),None)
                lines=['进程完整命令详情（Esc返回）']
                if row:
                    lines.append(f"主机 {row['host']}  用户 {row['user']}  PID {row['pid']}  状态 {row['state']}")
                    text=row['command'];part='';used=0
                    for char in text:
                        cost=0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in 'WF' else 1
                        if used+cost>max(10,width-4):lines.append(part);part='';used=0
                        part+=char;used+=cost
                    if part:lines.append(part)
                else:lines.append('该进程已结束或当前不可读取。')
            offset=max(0,min(offset,max(0,len(lines)-body)))
            if time.monotonic()-last>0.5:
                stdscr.erase()
                header=f'Linux任务总览  {datetime.now().astimezone():%m-%d %H:%M:%S}  '+('【画面暂停，后台任务继续】' if paused else '只读监控')
                title='1 总览 | 2 全部进程/命令 | 3 服务 | 4 用户 | 5 事件'
                footer=('搜索：'+query if entering else f'q退出 ↑↓/PgDn翻页 /搜索 回车详情 c/m排序 空格暂停 {offset+1}/{max(1,len(lines))}')
                for y,text,attr in [(0,header,curses.A_BOLD|color),(1,title,curses.A_REVERSE),
                                    *[(i+2,line,0) for i,line in enumerate(lines[offset:offset+body])],(height-1,footer,curses.A_REVERSE)]:
                    if 0<=y<height:
                        try:stdscr.addstr(y,0,fit(text,max(1,width-1)),attr)
                        except curses.error:pass
                stdscr.refresh();last=time.monotonic()
            try:key=stdscr.get_wch()
            except curses.error:continue
            last=0
            if entering:
                if key in ('\n','\r'):entering=False;offset=0
                elif key=='\x1b':entering=False;query=''
                elif key in ('\b','\x7f',curses.KEY_BACKSPACE):query=query[:-1]
                elif isinstance(key,str) and key.isprintable():query=(query+key)[:128]
                continue
            if key in ('q','Q'):break
            if key=='\x1b' and detail:detail=None;offset=0
            elif key in ('\n','\r') and tab==2 and not detail:
                rows=process_rows(cached,query,sort)
                if rows:
                    row=rows[min(len(rows)-1,max(0,offset-2))];detail=(row['host'],row['pid']);offset=0
            elif isinstance(key,str) and key in '12345':tab=int(key);offset=0;detail=None
            elif key in (curses.KEY_DOWN,'j'):offset+=1
            elif key in (curses.KEY_UP,'k'):offset-=1
            elif key in (curses.KEY_NPAGE,'n'):offset+=body
            elif key in (curses.KEY_PPAGE,'p'):offset-=body
            elif key=='/':entering=True;query=''
            elif key=='c':sort='cpu'
            elif key=='m':sort='rss'
            elif key==' ':paused=not paused
            elif key in (curses.KEY_HOME,'g'):offset=0
    curses.wrapper(ui)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--json',action='store_true')
    parser.add_argument('--host-probe',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if not sys.platform.startswith('linux'):parser.error('Run this monitor on Linux via VS Code Remote SSH')
    if args.host_probe:
        sampler=HostSampler();sampler.sample();time.sleep(1)
        print(json.dumps(sampler.sample(),ensure_ascii=False));return 0
    if args.config is None:parser.error('--config is required')
    config=read_json(args.config);collector=Collector(config);collector.start()
    try:
        if args.once or args.json or not sys.stdout.isatty():
            collector.stop.wait(3)
            value=collector.snapshot()
            print(json.dumps(value,ensure_ascii=False,indent=2) if args.json else '\n'.join(overview(value)))
        else:screen(collector)
    except KeyboardInterrupt:pass
    finally:collector.stop.set()
    return 0


if __name__=='__main__':raise SystemExit(main())
