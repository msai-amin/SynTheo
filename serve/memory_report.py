#!/usr/bin/env python3
"""Prove the resident-trio profile leaves >= config.memory_budget.min_headroom_gb free.

Reads live GPU/unified-memory usage via nvidia-smi and compares against the budget
in config/syntheo.yaml. Run this with all three model containers up.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with open(REPO_ROOT / "config" / "syntheo.yaml") as f:
        return yaml.safe_load(f)


def gpu_memory_used_total_mib() -> tuple[int, int]:
    out = subprocess.check_output(
        [
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        text=True,
    ).strip()
    used_s, total_s = out.splitlines()[0].split(",")
    return int(used_s.strip()), int(total_s.strip())


def main() -> None:
    cfg = load_config()
    min_headroom_gb = cfg["memory_budget"]["min_headroom_gb"]
    total_unified_gb = cfg["memory_budget"]["total_unified_gb"]

    try:
        used_mib, total_mib = gpu_memory_used_total_mib()
        used_gb = used_mib / 1024
        total_gb = total_mib / 1024 if total_mib > 0 else total_unified_gb
    except (subprocess.CalledProcessError, ValueError):
        # GB10 unified memory sometimes reports "Not Supported" via query-gpu;
        # fall back to /proc/meminfo for the unified pool.
        meminfo = Path("/proc/meminfo").read_text()
        mem_total_kb = int(
            next(ln for ln in meminfo.splitlines() if ln.startswith("MemTotal:")).split()[1]
        )
        mem_avail_kb = int(
            next(ln for ln in meminfo.splitlines() if ln.startswith("MemAvailable:")).split()[1]
        )
        total_gb = mem_total_kb / 1024 / 1024
        used_gb = total_gb - mem_avail_kb / 1024 / 1024

    headroom_gb = total_gb - used_gb
    print(f"total:    {total_gb:.1f} GB")
    print(f"used:     {used_gb:.1f} GB")
    print(f"headroom: {headroom_gb:.1f} GB (required >= {min_headroom_gb} GB)")

    if headroom_gb < min_headroom_gb:
        print("FAIL: insufficient headroom", file=sys.stderr)
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
