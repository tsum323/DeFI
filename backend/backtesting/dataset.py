"""Assemble a backtest Dataset for a configured pool from the local DB and data files."""

from __future__ import annotations

import json
from pathlib import Path

from backend.backtesting.events import block_timestamps, load_pool_events
from backend.backtesting.position_sim import Dataset

ROOT = Path(__file__).resolve().parents[2]


def load_dataset(cfg, pool_key: str | None = None, to_block: int | None = None) -> Dataset:
    key = pool_key or cfg.primary_pool
    p = cfg.pools[key]
    db = ROOT / cfg.lab["database"]["url"].removeprefix("sqlite:///")
    eth_path = ROOT / "data" / "eth_usd_daily.json"
    if not eth_path.exists():
        raise FileNotFoundError("data/eth_usd_daily.json missing: run python -m scripts.fetch_eth_usd")
    costs = cfg.lab["costs"]
    return Dataset(
        events=load_pool_events(db, p.address, to_block=to_block),
        blocks=block_timestamps(db),
        eth_usd=json.loads(eth_path.read_text())["rows"],
        fee=p.fee, tick_spacing=p.tick_spacing,
        gas_units={"gas_units": costs["gas_units"], "gas_units_p90": costs["gas_units_p90"]},
        decimals0=cfg.token(p.network, p.token0).decimals,
        decimals1=cfg.token(p.network, p.token1).decimals,
    )


def block_at_or_after(ds: Dataset, timestamp: int) -> int:
    """First stored block with timestamp >= `timestamp` (blocks with pool events)."""
    cands = [b for b, (ts, _) in ds.blocks.items() if ts >= timestamp]
    if not cands:
        raise ValueError("timestamp after the last stored block")
    return min(cands)


def cached_state_at(ds: Dataset, block: int, cache_dir: Path | None = None):
    """Pool state at the end of `block`, cached on disk (data/replay_cache/).
    The cache key includes the number and last key of the events up to `block`,
    so a re-synced or extended history never reuses a stale state."""
    import hashlib
    import pickle

    from backend.backtesting.position_sim import state_at
    cache_dir = cache_dir or ROOT / "data" / "replay_cache"
    upto = [e for e in ds.events if e.block <= block]
    tag = hashlib.sha256(repr((len(upto), (upto[-1].block, upto[-1].log_index) if upto else None,
                               ds.fee, ds.tick_spacing)).encode()).hexdigest()[:16]
    path = cache_dir / f"state_{block}_{tag}.pkl"
    if path.exists():
        return pickle.loads(path.read_bytes())
    st = state_at(ds, block)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(pickle.dumps(st, protocol=pickle.HIGHEST_PROTOCOL))
    tmp.replace(path)
    return st
