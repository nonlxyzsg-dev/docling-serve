#!/usr/bin/env python3
"""Run docling-serve benchmarks against a set of fixtures.

This script drives a local docling-serve + mock-vlm stack (see
docker-compose.yml) and records wall-clock + VLM timeline metrics for
each input document. Output is a single JSON file suitable for
comparison via compare.py.

Example:
    python run.py \\
        --endpoint http://localhost:5001/v1/convert/file \\
        --mock-vlm-timeline http://localhost:4000/timeline \\
        --mock-vlm-reset http://localhost:4000/timeline/reset \\
        --config configs/prod_like.json \\
        --fixtures fixtures/ \\
        --output results/baseline.json \\
        --label baseline
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:
    sys.stderr.write(
        "error: 'requests' is required. Install with: pip install requests\n"
    )
    sys.exit(1)


# ---------- metrics ----------


def sha256(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def compute_vlm_metrics(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Given a list of mock-vlm timeline entries, compute inflight metrics.

    An entry = {"ts_start", "ts_end", "duration_s", "request_id"}. The
    timeline is sampled at 100 ms granularity to count concurrent
    in-flight requests. A "wave" is an activity segment ended by at
    least 1 s of zero inflight.
    """
    if not entries:
        return {
            "vlm_requests": 0,
            "vlm_peak_inflight": 0,
            "vlm_mean_inflight": 0.0,
            "vlm_waves": 0,
            "vlm_total_active_s": 0.0,
            "vlm_total_idle_s": 0.0,
        }

    t_start = min(e["ts_start"] for e in entries)
    t_end = max(e["ts_end"] for e in entries)
    if t_end <= t_start:
        return {
            "vlm_requests": len(entries),
            "vlm_peak_inflight": 1,
            "vlm_mean_inflight": 1.0,
            "vlm_waves": 1,
            "vlm_total_active_s": 0.0,
            "vlm_total_idle_s": 0.0,
        }

    step = 0.1  # 100 ms
    n_steps = max(1, int((t_end - t_start) / step) + 1)
    samples = [0] * n_steps
    for e in entries:
        i0 = max(0, int((e["ts_start"] - t_start) / step))
        i1 = min(n_steps - 1, int((e["ts_end"] - t_start) / step))
        for i in range(i0, i1 + 1):
            samples[i] += 1

    peak = max(samples)
    mean = sum(samples) / len(samples)
    active = sum(1 for s in samples if s > 0) * step
    idle = sum(1 for s in samples if s == 0) * step

    # Wave count: number of non-zero runs separated by ≥ 1 s of zero.
    waves = 0
    in_wave = False
    zero_run = 0
    zero_threshold_steps = int(1.0 / step)
    for s in samples:
        if s > 0:
            if not in_wave:
                waves += 1
                in_wave = True
            zero_run = 0
        else:
            zero_run += 1
            if in_wave and zero_run >= zero_threshold_steps:
                in_wave = False

    return {
        "vlm_requests": len(entries),
        "vlm_peak_inflight": peak,
        "vlm_mean_inflight": round(mean, 3),
        "vlm_waves": waves,
        "vlm_total_active_s": round(active, 2),
        "vlm_total_idle_s": round(idle, 2),
    }


# ---------- request construction ----------


def build_form(
    file_path: Path, request_form: dict[str, Any], picture_api_cfg: dict[str, Any]
) -> tuple[list[tuple[str, Any]], dict[str, Any]]:
    """Construct the multipart form payload for /v1/convert/file."""
    data: list[tuple[str, Any]] = [(k, str(v)) for k, v in request_form.items()]
    data.append(("picture_description_api", json.dumps(picture_api_cfg)))

    files = {
        "files": (file_path.name, file_path.read_bytes(), "application/octet-stream"),
    }
    return data, files


# ---------- timeline fetch ----------


def reset_timeline(url: str | None) -> None:
    if not url:
        return
    try:
        requests.post(url, timeout=10)
    except requests.RequestException as exc:
        print(f"warn: failed to reset mock-vlm timeline: {exc}", file=sys.stderr)


def fetch_timeline(url: str, since: float, until: float) -> list[dict[str, Any]]:
    resp = requests.get(url, params={"since": since, "until": until}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("entries", [])


# ---------- document run ----------


def run_document(
    file_path: Path,
    doc_group: str,
    endpoint: str,
    request_form: dict[str, Any],
    picture_api_cfg: dict[str, Any],
    mock_vlm_timeline_url: str | None,
    timeout_s: int,
) -> dict[str, Any]:
    print(f"  [{doc_group}] {file_path.name} ... ", end="", flush=True)
    data, files = build_form(file_path, request_form, picture_api_cfg)

    t_start = time.time()
    perf_start = time.perf_counter()
    try:
        resp = requests.post(endpoint, data=data, files=files, timeout=timeout_s)
        wall_time = time.perf_counter() - perf_start
        t_end = time.time()
    except requests.RequestException as exc:
        wall_time = time.perf_counter() - perf_start
        t_end = time.time()
        print(f"ERROR after {wall_time:.1f}s: {exc}")
        return {
            "name": file_path.name,
            "group": doc_group,
            "wall_time_s": round(wall_time, 2),
            "md_size_bytes": 0,
            "md_sha256": "",
            "errors": [str(exc)],
        }

    md_bytes = b""
    md_text = ""
    errors: list[str] = []

    if resp.status_code != 200:
        errors.append(f"HTTP {resp.status_code}: {resp.text[:500]}")
    else:
        try:
            payload = resp.json()
            # docling-serve returns {"document": {"md_content": "..."} , ...}
            document = payload.get("document") or payload
            md_text = document.get("md_content") or document.get("text_content") or ""
            md_bytes = md_text.encode("utf-8")
        except json.JSONDecodeError as exc:
            errors.append(f"json decode: {exc}")
            md_bytes = resp.content
            md_text = resp.text

    vlm_metrics: dict[str, Any] = {}
    if mock_vlm_timeline_url:
        try:
            entries = fetch_timeline(mock_vlm_timeline_url, t_start, t_end + 1.0)
            vlm_metrics = compute_vlm_metrics(entries)
        except requests.RequestException as exc:
            errors.append(f"timeline fetch: {exc}")

    result: dict[str, Any] = {
        "name": file_path.name,
        "group": doc_group,
        "wall_time_s": round(wall_time, 2),
        "md_size_bytes": len(md_bytes),
        "md_sha256": sha256(md_bytes) if md_bytes else "",
        "errors": errors,
    }
    result.update(vlm_metrics)

    status = "OK" if not errors else "ERR"
    print(
        f"{status} wall={wall_time:.1f}s md={len(md_bytes)}B "
        f"vlm_req={vlm_metrics.get('vlm_requests', 0)} "
        f"peak={vlm_metrics.get('vlm_peak_inflight', 0)} "
        f"waves={vlm_metrics.get('vlm_waves', 0)}"
    )
    return result


# ---------- entry ----------


def main() -> int:
    parser = argparse.ArgumentParser(description="docling-serve benchmark runner")
    parser.add_argument("--endpoint", required=True, help="e.g. http://localhost:5001/v1/convert/file")
    parser.add_argument("--config", required=True, help="path to config JSON (e.g. configs/prod_like.json)")
    parser.add_argument("--fixtures", required=True, help="path to fixtures/ directory")
    parser.add_argument("--output", required=True, help="path to write results JSON")
    parser.add_argument("--label", default="unlabelled", help="label to include in results metadata")
    parser.add_argument("--mock-vlm-timeline", default=None, help="URL of mock-vlm /timeline (optional)")
    parser.add_argument("--mock-vlm-reset", default=None, help="URL of mock-vlm /timeline/reset (optional)")
    parser.add_argument("--only", default=None, help="only run a single fixtures subdirectory")
    parser.add_argument("--timeout", type=int, default=1800, help="per-request timeout in seconds")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = json.loads(config_path.read_text())
    request_form = config["request_form"]
    picture_api_cfg = config["picture_description_api"]

    fixtures_root = Path(args.fixtures)
    if not fixtures_root.is_dir():
        sys.stderr.write(f"error: fixtures path not found: {fixtures_root}\n")
        return 2

    groups = sorted(
        d for d in fixtures_root.iterdir() if d.is_dir() and not d.name.startswith(".")
    )
    if args.only:
        groups = [g for g in groups if g.name == args.only]
        if not groups:
            sys.stderr.write(f"error: no fixture subdir named {args.only!r}\n")
            return 2

    print(f"benchmarks: label={args.label} endpoint={args.endpoint}")
    print(f"groups: {', '.join(g.name for g in groups)}")

    reset_timeline(args.mock_vlm_reset)

    results: list[dict[str, Any]] = []
    for group_dir in groups:
        doc_files = sorted(
            p
            for p in group_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".pdf", ".docx", ".doc", ".html", ".pptx", ".xlsx"}
        )
        if not doc_files:
            print(f"  [{group_dir.name}] (empty, skipped)")
            continue
        for doc_path in doc_files:
            results.append(
                run_document(
                    doc_path,
                    group_dir.name,
                    args.endpoint,
                    request_form,
                    picture_api_cfg,
                    args.mock_vlm_timeline,
                    args.timeout,
                )
            )

    summary = {
        "label": args.label,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "config": {
            "docling_version": config.get("docling_version"),
            "docling_serve_version": config.get("docling_serve_version"),
            "docling_serve_env": config.get("docling_serve_env", {}),
            "picture_description_api": {
                "concurrency": picture_api_cfg.get("concurrency"),
                "url": picture_api_cfg.get("url"),
            },
        },
        "documents": results,
    }

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"\nwrote {output_path} ({len(results)} documents)")
    errors_total = sum(1 for r in results if r.get("errors"))
    if errors_total:
        print(f"errors: {errors_total}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
