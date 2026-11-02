"""Is the stored historical study still valid? (the scheduler re-runs heavy research only then)

The study (every strategy x every 91-day segment since 2021-08-06) is the foundation of the
system and is never recomputed on a timer. It is outdated only when
  * the result-code version changed (math / backtester / data code: stored rows no longer
    match the code — only then are they recomputed), or
  * a new full 91-day segment exists after the study end (new data to validate on;
    old segments are reused, only the new one is computed).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEGMENT_DAYS = 91


def latest_study() -> dict | None:
    files = sorted((ROOT / "data_snapshots" / "validation").glob("validation_*.json"),
                   key=lambda p: p.stat().st_mtime)
    return json.loads(files[-1].read_text()) if files else None


def study_outdated(now: float | None = None) -> str | None:
    """None if the stored study is current, else the reason it must be extended."""
    from backend.strategies.mass_test import code_version
    doc = latest_study()
    if doc is None:
        return "no stored study"
    if doc["code_version"] != code_version():
        return f"result code changed ({doc['code_version']} -> {code_version()})"
    end = max(s["end_ts"] for s in doc["segments"])
    now = now or time.time()
    if now - end >= SEGMENT_DAYS * 86_400:
        day = datetime.fromtimestamp(end + SEGMENT_DAYS * 86_400, timezone.utc).date()
        return f"a new {SEGMENT_DAYS}-day segment is complete (since {day})"
    return None
