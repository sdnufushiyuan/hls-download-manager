#!/usr/bin/env python3
"""Read-only HLS download dashboard for a VS Code remote terminal."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import time
import unicodedata

from hls_progress_state import ProgressCollector
from hls_progress_rates import BlockActivity, ProgressRates


def bar(done, total, width=30):
    fraction = min(1.0, max(0.0, done / total)) if total else 0.0
    filled = int(fraction * width)
    return f"[{'#' * filled}{'-' * (width - filled)}] {fraction:7.3%}"


def fit(text, width):
    """Clip by terminal columns, including Chinese characters."""
    columns, output = 0, []
    for char in str(text):
        if ord(char) < 32 or ord(char) == 127:
            char = " "
        cost = 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if columns + cost > width - 1:
            return "".join(output) + "…"
        output.append(char)
        columns += cost
    return "".join(output)


def rate_lines(snapshot):
    activity = snapshot.get("download_activity", {})
    rate = snapshot.get("throughput", {})
    blocks = activity.get("blocks_per_minute")
    if blocks is None:
        first = "实际落盘速度：日志测速采样中/暂不可用"
    else:
        first = (f"实际落盘速度：{blocks:,.1f} 块/分钟"
                 f"（近 {activity.get('window_seconds', 0) / 60:.1f} 分钟；不计缓存复用）")
    per_day = rate.get("candidates_per_day")
    if per_day is None:
        second = f"候选处理速度：采样中（至少 {rate.get('minimum_seconds', 120):g} 秒及 {rate.get('minimum_scenes', 3)} 景进展）"
    else:
        second = (f"候选处理速度：{per_day / 24:,.1f} 景/小时，约 {per_day:,.0f} 景/天"
                  f"（采样 {rate.get('window_seconds', 0) / 60:.1f} 分钟）")
    status = rate.get("status", "sampling")
    if status == "complete":
        third = "预计剩余：0，全部完成。"
    elif status == "ready":
        seconds = rate["remaining_seconds"]
        duration = (f"{seconds / 86400:.1f} 天" if seconds >= 86400 else
                    f"{seconds / 3600:.1f} 小时" if seconds >= 3600 else f"{seconds / 60:.0f} 分钟")
        finish = rate.get("estimated_finish_utc")
        try:
            finish = datetime.fromisoformat(finish).astimezone().strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
            finish = None
        third = f"预计剩余：约 {duration}" + (f"；预计完成 {finish}" if finish else "")
        third += "（短时估算）" if rate.get("short_window") else "（滚动估算）"
    else:
        third = "预计剩余：" + rate.get("reason", "采样中，积累有效进展后自动显示")
    return [first, second, third]


def render(snapshot, width=110, interval=5):
    service = snapshot.get("service", {})
    active, result = service.get("active", "unknown"), service.get("result", "unknown")
    substate = service.get("substate", "unknown")
    complete = snapshot.get("archive_complete", False)
    status = ("全部完成" if complete else "下载运行中" if active == "active" else
              "等待服务自动重启，随后继续断点下载" if active == "activating" and substate == "auto-restart" else
              "任务失败，请查看日志" if active == "failed" or result not in ("success", "unknown", "") else
              "任务已停止" if active == "inactive" else "正在启动/服务状态待确认")
    if active == "active" and snapshot.get("failed_jobs"):
        status = "部分任务失败，已暂停领取新任务"
    elif active == "active" and any(record.get("phase") == "retrying" for record in snapshot.get("nodes", {}).values()):
        status = "等待网络恢复，自动重试"
    timestamp = snapshot.get("updated_at", "未知")
    try:
        timestamp = datetime.fromisoformat(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError):
        pass
    lines = [f"HLS 全历史下载进度    {timestamp}",
             f"服务：{status}  ({active}/{service.get('substate', 'unknown')})",
             f"本轮启动：{service.get('started') or '未知'}", ""]
    proxy = snapshot.get("proxy", {})
    proxy_active = proxy.get("active", "unknown")
    if proxy_active in ("activating", "failed", "deactivating"):
        proxy_status = "下载代理恢复中"
    elif proxy_active == "active":
        proxy_status = "转发进程运行；网络可用性由实际下载反馈"
    elif proxy_active == "inactive":
        proxy_status = "下载代理已停止，网络转发不可用"
    else:
        proxy_status = "下载代理状态待确认"
    lines.insert(3, f"代理：{proxy_status}  ({proxy_active}/{proxy.get('substate', 'unknown')})"
                 f"  本轮重启：{proxy.get('restarts') if proxy.get('restarts') is not None else '未知'}")
    if proxy_active == "active" and proxy.get("route") == "windows_ssh_relay":
        lines.insert(4, "链路：Linux → Windows SSH中继 → 新加坡；Windows需保持在线。")
    elif proxy_active == "active" and proxy.get("route") == "linux_direct":
        lines.insert(4, "链路：Linux → 新加坡 SSH 2222 直连；不依赖 Windows。")
    elif proxy_active == "active" and proxy.get("route") == "linux_tls":
        lines.insert(4, "链路：Linux → 新加坡 TLS/SSH 2222；不依赖 Windows。")
    elif proxy_active == "active" and proxy.get("route") == "beijing_tls":
        lines.insert(4, "链路：Linux → 北京受限转发 → 新加坡 TLS/SSH 2222；不依赖 Windows。")
    elif proxy_active == "active" and proxy.get("route") == "linux_mihomo":
        lines.insert(4, "链路：Linux → Mihomo → 已有Clash节点；不依赖 Windows。")
    done, total = snapshot.get("completed_scenes", 0), snapshot.get("total_scenes", 0)
    bar_width = min(36, max(14, width - 52))
    lines.extend([f"候选处理  {bar(done, total, bar_width)}  {done:,} / {total:,}（库存参考）",
                  f"成功月任务：{snapshot.get('completed_jobs', 0):,} / {snapshot.get('total_jobs', 0):,}"
                  f"    失败任务：{len(snapshot.get('failed_jobs', []))}",
                  f"优化跳过：无晴空 {snapshot.get('skipped_no_clear_scenes', 0):,} 景；"
                  f"全重叠 {snapshot.get('skipped_overlap_scenes', 0):,} 景；"
                  f"空/重复块 {snapshot.get('skipped_empty_tiles', 0):,} 块", ""])
    lines.extend(rate_lines(snapshot) + [""])
    phases = {"running": "处理中", "downloading": "筛选/下载中", "preflight": "预检中", "idle": "等待任务", "waiting": "等待任务", "stopped": "已停止",
              "failed": "失败", "unknown": "待确认", "verifying": "核验/收尾", "complete": "已完成",
              "retrying": "等待网络恢复，自动重试"}
    for node in ("node1", "node2", "master"):
        record = snapshot.get("nodes", {}).get(node, {})
        phase = phases.get(record.get("phase"), record.get("phase") or "待确认")
        label = "master（管理节点）" if node == "master" else node
        lines.append(f"{label}  {phase}  {record.get('job') or '—'}")
        retry = record.get("retry") or {}
        if retry:
            if record.get("phase") == "retrying":
                lines.append(f"  恢复重试 {retry.get('attempt', '?')}/{retry.get('total_retries', '?')}；"
                             f"本次等待 {retry.get('wait_seconds', '?')} 秒，剩余约 {retry.get('remaining_seconds', '?')} 秒。")
            elif record.get("phase") == "downloading":
                lines.append(f"  网络恢复重试 {retry.get('attempt', '?')}/{retry.get('total_retries', '?')}；核验旧块后继续下载。")
        count, target = record.get("completed_scenes", 0), record.get("total_scenes", 0)
        if target:
            lines.append(f"  本月候选 {bar(count, target, bar_width)}  {count:,} / {target:,}")
        if record.get("current_scene"):
            lines.append(f"  待完成景：{record.get('sensor', '')} {record['current_scene']}")
            lines.append(f"  该景已落盘：{record.get('tile_count_current', 0):,} 块（.part 不计入）")
        elif target and count == target:
            lines.append("  本月候选已处理，等待任务结束确认。")
        lines.append("")
    lines.append("研究区         已处理候选 / 库存参考     成功月 / 总月    状态")
    for region in snapshot.get("regions", []):
        active_region = any((record.get("job") or "").startswith(region['code'] + '/')
                            for record in snapshot.get('nodes', {}).values())
        region_status = ("已完成，可后续处理" if region.get('ready_for_processing') else
                         "下载/核验中" if active_region else "待完成")
        lines.append(f"  {region['code']:<5} {region.get('completed_scenes', 0):>8,} / "
                     f"{region.get('total_scenes', 0):<8,}      "
                     f"{region.get('completed_jobs', 0):>3} / {region.get('total_jobs', 0)}    "
                     f"{region_status}")
    lines.append("")
    nas = snapshot.get("nas", {})
    if nas.get("mounted") and nas.get("free_gib") is not None:
        lines.append(f"NAS：已挂载，剩余 {nas['free_gib']:,.1f} GiB    保留底线：256 GiB")
    else:
        lines.append("NAS：挂载或剩余空间待确认")
    for warning in snapshot.get("warnings", [])[:4]:
        lines.append(f"提示：{warning}")
    for job in snapshot.get("failed_jobs", [])[:2]:
        lines.append(f"失败任务：{job}")
    lines.extend(["", f"每 {interval:g} 秒刷新；候选包含复用/跳过，工期按最近15分钟进度滚动估算。",
                  "Ctrl+C 只退出面板，后台下载继续。"])
    return "\n".join(fit(line, max(50, width)) for line in lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="打印一次状态，不持续刷新")
    parser.add_argument("--json", action="store_true", help="打印一次 JSON 状态")
    parser.add_argument("--interval", type=float, default=5.0, help="刷新间隔秒数，至少 3 秒")
    parser.add_argument("--program-root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args(argv)
    if not 3 <= args.interval <= 300:
        parser.error("刷新间隔需要在 3–300 秒之间。")
    collector = ProgressCollector(program_root=args.program_root)
    rates, activity = ProgressRates(), BlockActivity()
    watching = not (args.once or args.json or not sys.stdout.isatty())
    try:
        if watching:
            print("正在读取下载状态…", flush=True)
        while True:
            snapshot = collector.snapshot()
            snapshot["download_activity"] = activity.snapshot(snapshot)
            snapshot["throughput"] = rates.update(snapshot)
            if args.json:
                print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            else:
                if watching:
                    sys.stdout.write("\x1b[2J\x1b[H")
                print(render(snapshot, shutil.get_terminal_size((110, 35)).columns, args.interval), flush=True)
            if not watching:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已退出进度面板；后台下载继续。", flush=True)
    except (OSError, ValueError, RuntimeError) as error:
        # Keep external command contents/URLs out of the UI.
        print(f"进度读取失败（{type(error).__name__}）。后台下载不受影响。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
