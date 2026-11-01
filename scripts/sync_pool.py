"""Stage 2: load the full event history of a pool into the database.

Starts at the pool's creation block (from data/pool_verification.json, produced
by scripts.verify_pools) and is resumable: re-running continues from
sync_state.last_block. Only blocks with >= `confirmations` are ingested.

Run: python -m scripts.sync_pool [--pool wbtc_usdc_3000] [--to-block N]
                                 [--max-blocks N] [--max-minutes M]
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

from sqlalchemy import insert, select

from backend.core.config import load_config
from backend.data_engine.rpc import select_client
from backend.data_engine.sync import PoolSyncer, coverage_report
from backend.database import schema as S

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=cfg.primary_pool)
    ap.add_argument("--to-block", type=int)
    ap.add_argument("--max-blocks", type=int, help="limit this run to N blocks after resume point")
    ap.add_argument("--max-minutes", type=float)
    ap.add_argument("--start-block", type=int, help="override creation block (testing only)")
    args = ap.parse_args()

    p = cfg.pools[args.pool]
    net = cfg.networks[p.network]
    de = cfg.lab["data_engine"]
    ver_path = ROOT / "data" / "pool_verification.json"
    ver = json.loads(ver_path.read_text())["pools"].get(args.pool, {}) if ver_path.exists() else {}
    if args.start_block is None and not ver.get("verified"):
        raise SystemExit(f"{args.pool}: not verified on-chain — run python -m scripts.verify_pools")
    start = args.start_block if args.start_block is not None else ver["created_block"]

    client = select_client(net, de)
    head = client.block_number()
    safe = head - de["confirmations"]
    to_block = min(args.to_block or safe, safe)

    engine = S.make_engine(cfg.lab["database"]["url"])
    with engine.begin() as conn:
        if conn.execute(select(S.pools).where(S.pools.c.chain_id == net.chain_id,
                                              S.pools.c.address == p.address)).first() is None:
            conn.execute(insert(S.pools).values(
                chain_id=net.chain_id, address=p.address, key=args.pool,
                token0=cfg.token(p.network, p.token0).address,
                token1=cfg.token(p.network, p.token1).address,
                fee=p.fee, tick_spacing=p.tick_spacing,
                created_block=ver.get("created_block"), created_tx=ver.get("created_tx"),
                verified_at_block=ver.get("state_block"), verified_source=client.name))

    syncer = PoolSyncer(client, engine, net.chain_id, p.address,
                        chunk_blocks=de["logs_chunk_blocks"],
                        min_chunk_blocks=de["min_chunk_blocks"])
    resume = (syncer.last_block() or start - 1) + 1
    if args.max_blocks:
        to_block = min(to_block, resume + args.max_blocks - 1)
    total = max(0, to_block - resume + 1)
    print(f"{args.pool} {p.address}: blocks {resume}..{to_block} ({total} blocks), head={head}")
    t0 = time.monotonic()

    def progress(hi, st):
        done = hi - resume + 1
        print(f"  ..{hi}  {100 * done / max(total, 1):5.1f}%  logs={st.logs} "
              f"windows={st.windows} rpc={client.requests_sent} {time.monotonic() - t0:.0f}s",
              flush=True)

    try:
        syncer.run(start, to_block,
                   max_seconds=args.max_minutes * 60 if args.max_minutes else None,
                   progress=progress)
    finally:
        rep = coverage_report(engine, net.chain_id, p.address)
        print(json.dumps(rep, indent=2))


if __name__ == "__main__":
    main()
