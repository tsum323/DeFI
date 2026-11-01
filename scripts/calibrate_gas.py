"""Stage 2: calibrate LP gas costs from real transaction receipts.

Samples transactions of the primary pool that went directly to the
NonfungiblePositionManager (Mint/Burn rows with owner == NPM) and single-hop
swaps, stratified by year, fetches eth_getTransactionReceipt and classifies
each tx by the events in its receipt:

  mint_new_position               NFT minted (Transfer from 0x0) + IncreaseLiquidity
  increase_liquidity              IncreaseLiquidity on an existing NFT
  decrease_liquidity_and_collect  DecreaseLiquidity + Collect in one tx
  swap_for_rebalance              plain single-hop router swap: one v3 Swap (this pool),
                                  Swap.sender == tx.to, <= 4 logs in the receipt

Only successful txs with `to` == NPM count for LP actions; anything else is
"other". Output: data/gas_calibration.json (gasUsed percentiles, per year).
gasUsed per sample is EXACT; the medians used by the backtester are MODELED.

Run: python -m scripts.calibrate_gas [--per-year 60] [--endpoint drpc]
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.core.config import load_config
from backend.data_engine.abi import POOL_EVENTS, topic
from backend.data_engine.rpc import EndpointUnavailable, RpcError, select_client

ROOT = Path(__file__).resolve().parent.parent
T_TRANSFER = topic("Transfer(address,address,uint256)")
T_INC = topic("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
T_DEC = topic("DecreaseLiquidity(uint256,uint128,uint256,uint256)")
T_NPM_COLLECT = topic("Collect(uint256,address,uint256,uint256)")
T_SWAP = next(e.topic0 for e in POOL_EVENTS if e.name == "Swap")
ZERO_WORD = "0x" + "00" * 32
CATEGORIES = ("mint_new_position", "increase_liquidity",
              "decrease_liquidity_and_collect", "swap_for_rebalance")


def classify(rc: dict, npm: str, pool: str) -> str:
    if rc.get("status") != "0x1":
        return "failed"
    npm, pool = npm.lower(), pool.lower()
    t = [(lg["address"].lower(), lg["topics"][0].lower() if lg["topics"] else "", lg["topics"])
         for lg in rc["logs"]]
    swaps = [(a, tp) for a, t0, tp in t if t0 == T_SWAP]
    if len(swaps) == 1 and swaps[0][0] == pool and not any(a == npm for a, _, _ in t):
        # Plain router swap: the tx target itself called pool.swap (Swap.sender
        # == tx.to) and the receipt has only Swap + 2 token Transfers (+ Approval).
        sender = "0x" + swaps[0][1][1][-40:].lower()
        if sender == (rc.get("to") or "").lower() and len(t) <= 4:
            return "swap_for_rebalance"
        return "swap_other"
    if (rc.get("to") or "").lower() != npm or swaps:
        return "other"
    inc = sum(1 for a, t0, _ in t if a == npm and t0 == T_INC)
    dec = sum(1 for a, t0, _ in t if a == npm and t0 == T_DEC)
    col = sum(1 for a, t0, _ in t if a == npm and t0 == T_NPM_COLLECT)
    nft_mint = sum(1 for a, t0, tp in t
                   if a == npm and t0 == T_TRANSFER and len(tp) == 4 and tp[1].lower() == ZERO_WORD)
    if inc == 1 and dec == 0 and nft_mint == 1:
        return "mint_new_position"
    if inc == 1 and dec == 0 and nft_mint == 0 and col == 0:
        return "increase_liquidity"
    if dec == 1 and col == 1 and inc == 0:
        return "decrease_liquidity_and_collect"
    return "other"


def _stats(xs: list[int]) -> dict:
    xs = sorted(xs)
    if not xs:
        return {"n": 0}
    q = statistics.quantiles(xs, n=20, method="inclusive") if len(xs) > 1 else [xs[0]] * 19
    return {"n": len(xs), "median": int(statistics.median(xs)), "p25": int(q[4]),
            "p75": int(q[14]), "p90": int(q[17]), "min": xs[0], "max": xs[-1]}


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=cfg.primary_pool)
    ap.add_argument("--per-year", type=int, default=80, help="samples per source table per year")
    ap.add_argument("--endpoint", default="drpc")
    ap.add_argument("--seed", type=int, default=20261101)
    args = ap.parse_args()
    p = cfg.pools[args.pool]
    net = cfg.networks[p.network]
    npm = net.uniswap_v3.nonfungible_position_manager
    eps = {ep["name"]: ep for ep in net.rpc}
    client = select_client(net.model_copy(update={"rpc": [eps[args.endpoint]]}),
                           cfg.lab["data_engine"])

    db_path = cfg.lab["database"]["url"].removeprefix("sqlite:///")
    db = sqlite3.connect(f"file:{ROOT / db_path}?mode=ro", uri=True)
    addr = p.address.lower()
    year_of = "strftime('%Y', b.timestamp, 'unixepoch')"
    sources = {
        "ev_mint": f"owner = '{npm}'", "ev_burn": f"owner = '{npm}'", "ev_swap": "1=1",
    }
    rng = random.Random(args.seed)
    picks: list[tuple[str, str, int]] = []
    for table, cond in sources.items():
        rows = db.execute(
            f"select distinct e.tx_hash, {year_of}, e.block_number from {table} e "
            f"join blocks b on b.chain_id = e.chain_id and b.number = e.block_number "
            f"where lower(e.address) = ? and {cond}", (addr,)).fetchall()
        by_year: dict[str, list] = {}
        for h, y, bn in rows:
            by_year.setdefault(y, []).append((h, y, bn))
        for y in sorted(by_year):
            picks += rng.sample(by_year[y], min(args.per_year, len(by_year[y])))
    seen, uniq = set(), []
    for h, y, bn in picks:
        if h not in seen:
            seen.add(h)
            uniq.append((h, y, bn))
    print(f"{len(uniq)} transactions to fetch via {client.name}")

    samples: dict[str, list[dict]] = {}
    errors = 0
    t0 = time.monotonic()
    for i, (h, y, bn) in enumerate(uniq, 1):
        try:
            rc = client.call("eth_getTransactionReceipt", [h])
        except (EndpointUnavailable, RpcError) as exc:
            errors += 1
            print(f"  {h}: {exc}")
            continue
        cat = classify(rc, npm, p.address)
        samples.setdefault(cat, []).append({
            "tx": h, "block": bn, "year": y, "gas_used": int(rc["gasUsed"], 16),
            "effective_gas_price": int(rc.get("effectiveGasPrice", "0x0"), 16)})
        if i % 100 == 0:
            print(f"  {i}/{len(uniq)}  {time.monotonic() - t0:.0f}s", flush=True)

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pool": args.pool, "address": p.address, "npm": npm, "endpoint": client.name,
        "seed": args.seed, "per_year": args.per_year, "fetch_errors": errors,
        "label": "gas_used samples EXACT; summary statistics MODELED",
        "categories": {}, "counts": {k: len(v) for k, v in samples.items()},
    }
    for cat in CATEGORIES:
        xs = samples.get(cat, [])
        years = sorted({s["year"] for s in xs})
        recent = [s["gas_used"] for s in xs if s["year"] >= str(max(int(y) for y in years) - 1)] \
            if years else []
        result["categories"][cat] = {
            "all": _stats([s["gas_used"] for s in xs]),
            "last_2_years": _stats(recent),
            "by_year": {y: _stats([s["gas_used"] for s in xs if s["year"] == y]) for y in years},
        }
    result["samples"] = samples
    out = ROOT / "data" / "gas_calibration.json"
    out.write_text(json.dumps(result, indent=1) + "\n")
    for cat in CATEGORIES:
        c = result["categories"][cat]
        print(f"{cat:32s} all={c['all']}  last2y_median={c['last_2_years'].get('median')}")
    print(f"other={len(samples.get('other', []))} failed={len(samples.get('failed', []))} "
          f"errors={errors}  written {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
