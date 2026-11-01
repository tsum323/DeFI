"""SQLAlchemy Core schema for the data layer (stage 2).

Integers that can exceed 64 bits (uint128/uint160/int256, fee growth) are
stored as decimal TEXT to keep exact precision on SQLite and PostgreSQL alike.
Natural key of every log-derived row: (chain_id, block_number, log_index) —
re-ingesting a window is idempotent.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger, Boolean, Column, Integer, MetaData, String, Table, Text,
    create_engine, event,
)
from sqlalchemy.engine import Engine

from backend.data_engine.abi import POOL_CREATED, POOL_EVENTS, EventSpec
from backend.data_engine.abi import _bits as int_bits

metadata = MetaData()

pools = Table(
    "pools", metadata,
    Column("chain_id", Integer, primary_key=True),
    Column("address", String(42), primary_key=True),
    Column("key", String(64), nullable=False),
    Column("token0", String(42), nullable=False),
    Column("token1", String(42), nullable=False),
    Column("fee", Integer, nullable=False),
    Column("tick_spacing", Integer, nullable=False),
    Column("created_block", BigInteger),
    Column("created_tx", String(66)),
    Column("verified_at_block", BigInteger),
    Column("verified_source", String(64)),
)

blocks = Table(
    "blocks", metadata,
    Column("chain_id", Integer, primary_key=True),
    Column("number", BigInteger, primary_key=True),
    Column("hash", String(66), nullable=False),
    Column("timestamp", BigInteger, nullable=False),
    Column("base_fee_per_gas", Text),  # NULL before London (block 12965000)
    Column("source", String(64), nullable=False),
)

def _log_key() -> list[Column]:
    return [Column("chain_id", Integer, primary_key=True),
            Column("block_number", BigInteger, primary_key=True),
            Column("log_index", Integer, primary_key=True)]

raw_logs = Table(
    "raw_logs", metadata,
    *_log_key(),
    Column("block_hash", String(66), nullable=False),
    Column("tx_hash", String(66), nullable=False),
    Column("tx_index", Integer, nullable=False),
    Column("address", String(42), nullable=False, index=True),
    Column("topic0", String(66), nullable=False, index=True),
    Column("topics", Text, nullable=False),  # JSON list
    Column("data", Text, nullable=False),
    Column("source", String(64), nullable=False),
)

sync_state = Table(
    "sync_state", metadata,
    Column("chain_id", Integer, primary_key=True),
    Column("address", String(42), primary_key=True),
    Column("stream", String(32), primary_key=True),  # 'pool_logs'
    Column("start_block", BigInteger, nullable=False),
    Column("last_block", BigInteger, nullable=False),  # fully covered up to (inclusive)
    Column("updated_at", BigInteger, nullable=False),
)

sync_gaps = Table(
    "sync_gaps", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("chain_id", Integer, nullable=False),
    Column("address", String(42), nullable=False),
    Column("from_block", BigInteger, nullable=False),
    Column("to_block", BigInteger, nullable=False),
    Column("error", Text, nullable=False),
    Column("source", String(64), nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("resolved", Boolean, nullable=False, default=False),
)

_SQL_TYPE = {"address": String(42), "bool": Boolean}


def _event_table(spec: EventSpec) -> Table:
    cols = _log_key()
    cols += [Column("address", String(42), nullable=False, index=True),
             Column("tx_hash", String(66), nullable=False)]
    for name, typ, _ in spec.fields:
        small = typ.startswith(("int", "uint")) and int_bits(typ) <= 32
        sql = _SQL_TYPE.get(typ) or (Integer if small else Text)
        cols.append(Column(name, sql, nullable=False))
    return Table(spec.table, metadata, *cols)


event_tables: dict[str, Table] = {e.table: _event_table(e) for e in POOL_EVENTS}
pool_created_table = _event_table(POOL_CREATED)


def big_int_columns(spec: EventSpec) -> set[str]:
    t = event_tables.get(spec.table, pool_created_table)
    return {n for n, typ, _ in spec.fields
            if typ.startswith(("int", "uint")) and isinstance(t.c[n].type, Text)}


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite:///"):
        from pathlib import Path
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:" and not Path(path).is_absolute():
            root = Path(__file__).resolve().parents[2]
            path = str(root / path)
            url = "sqlite:///" + path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    metadata.create_all(engine)
    return engine


__all__ = ["metadata", "pools", "blocks", "raw_logs", "sync_state", "sync_gaps",
           "event_tables", "pool_created_table", "make_engine"]
