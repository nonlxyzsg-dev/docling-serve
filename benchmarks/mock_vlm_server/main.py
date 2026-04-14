"""Mock VLM server — OpenAI-compatible chat completions endpoint.

Purpose: reproduce the "waves" behaviour caused by docling's internal barriers
without needing a real SGLang backend. Every incoming request is logged with
its start/end timestamps; the timeline is exposed via GET /timeline so that
run.py can compute peak/mean inflight and wave count.

Environment variables:
    MOCK_VLM_LATENCY_MEAN_S    default 5.0
    MOCK_VLM_LATENCY_STDDEV_S  default 2.0
    MOCK_VLM_LATENCY_MIN_S     default 0.5
    MOCK_VLM_TIMELINE_PATH     default /data/mock_vlm_timeline.jsonl
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

LATENCY_MEAN = float(os.environ.get("MOCK_VLM_LATENCY_MEAN_S", "5.0"))
LATENCY_STDDEV = float(os.environ.get("MOCK_VLM_LATENCY_STDDEV_S", "2.0"))
LATENCY_MIN = float(os.environ.get("MOCK_VLM_LATENCY_MIN_S", "0.5"))
TIMELINE_PATH = Path(os.environ.get("MOCK_VLM_TIMELINE_PATH", "/data/mock_vlm_timeline.jsonl"))

TIMELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
# Truncate on startup so each benchmark run has a clean timeline.
TIMELINE_PATH.write_text("")

_write_lock = asyncio.Lock()
app = FastAPI(title="mock-vlm")


async def _append_timeline(entry: dict[str, Any]) -> None:
    async with _write_lock:
        with TIMELINE_PATH.open("a") as f:
            f.write(json.dumps(entry, separators=(",", ":")))
            f.write("\n")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    ts_start = time.time()

    # Drain body for realism; we don't inspect it.
    try:
        _ = await request.json()
    except Exception:
        pass

    latency = max(LATENCY_MIN, random.gauss(LATENCY_MEAN, LATENCY_STDDEV))
    await asyncio.sleep(latency)

    ts_end = time.time()
    await _append_timeline(
        {
            "request_id": request_id,
            "ts_start": ts_start,
            "ts_end": ts_end,
            "duration_s": ts_end - ts_start,
        }
    )

    return {
        "id": f"chatcmpl-{request_id}",
        "object": "chat.completion",
        "created": int(ts_start),
        "model": "mock-vlm",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "<MOCK IMAGE DESCRIPTION>",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 4,
            "total_tokens": 4,
        },
    }


@app.get("/timeline")
async def timeline(since: float | None = None, until: float | None = None) -> dict[str, Any]:
    """Return timeline entries filtered by time window.

    Both bounds are optional Unix timestamps. An entry is included if its
    ts_start falls within [since, until]. Missing bounds = open-ended.
    """
    entries: list[dict[str, Any]] = []
    if TIMELINE_PATH.exists():
        with TIMELINE_PATH.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since is not None and entry["ts_start"] < since:
                    continue
                if until is not None and entry["ts_start"] > until:
                    continue
                entries.append(entry)
    return {"entries": entries, "count": len(entries)}


@app.post("/timeline/reset")
async def timeline_reset() -> dict[str, str]:
    async with _write_lock:
        TIMELINE_PATH.write_text("")
    return {"status": "reset"}
