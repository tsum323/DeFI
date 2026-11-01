"""Load the decoded event stream of one pool in strict (block, logIndex) order."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from backend.data_engine.abi import POOL_EVENTS
from backend.database import schema as S

_INT_TYPES = ("int", "uint")


@dataclass(frozen=True, slots=True)
class PoolEvent:
    block: int
    log_index: int
    name: str
    tx_hash: str
    f: dict  # decoded fields, big integers as Python int


def _converters(spec) -> dict:
    big = S.big_int_columns(spec)
    return {n: (int if n in big else None) for n, _, _ in spec.fields}


def load_pool_events(db_path: str | Path, address: str, from_block: int = 0,
                     to_block: int | None = None) -> list[PoolEvent]:
    """All decoded events of `address` in [from_block, to_block], ordered."""
    db = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    addr = address.lower()
    hi = to_block if to_block is not None else 1 << 62
    out: list[PoolEvent] = []
    for spec in POOL_EVENTS:
        names = [n for n, _, _ in spec.fields]
        conv = _converters(spec)
        cols = ", ".join(f'"{n}"' for n in names)
        for row in db.execute(
                f"select block_number, log_index, tx_hash, {cols} from {spec.table} "
                f"where lower(address) = ? and block_number between ? and ?", (addr, from_block, hi)):
            vals = {}
            for n, v in zip(names, row[3:]):
                c = conv[n]
                vals[n] = c(v) if c is not None and v is not None else v
            out.append(PoolEvent(row[0], row[1], spec.name, row[2], vals))
    out.sort(key=lambda e: (e.block, e.log_index))
    return out


def block_timestamps(db_path: str | Path) -> dict[int, tuple[int, int | None]]:
    """block -> (timestamp, base_fee_per_gas) for every stored block."""
    db = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    return {n: (ts, int(bf) if bf is not None else None)
            for n, ts, bf in db.execute("select number, timestamp, base_fee_per_gas from blocks")}
