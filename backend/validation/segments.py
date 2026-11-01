"""Time segments of the history and the per-segment result table (stage 6).

Every strategy is re-opened at the start of every segment with the same
capital, so each (strategy, segment) result is an independent sample. IS/OOS,
walk-forward, regime slices and sensitivity are all computed from this table.
Segments are contiguous, non-overlapping, ordered: the last `oos_segments` are
out-of-sample and are only revealed to the validation step (ARCHITECTURE §4).
"""

from __future__ import annotations

import bisect
import json
import math
import time
from dataclasses import asdict, dataclass

from sqlalchemy import insert, select

from backend.backtesting.dataset import cached_state_at
from backend.backtesting.market_view import LN_TICK
from backend.backtesting.position_sim import LONDON_BLOCK, Dataset, PositionResult, simulate_many
from backend.database import schema as S
from backend.strategies.generator import StrategyParams
from backend.strategies.mass_test import _hash, code_version

DAY = 86_400
# Sensitivity variants (SPEC §9): gas x2, trading volume x0.5 / x1.5 (fees scale with volume
# on the fixed price path; MODELED approximation).
VARIANTS = {"base": {}, "gas_x2": {"gas_multiplier": 2.0},
            "volume_x0.5": {"fee_multiplier": 0.5}, "volume_x1.5": {"fee_multiplier": 1.5}}


@dataclass(frozen=True)
class Segment:
    index: int
    start_block: int
    end_block: int
    start_ts: int
    end_ts: int
    role: str                 # IS | OOS
    price_change: float       # descriptive (whole segment), used for regime slices only
    realized_vol: float
    trend: str = ""           # up | down | flat
    vol_class: str = ""       # high | low (vs median of all segments)


def make_segments(ds: Dataset, start_ts: int, end_ts: int, days: int = 91,
                  oos_segments: int = 7) -> list[Segment]:
    blocks = sorted(b for b in ds.blocks if b >= LONDON_BLOCK)
    ts_of = [ds.blocks[b][0] for b in blocks]
    ev_blocks = [e.block for e in ds.events]
    raw = []
    t = start_ts
    while t + days * DAY <= end_ts:
        i = bisect.bisect_left(ts_of, t)
        j = bisect.bisect_left(ts_of, t + days * DAY) - 1
        if i >= len(blocks) or j <= i:
            break
        raw.append((blocks[i], blocks[j], ts_of[i], ts_of[j]))
        t += days * DAY
    ticks = _block_end_ticks(ds)
    segs = []
    for k, (b0, b1, t0, t1) in enumerate(raw):
        tk0, tk1 = _tick_at(ticks, ev_blocks, b0), _tick_at(ticks, ev_blocks, b1)
        change = math.exp((tk1 - tk0) * LN_TICK) - 1
        vol = _segment_vol(ds, ticks, b0, b1)
        role = "OOS" if k >= len(raw) - oos_segments else "IS"
        segs.append(Segment(k, b0, b1, t0, t1, role, change, vol))
    if not segs:
        return []
    med = sorted(s.realized_vol for s in segs)[len(segs) // 2]
    out = []
    for s in segs:
        trend = "up" if s.price_change > 0.10 else "down" if s.price_change < -0.10 else "flat"
        out.append(Segment(**{**asdict(s), "trend": trend,
                              "vol_class": "high" if s.realized_vol >= med else "low"}))
    return out


def _block_end_ticks(ds: Dataset) -> dict[int, int]:
    out: dict[int, int] = {}
    for e in ds.events:
        if e.name in ("Swap", "Initialize"):
            out[e.block] = e.f["tick"]
    return out


def _tick_at(ticks: dict[int, int], ev_blocks: list[int], block: int) -> int:
    keys = sorted(ticks)
    i = bisect.bisect_right(keys, block) - 1
    return ticks[keys[max(i, 0)]]


def _segment_vol(ds: Dataset, ticks: dict[int, int], b0: int, b1: int) -> float:
    samples, last_ts, last_tick = [], None, None
    for b in sorted(k for k in ticks if b0 <= k <= b1):
        ts = ds.blocks[b][0]
        if last_ts is None or ts >= last_ts + 3600:
            if last_tick is not None:
                samples.append((ticks[b] - last_tick) * LN_TICK)
            last_ts, last_tick = ts, ticks[b]
    if len(samples) < 2:
        return 0.0
    mu = sum(samples) / len(samples)
    var = sum((x - mu) ** 2 for x in samples) / (len(samples) - 1)
    return math.sqrt(var * 24 * 365)


def segment_descriptor(base: dict, seg: Segment, variant: str) -> dict:
    return {**base, "from_block": seg.start_block, "to_block": seg.end_block,
            "segment": seg.index, "role": seg.role, "variant": variant,
            **VARIANTS[variant]}


def run_segment_table(engine, ds: Dataset, params: list[StrategyParams], segments: list[Segment],
                      base_descriptor: dict, *, variants_oos: tuple[str, ...] = tuple(VARIANTS),
                      variants_is: tuple[str, ...] = ("base",), batch: int = 45,
                      progress=None) -> dict:
    """Compute (and store) every (strategy, segment, variant) result not done yet."""
    version = code_version()
    jobs = []  # (params, descriptor, dataset_hash)
    for seg in segments:
        for var in (variants_oos if seg.role == "OOS" else variants_is):
            d = segment_descriptor(base_descriptor, seg, var)
            jobs.append((seg, var, d, _hash(d)))
    with engine.begin() as conn:
        for seg, var, d, h in jobs:
            if conn.execute(select(S.datasets.c.dataset_hash).where(
                    S.datasets.c.dataset_hash == h)).first() is None:
                conn.execute(insert(S.datasets).values(
                    dataset_hash=h, pool=d["pool"], from_block=d["from_block"], to_block=d["to_block"],
                    description_json=json.dumps(d)))
        for p in params:
            if conn.execute(select(S.strategies.c.spec_hash).where(
                    S.strategies.c.spec_hash == p.spec_hash)).first() is None:
                conn.execute(insert(S.strategies).values(
                    spec_hash=p.spec_hash, name=p.name, params_json=json.dumps(asdict(p))))
        done = set(conn.execute(select(S.experiments.c.dedup_key).where(
            S.experiments.c.code_version == version, S.experiments.c.status == "done")).scalars())
    todo_by_param: dict[str, list] = {}
    for p in params:
        for seg, var, d, h in jobs:
            key = _hash([p.spec_hash, h, version])
            if key not in done:
                todo_by_param.setdefault(p.spec_hash, []).append((p, seg, var, d, h, key))
    stats = {"code_version": version, "jobs": len(params) * len(jobs),
             "skipped_done": len(params) * len(jobs) - sum(len(v) for v in todo_by_param.values()),
             "done": 0, "failed": 0}
    groups = list(todo_by_param.values())
    if not groups:
        return stats
    first_start = min(seg.start_block for seg in segments)
    state = cached_state_at(ds, first_start - 1)
    for i in range(0, len(groups), batch):
        chunk = [job for g in groups[i:i + batch] for job in g]
        specs = []
        for p, seg, var, d, h, key in chunk:
            sp = p.to_spec(seg.start_block, seg.end_block, d["capital_token1"], d["gas_set"],
                           d["priority_fee_gwei"])
            specs.append(sp.__class__(**{**asdict(sp), **VARIANTS[var]}))
        t0 = int(time.time())
        results = simulate_many(ds, specs, start_state=state, return_exceptions=True)
        rows = []
        for (p, seg, var, d, h, key), r in zip(chunk, results):
            ok = isinstance(r, PositionResult)
            rows.append(dict(
                dedup_key=key, spec_hash=p.spec_hash, dataset_hash=h, code_version=version,
                status="done" if ok else "failed",
                rejection_reason=None if ok else f"{type(r).__name__}: {r}"[:2000],
                metrics_json=json.dumps(r.metrics) if ok else None, actions_json=None,
                label="MODELED", started_at=t0, finished_at=int(time.time())))
            stats["done" if ok else "failed"] += 1
        with engine.begin() as conn:
            keys = [r["dedup_key"] for r in rows]
            conn.execute(S.experiments.delete().where(S.experiments.c.dedup_key.in_(keys)))
            conn.execute(insert(S.experiments), rows)
        if progress:
            progress(min(i + batch, len(groups)), len(groups))
    return stats


def load_segment_table(engine, base_descriptor: dict, segments: list[Segment],
                       version: str | None = None) -> dict:
    """{(spec_hash, segment_index, variant): metrics} for the given study."""
    version = version or code_version()
    want = {}
    for seg in segments:
        for var in VARIANTS:
            want[_hash(segment_descriptor(base_descriptor, seg, var))] = (seg.index, var)
    out = {}
    with engine.connect() as conn:
        for sh, dh, mj, st in conn.execute(
                select(S.experiments.c.spec_hash, S.experiments.c.dataset_hash,
                       S.experiments.c.metrics_json, S.experiments.c.status)
                .where(S.experiments.c.code_version == version,
                       S.experiments.c.dataset_hash.in_(list(want)))):
            seg_i, var = want[dh]
            out[(sh, seg_i, var)] = json.loads(mj) if st == "done" else None
    return out
