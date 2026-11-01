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
