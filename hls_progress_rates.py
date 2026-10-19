"""Read-only throughput and rolling ETA for the terminal progress dashboard."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
import json
import math
import re
import subprocess
import time

import hls_progress_state as state


class ProgressRates:
    """Use monotonic elapsed time; cache replays and service changes cannot add speed."""

    def __init__(self, window=900, minimum_seconds=120, minimum_scenes=3):
        self.window = window
        self.minimum_seconds = minimum_seconds
        self.minimum_scenes = minimum_scenes
        self.samples = deque()
        self.key = None

    def update(self, snapshot, now=None, wall_now=None):
        now = time.monotonic() if now is None else now
        wall_now = datetime.now(timezone.utc) if wall_now is None else wall_now
        service, proxy = snapshot.get("service", {}), snapshot.get("proxy", {})
        done, total = snapshot.get("completed_scenes"), snapshot.get("total_scenes")
        result = {"status": "sampling", "window_seconds": 0, "processed_candidates": 0,
                  "candidates_per_minute": None, "candidates_per_day": None,
                  "remaining_seconds": None, "estimated_finish_utc": None,
                  "minimum_seconds": self.minimum_seconds, "minimum_scenes": self.minimum_scenes}
        if snapshot.get("archive_complete"):
            self.samples.clear()
            return {**result, "status": "complete", "remaining_seconds": 0}
        if type(done) is not int or type(total) is not int or not 0 <= done <= total or not total:
            self.samples.clear()
            return {**result, "status": "unavailable", "reason": "进度计数暂不可用"}
        if service.get("active") != "active" or not snapshot.get("nas", {}).get("mounted"):
            self.samples.clear()
            return {**result, "status": "paused", "reason": "服务未运行或NAS状态待确认"}
        if snapshot.get("warnings"):
            self.samples.clear()
            return {**result, "status": "unavailable", "reason": "进度读取存在提示，暂不估算"}
        if done == total:
            return {**result, "status": "verifying", "reason": "候选已处理，等待全部月份核验及结束确认"}
        key = (service.get("invocation_id"), proxy.get("invocation_id"), proxy.get("route"), total)
        if (key != self.key or (self.samples and
                (done < self.samples[-1][1] or now <= self.samples[-1][0] or
                 now - self.samples[-1][0] > self.window))):
            self.samples.clear()
        self.key = key
        self.samples.append((now, done))
        while len(self.samples) > 1 and self.samples[0][0] < now - self.window:
            self.samples.popleft()
        elapsed = now - self.samples[0][0]
        delta = done - self.samples[0][1]
        result.update(window_seconds=round(elapsed, 1), processed_candidates=delta)
        if snapshot.get("failed_jobs"):
            return {**result, "status": "blocked", "reason": "存在失败任务，暂不推算全部完成时间"}
        nodes = list(snapshot.get("nodes", {}).values())
        if nodes and all(n.get("phase") in ("retrying", "waiting", "stopped", "idle") for n in nodes):
            return {**result, "status": "paused", "reason": "所有节点正在等待或重试"}
        if proxy.get("active") not in (None, "active"):
            return {**result, "status": "paused", "reason": "代理恢复中"}
        if elapsed < self.minimum_seconds:
            return result
        rate = delta / elapsed
        result.update(candidates_per_minute=rate * 60, candidates_per_day=rate * 86400)
        if delta < self.minimum_scenes:
            return {**result, "status": "waiting_progress", "reason": "等待更多候选完成后估算"}
        remaining = (total - done) / rate
        if not math.isfinite(remaining) or remaining < 0:
            return {**result, "status": "unavailable", "reason": "估算样本暂不可用"}
        finish = None
        try:
            finish = (wall_now + timedelta(seconds=remaining)).isoformat()
        except OverflowError:
            pass
        return {**result, "status": "ready", "remaining_seconds": remaining,
                "estimated_finish_utc": finish, "short_window": elapsed < self.window}


class BlockActivity:
    """Count committed, newly downloaded tiles from bounded local journal history."""

    committed = re.compile(r"^\s*\d+/\d+\s+r\d+_c\d+\.tif\s+已下载；有效像元\s+\d+")

    def __init__(self, window=300, runner=None):
        self.window = window
        self.runner = runner
        self.events = {}
        self.cursor = None
        self.key = None
        self.start = None

    def snapshot(self, snapshot, now=None):
        now = time.time() if now is None else now
        service, proxy = snapshot.get("service", {}), snapshot.get("proxy", {})
        invocation = service.get("invocation_id")
        result = {"status": "unavailable", "committed_blocks": 0,
                  "window_seconds": self.window, "blocks_per_minute": None}
        if service.get("active") != "active" or not invocation:
            return result
        key = (invocation, proxy.get("invocation_id"), proxy.get("route"))
        if key != self.key:
            self.events.clear()
            self.cursor = None
            self.start = now - self.window if self.key is None else now
            self.key = key
        cutoff = max(now - self.window, self.start)
        argv = ["journalctl", "--user", "-u", state.SERVICE, "--no-pager", "-n", "20000",
                "--output=json", "--output-fields=MESSAGE,_SYSTEMD_INVOCATION_ID,_SYSTEMD_USER_INVOCATION_ID,__CURSOR,__REALTIME_TIMESTAMP",
                "--grep=已下载；有效像元"]
        argv.append("--after-cursor=" + self.cursor if self.cursor else "--since=@" + str(int(cutoff)))
        try:
            raw = (self.runner or state._run_command)(argv)
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            self.cursor = None
            return result
        rows = raw.splitlines()
        if len(rows) >= 20000:
            self.cursor = None
            return result  # A truncated log cannot establish the full window rate.
        for line in rows:
            try:
                row = json.loads(line)
                cursor = row.get("__CURSOR")
                if isinstance(cursor, str) and cursor:
                    self.cursor = cursor
                event_invocation = row.get("_SYSTEMD_INVOCATION_ID") or row.get("_SYSTEMD_USER_INVOCATION_ID")
                stamp = int(row["__REALTIME_TIMESTAMP"]) / 1_000_000
                message = row.get("MESSAGE", "")
                if (isinstance(cursor, str) and cursor and event_invocation == invocation and
                        cutoff <= stamp <= now and isinstance(message, str) and self.committed.match(message)):
                    self.events[cursor] = stamp
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        self.events = {cursor: stamp for cursor, stamp in self.events.items() if cutoff <= stamp <= now}
        elapsed = min(self.window, now - self.start)
        count = len(self.events)
        return {**result, "status": "ready" if elapsed >= 30 else "sampling",
                "committed_blocks": count, "window_seconds": round(elapsed, 1),
                "blocks_per_minute": count * 60 / elapsed if elapsed >= 30 else None}
