#!/usr/bin/env python3
"""Render a VLM inflight-over-time plot from mock_vlm_timeline.jsonl.

Without matplotlib, prints a simple ASCII histogram. With matplotlib
installed (`pip install matplotlib`), writes a PNG.

Example:
    python plot_timeline.py \\
        --timeline profiles/baseline_porazhay/mock_vlm_timeline.jsonl \\
        --output profiles/baseline_porazhay/timeline.png
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_entries(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def sample_inflight(entries: list[dict[str, Any]], step: float = 0.5) -> tuple[list[float], list[int]]:
    if not entries:
        return [], []
    t0 = min(e["ts_start"] for e in entries)
    t1 = max(e["ts_end"] for e in entries)
    n = max(1, int((t1 - t0) / step) + 1)
    times = [i * step for i in range(n)]
    counts = [0] * n
    for e in entries:
        i0 = max(0, int((e["ts_start"] - t0) / step))
        i1 = min(n - 1, int((e["ts_end"] - t0) / step))
        for i in range(i0, i1 + 1):
            counts[i] += 1
    return times, counts


def ascii_plot(times: list[float], counts: list[int], width: int = 80) -> str:
    if not counts:
        return "(empty timeline)"
    peak = max(counts)
    # Downsample to `width` columns.
    if len(counts) > width:
        bucket = len(counts) / width
        bucketed = [
            max(counts[int(i * bucket) : int((i + 1) * bucket)] or [0])
            for i in range(width)
        ]
    else:
        bucketed = counts
    rows = []
    rows.append(f"peak={peak}  samples={len(counts)}  width={len(bucketed)}")
    for level in range(peak, 0, -1):
        line = "".join("#" if c >= level else " " for c in bucketed)
        rows.append(f"{level:3d} | {line}")
    rows.append("    +" + "-" * len(bucketed))
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot VLM inflight timeline")
    parser.add_argument("--timeline", required=True, help="path to mock_vlm_timeline.jsonl")
    parser.add_argument("--output", default=None, help="path to output PNG (requires matplotlib)")
    parser.add_argument("--step", type=float, default=0.5, help="sampling interval in seconds")
    args = parser.parse_args()

    entries = load_entries(Path(args.timeline))
    times, counts = sample_inflight(entries, step=args.step)

    print(ascii_plot(times, counts))

    if args.output:
        try:
            import matplotlib.pyplot as plt  # type: ignore
        except ImportError:
            sys.stderr.write("warning: matplotlib not installed, PNG not written\n")
            return 0
        fig, ax = plt.subplots(figsize=(12, 4))
        ax.plot(times, counts, drawstyle="steps-post")
        ax.set_xlabel("time (s)")
        ax.set_ylabel("inflight requests")
        ax.set_title(f"mock-vlm inflight  ({len(entries)} total)")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(args.output, dpi=100)
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
