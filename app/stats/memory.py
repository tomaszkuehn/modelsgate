"""Process memory statistics using psutil."""

import gc
import os
from typing import Optional
from datetime import datetime

import psutil


def _get_process() -> Optional[psutil.Process]:
    try:
        return psutil.Process(os.getpid())
    except Exception:
        return None


def memory_stats() -> dict:
    """Return memory usage stats for the current process and system."""
    proc = _get_process()
    stats: dict = {
        "timestamp": datetime.utcnow().isoformat(),
    }

    if proc is None:
        stats["error"] = "Could not access process info"
        return stats

    try:
        mem = proc.memory_info()
        stats["process"] = {
            "rss_mb": round(mem.rss / (1024 * 1024), 2),          # physical
            "vms_mb": round(mem.vms / (1024 * 1024), 2),          # virtual
            "private_mb": round(getattr(mem, "private", mem.rss) / (1024 * 1024), 2),
            "peak_rss_mb": round(getattr(mem, "peak_wset", 0) / (1024 * 1024), 2) if hasattr(mem, "peak_wset") else None,
        }
    except Exception:
        stats["process"] = {"error": "Could not read process memory"}

    try:
        vmem = psutil.virtual_memory()
        stats["system"] = {
            "total_gb": round(vmem.total / (1024**3), 1),
            "available_gb": round(vmem.available / (1024**3), 1),
            "used_percent": vmem.percent,
        }
    except Exception:
        stats["system"] = {"error": "Could not read system memory"}

    # Python object counts (top types)
    try:
        gc.collect()  # force collection for accurate count
        counts: dict = {}
        for obj in gc.get_objects():
            t = type(obj).__name__
            counts[t] = counts.get(t, 0) + 1
        top = sorted(counts.items(), key=lambda x: -x[1])[:15]
        stats["python_objects"] = dict(top)
    except Exception:
        stats["python_objects"] = {"error": "Could not count objects"}

    return stats


def memory_summary() -> str:
    """Return a one-line memory usage summary."""
    proc = _get_process()
    if proc is None:
        return "memory: unknown"
    try:
        mem = proc.memory_info()
        rss = mem.rss / (1024 * 1024)
        return f"{rss:.0f} MB RSS"
    except Exception:
        return "memory: unknown"
