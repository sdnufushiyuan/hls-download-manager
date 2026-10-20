#!/usr/bin/env python3
"""Plan or run six independent HLS regional archives from a frozen inventory."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, replace
from datetime import date
import json
import math
from pathlib import Path, PurePosixPath
import re
import signal
import sys
import threading
import time

import run_hls_cluster as cluster

REGION_CODES = ("SDR", "REM", "MES", "RIS", "OZN", "TXS")
MAX_INVENTORY_BYTES = 8 * 1024 * 1024
NETWORK_TEMPORARY_EXIT = 75
STORAGE_TEMPORARY_EXIT = 76
RECOVERABLE_EXITS = frozenset((NETWORK_TEMPORARY_EXIT, STORAGE_TEMPORARY_EXIT))


@dataclass(frozen=True)
class Month:
    start: date
    end: date
    scene_count: int


@dataclass(frozen=True)
class Region:
    code: str
    roi: str
    crs: str
    area_m2: float
    months: tuple[Month, ...]


@dataclass(frozen=True)
class Inventory:
    start: date
    end: date
    regions: tuple[Region, ...]


@dataclass(frozen=True)
class Job:
    code: str
    start: date
    end: date
    out: str
    scene_count: int
    node: str = ""

    @property
    def name(self):
        return f"{self.code}/{self.start.isoformat()}__{self.end.isoformat()}"


def _json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"inventory JSON 键重复：{key}")
        value[key] = item
    return value


def read_inventory(source):
    if source == "-":
        text = sys.stdin.read(MAX_INVENTORY_BYTES + 1)
    else:
        with Path(source).open("r", encoding="utf-8-sig") as stream:
            text = stream.read(MAX_INVENTORY_BYTES + 1)
    if len(text.encode("utf-8")) > MAX_INVENTORY_BYTES:
        raise ValueError("inventory 超过8 MiB，请检查是否误选了文件。")
    def reject_constant(_value):
        raise ValueError("inventory JSON 不允许 NaN 或 Infinity。")
    return parse_inventory(json.loads(text, object_pairs_hook=_json_object,
                                      parse_constant=reject_constant))


def _date(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError(f"{label} 必须为 YYYY-MM-DD。")
    return date.fromisoformat(value)


def _meter_crs(value):
    from pyproj import CRS
    from pyproj.exceptions import CRSError
    if not isinstance(value, str) or not re.fullmatch(r"EPSG:[1-9]\d{2,6}", value, re.IGNORECASE):
        raise ValueError("区域 crs 必须为明确的 EPSG 编号。")
    try:
        crs = CRS.from_user_input(value)
    except CRSError:
        raise ValueError("区域 EPSG 编号无法识别。") from None
    if not crs.is_projected or len(crs.axis_info) < 2 or any(
            abs(axis.unit_conversion_factor - 1) > 1e-10 for axis in crs.axis_info[:2]):
        raise ValueError("区域 crs 必须是以米为单位的投影；EPSG:5070 和米制 UTM 均支持。")
    return crs.to_string()


def parse_inventory(payload):
    """Only validated fields affect commands; provenance/other metadata is ignored."""
    if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ValueError("inventory 需要 schema_version=1 的 JSON 对象。")
    start, end = _date(payload.get("start"), "start"), _date(payload.get("end_exclusive"), "end_exclusive")
    expected = [(part.start, part.end) for part in cluster.split_months(start, end, "/nasdata/validation")]
    raw_regions = payload.get("regions")
    if not isinstance(raw_regions, list) or len(raw_regions) != len(REGION_CODES):
        raise ValueError("inventory 必须包含 SDR、REM、MES、RIS、OZN、TXS 六个区域。")
    regions, seen_codes, seen_rois = [], set(), set()
    for item in raw_regions:
        if not isinstance(item, dict):
            raise ValueError("区域记录必须是 JSON 对象。")
        code = item.get("code")
        if not isinstance(code, str) or code not in REGION_CODES or code in seen_codes:
            raise ValueError("区域 code 未知或重复。")
        seen_codes.add(code)
        if not isinstance(item.get("roi"), str):
            raise ValueError(f"{code} 缺少 ROI 路径。")
        roi = cluster.remote_path(item["roi"], f"{code}.roi")
        if roi.suffix.lower() not in (".shp", ".zip") or str(roi) in seen_rois:
            raise ValueError("ROI 必须为独立的 .shp/.zip Linux绝对路径。")
        seen_rois.add(str(roi))
        crs = _meter_crs(item.get("crs"))
        area = item.get("area_m2")
        if isinstance(area, bool) or not isinstance(area, (int, float)) or not math.isfinite(area) or area <= 0:
            raise ValueError(f"{code}.area_m2 必须为正的有限数。")
        raw_months = item.get("months")
        if not isinstance(raw_months, list) or len(raw_months) != len(expected):
            raise ValueError(f"{code} 月份记录必须覆盖全部全局分片，包括 scene_count=0 的空月份。")
        months = {}
        for month in raw_months:
            if not isinstance(month, dict):
                raise ValueError("月份记录必须为 JSON 对象。")
            boundary = (_date(month.get("start"), "month.start"),
                        _date(month.get("end_exclusive"), "month.end_exclusive"))
            count = month.get("scene_count")
            if type(count) is not int or count < 0 or count > 1_000_000:
                raise ValueError("scene_count 必须为0–1000000的整数。")
            if boundary not in expected or boundary in months:
                raise ValueError("月份重复或边界与全局自然月分片不一致。")
            months[boundary] = Month(*boundary, count)
        regions.append(Region(code, str(roi), crs, float(area), tuple(months[key] for key in expected)))
    return Inventory(start, end, tuple(regions))


def validate_network_policy(retries, delay):
    if type(retries) is not int or not 0 <= retries <= 8:
        raise ValueError("--network-job-retries 必须是0–8的整数。")
    if (isinstance(delay, bool) or not isinstance(delay, (int, float)) or
            not math.isfinite(delay) or not 1 <= delay <= 3600):
        raise ValueError("--network-retry-delay 必须是1–3600秒的有限数。")


def _network_retry_delay(value):
    try:
        delay = float(value)
        validate_network_policy(0, delay)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("等待时间必须是1–3600秒的有限数。") from None
    return delay


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--inventory", required=True, help="冻结的 inventory.json；- 表示从标准输入读 JSON")
    result.add_argument("--out", required=True, help="/nasdata 下的专用全历史输出根目录")
    result.add_argument("--nodes", nargs="+", choices=cluster.NODES, default=list(cluster.DEFAULT_NODES))
    result.add_argument("--require-mount", default="/nasdata")
    result.add_argument("--mount-source")
    result.add_argument("--project", help="可选覆盖；默认使用已部署项目配置")
    result.add_argument("--buffer-m", type=float, default=0)
    result.add_argument("--repair-geometries", action="store_true")
    result.add_argument("--reflectance-scale", type=float, default=1.0)
    result.add_argument("--aerosol", choices=("low", "climatology-low"), default="low")
    result.add_argument("--tile-size", type=int, default=512)
    result.add_argument("--workers", type=int, default=1,
                        help="每节点同时下载的块数，1–8；不改变科学参数或缓存标识")
    result.add_argument("--resume-pending-first", action="store_true",
                        help="先运行未完成月份；已完成提示仅用于排序，所有月份仍保留并核验")
    result.add_argument("--schedule", choices=("fair", "smallest-first"), default="fair",
                        help="smallest-first：按剩余候选数×面积估计体量，集中完成并核验一个区再进入下一区")
    result.add_argument("--max-scenes", type=int, help="默认至少1000，并按库存最大月数量增加10%%+10保护余量")
    result.add_argument("--limit", type=int, help="仅试取时使用：每区域每月前 N 景，非全局限额")
    result.add_argument("--network-job-retries", type=int, choices=range(9), default=4,
                        help="退出码75/76的月网络/NAS任务额外重试次数，0–8；默认合计最多5次")
    result.add_argument("--network-retry-delay", type=_network_retry_delay, default=300,
                        help="同节点同月网络恢复等待秒数，1–3600，默认300")
    modes = result.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="默认：只查看计划，不连接节点或写输出")
    modes.add_argument("--run", action="store_true", help="master 上预检后启动实际下载")
    return result


def region_arguments(args, inventory):
    validate_network_policy(args.network_job_retries, args.network_retry_delay)
    highest = max(month.scene_count for region in inventory.regions for month in region.months)
    ceiling = args.max_scenes if args.max_scenes is not None else max(1000, math.ceil(highest * 1.1) + 10)
    if ceiling < highest:
        raise ValueError(f"--max-scenes={ceiling} 小于库存最大月候选数量 {highest}。")
    validated = {}
    for region in inventory.regions:
        common = argparse.Namespace(**vars(args))
        common.roi, common.crs = region.roi, region.crs
        common.start, common.end = inventory.start.isoformat(), inventory.end.isoformat()
        common.max_scenes = ceiling
        cluster.validate(common)
        validated[region.code] = common
    return validated


def fair_jobs(inventory, out):
    root = cluster.remote_path(out, "--out")
    queues = []
    for region in sorted(inventory.regions, key=lambda item: (-item.area_m2, item.code)):
        queues.append(deque(Job(region.code, month.start, month.end,
                                str(root / region.code / f"{month.start.isoformat()}__{month.end.isoformat()}"),
                                month.scene_count) for month in region.months if month.scene_count))
    jobs = []
    while any(queues):
        for queue in queues:
            if queue:
                jobs.append(queue.popleft())
    return jobs


def processed_scene_hint(job):
    """Bounded scheduling hint only; every job must still validate its cache."""
    def read_hint():
        directory = Path(job.out)
        path = directory / "download_selection_summary.json"
        try:
            if directory.is_symlink() or directory.parent.is_symlink() or path.is_symlink():
                return False
            try:
                with path.open("r", encoding="utf-8") as stream:
                    text = stream.read(4097)
            except FileNotFoundError:
                # Legacy complete months have no selection summary. Marker
                # presence is only an ordering hint, never a cache validation.
                manifest = directory / "run_manifest.json"
                if manifest.is_symlink() or job.scene_count > 10_000:
                    return False
                with manifest.open("r", encoding="utf-8") as stream:
                    text = stream.read(8 * 1024 * 1024 + 1)
                if len(text) > 8 * 1024 * 1024:
                    return False
                saved = json.loads(text)
                scenes = saved.get("scenes") if isinstance(saved, dict) else None
                if not isinstance(scenes, list) or len(scenes) != job.scene_count:
                    return False
                for scene in scenes:
                    if (not isinstance(scene, dict) or scene.get("sensor") not in ("L30", "S30") or
                            not isinstance(scene.get("index"), str) or
                            not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", scene["index"]) or
                            scene["index"] in (".", "..")):
                        return False
                    scene_dir = directory / scene["sensor"] / scene["index"]
                    marker = scene_dir / "scene.json"
                    if scene_dir.is_symlink() or scene_dir.parent.is_symlink() or marker.is_symlink() or not marker.is_file():
                        return False
                return job.scene_count
            if len(text) <= 4096:
                hint = json.loads(text)
                if (isinstance(hint, dict) and
                        hint.get("policy") == "clear-land-exact-time-unique-pixels-v1" and
                        type(hint.get("processed_scenes")) is int and
                        0 <= hint["processed_scenes"] <= job.scene_count):
                    return hint["processed_scenes"]
        except (OSError, ValueError, UnicodeError):
            pass
        return False

    return int(read_hint())


def prioritize_pending_jobs(jobs):
    """Stable partition only: all hinted-complete jobs still validate normally."""
    pending, hinted_complete = [], []
    for job in jobs:
        (hinted_complete if processed_scene_hint(job) == job.scene_count else pending).append(job)
    return pending + hinted_complete


def smallest_region_jobs(inventory, jobs, *, read_hints=False, pending_first=False):
    """Estimate relative remaining work; never infer bytes or omit a month.

    Area × remaining candidates is a coarse upper-work proxy, not measured
    transfer size: cloud/overlap filtering and footprints change actual work.
    Plan mode uses inventory only; runtime hints may affect order, never validity.
    """
    groups, hints = {}, {}
    for job in jobs:
        groups.setdefault(job.code, []).append(job)
        hints[job.name] = processed_scene_hint(job) if read_hints else 0
    ranking = []
    for region in inventory.regions:
        group = groups.get(region.code, [])
        if not group:
            continue
        remaining = sum(job.scene_count - hints[job.name] for job in group)
        ranking.append({"code": region.code, "remaining_candidate_hint": remaining,
                        "area_km2": region.area_m2 / 1e6,
                        "relative_work_hint": remaining * (region.area_m2 / 1e6),
                        "months_to_validate": len(group)})
    ranking.sort(key=lambda row: (row["relative_work_hint"], row["area_km2"], row["code"]))
    ordered = []
    for row in ranking:
        group = groups[row["code"]]
        if pending_first:
            group = sorted(group, key=lambda job: hints[job.name] == job.scene_count)
        ordered.extend(group)
    return ordered, ranking


def run_region_jobs(jobs, nodes, runner, emit=print, check_cancelled=lambda: None, **policy):
    """A region barrier: join all month workers before dispatching another region."""
    groups, results = {}, {}
    for job in jobs:
        groups.setdefault(job.code, []).append(job)
    for code, group in groups.items():
        check_cancelled()
        emit(f"区域开始 {code}：集中运行并核验 {len(group)} 个月份。")
        completed = run_jobs(group, nodes, runner, emit, check_cancelled, **policy)
        results.update(completed)
        if len(completed) != len(group) or any(completed.values()):
            emit(f"区域未完成 {code}：后续研究区暂不启动。")
            break
        emit(f"区域完成 {code}：全部 {len(group)} 个月份已通过下载器核验，可开展该区后续处理。")
    return results


class ArchiveRunner(cluster.SSHRunner):
    def __init__(self, validated_args):
        super().__init__(next(iter(validated_args.values())))
        self.region_args = dict(validated_args)

    def __call__(self, shard, dry_run=False):
        status = super().__call__(shard, dry_run)
        # SSH transport failure is recoverable only for an already dispatched
        # download. Preflight failures still block all real downloads.
        return NETWORK_TEMPORARY_EXIT if not dry_run and shard.node != "master" and status == 255 else status

    def command_for(self, shard, dry_run=False):
        if shard.code not in self.region_args:
            raise ValueError("拒绝未知区域任务。")
        args = self.region_args[shard.code]
        expected = str(PurePosixPath(args.out) / shard.code /
                       f"{shard.start.isoformat()}__{shard.end.isoformat()}")
        if shard.out != expected:
            raise ValueError("任务输出路径与区域/月度目录不一致。")
        return cluster.ssh_argv(args, shard, dry_run)


def run_jobs(jobs, nodes, runner, emit=print, check_cancelled=lambda: None, *,
             network_job_retries=4, network_retry_delay=300):
    """Share months; resume temporary transport/NAS failures on the same node."""
    if not nodes or len(set(nodes)) != len(nodes) or any(node not in cluster.NODES for node in nodes):
        raise ValueError("只允许不重复的 node1/node2/master。")
    validate_network_policy(network_job_retries, network_retry_delay)
    queue, results = deque(jobs), {}
    lock, failed = threading.Lock(), threading.Event()

    def wait_for_network():
        deadline = time.monotonic() + network_retry_delay
        while True:
            check_cancelled()
            if failed.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.2, remaining))

    def worker(node):
        while True:
            with lock:
                if failed.is_set() or not queue:
                    return
                job = replace(queue.popleft(), node=node)
                emit(f"开始 {node} {job.name}，库存候选 {job.scene_count}")
            try:
                for attempt in range(network_job_retries + 1):
                    check_cancelled()
                    if attempt and failed.is_set():
                        break
                    status = runner(job)
                    if status not in RECOVERABLE_EXITS or attempt == network_job_retries:
                        break
                    retry = attempt + 1
                    recovery = "存储恢复" if status == STORAGE_TEMPORARY_EXIT else "网络恢复"
                    emit(f"{recovery}等待 {node} {job.name}，重试 {retry}/{network_job_retries}，"
                         f"等待 {network_retry_delay:g} 秒。")
                    if not wait_for_network():
                        break
                    check_cancelled()
                    if failed.is_set():
                        break
                    emit(f"{recovery}重试 {node} {job.name}，重试 {retry}/{network_job_retries}。")
            except cluster.Cancelled:
                return
            except Exception as error:
                emit(f"{node} {job.name} 失败（{type(error).__name__}）。")
                status = 1
            with lock:
                results[job.name] = status
                if status:
                    failed.set()
                emit(f"结束 {node} {job.name}，退出码 {status}，已结束 {len(results)}/{len(jobs)}")
    threads = [threading.Thread(target=worker, args=(node,), daemon=True) for node in nodes]
    for thread in threads:
        thread.start()
    for thread in threads:
        while thread.is_alive():
            check_cancelled()
            thread.join(timeout=0.2)
    check_cancelled()
    return results


def preflight(jobs, nodes, runner, emit=print):
    first = {}
    for job in jobs:
        first.setdefault(job.code, job)
    for code, job in first.items():
        for node in nodes:
            emit(f"离线预检 {node} {code}")
            if runner(replace(job, node=node), dry_run=True):
                raise RuntimeError("区域/节点 ROI、CRS 或 NAS 预检失败，未启动影像下载。")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        inventory = read_inventory(args.inventory)
        validated = region_arguments(args, inventory)
        jobs = fair_jobs(inventory, next(iter(validated.values())).out)
        region_priority = []
        if args.schedule == "smallest-first":
            jobs, region_priority = smallest_region_jobs(inventory, jobs)
        summary = {"start": inventory.start.isoformat(), "end_exclusive": inventory.end.isoformat(),
                   "out": next(iter(validated.values())).out, "nodes": args.nodes,
                   "tile_workers_per_node": args.workers,
                   "max_tiles_in_flight": args.workers * len(args.nodes),
                   "max_scenes_per_job": next(iter(validated.values())).max_scenes,
                   "schedule": args.schedule, "region_priority_inventory_only": region_priority,
                   "network_retry_policy": {"exit_code": NETWORK_TEMPORARY_EXIT,
                                            "mapped_download_ssh_exit_code": 255,
                                            "retries": args.network_job_retries,
                                            "max_attempts": args.network_job_retries + 1,
                                            "delay_seconds": args.network_retry_delay,
                                            "scope": "same_node_same_month"},
                   "storage_retry_policy": {"exit_code": STORAGE_TEMPORARY_EXIT,
                                            "retries": args.network_job_retries,
                                            "delay_seconds": args.network_retry_delay,
                                            "scope": "same_node_same_month"},
                   "job_count": len(jobs),
                   "empty_months_skipped": sum(month.scene_count == 0 for region in inventory.regions for month in region.months),
                   "regions": [{"code": region.code, "crs": region.crs,
                                "nonempty_months": sum(month.scene_count > 0 for month in region.months)}
                               for region in inventory.regions],
                   "jobs": [{"region": job.code, "start": job.start.isoformat(),
                             "end_exclusive": job.end.isoformat(), "out": job.out,
                             "scene_count": job.scene_count} for job in jobs]}
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        if not args.run:
            print("仅生成计划：未调用 EE、未连接节点、未创建输出/日志/锁文件。", flush=True)
            return 0
        cluster.require_master()
        if not jobs:
            print("库存所有月份候选数为0，无需启动下载。", flush=True)
            return 0
        runner = ArchiveRunner(validated)
        previous = {}
        try:
            signals = [signal.SIGINT, signal.SIGTERM]
            if hasattr(signal, "SIGHUP"):
                signals.append(signal.SIGHUP)
            for signum in signals:
                previous[signum] = signal.signal(signum, runner.record_signal)
            with cluster.output_lock(summary["out"]):
                try:
                    preflight(jobs, args.nodes, runner, lambda value: print(value, flush=True))
                    if args.schedule == "smallest-first":
                        jobs, ranking = smallest_region_jobs(inventory, jobs, read_hints=True,
                                                             pending_first=args.resume_pending_first)
                        print("区域优先顺序（剩余候选×面积估计，非实测字节）：" +
                              " → ".join(row["code"] for row in ranking), flush=True)
                        print("区域排序详情：" + json.dumps(ranking, ensure_ascii=False), flush=True)
                    elif args.resume_pending_first:
                        jobs = prioritize_pending_jobs(jobs)
                        print("断点优先：先运行未完成月份；全部月份仍在队列中，完成提示不替代原有核验。", flush=True)
                    scheduler = run_region_jobs if args.schedule == "smallest-first" else run_jobs
                    results = scheduler(jobs, args.nodes, runner,
                                       lambda value: print(value, flush=True), runner.check_cancelled,
                                       network_job_retries=args.network_job_retries,
                                       network_retry_delay=args.network_retry_delay)
                    failures = sum(status != 0 for status in results.values())
                    pending = len(jobs) - len(results)
                    print(f"结束：完成 {len(results)-failures}；失败 {failures}；未启动 {pending}。", flush=True)
                    if failures:
                        # Let systemd resume only exhausted temporary network
                        # failures. Permanent auth/NAS/data errors still stop.
                        failure_codes = {status for status in results.values() if status}
                        if failure_codes and failure_codes <= RECOVERABLE_EXITS:
                            return (STORAGE_TEMPORARY_EXIT if failure_codes == {STORAGE_TEMPORARY_EXIT}
                                    else NETWORK_TEMPORARY_EXIT)
                        return 1
                    return 1 if pending else 0
                finally:
                    runner.cancel()
        except cluster.Cancelled as error:
            runner.cancel()
            print("已停止新任务并关闭活动 SSH；节点前台控制器负责清理下载。", file=sys.stderr)
            return 128 + error.signum
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
