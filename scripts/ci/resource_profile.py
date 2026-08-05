#!/usr/bin/env python3
"""CPU / RAM / disk-IO profiler for CI jobs.

Runs as a background daemon: samples /proc every second, accumulates
stats, and on SIGTERM (or timeout) writes a JSON summary to the output
path. Pure stdlib — runs on the bare runner Python with zero deps.

Usage:
    python3 scripts/ci/resource_profile.py \\
        --output resource-profile.json \\
        --label "tests slice 1/8"

The composite action (.github/actions/profile) starts this as a
background process, runs the real command, then signals it to stop.

Output JSON shape:
    {
        "label": "tests slice 1/8",
        "duration_s": 42.3,
        "started_at": "2026-01-01T00:01:00Z",   # UTC bounds of the sample
        "completed_at": "2026-01-01T00:01:42Z", # window, for report placement
        "cpu": {
            "avg_usage_pct": 55.2,
            "peak_usage_pct": 89.1,
            "samples": 42
        },
        "memory": {                  # USED memory (MemTotal - MemAvailable)
            "avg_mb": 512.0,
            "peak_mb": 684.3,
            "samples": 42
        },
        "disk": {
            "total_mb": 12.4,        # sectors read+written, whole devices only
            "avg_ops_per_s": 5.2,    # completed read+write IOs per second
            "peak_ops_per_s": 20.1,
            "avg_util_pct": 31.0,    # busy time (iostat %util), whole devices
            "peak_util_pct": 96.0,
            "samples": 42
        },
        "series": {                  # downsampled timeline for the report overlay
            "interval_s": 1.0,       # seconds per point AFTER downsampling
            "points": 42,
            "cpu_pct": [12, 40, ...],   # 0-100 ints, one per point
            "mem_pct": [30, 31, ...],   # used/total, 0-100 ints
            "disk_pct": [5, 80, ...]    # busy-time util, 0-100 ints
        }
    }

The series is capped at ``_MAX_SERIES_POINTS`` points by mean-bucketing,
so a 40-minute job costs the same handful of KB as a 40-second one — the
CI timing report embeds every profile inline in a single HTML file.

Caveat: /proc/stat, /proc/meminfo, and /proc/diskstats are NODE-wide.
Inside a Kubernetes pod these numbers include neighbor pods sharing the
node — treat them as indicative, not exact per-job attribution.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone

_SAMPLE_INTERVAL_S = 1.0
_CLK_TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PROC_STAT = "/proc/stat"
_PROC_MEMINFO = "/proc/meminfo"
_PROC_DISKSTATS = "/proc/diskstats"

# Upper bound on points in the emitted ``series``. The CI timing report
# inlines every profile into one self-contained HTML file, so an
# unbounded 1Hz series from a 20-minute job would dominate its size.
# 180 points is ~1 point per 7s on a 20-minute job — finer than the
# overlay strip can resolve on screen.
_MAX_SERIES_POINTS = 180


def _read_cpu_usage(prev: dict | None) -> tuple[float, dict]:
    """Return (usage_pct since prev sample, current_jiffies_dict).

    Reads /proc/stat line 1 (aggregate CPU). usage_pct = non-idle / total.
    """
    try:
        with open(_PROC_STAT, encoding="ascii") as f:
            first_line = f.readline()
    except OSError:
        return (0.0, prev or {})

    parts = first_line.split()
    if len(parts) < 5:
        return (0.0, prev or {})

    # user, nice, system, idle, iowait, irq, softirq, steal, ...
    vals = [int(x) for x in parts[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    total = sum(vals)
    cur = {"total": total, "idle": idle}

    if prev and cur["total"] != prev["total"]:
        d_total = cur["total"] - prev["total"]
        d_idle = cur["idle"] - prev["idle"]
        if d_total > 0:
            return (max(0.0, (1.0 - d_idle / d_total) * 100.0), cur)

    return (0.0, cur)


def _read_mem_mb() -> float:
    """Return *used* memory in MB (MemTotal - MemAvailable) from /proc/meminfo.

    Falls back to MemTotal - MemFree on old kernels without MemAvailable.
    Note: in a container this is the host/node view, not the cgroup view —
    numbers can include neighbor pods on shared nodes.
    """
    try:
        with open(_PROC_MEMINFO, encoding="ascii") as f:
            text = f.read()
    except OSError:
        return 0.0

    total_kb = 0
    available_kb = -1
    free_kb = -1
    for line in text.splitlines():
        if line.startswith("MemTotal:"):
            total_kb = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            available_kb = int(line.split()[1])
        elif line.startswith("MemFree:"):
            free_kb = int(line.split()[1])

    if total_kb <= 0:
        return 0.0
    unused_kb = available_kb if available_kb >= 0 else max(free_kb, 0)
    return max(0, total_kb - unused_kb) / 1024.0


def _read_diskstats() -> dict[str, tuple[int, int, int]]:
    """Return {device: (io_ops_completed, sectors_read_plus_written, busy_ms)}.

    We track whole block devices only (skip partitions — track sda not
    sda1, nvme0n1 not nvme0n1p1) so IO isn't double-counted. Each
    diskstats line:
    major minor name reads_completed reads_merged sectors_read time_reading
    writes_completed writes_merged sectors_written time_writing ios_in_flight
    io_ticks ...

    ``io_ticks`` (field 13, index 12) is milliseconds during which the
    device had IO in flight. Its delta over a sample interval is the
    device busy time, i.e. iostat's ``%util`` — the saturation signal
    ops/s alone cannot give you (a device can be pinned at 100% busy on
    few large IOs, or barely busy on many small cached ones).
    """
    try:
        with open(_PROC_DISKSTATS, encoding="ascii") as f:
            lines = f.readlines()
    except OSError:
        return {}

    result = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 14:
            continue
        name = parts[2]
        # Skip virtual/removable devices
        if name.startswith(("loop", "ram", "sr")):
            continue
        # Skip partitions: sda1, vda2, mmcblk0p1, nvme0n1p1. For devices
        # whose base name ends in a digit (nvme0n1, mmcblk0), partitions
        # carry a 'p<N>' suffix; for sdX/vdX a bare trailing digit.
        if name.startswith(("nvme", "mmcblk")):
            if re.search(r"p\d+$", name):
                continue
        elif name[-1].isdigit():
            continue
        reads_completed = int(parts[3])
        writes_completed = int(parts[7])
        sectors = int(parts[5]) + int(parts[9])
        busy_ms = int(parts[12])
        result[name] = (reads_completed + writes_completed, sectors, busy_ms)

    return result


def _downsample(values: list[float], max_points: int = _MAX_SERIES_POINTS) -> list[int]:
    """Bucket ``values`` down to at most ``max_points``, as rounded 0-100 ints.

    Uses the bucket MEAN rather than picking every Nth sample: a spike
    that survives decimation by luck is misleading, whereas a mean keeps
    the area under the curve honest for an overlay whose whole job is to
    show "was this saturated". Values are clamped to 0-100 because the
    consumer draws them as a percentage-height sparkline.
    """
    if not values:
        return []
    n = len(values)
    if n <= max_points:
        return [int(round(min(100.0, max(0.0, v)))) for v in values]

    out: list[int] = []
    for i in range(max_points):
        lo = i * n // max_points
        hi = (i + 1) * n // max_points
        if hi <= lo:
            hi = lo + 1
        bucket = values[lo:hi]
        avg = sum(bucket) / len(bucket)
        out.append(int(round(min(100.0, max(0.0, avg)))))
    return out


def _read_mem_total_mb() -> float:
    """Return MemTotal in MB (0.0 if unreadable)."""
    try:
        with open(_PROC_MEMINFO, encoding="ascii") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0


def run_profiler(output_path: str, label: str, timeout_s: float = 0) -> None:
    """Sample resources until SIGTERM or timeout, then write JSON summary."""
    cpu_samples: list[float] = []
    mem_samples: list[float] = []
    mem_pct_samples: list[float] = []
    disk_prev = _read_diskstats()
    disk_total_sectors = 0
    disk_ops_samples: list[float] = []
    disk_util_samples: list[float] = []

    mem_total_mb = _read_mem_total_mb()

    cpu_prev: dict | None = None
    start = time.monotonic()
    # Wall-clock anchor for the sample window. The report needs to know WHEN
    # these samples happened, not just how long they lasted: the profiler
    # wraps one step, so on a job whose other steps (checkout, setup, post)
    # dominate, the profiled window is a slice in the middle of the bar.
    # Without this the overlay gets stretched across the whole job and the
    # x-axis lies. monotonic() drives the sampling (immune to clock steps);
    # this is only for placement.
    started_at = datetime.now(timezone.utc)
    last_sample = start
    running = [True]  # mutable for signal handler

    def _stop(*_):
        running[0] = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running[0]:
        cpu_pct, cpu_prev = _read_cpu_usage(cpu_prev)
        cpu_samples.append(cpu_pct)

        mem_mb = _read_mem_mb()
        mem_samples.append(mem_mb)
        mem_pct_samples.append((mem_mb / mem_total_mb * 100.0) if mem_total_mb > 0 else 0.0)

        # Wall time actually elapsed since the previous sample. Under a
        # loaded/throttled runner the loop can drift well past the
        # nominal interval, and dividing a busy-ms delta by the nominal
        # 1.0s would then report >100% util. Measure the real gap.
        now = time.monotonic()
        gap_s = max(now - last_sample, 1e-6)
        last_sample = now

        disk_cur = _read_diskstats()
        delta_sectors = 0
        delta_ops = 0
        delta_busy_ms = 0
        for dev, (ops, sectors, busy_ms) in disk_cur.items():
            prev_ops, prev_sectors, prev_busy = disk_prev.get(dev, (ops, sectors, busy_ms))
            delta_sectors += max(0, sectors - prev_sectors)
            delta_ops += max(0, ops - prev_ops)
            # Busiest single device, not the sum: summing across devices
            # exceeds 100% on a multi-disk node and is meaningless as a
            # saturation percentage.
            delta_busy_ms = max(delta_busy_ms, max(0, busy_ms - prev_busy))
        disk_total_sectors += delta_sectors
        disk_ops_samples.append(delta_ops / _SAMPLE_INTERVAL_S)
        disk_util_samples.append(min(100.0, delta_busy_ms / (gap_s * 1000.0) * 100.0))
        disk_prev = disk_cur

        elapsed = time.monotonic() - start
        if timeout_s > 0 and elapsed >= timeout_s:
            break

        time.sleep(_SAMPLE_INTERVAL_S)

    duration_s = time.monotonic() - start
    n = len(cpu_samples) or 1

    # Sectors are 512 bytes
    disk_read_written_mb = disk_total_sectors * 512 / (1024 * 1024)

    cpu_series = _downsample(cpu_samples)
    mem_series = _downsample(mem_pct_samples)
    disk_series = _downsample(disk_util_samples)
    n_points = max(len(cpu_series), len(mem_series), len(disk_series))

    summary = {
        "label": label,
        "duration_s": round(duration_s, 1),
        # ISO-8601 UTC bounds of the sample window, in the same format as
        # GitHub's job/step timestamps so the report can place the series
        # against a job bar's x-axis instead of stretching it to fill.
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "completed_at": (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
        "cpu": {
            "avg_usage_pct": round(sum(cpu_samples) / n, 1),
            "peak_usage_pct": round(max(cpu_samples, default=0.0), 1),
            "samples": len(cpu_samples),
        },
        "memory": {
            "avg_mb": round(sum(mem_samples) / n, 1),
            "peak_mb": round(max(mem_samples, default=0.0), 1),
            "total_mb": round(mem_total_mb, 1),
            "samples": len(mem_samples),
        },
        "disk": {
            "total_mb": round(disk_read_written_mb, 1),
            "avg_ops_per_s": round(sum(disk_ops_samples) / n, 1),
            "peak_ops_per_s": round(max(disk_ops_samples, default=0.0), 1),
            "avg_util_pct": round(sum(disk_util_samples) / n, 1),
            "peak_util_pct": round(max(disk_util_samples, default=0.0), 1),
            "samples": len(disk_ops_samples),
        },
        # Downsampled timeline. The timing report overlays this on each
        # job's gantt bar; consumers must read interval_s rather than
        # assuming 1Hz, since long jobs are bucketed.
        "series": {
            "interval_s": round(duration_s / n_points, 3) if n_points else 0,
            "points": n_points,
            "cpu_pct": cpu_series,
            "mem_pct": mem_series,
            "disk_pct": disk_series,
        },
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"resource_profile: wrote {output_path} ({n} samples, {duration_s:.1f}s)", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="CI resource profiler")
    parser.add_argument("--output", required=True, help="Output JSON path")
    parser.add_argument("--label", default="", help="Label for this profile")
    parser.add_argument("--timeout", type=float, default=0,
                        help="Max seconds to run (0 = until SIGTERM)")
    args = parser.parse_args()
    run_profiler(args.output, args.label, args.timeout)


if __name__ == "__main__":
    main()
