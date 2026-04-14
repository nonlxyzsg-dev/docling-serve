#!/usr/bin/env python3
"""Compare two benchmark result JSON files.

Example:
    python compare.py results/baseline.json results/after_task_03.json

Exit codes:
    0 — all documents unchanged or improved within tolerance
    1 — at least one document regressed beyond tolerance
    2 — input error (missing file, bad JSON, no matching documents)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


REGRESSION_WALL_TOLERANCE = 0.05  # ±5 % wall time regression is tolerated
MD_FUZZY_MIN_RATIO = 0.95  # below this, markdown is considered drifted


def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        sys.stderr.write(f"error: not a file: {path}\n")
        sys.exit(2)
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"error: bad json in {path}: {exc}\n")
        sys.exit(2)


def fmt_delta_pct(base: float, cur: float) -> str:
    if base == 0:
        return "n/a"
    pct = (cur - base) / base * 100.0
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def fmt_seconds(v: Any) -> str:
    if v is None:
        return "-"
    return f"{float(v):.1f}s"


def fmt_int(v: Any) -> str:
    return "-" if v is None else str(int(v))


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> int:
    base_docs = {d["name"]: d for d in baseline.get("documents", [])}
    cur_docs = {d["name"]: d for d in current.get("documents", [])}

    all_names = sorted(set(base_docs) | set(cur_docs))
    if not all_names:
        sys.stderr.write("error: no documents in either file\n")
        return 2

    regressions: list[str] = []

    print(f"# benchmark comparison: {baseline.get('label', '?')} vs {current.get('label', '?')}")
    print()
    print("## wall time")
    print()
    print("| Document | baseline | current | Δ | md match |")
    print("| --- | --- | --- | --- | --- |")
    for name in all_names:
        b = base_docs.get(name)
        c = cur_docs.get(name)

        if b is None:
            print(f"| {name} | — | {fmt_seconds(c.get('wall_time_s'))} | new | — |")
            continue
        if c is None:
            print(f"| {name} | {fmt_seconds(b.get('wall_time_s'))} | — | missing | — |")
            regressions.append(f"{name}: missing in current run")
            continue

        b_wall = float(b.get("wall_time_s") or 0)
        c_wall = float(c.get("wall_time_s") or 0)
        delta = fmt_delta_pct(b_wall, c_wall)

        b_hash = b.get("md_sha256") or ""
        c_hash = c.get("md_sha256") or ""
        if b_hash and c_hash and b_hash == c_hash:
            md_marker = "exact"
        elif b_hash and c_hash:
            # Hashes differ; fuzzy ratio is only meaningful if we still have
            # plain text, but we only stored the hash. Use size delta as a
            # coarse signal instead.
            b_size = int(b.get("md_size_bytes") or 0)
            c_size = int(c.get("md_size_bytes") or 0)
            if b_size == 0:
                md_marker = "drift"
            else:
                ratio = 1.0 - abs(b_size - c_size) / max(b_size, 1)
                md_marker = f"size≈{ratio:.2f}"
                if ratio < MD_FUZZY_MIN_RATIO:
                    regressions.append(f"{name}: markdown size drifted (ratio {ratio:.2f})")
        else:
            md_marker = "—"

        if b.get("errors") or c.get("errors"):
            md_marker += " (errors)"

        print(f"| {name} | {b_wall:.1f}s | {c_wall:.1f}s | {delta} | {md_marker} |")

        # Regression check: if wall time grew by more than tolerance, flag it.
        if b_wall > 0 and c_wall > b_wall * (1 + REGRESSION_WALL_TOLERANCE):
            regressions.append(
                f"{name}: wall time regressed {delta} ({b_wall:.1f}s → {c_wall:.1f}s)"
            )

    print()
    print("## VLM parallelism (mock-vlm timeline)")
    print()
    print("| Document | peak_inflight (base → cur) | mean_inflight | waves |")
    print("| --- | --- | --- | --- |")
    for name in all_names:
        b = base_docs.get(name) or {}
        c = cur_docs.get(name) or {}
        bp = fmt_int(b.get("vlm_peak_inflight"))
        cp = fmt_int(c.get("vlm_peak_inflight"))
        bm = "-" if b.get("vlm_mean_inflight") is None else f"{float(b['vlm_mean_inflight']):.2f}"
        cm = "-" if c.get("vlm_mean_inflight") is None else f"{float(c['vlm_mean_inflight']):.2f}"
        bw = fmt_int(b.get("vlm_waves"))
        cw = fmt_int(c.get("vlm_waves"))
        print(f"| {name} | {bp} → {cp} | {bm} → {cm} | {bw} → {cw} |")

    print()
    if regressions:
        print("## regressions")
        for r in regressions:
            print(f"- {r}")
        return 1

    print("## no regressions detected")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two benchmark results")
    parser.add_argument("baseline", help="results JSON for the baseline run")
    parser.add_argument("current", help="results JSON for the current run")
    args = parser.parse_args()

    baseline = load(Path(args.baseline))
    current = load(Path(args.current))
    return compare(baseline, current)


if __name__ == "__main__":
    sys.exit(main())
