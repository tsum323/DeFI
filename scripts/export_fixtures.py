"""Write a small set of REAL pool logs to tests/fixtures/real_pool_logs.json.

Per event type: the first, the last and a few seeded-random logs of the
primary pool, in eth_getLogs format (EXACT on-chain data, not synthetic).
Run after sync: python -m scripts.export_fixtures [--per-type 6]
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
from pathlib import Path

from backend.core.config import load_config
from backend.data_engine.abi import POOL_EVENTS

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-type", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20261101)
    args = ap.parse_args()
    p = cfg.pools[cfg.primary_pool]
    db_path = cfg.lab["database"]["url"].removeprefix("sqlite:///")
    db = sqlite3.connect(f"file:{ROOT / db_path}?mode=ro", uri=True)
    rng = random.Random(args.seed)
    out = []
    for spec in POOL_EVENTS:
        keys = db.execute("select block_number, log_index from raw_logs where lower(address) = ? "
                          "and topic0 = ? order by block_number, log_index",
                          (p.address.lower(), spec.topic0)).fetchall()
        if not keys:
            continue
        chosen = {keys[0], keys[-1]}
        rest = [k for k in keys if k not in chosen]
        chosen |= set(rng.sample(rest, min(max(args.per_type - 2, 0), len(rest))))
        for bn, li in sorted(chosen):
            r = db.execute("select block_hash, tx_hash, tx_index, address, topics, data "
                           "from raw_logs where block_number = ? and log_index = ?", (bn, li)).fetchone()
            out.append({"event": spec.name, "address": r[3], "topics": json.loads(r[4]),
                        "data": r[5], "blockNumber": hex(bn), "logIndex": hex(li),
                        "blockHash": r[0], "transactionHash": r[1], "transactionIndex": hex(r[2])})
    doc = {"label": "EXACT real on-chain logs (Ethereum mainnet)", "pool": cfg.primary_pool,
           "address": p.address, "tick_spacing": p.tick_spacing, "logs": out}
    path = ROOT / "tests" / "fixtures" / "real_pool_logs.json"
    path.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"wrote {len(out)} logs -> {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
