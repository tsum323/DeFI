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


def _math_cases(db, addr: str, rng: random.Random, n: int) -> dict:
    """Real Mint/Burn events with the pool price just before them, and pairs of
    consecutive swaps (same liquidity, nothing in between) for math-core tests."""
    ev = db.execute(
        "select r.block_number, r.log_index, r.topic0 from raw_logs r where lower(r.address) = ? "
        "order by r.block_number, r.log_index", (addr,)).fetchall()
    topic = {e.name: e.topic0 for e in POOL_EVENTS}
    q = {"Swap": "select sqrtPriceX96, tick, liquidity, amount0, amount1 from ev_swap",
         "Mint": "select tickLower, tickUpper, amount, amount0, amount1 from ev_mint",
         "Burn": "select tickLower, tickUpper, amount, amount0, amount1 from ev_burn"}
    rows = {k: {(r[0], r[1]): r[2:] for r in db.execute(
        sql.replace("select ", "select block_number, log_index, ", 1) + " where lower(address) = ?",
        (addr,))} for k, sql in q.items()}
    kind_of = {topic[k]: k for k in q}
    mint_cases, swap_pairs = [], []
    last_swap, prev = None, None
    for bn, li, t0 in ev:
        kind = kind_of.get(t0)
        if kind is None:
            continue
        row = rows[kind][(bn, li)]
        if kind == "Swap":
            if prev and prev[0] == "Swap" and prev[1][2] == row[2] and prev[1][0] != row[0]:
                swap_pairs.append({"block": bn, "log_index": li, "sqrt_before": prev[1][0],
                                   "sqrt_after": row[0], "liquidity": row[2],
                                   "amount0": row[3], "amount1": row[4]})
            last_swap = row
        elif last_swap is not None and int(row[2]) > 0:
            mint_cases.append({"event": kind, "block": bn, "log_index": li,
                               "sqrt_price": last_swap[0], "tick": last_swap[1],
                               "tick_lower": row[0], "tick_upper": row[1], "liquidity": row[2],
                               "amount0": row[3], "amount1": row[4]})
        prev = (kind, row)
    return {"mint_burn_cases": rng.sample(mint_cases, min(n, len(mint_cases))),
            "swap_pairs": rng.sample(swap_pairs, min(n, len(swap_pairs)))}


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
           "address": p.address, "tick_spacing": p.tick_spacing, "fee": p.fee, "logs": out,
           **_math_cases(db, p.address.lower(), rng, args.per_type * 5)}
    path = ROOT / "tests" / "fixtures" / "real_pool_logs.json"
    path.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"wrote {len(out)} logs -> {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
