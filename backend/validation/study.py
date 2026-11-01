"""Shared setup of the stage-6 study (segments + base descriptor) for scripts and the AI loop."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from backend.backtesting.position_sim import Dataset
from backend.strategies.mass_test import dataset_descriptor
from backend.validation.segments import Segment, make_segments

DEFAULT_FROM, DEFAULT_TO = "2021-08-06", "2026-11-01"


def ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


@dataclass
class Study:
    segments: list[Segment]
    base: dict


def setup_study(cfg, ds: Dataset, start: str = DEFAULT_FROM, end: str = DEFAULT_TO,
                segment_days: int = 91, oos: int = 7, capital: float = 10_000.0) -> Study:
    p = cfg.pools[cfg.primary_pool]
    segs = make_segments(ds, ts(start), ts(end), segment_days, oos)
    base = dataset_descriptor(ds, cfg.primary_pool, p.address, segs[0].start_block, segs[-1].end_block,
                              int(capital * 10 ** ds.decimals1), "gas_units",
                              cfg.lab["costs"]["priority_fee_gwei"])
    base.pop("from_block"), base.pop("to_block")
    base["segment_days"] = segment_days
    return Study(segs, base)
