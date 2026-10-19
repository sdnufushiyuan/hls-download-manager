#!/usr/bin/env python3
"""Read-only, standard-library progress collector for the running HLS archive.

No Earth Engine request, credential read, data write or service mutation is made.
Candidate scenes are the frozen inventory denominator, not bytes or clear pixels.
Successful job log records avoid rescanning completed scene directories. Unknown
historical jobs are recovered from validated completion records once; subsequent
polls inspect only active jobs and their first unfinished scene.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess


REGION_CODES = ("SDR", "REM", "MES", "RIS", "OZN", "TXS")
NODES = ("node1", "node2", "master")
SERVICE = "hls-archive.service"
PROXY_SERVICE = "hls-download-proxy.service"
NAS_SOURCE = os.environ.get("HLS_NAS_SOURCE", "//nas.example.org/data")
OUTPUT_ROOT = Path(os.environ.get("HLS_OUTPUT_ROOT", "/nasdata/HLS/archive"))
MONTH_RE = re.compile(r"\d{4}-\d{2}-\d{2}__\d{4}-\d{2}-\d{2}\Z")
INDEX_RE = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
TILE_RE = re.compile(r"r\d{6,}_c\d{6,}\.json\Z")
TILE_FILE_RE = re.compile(r"r\d{6,}_c\d{6,}\.tif\Z")
JOB_PATTERN = r"((?:SDR|REM|MES|RIS|OZN|TXS)/\d{4}-\d{2}-\d{2}__\d{4}-\d{2}-\d{2})"
NODE_PATTERN = r"(node[12]|master)"
START_RE = re.compile(r"^开始 " + NODE_PATTERN + " " + JOB_PATTERN + r"，库存候选 \d+$")
END_RE = re.compile(r"^结束 " + NODE_PATTERN + " " + JOB_PATTERN + r"，退出码 (\d+)，已结束 \d+/\d+$")
FINAL_RE = re.compile(r"^结束：完成 (\d+)；失败 (\d+)；未启动 (\d+)。$")
RETRY_WAIT_RE = re.compile(r"^网络恢复等待 " + NODE_PATTERN + " " + JOB_PATTERN + r"，重试 (\d+)/(\d+)，等待 (\d+(?:\.\d+)?) 秒。$")
RETRY_START_RE = re.compile(r"^网络恢复重试 " + NODE_PATTERN + " " + JOB_PATTERN + r"，重试 (\d+)/(\d+)。$")
JOURNAL_FILTER = r"^(开始 (node[12]|master) |结束 (node[12]|master) |网络恢复等待 (node[12]|master) |网络恢复重试 (node[12]|master) |结束：|错误：|离线预检 |(node[12]|master) (SDR|REM|MES|RIS|OZN|TXS)/|已停止新任务)"


def _read_json(path, max_bytes):
    """Bounded data reads; symlink records are not accepted."""
    if path.is_symlink():
        raise ValueError("symbolic link record")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("record too large")
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("record must be an object")
    return value


def _run_command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=8, check=False)
    if result.returncode:
        # journalctl uses status 1 for an empty filtered/cursor result. Real
        # permission and invalid-cursor errors include stderr and stay warnings.
        if argv[0] == "journalctl" and result.returncode == 1 and not result.stderr.strip():
            return ""
        raise RuntimeError("command unsuccessful")
    return result.stdout


@dataclass
class _Month:
    name: str
    code: str
    expected: int
    path: Path
    scenes: list = field(default_factory=list)
    recipe_hash: str = ""
    signature: tuple | None = None
    complete: set = field(default_factory=set)
    loaded: bool = False
    recovered: bool = False
    prefix: int = 0
    current_tile_scene: str = ""
    current_tiles: set = field(default_factory=set)
    selection_signature: tuple | None = None
    selection_counts: dict = field(default_factory=dict)


class ProgressCollector:
    """Collect progress in memory. Reuse the instance between snapshot() calls."""

    def __init__(self, program_root=Path(__file__).resolve().parent):
        self.program_root = Path(program_root)
        self.run_root = Path(os.environ.get("HLS_RUN_ROOT", str(self.program_root / "runs" / "archive")))
        self.output_root = OUTPUT_ROOT
        self.service_name = SERVICE
        self.months = {}
        self._configuration_warnings = []
        self._job_status = {}
        self._active_nodes = {node: None for node in NODES}
        self._invocation = None
        self._cursor = None
        self._discovered = set()
        self._last_current_events = []
        self._preflight = False
        self._dirty_jobs = set()
        self._archive_final_success = False
        self._current_failed = set()
        self._retries = {}
        self._load_configuration()

    def _load_configuration(self):
        try:
            settings = _read_json(self.run_root / "run.json", 64 * 1024)
            # Only this known service can be monitored; no arbitrary unit command.
            if settings.get("service") not in (None, SERVICE):
                self._configuration_warnings.append("任务配置的服务名称与本监控不一致。")
            output = settings.get("output_root")
            if output is not None and str(output) != str(OUTPUT_ROOT):
                self._configuration_warnings.append("任务保存目录与本监控默认目录不一致。")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            self._configuration_warnings.append("任务配置不可读；使用固定任务服务和NAS目录。")
        try:
            inventory = _read_json(self.run_root / "inventory.json", 8 * 1024 * 1024)
            if inventory.get("schema_version") != 1 or not isinstance(inventory.get("regions"), list):
                raise ValueError("invalid inventory")
            seen = set()
            for region in inventory["regions"]:
                code = region.get("code")
                if code not in REGION_CODES or code in seen:
                    raise ValueError("invalid region")
                seen.add(code)
                for month in region.get("months", []):
                    boundary = str(month.get("start")) + "__" + str(month.get("end_exclusive"))
                    count = month.get("scene_count")
                    if not MONTH_RE.fullmatch(boundary) or type(count) is not int or count < 0:
                        raise ValueError("invalid month")
                    if not count:
                        continue
                    name = code + "/" + boundary
                    if name in self.months:
                        raise ValueError("duplicate month")
                    self.months[name] = _Month(name, code, count, self.output_root / code / boundary)
            if seen != set(REGION_CODES):
                raise ValueError("incomplete regions")
        except (OSError, ValueError, TypeError, AttributeError):
            self.months.clear()
            self._configuration_warnings.append("冻结库存无法读取；总量未知，不能计算总进度。")

    def _service(self, warnings):
        state = {"active": "unknown", "substate": "unknown", "result": "unknown",
                 "started": "", "invocation_id": ""}
        try:
            output = _run_command(["systemctl", "--user", "show", self.service_name,
                                   "--property=ActiveState,SubState,Result,ActiveEnterTimestamp,InvocationID"])
            values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            state.update(active=values.get("ActiveState", "unknown"),
                         substate=values.get("SubState", "unknown"),
                         result=values.get("Result", "unknown"),
                         started=values.get("ActiveEnterTimestamp", ""),
                         invocation_id=values.get("InvocationID", ""))
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            warnings.append("读取systemd状态失败或超时；这不表示下载已停止。")
        return state

    def _proxy(self, warnings):
        """Only inspect the local SSH-forwarding unit; do not probe the network."""
        state = {"active": "unknown", "substate": "unknown", "restarts": None}
        try:
            output = _run_command(["systemctl", "--user", "show", PROXY_SERVICE,
                                   "--property=ActiveState,SubState,NRestarts,InvocationID"])
            values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            restarts = values.get("NRestarts", "")
            state.update(active=values.get("ActiveState", "unknown"),
                         substate=values.get("SubState", "unknown"),
                         invocation_id=values.get("InvocationID", ""),
                         restarts=int(restarts) if restarts.isdecimal() else None)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            warnings.append("下载代理服务状态读取失败或超时；连接状态待确认。")
        try:
            route = _read_json(self.program_root / "runs/hls_ssh_route.json", 65536)
            if route.get("route") in ("windows_ssh_relay", "linux_direct", "linux_tls", "beijing_tls", "linux_mihomo"):
                state["route"] = route["route"]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            warnings.append("代理线路记录暂不可读；实际下载反馈仍是网络可用性依据。")
        return state

    def _journal(self, service, warnings):
        invocation = service["invocation_id"] if service["active"] != "unknown" else self._invocation
        changed = invocation != self._invocation
        if changed:
            self._active_nodes = {node: None for node in NODES}
            self._preflight = False
            self._current_failed.clear()
            self._retries.clear()
            self._invocation = invocation
            # Replay bounded history to restore this invocation's node assignments.
            self._cursor = None
        argv = ["journalctl", "--user", "-u", self.service_name, "--no-pager", "-n", "20000",
                "--output=json", "--output-fields=MESSAGE,_SYSTEMD_INVOCATION_ID,_SYSTEMD_USER_INVOCATION_ID,__CURSOR,__REALTIME_TIMESTAMP",
                "--grep=" + JOURNAL_FILTER]
        if self._cursor:
            argv.append("--after-cursor=" + self._cursor)
        try:
            output = _run_command(argv)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            if self._cursor:
                self._cursor = None  # Next poll retries bounded history after rotation.
            warnings.append("任务日志读取失败或超时；节点分工使用上次缓存。")
            return
        events = []
        for line in output.splitlines():
            try:
                item = json.loads(line)
                message = item.get("MESSAGE", "")
                if not isinstance(message, str):
                    continue
                cursor = item.get("__CURSOR")
                if isinstance(cursor, str):
                    self._cursor = cursor
                event_invocation = item.get("_SYSTEMD_INVOCATION_ID") or item.get("_SYSTEMD_USER_INVOCATION_ID")
                current = bool(invocation and event_invocation == invocation)
                match = START_RE.fullmatch(message)
                if match:
                    node, name = match.groups()
                    if name not in self.months:
                        continue
                    self._job_status[name] = "running"
                    self._archive_final_success = False
                    if current:
                        self._active_nodes[node] = name
                        self._preflight = False
                        self._retries.pop(node, None)
                    events.append(message)
                    continue
                match = END_RE.fullmatch(message)
                if match:
                    node, name, exit_code = match.groups()
                    if name not in self.months:
                        continue
                    self._job_status[name] = "success" if int(exit_code) == 0 else "failed"
                    self._dirty_jobs.add(name)
                    if current:
                        if int(exit_code):
                            self._current_failed.add(name)
                        else:
                            self._current_failed.discard(name)
                    if current and self._active_nodes[node] == name:
                        self._active_nodes[node] = None
                    if current and self._retries.get(node, {}).get("job") == name:
                        self._retries.pop(node, None)
                    events.append(message)
                    continue
                waiting = RETRY_WAIT_RE.fullmatch(message)
                retrying = RETRY_START_RE.fullmatch(message)
                if waiting or retrying:
                    match = waiting or retrying
                    node, name, attempt, total = match.groups()[:4]
                    attempt, total = int(attempt), int(total)
                    wait_seconds = float(waiting.group(5)) if waiting else 0
                    if name not in self.months or not 1 <= attempt <= total <= 100 or (waiting and not 1 <= wait_seconds <= 3600):
                        continue
                    # Preserve the existing "300 秒" display for integral delays.
                    if isinstance(wait_seconds, float) and wait_seconds.is_integer():
                        wait_seconds = int(wait_seconds)
                    self._job_status[name] = "running"
                    if current:
                        now_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
                        try:
                            timestamp_us = int(item.get("__REALTIME_TIMESTAMP", now_us))
                        except (ValueError, TypeError):
                            timestamp_us = now_us
                        self._active_nodes[node] = name
                        self._preflight = False
                        self._current_failed.discard(name)
                        self._retries[node] = {"job": name, "attempt": attempt,
                                               "total_retries": total, "wait_seconds": wait_seconds,
                                               "until_us": timestamp_us + round(wait_seconds * 1_000_000),
                                               "waiting": bool(waiting)}
                    events.append(message)
                    continue
                if current and message.startswith("离线预检 "):
                    self._preflight = True
                final = FINAL_RE.fullmatch(message)
                if final:
                    done, failed, pending = map(int, final.groups())
                    self._archive_final_success = done == len(self.months) and failed == 0 and pending == 0
                if message.startswith(("结束：", "错误：", "已停止新任务", "离线预检 ")):
                    events.append(message)
            except (ValueError, TypeError, AttributeError):
                continue
        if events:
            self._last_current_events = events[-5:]

    def _nas(self, warnings):
        info = {"mounted": False, "free_gib": None}
        try:
            output = _run_command(["findmnt", "--noheadings", "--output", "SOURCE,FSTYPE", "--target", "/nasdata"])
            rows = [line.split() for line in output.splitlines()]
            info["mounted"] = any(len(row) >= 2 and row[0] == NAS_SOURCE and row[1] == "cifs" for row in rows)
            if not info["mounted"]:
                warnings.append("NAS挂载来源不匹配或已断开；不读取可能残留的本地目录。")
                return info
            capacity = os.statvfs("/nasdata")
            info["free_gib"] = capacity.f_bavail * capacity.f_frsize / (1024 ** 3)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            warnings.append("NAS挂载/容量读取失败或超时；展示上次已确认进度。")
        return info

    def _discover(self, warnings):
        new = []
        for code in REGION_CODES:
            region_path = self.output_root / code
            try:
                if region_path.is_symlink():
                    continue
                with os.scandir(region_path) as entries:
                    for entry in entries:
                        name = code + "/" + entry.name
                        if name in self.months and name not in self._discovered and entry.is_dir(follow_symlinks=False):
                            self._discovered.add(name)
                            new.append(name)
            except FileNotFoundError:
                continue
            except OSError:
                warnings.append(f"{code}月目录暂时不可读；保留已确认计数。")
        return new

    def _manifest(self, month, warnings):
        path = month.path / "run_manifest.json"
        try:
            if month.path.is_symlink() or path.is_symlink():
                raise ValueError("symlink manifest")
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == month.signature and month.loaded:
                return True
            data = _read_json(path, 16 * 1024 * 1024)
            recipe_hash, scenes = data.get("recipe_hash"), data.get("scenes")
            if not isinstance(recipe_hash, str) or not recipe_hash or not isinstance(scenes, list) or len(scenes) > 10000:
                raise ValueError("invalid manifest")
            keys = set()
            for scene in scenes:
                if (not isinstance(scene, dict) or scene.get("sensor") not in ("L30", "S30") or
                        not isinstance(scene.get("index"), str) or not INDEX_RE.fullmatch(scene["index"]) or
                        not isinstance(scene.get("id"), str)):
                    raise ValueError("invalid scene metadata")
                collection = "NASA/HLS/HLS" + scene["sensor"] + "/v002/"
                if scene["id"] != collection + scene["index"]:
                    raise ValueError("invalid scene asset")
                key = (scene["sensor"], scene["index"])
                if key in keys:
                    raise ValueError("duplicate scene")
                keys.add(key)
            month.scenes, month.recipe_hash, month.signature = scenes, recipe_hash, signature
            month.complete.clear()
            month.prefix, month.current_tile_scene = 0, ""
            month.current_tiles.clear()
            month.loaded, month.recovered = True, False
            if len(scenes) != month.expected:
                warnings.append(f"{month.name}实际查询{len(scenes)}景，冻结库存{month.expected}景；总景数百分比仅作库存参考。")
            return True
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError, AttributeError):
            warnings.append(f"{month.name}月manifest暂不可读或内容无效。")
            return False

    @staticmethod
    def _scene_key(scene):
        return (scene["sensor"], scene["index"])

    def _scene_complete(self, month, scene, warnings=None):
        directory = month.path / scene["sensor"] / scene["index"]
        try:
            if directory.is_symlink() or directory.parent.is_symlink():
                return False
            record = _read_json(directory / "scene.json", 2 * 1024 * 1024)
            if any(record.get(key) != scene[key] for key in ("id", "index", "sensor")) or record.get("recipe_hash") != month.recipe_hash:
                return False
            tiles = record.get("tiles")
            if not isinstance(tiles, list):
                return False
            names = set()
            for tile in tiles:
                if not isinstance(tile, dict) or not isinstance(tile.get("file"), str) or not TILE_FILE_RE.fullmatch(tile["file"]):
                    return False
                if tile["file"] in names or type(tile.get("valid_pixels")) is not int or tile["valid_pixels"] < 0:
                    return False
                names.add(tile["file"])
            # An empty relevant-tile list and all-NoData tiles are valid completions.
            return True
        except FileNotFoundError:
            return False
        except OSError:
            if warnings is not None:
                warnings.append(f"{month.name}/{scene['sensor']}/{scene['index']}完成记录暂不可读；进度保守显示。")
            return False
        except (ValueError, TypeError, AttributeError):
            return False

    def _recover(self, month, warnings=None):
        """Scan only directories which actually exist, once per manifest version."""
        known = {self._scene_key(scene): scene for scene in month.scenes}
        for sensor in ("L30", "S30"):
            directory = month.path / sensor
            try:
                if directory.is_symlink():
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        key = (sensor, entry.name)
                        if key in known and entry.is_dir(follow_symlinks=False) and self._scene_complete(month, known[key], warnings):
                            month.complete.add(key)
            except FileNotFoundError:
                continue
            except OSError:
                if warnings is not None:
                    warnings.append(f"{month.name}/{sensor}景目录暂不可读；保留已确认进度。")
        month.recovered = True
        self._advance_prefix(month)

    def _advance_prefix(self, month):
        while month.prefix < len(month.scenes) and self._scene_key(month.scenes[month.prefix]) in month.complete:
            month.prefix += 1

    def _refresh_active(self, month, warnings=None):
        self._advance_prefix(month)
        # Bound a refresh even if a resumed worker skips a long cached sequence.
        for _ in range(64):
            if month.prefix >= len(month.scenes):
                break
            scene = month.scenes[month.prefix]
            if not self._scene_complete(month, scene, warnings):
                break
            month.complete.add(self._scene_key(scene))
            month.prefix += 1

    def _current_tiles(self, month, scene):
        if scene["id"] != month.current_tile_scene:
            month.current_tile_scene = scene["id"]
            month.current_tiles.clear()
        directory = month.path / scene["sensor"] / scene["index"]
        existing = set()
        try:
            if directory.is_symlink() or directory.parent.is_symlink():
                return 0
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not TILE_RE.fullmatch(entry.name) or not entry.is_file(follow_symlinks=False):
                        continue
                    existing.add(entry.name)
                    if entry.name in month.current_tiles:
                        continue
                    try:
                        record = _read_json(Path(entry.path), 1024 * 1024)
                        if record.get("recipe_hash") == month.recipe_hash and record.get("scene_id") == scene["id"]:
                            month.current_tiles.add(entry.name)
                    except (OSError, ValueError, TypeError):
                        pass
            month.current_tiles.intersection_update(existing)
        except OSError:
            pass
        return len(month.current_tiles)

    def _selection_summary(self, month, warnings):
        if not month.loaded:
            return
        path = month.path / "download_selection_summary.json"
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            if signature == month.selection_signature:
                return
            value = _read_json(path, 65536)
            fields = ("processed_scenes", "skipped_no_clear_scenes", "skipped_overlap_scenes", "skipped_empty_tiles")
            if (value.get("recipe_hash") != month.recipe_hash or
                    any(type(value.get(k)) is not int or value[k] < 0 for k in fields) or
                    value["processed_scenes"] > len(month.scenes) or
                    value["skipped_no_clear_scenes"] + value["skipped_overlap_scenes"] > value["processed_scenes"]):
                raise ValueError("invalid selection summary")
            month.selection_counts = {k: value[k] for k in fields}
            month.selection_signature = signature
        except FileNotFoundError:
            pass  # Completed legacy months have no optimization summary.
        except (OSError, ValueError, TypeError):
            warnings.append(f"{month.name}下载筛选统计暂不可读；保留已确认进度。")

    def snapshot(self):
        warnings = list(self._configuration_warnings)
        service = self._service(warnings)
        proxy = self._proxy(warnings)
        self._journal(service, warnings)
        nas = self._nas(warnings)
        current_jobs = {name for name in self._active_nodes.values() if name}
        if nas["mounted"]:
            new = set(self._discover(warnings))
            candidates = new | current_jobs | self._dirty_jobs | {name for name, status in self._job_status.items()
                                             if status == "success" and not self.months[name].loaded}
            for name in candidates:
                month = self.months[name]
                if not self._manifest(month, warnings):
                    continue
                if self._job_status.get(name) == "success":
                    month.complete = {self._scene_key(scene) for scene in month.scenes}
                    month.prefix = len(month.scenes)
                    month.recovered = True
                else:
                    if not month.recovered or (name in self._dirty_jobs and self._job_status.get(name) == "failed"):
                        self._recover(month, warnings)
                    if name in current_jobs:
                        self._refresh_active(month, warnings)
                self._dirty_jobs.discard(name)
        region_rows = []
        completed_jobs = 0
        completed_scenes = 0
        for code in REGION_CODES:
            months = [month for month in self.months.values() if month.code == code]
            for month in months:
                if nas["mounted"]:
                    self._selection_summary(month, warnings)
                if month.loaded and len(month.scenes) != month.expected:
                    warnings.append(f"{month.name}实际查询{len(month.scenes)}景，冻结库存{month.expected}景；总景数百分比仅作库存参考。")
            scenes_done = sum(len(month.complete) for month in months)
            jobs_done = sum(month.loaded and len(month.complete) == len(month.scenes) and
                            (self._job_status.get(month.name) == "success" or
                             (month.name not in current_jobs and self._job_status.get(month.name) not in ("running", "failed")))
                            for month in months)
            completed_jobs += jobs_done
            completed_scenes += scenes_done
            region_rows.append({"code": code, "completed_scenes": scenes_done,
                                "total_scenes": sum(month.expected for month in months),
                                "completed_jobs": jobs_done, "total_jobs": len(months)})
        nodes = {}
        for node in NODES:
            name = self._active_nodes[node]
            is_running = service["active"] in ("active", "activating")
            phase = ("unknown" if service["active"] == "unknown" else "stopped" if not is_running else
                     "downloading" if name else "preflight" if self._preflight else "waiting")
            retry = self._retries.get(node, {})
            if retry.get("job") != name:
                retry = {}
            if phase == "downloading" and retry.get("waiting"):
                phase = "retrying"
            now_us = int(datetime.now(timezone.utc).timestamp() * 1_000_000)
            retry_view = ({"attempt": retry["attempt"], "total_retries": retry["total_retries"],
                           "wait_seconds": retry["wait_seconds"],
                           "remaining_seconds": min(retry["wait_seconds"], max(0, (retry["until_us"] - now_us + 999999) // 1000000))}
                          if retry else None)
            value = {"job": name, "phase": phase, "completed_scenes": 0, "total_scenes": 0,
                     "current_scene": "", "sensor": "", "tile_count_current": 0,
                     "retry": retry_view}
            if name:
                month = self.months[name]
                value.update(completed_scenes=len(month.complete),
                             total_scenes=len(month.scenes) if month.loaded else month.expected)
                if month.loaded and month.prefix < len(month.scenes):
                    scene = month.scenes[month.prefix]
                    value.update(current_scene=scene["index"], sensor=scene["sensor"])
                    if nas["mounted"] and is_running:
                        value["tile_count_current"] = self._current_tiles(month, scene)
                elif month.loaded and month.scenes and phase == "downloading":
                    value["phase"] = "verifying"
            nodes[node] = value
        if completed_scenes > sum(month.expected for month in self.months.values()):
            warnings.append("实际已完成景数超过冻结库存；以月任务完成数判断归档结束。")
        archive_complete = bool(self.months and completed_jobs == len(self.months) and
                                self._archive_final_success and nas["mounted"] and
                                service["active"] == "inactive" and service["result"] == "success")
        if self.months and completed_jobs == len(self.months) and not self._archive_final_success:
            warnings.append("所有月内容已落盘，但尚未确认归档服务最终正常完成。")
        return {"updated_at": datetime.now(timezone.utc).isoformat(), "service": service,
                "total_scenes": sum(month.expected for month in self.months.values()),
                "completed_scenes": completed_scenes, "total_jobs": len(self.months),
                "completed_jobs": completed_jobs,
                **{key: sum(m.selection_counts.get(key, 0) for m in self.months.values())
                   for key in ("skipped_no_clear_scenes", "skipped_overlap_scenes", "skipped_empty_tiles")},
                "failed_jobs": sorted(self._current_failed),
                "nodes": nodes, "regions": region_rows, "nas": nas, "proxy": proxy,
                "warnings": list(dict.fromkeys(warnings)), "recent_events": list(self._last_current_events),
                "archive_complete": archive_complete}
