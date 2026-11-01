"""Portable, compressed snapshots of a synced pool (stage 2).

The SQLite database lives only inside an ephemeral container, so the
source-of-truth tables are exported to xz-compressed CSV parts that fit in
git (each part well under GitHub's 100 MB file limit):

  blocks_NNN.csv.xz  number, hash, timestamp, base_fee_per_gas, source
  logs_NNN.csv.xz    block_number, log_index, tx_hash, tx_index, topics, data, source
  manifest.json      pool row, sync coverage, row counts, sha256 of every part

Decoded ev_* tables are NOT exported: restore re-decodes them from raw logs
with the same decoder the syncer uses, so they are reproducible and EXACT.
block_hash is stored once per block (blocks part) and joined back on restore.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import lzma
import time
from pathlib import Path

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from backend.data_engine.sync import _insert_ignore, decode_log_row
from backend.database import schema as S

FORMAT = "defi-lab-pool-snapshot/1"
LOG_COLS = ["block_number", "log_index", "tx_hash", "tx_index", "topics", "data", "source"]
BLOCK_COLS = ["number", "hash", "timestamp", "base_fee_per_gas", "source"]


class SnapshotError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_part(path: Path, header: list[str], rows: list[tuple], preset: int) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(rows)
    path.write_bytes(lzma.compress(buf.getvalue().encode(), preset=preset))


def _read_part(path: Path) -> tuple[list[str], list[list[str]]]:
    rows = list(csv.reader(io.StringIO(lzma.decompress(path.read_bytes()).decode())))
    return rows[0], rows[1:]


def export_pool(engine: Engine, chain_id: int, address: str, out_dir: Path, *,
                log_rows_per_part: int = 120_000, block_rows_per_part: int = 150_000,
                preset: int = 6) -> dict:
    """Export blocks + raw logs of one pool up to its covered last_block."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in list(out_dir.glob("*.csv.xz")) + list(out_dir.glob("manifest.json")):
        old.unlink()
    with engine.connect() as conn:
        st = conn.execute(select(S.sync_state).where(
            S.sync_state.c.chain_id == chain_id, S.sync_state.c.address == address,
            S.sync_state.c.stream == "pool_logs")).first()
        if st is None:
            raise SnapshotError(f"{address}: nothing synced")
        pool = conn.execute(select(S.pools).where(
            S.pools.c.chain_id == chain_id, S.pools.c.address == address)).first()
        lo, hi = st.start_block, st.last_block
        rl = S.raw_logs
        addrs = conn.execute(select(rl.c.address).distinct().where(
            rl.c.chain_id == chain_id, func.lower(rl.c.address) == address.lower(),
            rl.c.block_number.between(lo, hi))).scalars().all()
        if len(addrs) > 1:
            raise SnapshotError(f"mixed address spellings in raw_logs: {addrs}")
        log_address = addrs[0] if addrs else address.lower()
        logs = conn.execute(
            select(*(rl.c[c] for c in LOG_COLS))
            .where(rl.c.chain_id == chain_id, rl.c.address == log_address,
                   rl.c.block_number.between(lo, hi))
            .order_by(rl.c.block_number, rl.c.log_index)).all()
        # blocks referenced by this pool's logs (blocks table is shared across pools)
        b = S.blocks
        blocks = conn.execute(
            select(*(b.c[c] for c in BLOCK_COLS))
            .where(b.c.chain_id == chain_id,
                   b.c.number.in_(select(rl.c.block_number).where(
                       rl.c.chain_id == chain_id, rl.c.address == log_address,
                       rl.c.block_number.between(lo, hi))))
            .order_by(b.c.number)).all()
        hash_of = {n: h for n, h, *_ in blocks}
        for row in conn.execute(select(rl.c.block_number, rl.c.block_hash).where(
                rl.c.chain_id == chain_id, rl.c.address == log_address,
                rl.c.block_number.between(lo, hi))):
            if hash_of.get(row.block_number, "").lower() != row.block_hash.lower():
                raise SnapshotError(f"raw_logs.block_hash differs from blocks at {row.block_number}")
        open_gaps = conn.execute(select(func.count()).select_from(S.sync_gaps).where(
            S.sync_gaps.c.chain_id == chain_id, S.sync_gaps.c.address == address,
            S.sync_gaps.c.resolved.is_(False))).scalar_one()

    files = []
    for kind, header, rows, per in (("blocks", BLOCK_COLS, blocks, block_rows_per_part),
                                    ("logs", LOG_COLS, logs, log_rows_per_part)):
        for i in range(0, max(len(rows), 1), per):
            part = [tuple(r) for r in rows[i:i + per]]
            if not part:
                continue
            name = f"{kind}_{i // per:03d}.csv.xz"
            _write_part(out_dir / name, header, part, preset)
            files.append({"name": name, "kind": kind, "rows": len(part),
                          "first_block": part[0][0], "last_block": part[-1][0],
                          "bytes": (out_dir / name).stat().st_size,
                          "sha256": _sha256(out_dir / name)})
    manifest = {
        "format": FORMAT,
        "created_at": int(time.time()),
        "chain_id": chain_id,
        "address": address,
        "log_address": log_address,
        "pool": dict(pool._mapping) if pool else None,
        "coverage": {"start_block": lo, "last_block": hi, "open_gaps": open_gaps},
        "counts": {"blocks": len(blocks), "raw_logs": len(logs)},
        "files": files,
        "note": "EXACT on-chain data. ev_* tables are re-decoded from raw logs on restore.",
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def restore_pool(engine: Engine, snap_dir: Path, *, batch: int = 20_000) -> dict:
    """Load a snapshot into `engine` (idempotent: existing rows are kept)."""
    m = json.loads((snap_dir / "manifest.json").read_text())
    if m.get("format") != FORMAT:
        raise SnapshotError(f"unsupported snapshot format {m.get('format')!r}")
    for f in m["files"]:
        if _sha256(snap_dir / f["name"]) != f["sha256"]:
            raise SnapshotError(f"{f['name']}: sha256 mismatch (corrupted or incomplete file)")
    chain_id, address, log_address = m["chain_id"], m["address"], m["log_address"]
    S.metadata.create_all(engine)

    hash_of: dict[int, str] = {}
    n_blocks = n_logs = 0
    decoded_counts: dict[str, int] = {}
    with engine.begin() as conn:
        if m.get("pool"):
            _insert_ignore(conn, S.pools, [m["pool"]])
        for f in (x for x in m["files"] if x["kind"] == "blocks"):
            header, rows = _read_part(snap_dir / f["name"])
            if header != BLOCK_COLS or len(rows) != f["rows"]:
                raise SnapshotError(f"{f['name']}: unexpected header or row count")
            recs = []
            for number, bhash, ts, base_fee, source in rows:
                hash_of[int(number)] = bhash
                recs.append(dict(chain_id=chain_id, number=int(number), hash=bhash,
                                 timestamp=int(ts), base_fee_per_gas=base_fee or None,
                                 source=source))
            for i in range(0, len(recs), batch):
                _insert_ignore(conn, S.blocks, recs[i:i + batch])
            n_blocks += len(recs)
        for f in (x for x in m["files"] if x["kind"] == "logs"):
            header, rows = _read_part(snap_dir / f["name"])
            if header != LOG_COLS or len(rows) != f["rows"]:
                raise SnapshotError(f"{f['name']}: unexpected header or row count")
            raw, dec = [], {}
            for bn, li, txh, txi, topics, data, source in rows:
                bn, li = int(bn), int(li)
                if bn not in hash_of:
                    raise SnapshotError(f"log at block {bn} has no block row")
                tl = json.loads(topics)
                raw.append(dict(chain_id=chain_id, block_number=bn, log_index=li,
                                block_hash=hash_of[bn], tx_hash=txh, tx_index=int(txi),
                                address=log_address, topic0=tl[0].lower() if tl else "",
                                topics=topics, data=data, source=source))
                d = decode_log_row(chain_id, {
                    "address": log_address, "topics": tl, "data": data,
                    "blockNumber": hex(bn), "logIndex": hex(li), "transactionHash": txh})
                if d is not None:
                    dec.setdefault(d[0], []).append(d[1])
            for i in range(0, len(raw), batch):
                _insert_ignore(conn, S.raw_logs, raw[i:i + batch])
            for table, recs in dec.items():
                for i in range(0, len(recs), batch):
                    _insert_ignore(conn, S.event_tables[table], recs[i:i + batch])
                decoded_counts[table] = decoded_counts.get(table, 0) + len(recs)
            n_logs += len(raw)
        cov = m["coverage"]
        st = conn.execute(select(S.sync_state).where(
            S.sync_state.c.chain_id == chain_id, S.sync_state.c.address == address,
            S.sync_state.c.stream == "pool_logs")).first()
        if st is None:
            conn.execute(insert(S.sync_state).values(
                chain_id=chain_id, address=address, stream="pool_logs",
                start_block=cov["start_block"], last_block=cov["last_block"],
                updated_at=int(time.time())))
        elif st.last_block < cov["last_block"] and st.start_block >= cov["start_block"]:
            conn.execute(S.sync_state.update().where(
                S.sync_state.c.chain_id == chain_id, S.sync_state.c.address == address,
                S.sync_state.c.stream == "pool_logs").values(
                    start_block=cov["start_block"], last_block=cov["last_block"],
                    updated_at=int(time.time())))
    if n_blocks != m["counts"]["blocks"] or n_logs != m["counts"]["raw_logs"]:
        raise SnapshotError("row counts differ from manifest")
    return {"blocks": n_blocks, "raw_logs": n_logs, "decoded": decoded_counts,
            "coverage": m["coverage"]}
