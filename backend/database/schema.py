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


# ---------------------------------------------------------------- research
strategies = Table(
    "strategies", metadata,
    Column("spec_hash", String(64), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("params_json", Text, nullable=False),
)

datasets = Table(
    "datasets", metadata,
    Column("dataset_hash", String(64), primary_key=True),
    Column("pool", String(64), nullable=False),
    Column("from_block", BigInteger, nullable=False),
    Column("to_block", BigInteger, nullable=False),
    Column("description_json", Text, nullable=False),  # what the hash covers
)

experiments = Table(
    "experiments", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("dedup_key", String(64), nullable=False, unique=True),  # spec + dataset + code
    Column("spec_hash", String(64), nullable=False, index=True),
    Column("dataset_hash", String(64), nullable=False, index=True),
    Column("code_version", String(64), nullable=False),
    Column("status", String(16), nullable=False),  # done | failed | rejected
    Column("rejection_reason", Text),
    Column("metrics_json", Text),
    Column("actions_json", Text),
    Column("label", String(16), nullable=False, default="MODELED"),
    Column("started_at", BigInteger, nullable=False),
    Column("finished_at", BigInteger),
)


validation_reports = Table(
    "validation_reports", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("study_key", String(64), nullable=False, index=True),
    Column("spec_hash", String(64), nullable=False),
    Column("code_version", String(64), nullable=False),
    Column("score", Integer, nullable=False),          # robustness 0..100 (x100 for 2 decimals)
    Column("components_json", Text, nullable=False),
    Column("flags_json", Text, nullable=False),
    Column("created_at", BigInteger, nullable=False),
)


# ------------------------------------------------------------ AI researcher
hypotheses = Table(
    "hypotheses", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cycle_id", String(64), nullable=False, index=True),
    Column("created_at", BigInteger, nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("rationale", Text, nullable=False),
    Column("params_json", Text, nullable=False),       # list of strategy parameter sets
    Column("test_plan", Text, nullable=False),
    Column("expected_outcome", Text, nullable=False),
    Column("status", String(24), nullable=False),      # proposed | invalid | duplicate | tested | failed
    Column("verdict", String(16)),                     # confirmed | rejected | inconclusive
    Column("confidence", String(8)),                   # low | medium | high (computed by Python)
    Column("result_json", Text),                       # numbers computed by Python only
    Column("conclusion", Text),                        # deterministic summary of the result
    Column("ai_commentary", Text),                     # model-written analysis (no new numbers)
    Column("created_by_model", String(64), nullable=False),
    Column("code_version", String(64)),
)

hypothesis_experiments = Table(
    "hypothesis_experiments", metadata,
    Column("hypothesis_id", Integer, primary_key=True),
    Column("spec_hash", String(64), primary_key=True),
)


# ------------------------------------------------------------ signals
signals = Table(  # append-only: one row per issued signal, never updated
    "signals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", BigInteger, nullable=False),
    Column("block", BigInteger, nullable=False),
    Column("pool", String(64), nullable=False),
    Column("signal_type", String(24), nullable=False),  # OPEN CANDIDATE | WAIT | ...
    Column("spec_hash", String(64)),
    Column("payload_json", Text, nullable=False),        # full SPEC 11.3 content
    Column("label", String(16), nullable=False, default="SCENARIO"),
    Column("parent_id", Integer),                        # signal this one follows up
)

signal_events = Table(  # status history of signals (monitoring, SPEC 11.5)
    "signal_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("signal_id", Integer, nullable=False, index=True),
    Column("created_at", BigInteger, nullable=False),
    Column("block", BigInteger, nullable=False),
    Column("status", String(24), nullable=False),
    Column("reason", Text, nullable=False),
    Column("details_json", Text),
)

market_evaluations = Table(  # every evaluation, including ones that changed nothing
    "market_evaluations", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", BigInteger, nullable=False),
    Column("block", BigInteger, nullable=False),
    Column("decision", String(24), nullable=False),
    Column("snapshot_json", Text, nullable=False),
    Column("reasons_json", Text, nullable=False),
)


# ------------------------------------------------------------ virtual portfolio
virtual_positions = Table(  # parameters are written once, never changed (SPEC 12)
    "virtual_positions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("signal_id", Integer, nullable=False, unique=True),
    Column("created_at", BigInteger, nullable=False),
    Column("open_block", BigInteger, nullable=False),
    Column("spec_hash", String(64), nullable=False),
    Column("params_json", Text, nullable=False),
    Column("capital_token1", Text, nullable=False),
    Column("valid_until", BigInteger, nullable=False),
    Column("signal_payload_sha256", String(64), nullable=False),  # immutability check
    Column("opening_json", Text, nullable=False),  # range + amounts promised by the signal
)

virtual_marks = Table(  # derived: recomputed from the immutable parameters and exact data
    "virtual_marks", metadata,
    Column("position_id", Integer, primary_key=True),
    Column("block", BigInteger, primary_key=True),
    Column("timestamp", BigInteger, nullable=False),
    Column("mark_json", Text, nullable=False),
)

virtual_results = Table(  # latest state of every position (one row per position)
    "virtual_results", metadata,
    Column("position_id", Integer, primary_key=True),
    Column("status", String(16), nullable=False),     # open | closed
    Column("close_block", BigInteger),
    Column("close_reason", Text),
    Column("outcome", String(16)),                    # success | failure (closed only)
    Column("last_block", BigInteger, nullable=False),
    Column("result_json", Text, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
)


forward_tests = Table(  # candidates watched on data that did not exist when they were chosen
    "forward_tests", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", BigInteger, nullable=False),
    Column("start_block", BigInteger, nullable=False),
    Column("spec_hash", String(64), nullable=False),
    Column("params_json", Text, nullable=False),
    Column("capital_token1", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("result_json", Text),
    Column("updated_at", BigInteger),
)


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
           "event_tables", "pool_created_table", "strategies", "datasets", "experiments",
           "validation_reports", "hypotheses", "hypothesis_experiments", "signals", "signal_events",
           "market_evaluations", "virtual_positions", "virtual_marks",
           "virtual_results", "forward_tests", "make_engine"]
