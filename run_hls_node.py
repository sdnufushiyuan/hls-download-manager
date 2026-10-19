#!/usr/bin/env python3
"""Run one HLS task on node1/node2/master with an owned downloader child.

Use --check for a tiny synthetic GeoTIFF verification, or pass downloader
arguments. This controller keeps ownership of its proxy and downloader child;
it closes owned children on completion, proxy failure, terminal hangup, or cancellation.
On master it borrows the existing systemd proxy without stopping that service.
"""

from pathlib import Path
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time

PROGRAM = Path(__file__).resolve().parent
NODES = ("node1", "node2", "master")
PROXY_PORT = 18119
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
MASTER_PROXY_URL = "http://127.0.0.1:8118"
_pending_signal = None


class NodeError(Exception):
    pass


class TransientNodeError(NodeError):
    pass


class NodeInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def record_interrupt(signum, _frame):
    # Never raise inside Popen: a child can exist before it has been assigned.
    global _pending_signal
    if _pending_signal is None:
        _pending_signal = signum


def check_interrupted():
    if _pending_signal is not None:
        raise NodeInterrupted(_pending_signal)


def child_environment():
    environment = os.environ.copy()
    # Keep shared NAS space available for other users during this archive job.
    environment['HLS_MIN_FREE_GIB'] = '256'
    for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
                 'NUMEXPR_NUM_THREADS', 'GDAL_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS'):
        environment[name] = '1'
    return environment


CHECK_CODE = r'''
from pathlib import Path
import os, sys
try:
    from download_hls_optram import resolve_project, initialize_ee, BANDS
    import requests, numpy as np
    from rasterio.io import MemoryFile
    from rasterio.crs import CRS
    from rasterio.transform import Affine
    proxy = os.environ['HLS_NODE_PROXY']
    project, _ = resolve_project()
    ee = initialize_ee(project, proxy)
    values = [0.1, 0.3, 0.2, 0.5, 1.6, 64.0]
    transform = [30, 0, 300000, 0, -30, 3400000]
    image = ee.Image.constant(values).rename(list(BANDS)).toFloat()
    url = image.getDownloadURL({'crs': 'EPSG:32650', 'crs_transform': transform,
                               'dimensions': [1, 1], 'format': 'GEO_TIFF'})
    with requests.Session() as session:
        session.trust_env = False
        with session.get(url, proxies={'http': proxy, 'https': proxy},
                         stream=True, timeout=(10, 30), verify=True) as response:
            if response.status_code != 200:
                print('Node check HTTP status:', response.status_code, flush=True)
                raise RuntimeError('Unexpected status')
            data = bytearray()
            for chunk in response.iter_content(16384):
                data.extend(chunk)
                if len(data) > 1024 * 1024:
                    raise RuntimeError('Bounded synthetic response exceeded limit')
    with MemoryFile(bytes(data)) as mem:
        with mem.open() as ds:
            assert ds.count == 6 and ds.width == ds.height == 1
            assert ds.crs == CRS.from_epsg(32650)
            assert ds.transform.almost_equals(Affine(*transform), precision=1e-6)
            assert all(x == 'float32' for x in ds.dtypes)
            assert np.allclose(ds.read()[:, 0, 0], values, rtol=0, atol=1e-6)
    print('Node EE + Singapore proxy + 6-band GeoTIFF check: PASS', flush=True)
except Exception as exc:
    # Exception messages and raw traces may contain signed download URLs.
    print('Node download check failed:', type(exc).__name__, flush=True)
    sys.exit(1)
'''


def validated_download_args(argv):
    from download_hls_optram import parser
    if any(arg == "--proxy" or arg.startswith("--proxy=") for arg in argv):
        raise NodeError("节点代理由控制脚本管理，请不要传 --proxy。")
    arguments = parser()
    arguments.allow_abbrev = False
    return arguments.parse_args(argv)


def lock_node(node):
    import fcntl
    directory = Path("/tmp") / f"hls-optram-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise NodeError("节点临时锁目录的所有者或权限不正确。")
    fd = os.open(directory / f"{node}.lock",
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise NodeError("本节点已有 HLS 下载任务，请等待它结束。") from None
    return fd


def require_free_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # OpenSSH also uses SO_REUSEADDR. A finished task can leave accepted
        # connections in TIME_WAIT; those are not an active proxy listener.
        # Reuse still rejects a real listener owned by another task.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            raise NodeError(f"节点 127.0.0.1:{port} 已被占用；未复用其他代理。") from None


def wait_proxy(proxy, timeout=15):
    deadline = time.monotonic() + timeout
    address = re.compile(rf"(?:^|\s)127\.0\.0\.1:{PROXY_PORT}(?:\s|$)")
    owner = re.compile(rf"\bpid={proxy.pid}(?:,|\))")
    while time.monotonic() < deadline:
        check_interrupted()
        code = proxy.poll()
        if code is not None:
            if code == 255:
                raise TransientNodeError("节点到 master 的 SSH 转发暂时失败；可等待网络恢复后重试。")
            raise NodeError(f"节点到 master 的 SSH 转发失败（退出码 {code}）。")
        state = subprocess.run(["ss", "-H", "-ltnp"], capture_output=True,
                               text=True, timeout=3, check=True)
        if any(address.search(line) and owner.search(line)
               for line in state.stdout.splitlines()):
            return
        time.sleep(0.15)
    raise TransientNodeError("未检测到本任务的节点代理监听，可等待网络恢复后重试。")


def stop_child(child, grace=4):
    if child is None:
        return
    if child.poll() is None:
        try:
            child.terminate()
        except ProcessLookupError:
            pass
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            child.kill()
        except ProcessLookupError:
            pass
        child.wait(timeout=grace)


def require_master_proxy():
    """Borrow the existing managed proxy; this wrapper never owns or stops it."""
    state = subprocess.run(["systemctl", "--user", "show", "hls-download-proxy.service",
                            "--property=ActiveState", "--value"],
                           capture_output=True, text=True, timeout=5)
    if state.returncode != 0 or state.stdout.strip() != "active":
        raise TransientNodeError("master 的新加坡代理服务暂未运行，请等待网络恢复。")
    try:
        with socket.create_connection(("127.0.0.1", 8118), timeout=3):
            pass
    except OSError:
        raise TransientNodeError("master 的新加坡代理端口暂不可用，请等待网络恢复。") from None


def reuse_completed_locally(python, argv):
    """Finish an existing month without creating/requiring any network proxy.

    The downloader owns recipe/grid/record validation. Exit 3 means more work
    remains; permanent cache errors must not be hidden behind a network retry.
    """
    child = None
    try:
        check_interrupted()
        child = subprocess.Popen([str(python), "-u", str(PROGRAM / "download_hls_optram.py"),
            *argv, "--resume-complete-only"], cwd=PROGRAM, env=child_environment(),
            stdin=subprocess.DEVNULL)
        result = monitor_children(child, None)
        return None if result == 3 else result
    finally:
        stop_child(child)


def monitor_children(download, proxy, timeout=None):
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        check_interrupted()
        result = download.poll()
        if result is not None:
            return result
        if proxy is not None and proxy.poll() is not None:
            raise TransientNodeError("下载期间节点代理已退出，正在停止当前任务。")
        if deadline and time.monotonic() >= deadline:
            raise NodeError("节点连通检查超时，正在清理本次检查。")
        time.sleep(0.2)


def run(argv):
    node = socket.gethostname().split(".")[0]
    if sys.platform != "linux" or node not in NODES:
        raise NodeError("此脚本仅在已配置的 Linux node1/node2/master 上执行。")
    check_only = argv == ["--check"]
    args = None if check_only else validated_download_args(argv)
    python = PROGRAM / ".venv/bin/python"
    if not os.access(python, os.X_OK):
        raise NodeError("共享 Python 环境不可执行。")
    lock_fd = lock_node(node)
    proxy = download = None
    try:
        check_interrupted()
        if args and args.dry_run:
            download = subprocess.Popen([str(python), str(PROGRAM / "download_hls_optram.py"), *argv],
                                        cwd=PROGRAM, env=child_environment(), stdin=subprocess.DEVNULL)
            return monitor_children(download, None)
        if args and not args.list_only:
            result = reuse_completed_locally(python, argv)
            if result is not None:
                return result
        proxy_url = MASTER_PROXY_URL if node == "master" else PROXY_URL
        if node == "master":
            require_master_proxy()
            print("master：使用现有新加坡代理；共享代理由 systemd 管理。", flush=True)
        else:
            require_free_port(PROXY_PORT)
            check_interrupted()
            proxy = subprocess.Popen([
                "ssh", "-nNT", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "UpdateHostKeys=no",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
                "-o", "ServerAliveCountMax=2", "-L",
                f"127.0.0.1:{PROXY_PORT}:127.0.0.1:8118", "master"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, start_new_session=True)
            wait_proxy(proxy)
            print(f"{node}：本任务代理已接通 master → 新加坡。", flush=True)
        check_interrupted()
        if check_only:
            environment = child_environment()
            environment["HLS_NODE_PROXY"] = proxy_url
            command = [str(python), "-u", "-c", CHECK_CODE]
        else:
            environment = child_environment()
            command = [str(python), "-u", str(PROGRAM / "download_hls_optram.py"),
                       *argv, "--proxy", proxy_url]
        download = subprocess.Popen(command, cwd=PROGRAM, env=environment,
                                    stdin=subprocess.DEVNULL)
        return monitor_children(download, proxy, timeout=90 if check_only else None)
    finally:
        # Ignore further terminal signals while reclaiming our own children.
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal.SIG_IGN)
        try:
            stop_child(download)
        finally:
            try:
                stop_child(proxy)
                if proxy is not None and proxy.stderr is not None:
                    proxy.stderr.close()
            finally:
                os.close(lock_fd)


def main(argv=None):
    global _pending_signal
    _pending_signal = None
    for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, record_interrupt)
    try:
        return run(list(sys.argv[1:] if argv is None else argv))
    except NodeInterrupted as exc:
        print("节点任务已中断，本任务持有的子进程已清理。", file=sys.stderr)
        return 128 + exc.signum
    except TransientNodeError as exc:
        print(f"节点网络暂时失败：{exc}", file=sys.stderr)
        return 75
    except NodeError as exc:
        print(f"节点任务错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"节点任务错误：{type(exc).__name__}。请检查节点任务及代理状态。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
