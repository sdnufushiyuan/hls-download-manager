#!/usr/bin/env python3
"""Plan HLS monthly shards, or run foreground tasks on selected known nodes."""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shlex
import signal
import socket
import subprocess
import sys
import threading

NODES = ("node1", "node2", "master")
DEFAULT_NODES = ("node1", "node2")
MASTER_HOSTS = {"master"}
WRAPPER = str(Path(__file__).resolve().with_name("run_hls_node.sh"))


@dataclass(frozen=True)
class Shard:
    start: date
    end: date
    node: str
    out: str

    @property
    def name(self):
        return f"{self.start.isoformat()}__{self.end.isoformat()}"


def remote_path(value, label):
    if not value or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} 不能为空或含控制字符。")
    path = PurePosixPath(value)
    if not path.is_absolute() or path.anchor != "/" or ".." in path.parts:
        raise ValueError(f"{label} 必须是 Linux 绝对路径，不能含 ..。")
    return path


def split_months(start, end, out, nodes=DEFAULT_NODES):
    if start >= end:
        raise ValueError("开始日期必须早于结束日期；结束日期不包含在下载范围内。")
    if not nodes or len(set(nodes)) != len(nodes) or any(node not in NODES for node in nodes):
        raise ValueError("节点只允许 node1/node2/master，并且不能重复。")
    shards = []
    current = start
    while current < end:
        boundary = (date(current.year + 1, 1, 1) if current.month == 12
                    and current.year < 9999 else
                    date(current.year, current.month + 1, 1) if current.month < 12 else end)
        following = min(boundary, end)
        name = f"{current.isoformat()}__{following.isoformat()}"
        shards.append(Shard(current, following, nodes[len(shards) % len(nodes)],
                            str(PurePosixPath(out) / name)))
        current = following
    return shards


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--roi", required=True, help="共享目录中的 Linux .shp/.zip 绝对路径")
    result.add_argument("--start", required=True, help="YYYY-MM-DD，包含")
    result.add_argument("--end", required=True, help="YYYY-MM-DD，不包含")
    result.add_argument("--out", required=True, help="/nasdata 内的专用输出根目录")
    result.add_argument("--nodes", nargs="+", choices=NODES, default=list(DEFAULT_NODES))
    result.add_argument("--require-mount", default="/nasdata")
    result.add_argument("--mount-source")
    result.add_argument("--project", help="可选；不提供时使用下载包现有项目配置")
    result.add_argument("--crs")
    result.add_argument("--buffer-m", type=float, default=0)
    result.add_argument("--repair-geometries", action="store_true")
    result.add_argument("--reflectance-scale", type=float, default=1.0)
    result.add_argument("--aerosol", choices=("low", "climatology-low"), default="low")
    result.add_argument("--tile-size", type=int, default=512)
    result.add_argument("--workers", type=int, default=1,
                        help="每节点同时下载的块数，1–8；不改变影像/网格参数")
    result.add_argument("--max-scenes", type=int, default=1000)
    result.add_argument("--limit", type=int, help="每个月分片的前 N 景；不是整个任务共 N 景")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="默认：只打印计划，不联网或写文件")
    mode.add_argument("--run", action="store_true", help="在 master 检查 ROI/NAS 后真正启动节点任务")
    return result


def validate(args):
    roi = remote_path(args.roi, "--roi")
    if roi.suffix.lower() not in (".shp", ".zip"):
        raise ValueError("--roi 需要 .shp 或 .zip。")
    out = remote_path(args.out, "--out")
    if args.require_mount != "/nasdata":
        raise ValueError("本部署的 --require-mount 必须为 /nasdata。")
    if not out.is_relative_to(PurePosixPath("/nasdata")) or out == PurePosixPath("/nasdata"):
        raise ValueError("--out 必须是 /nasdata 下的专用子目录。")
    if not math.isfinite(args.buffer_m) or args.buffer_m < 0:
        raise ValueError("--buffer-m 必须为非负有限数。")
    if not math.isfinite(args.reflectance_scale) or args.reflectance_scale <= 0:
        raise ValueError("--reflectance-scale 必须为正的有限数。")
    if not 64 <= args.tile_size <= 768 or args.max_scenes < 1:
        raise ValueError("--tile-size 需为64–768；--max-scenes 必须为正数。")
    if not 1 <= args.workers <= 8:
        raise ValueError("--workers 需为1–8。")
    if args.limit is not None and args.limit < 1:
        raise ValueError("--limit 必须为正数。")
    args.roi, args.out = str(roi), str(out)
    return split_months(date.fromisoformat(args.start), date.fromisoformat(args.end),
                        args.out, tuple(args.nodes))


def downloader_argv(args, shard, dry_run=False):
    argv = [WRAPPER, "--roi", args.roi, "--start", shard.start.isoformat(),
            "--end", shard.end.isoformat(), "--out", shard.out,
            "--require-mount", args.require_mount, "--buffer-m", str(args.buffer_m),
            "--reflectance-scale", str(args.reflectance_scale), "--aerosol", args.aerosol,
            "--tile-size", str(args.tile_size), "--workers", str(args.workers),
            "--max-scenes", str(args.max_scenes)]
    for flag, value in (("--mount-source", args.mount_source), ("--project", args.project),
                        ("--crs", args.crs), ("--limit", args.limit)):
        if value is not None:
            argv.extend([flag, str(value)])
    if args.repair_geometries:
        argv.append("--repair-geometries")
    if dry_run:
        argv.append("--dry-run")
    return argv


def ssh_argv(args, shard, dry_run=False):
    if shard.node not in NODES:
        raise ValueError("拒绝连接未知节点。")
    if shard.node == "master":
        # Run the same owning wrapper locally; no SSH-to-self or extra proxy.
        return downloader_argv(args, shard, dry_run)
    # A foreground PTY delivers HUP to the node wrapper on SSH disconnection.
    # shlex.join quotes each remote argument; local subprocess never uses a shell.
    return ["ssh", "-tt", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", shard.node,
            shlex.join(downloader_argv(args, shard, dry_run))]


class CleanupError(RuntimeError):
    def __init__(self, pids):
        self.pids = tuple(pids)
        super().__init__("SSH 清理未能确认退出，PID=" + ",".join(map(str, pids)) +
                         "；请检查节点任务，当前输出根目录将保留清理失败标记。")


@contextmanager
def output_lock(out, directory=None):
    import fcntl  # Only the explicit Linux --run path needs this module.
    resolved = str(Path(out).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parent
    lock_file = directory / f".deployment-cluster-{digest}.lock"
    descriptor = os.open(lock_file, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("同一输出根目录已有集群任务运行；请等待该任务结束。") from None
        previous = os.read(descriptor, 65536)
        if previous:
            try:
                state = json.loads(previous)
            except (ValueError, UnicodeError):
                raise RuntimeError(f"输出锁记录异常，请先人工检查 {lock_file}。") from None
            if not isinstance(state, dict):
                raise RuntimeError(f"输出锁记录异常，请先人工检查 {lock_file}。")
            if state.get("cleanup_failed"):
                raise RuntimeError(f"上次 SSH 清理失败；核查节点后清空锁文件内容再运行：{lock_file}")
        try:
            yield
        except CleanupError as error:
            state = json.dumps({"cleanup_failed": True, "pids": error.pids}).encode("utf-8")
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.ftruncate(descriptor, 0)
            os.write(descriptor, state)
            os.fsync(descriptor)
            raise
    finally:
        os.close(descriptor)
        # Keep the inode: unlinking an advisory lock permits overlapping holders.


class SSHRunner:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.active = set()
        self.cancelled = False
        self.pending_signal = None
        self.cleaning = False

    def record_signal(self, signum, _frame):
        # A handler must not interrupt Popen before its child has been registered.
        # Repeated signals during cleanup must not interrupt waits or release locks.
        if not self.cleaning and self.pending_signal is None:
            self.pending_signal = signum

    def check_cancelled(self):
        if self.pending_signal is not None and not self.cleaning:
            raise Cancelled(self.pending_signal)

    def command_for(self, shard, dry_run=False):
        """Override argument selection without mutating runner state between jobs."""
        return ssh_argv(self.args, shard, dry_run)

    def __call__(self, shard, dry_run=False):
        with self.lock:
            self.check_cancelled()
            if self.cancelled:
                return 130
            child = subprocess.Popen(self.command_for(shard, dry_run), stdin=subprocess.DEVNULL)
            self.active.add(child)
        try:
            while True:
                self.check_cancelled()
                try:
                    return child.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if child.poll() is not None:
                with self.lock:
                    self.active.discard(child)

    def cancel(self):
        self.cleaning = True
        with self.lock:
            self.cancelled = True
            active = list(self.active)
        for child in active:
            if child.poll() is None:
                try:
                    child.terminate()
                except OSError:
                    # A just-exited process or another failure must not skip peers.
                    pass
        unfinished = []
        for child in active:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    child.kill()
                except OSError:
                    pass
                try:
                    child.wait(timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    unfinished.append(child.pid)
                    continue
            except OSError:
                if child.poll() is None:
                    unfinished.append(child.pid)
                    continue
            with self.lock:
                self.active.discard(child)
        if unfinished:
            raise CleanupError(unfinished)


def run_shards(shards, runner, emit=print, check_cancelled=lambda: None):
    """One worker per node; on failure stop new shards and finish active shards."""
    queues = defaultdict(list)
    for shard in shards:
        queues[shard.node].append(shard)
    failed = threading.Event()
    results, lock = {}, threading.Lock()

    def worker(node):
        for shard in queues[node]:
            with lock:
                if failed.is_set():
                    return
                emit(f"开始 {node} {shard.name}")
            try:
                status = runner(shard)
            except Cancelled:
                return
            except Exception as error:
                emit(f"{node} 启动/等待失败（{type(error).__name__}）。")
                status = 1
            with lock:
                results[shard.name] = status
                if status:
                    failed.set()
                emit(f"结束 {node} {shard.name}，退出码 {status}")

    threads = [threading.Thread(target=worker, args=(node,), daemon=True) for node in queues]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            check_cancelled()
            thread.join(timeout=0.2)
    check_cancelled()
    return results


class Cancelled(Exception):
    def __init__(self, signum):
        self.signum = signum


def require_master():
    hostname = socket.gethostname().split(".")[0]
    if not sys.platform.startswith("linux") or hostname not in MASTER_HOSTS:
        raise RuntimeError("--run 只允许在已部署的 Linux master 上执行；其他机器可使用 --plan。")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        shards = validate(args)
        print(json.dumps([{"node": item.node, "start": item.start.isoformat(),
                           "end_exclusive": item.end.isoformat(), "out": item.out}
                          for item in shards], ensure_ascii=False, indent=2), flush=True)
        if not args.run:
            print("仅生成计划：未连接节点、未检查远程文件、未写输出或锁文件。", flush=True)
            return 0
        require_master()
        runner = SSHRunner(args)
        previous = {}
        try:
            cancellation_signals = [signal.SIGINT, signal.SIGTERM]
            if hasattr(signal, "SIGHUP"):
                cancellation_signals.append(signal.SIGHUP)
            for signum in cancellation_signals:
                previous[signum] = signal.signal(signum, runner.record_signal)
            with output_lock(args.out):
                try:
                    # ROI/geometry, grid and NAS checks run without EE authentication.
                    # Complete every preflight before the first real download starts.
                    for shard in shards:
                        print(f"离线预检 {shard.node} {shard.name}", flush=True)
                        if runner(shard, dry_run=True):
                            raise RuntimeError("节点 ROI/NAS 预检失败，未启动影像下载。")
                    results = run_shards(shards, runner, lambda value: print(value, flush=True),
                                         runner.check_cancelled)
                    failures = sum(status != 0 for status in results.values())
                    pending = len(shards) - len(results)
                    print(f"完成 {len(results)-failures} 个分片；失败 {failures}；未启动 {pending}。", flush=True)
                    return 1 if failures or pending else 0
                finally:
                    # Keep the output lock until SSH children have been stopped.
                    runner.cancel()
        except Cancelled as error:
            runner.cancel()
            print("已停止排新任务并关闭活动 SSH；节点前台 wrapper 收到 HUP 后终止下载。", file=sys.stderr)
            return 128 + error.signum
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
