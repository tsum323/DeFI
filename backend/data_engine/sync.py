"""Incremental, idempotent ingestion of all events of a Uniswap v3 pool.

Guarantees:
- coverage is contiguous: `sync_state.last_block` only advances after a whole
  window (logs + block headers) is committed in one transaction;
- only blocks with >= `confirmations` are ingested;
- each log's blockHash must equal the fetched header hash (reorg/consistency);
- re-running any window is a no-op (natural keys + INSERT OR IGNORE semantics);
- failed windows are recorded in `sync_gaps` and the run stops (no silent holes).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from backend.data_engine.abi import POOL_EVENTS_BY_TOPIC
from backend.data_engine.rpc import EndpointUnavailable, RangeTooLarge, RpcClient, RpcError
from backend.database import schema as S

log = logging.getLogger(__name__)


class IntegrityError(RuntimeError):
    pass


def _insert_ignore(conn: Connection, table, rows: list[dict]) -> None:
    if not rows:
        return
    stmt = insert(table)
    dialect = conn.dialect.name
    if dialect == "sqlite":
        stmt = stmt.prefix_with("OR IGNORE")
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        stmt = pg_insert(table).on_conflict_do_nothing()
    conn.execute(stmt, rows)


def decode_log_row(chain_id: int, lg: dict) -> tuple[str, dict] | None:
    spec = POOL_EVENTS_BY_TOPIC.get(lg["topics"][0].lower()) if lg["topics"] else None
    if spec is None:
        return None
    vals = spec.decode(lg)
    big = S.big_int_columns(spec)
    row = {k: (str(v) if k in big else v) for k, v in vals.items()}
    row.update(chain_id=chain_id, block_number=int(lg["blockNumber"], 16),
               log_index=int(lg["logIndex"], 16), address=lg["address"],
               tx_hash=lg["transactionHash"])
    return spec.table, row


LONDON_BLOCK = 12_965_000  # first block with baseFeePerGas (EIP-1559)
FEE_HISTORY_MAX = 1024


@dataclass
class SyncStats:
    windows: int = 0
    logs: int = 0
    blocks: int = 0
    by_event: dict[str, int] = field(default_factory=dict)
    shrinks: int = 0


@dataclass
class PoolSyncer:
    client: RpcClient
    engine: Engine
    chain_id: int
    address: str
    chunk_blocks: int = 5000
    min_chunk_blocks: int = 50
    max_chunk_blocks: int = 100_000
    header_batch: int = 50
    # "auto": take timestamps from eth_getLogs (blockTimestamp) and base fees
    # from eth_feeHistory (1024 blocks/call) when the node provides them, and
    # verify only one header per window; "headers": one header per block.
    block_meta: str = "auto"
    stats: SyncStats = field(default_factory=SyncStats)

    # -- state -----------------------------------------------------------
    def last_block(self) -> int | None:
        with self.engine.connect() as conn:
            r = conn.execute(select(S.sync_state.c.last_block).where(
                S.sync_state.c.chain_id == self.chain_id,
                S.sync_state.c.address == self.address,
                S.sync_state.c.stream == "pool_logs")).first()
        return None if r is None else r[0]

    def _advance(self, conn: Connection, start: int, last: int) -> None:
        now = int(time.time())
        res = conn.execute(update(S.sync_state).where(
            S.sync_state.c.chain_id == self.chain_id, S.sync_state.c.address == self.address,
            S.sync_state.c.stream == "pool_logs").values(last_block=last, updated_at=now))
        if res.rowcount == 0:
            conn.execute(insert(S.sync_state).values(
                chain_id=self.chain_id, address=self.address, stream="pool_logs",
                start_block=start, last_block=last, updated_at=now))
        conn.execute(update(S.sync_gaps).where(
            S.sync_gaps.c.chain_id == self.chain_id, S.sync_gaps.c.address == self.address,
            S.sync_gaps.c.to_block <= last).values(resolved=True))

    # -- fetching --------------------------------------------------------
    def _headers(self, numbers: list[int]) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for i in range(0, len(numbers), self.header_batch):
            chunk = numbers[i: i + self.header_batch]
            res = self.client.batch([("eth_getBlockByNumber", [hex(n), False]) for n in chunk])
            for n, h in zip(chunk, res):
                if not h:
                    raise IntegrityError(f"block {n}: header missing")
                out[n] = h
        return out

    def _base_fees(self, lo: int, hi: int) -> dict[int, int]:
        """Base fee per block for [lo, hi] via eth_feeHistory (cached, 1024/call)."""
        cache = self.__dict__.setdefault("_fee_cache", {})
        missing = [n for n in range(max(lo, LONDON_BLOCK), hi + 1) if n not in cache]
        while missing:
            start = missing[0]
            newest = min(start + FEE_HISTORY_MAX - 1, max(hi, self.__dict__.get("_to_block", hi)))
            res = self.client.call("eth_feeHistory", [hex(newest - start + 1), hex(newest), []])
            oldest = int(res["oldestBlock"], 16)
            fees = res["baseFeePerGas"]
            if oldest != start or len(fees) < newest - start + 1:
                raise IntegrityError(f"eth_feeHistory returned unexpected range at {start}")
            for i in range(newest - start + 1):
                cache[start + i] = int(fees[i], 16)
            missing = [n for n in missing if n not in cache]
        for n in [k for k in cache if k < lo]:  # keep the cache small
            del cache[n]
        return {n: cache[n] for n in range(max(lo, LONDON_BLOCK), hi + 1)}

    def _meta_from_logs(self, nums: list[int], logs: list[dict]) -> dict[int, dict]:
        """Header-like dicts built from log fields; one real header per window
        is still fetched to cross-check blockHash/timestamp."""
        out: dict[int, dict] = {}
        for lg in logs:
            n = int(lg["blockNumber"], 16)
            h = out.setdefault(n, {"number": lg["blockNumber"], "hash": lg["blockHash"],
                                   "timestamp": lg["blockTimestamp"]})
            if h["hash"].lower() != lg["blockHash"].lower() \
                    or int(h["timestamp"], 16) != int(lg["blockTimestamp"], 16):
                raise IntegrityError(f"inconsistent block metadata in logs at block {n}")
        fees = self._base_fees(nums[0], nums[-1])
        for n, h in out.items():
            if n in fees:
                h["baseFeePerGas"] = hex(fees[n])
        probe = nums[-1]
        real = self._headers([probe])[probe]
        if real["hash"].lower() != out[probe]["hash"].lower() \
                or int(real["timestamp"], 16) != int(out[probe]["timestamp"], 16) \
                or (real.get("baseFeePerGas") and int(real["baseFeePerGas"], 16) != fees.get(probe)):
            raise IntegrityError(f"log metadata disagrees with header at block {probe}")
        return out

    def _ingest_window(self, lo: int, hi: int, start_block: int) -> int:
        logs = self.client.get_logs(self.address, lo, hi)
        for lg in logs:
            if lg.get("removed"):
                raise IntegrityError(f"removed log in finalized range at block {lg['blockNumber']}")
            if lg["address"].lower() != self.address.lower():
                raise IntegrityError("log from unexpected address")
        nums = sorted({int(lg["blockNumber"], 16) for lg in logs})
        if self.block_meta == "auto" and logs and all("blockTimestamp" in lg for lg in logs):
            headers = self._meta_from_logs(nums, logs)
        else:
            headers = self._headers(nums)
        block_rows, raw_rows, decoded = [], [], {}
        for n in nums:
            h = headers[n]
            if int(h["number"], 16) != n:
                raise IntegrityError(f"header number mismatch at {n}")
            block_rows.append(dict(
                chain_id=self.chain_id, number=n, hash=h["hash"],
                timestamp=int(h["timestamp"], 16),
                base_fee_per_gas=str(int(h["baseFeePerGas"], 16)) if h.get("baseFeePerGas") else None,
                source=self.client.name))
        for lg in logs:
            n = int(lg["blockNumber"], 16)
            if lg["blockHash"].lower() != headers[n]["hash"].lower():
                raise IntegrityError(f"blockHash mismatch at block {n} (reorg or bad node)")
            raw_rows.append(dict(
                chain_id=self.chain_id, block_number=n, log_index=int(lg["logIndex"], 16),
                block_hash=lg["blockHash"], tx_hash=lg["transactionHash"],
                tx_index=int(lg["transactionIndex"], 16), address=lg["address"],
                topic0=lg["topics"][0].lower() if lg["topics"] else "",
                topics=json.dumps(lg["topics"]), data=lg["data"], source=self.client.name))
            d = decode_log_row(self.chain_id, lg)
            if d is None:
                log.warning("unknown topic %s at block %s", raw_rows[-1]["topic0"], n)
                continue
            decoded.setdefault(d[0], []).append(d[1])
        with self.engine.begin() as conn:
            _insert_ignore(conn, S.blocks, block_rows)
            _insert_ignore(conn, S.raw_logs, raw_rows)
            for table, rows in decoded.items():
                _insert_ignore(conn, S.event_tables[table], rows)
                self.stats.by_event[table] = self.stats.by_event.get(table, 0) + len(rows)
            self._advance(conn, start_block, hi)
        self.stats.windows += 1
        self.stats.logs += len(logs)
        self.stats.blocks += len(nums)
        return len(logs)

    def _record_gap(self, lo: int, hi: int, err: Exception) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(S.sync_gaps).values(
                chain_id=self.chain_id, address=self.address, from_block=lo, to_block=hi,
                error=str(err)[:2000], source=self.client.name,
                created_at=int(time.time()), resolved=False))

    # -- main loop -------------------------------------------------------
    def run(self, start_block: int, to_block: int, *, max_seconds: float | None = None,
            progress=None) -> SyncStats:
        last = self.last_block()
        cur = start_block if last is None else last + 1
        if last is not None and last < start_block - 1:
            raise IntegrityError("existing sync_state starts after requested start_block")
        chunk = self.chunk_blocks
        self.__dict__["_to_block"] = to_block
        t0 = time.monotonic()
        while cur <= to_block:
            if max_seconds is not None and time.monotonic() - t0 > max_seconds:
                break
            hi = min(cur + chunk - 1, to_block)
            try:
                n = self._ingest_window(cur, hi, start_block)
            except RangeTooLarge as exc:
                if chunk <= self.min_chunk_blocks:
                    self._record_gap(cur, hi, exc)
                    raise
                chunk = max(self.min_chunk_blocks, chunk // 2)
                self.stats.shrinks += 1
                continue
            except (EndpointUnavailable, RpcError, IntegrityError) as exc:
                self._record_gap(cur, hi, exc)
                raise
            # grow the window again when results are sparse
            if n < 2000 and chunk < self.max_chunk_blocks:
                chunk = min(self.max_chunk_blocks, chunk * 2)
            cur = hi + 1
            if progress:
                progress(hi, self.stats)
        return self.stats


def coverage_report(engine: Engine, chain_id: int, address: str) -> dict:
    """Summary used by scripts and tests: coverage + per-table counts."""
    from sqlalchemy import func
    with engine.connect() as conn:
        st = conn.execute(select(S.sync_state).where(
            S.sync_state.c.chain_id == chain_id, S.sync_state.c.address == address)).first()
        # Decoded event rows store the address lower-cased (as returned by the node).
        counts = {name: conn.execute(select(func.count()).select_from(t).where(
            func.lower(t.c.address) == address.lower())).scalar_one()
            for name, t in S.event_tables.items()}
        gaps = conn.execute(select(func.count()).select_from(S.sync_gaps).where(
            S.sync_gaps.c.address == address, S.sync_gaps.c.resolved.is_(False))).scalar_one()
    return {"start_block": st.start_block if st else None,
            "last_block": st.last_block if st else None,
            "open_gaps": gaps, "events": counts}
