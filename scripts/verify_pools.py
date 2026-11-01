"""Stage 2: confirm configured pools on-chain and find their creation blocks.

For every pool in config/pools.yaml:
  Factory.getPool(token0, token1, fee) == configured (CREATE2-derived) address,
  pool.token0/token1/fee/tickSpacing match config, PoolCreated event found.
Results are written to data/pool_verification.json (no secrets inside).

Run: python -m scripts.verify_pools [--pools wbtc_usdc_3000,wbtc_usdc_500]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.core.config import load_config
from backend.data_engine.onchain import find_pool_creation, get_pool, read_pool
from backend.data_engine.rpc import select_client

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", help="comma-separated pool keys (default: all enabled)")
    ap.add_argument("--extra-fees", default="100,10000",
                    help="also check whether WBTC/USDC pools exist for these fee tiers")
    args = ap.parse_args()

    cfg = load_config()
    keys = args.pools.split(",") if args.pools else [k for k, p in cfg.pools.items() if p.enabled]
    results: dict = {}
    net_name = cfg.pools[keys[0]].network
    net = cfg.networks[net_name]
    dep = net.uniswap_v3
    c = select_client(net, cfg.lab.get("data_engine"), require_archive=True)
    head = c.block_number()
    ref = head - cfg.lab["data_engine"]["confirmations"]
    print(f"head={head} reference_block={ref}")

    for key in keys:
        p = cfg.pools[key]
        t0 = cfg.token(p.network, p.token0).address
        t1 = cfg.token(p.network, p.token1).address
        r: dict = {"configured_address": p.address, "fee": p.fee}
        onchain = get_pool(c, dep.factory, t0, t1, p.fee, ref)
        r["factory_getPool"] = onchain
        r["address_matches"] = onchain == p.address
        if onchain:
            st = read_pool(c, onchain, ref)
            r.update(
                token0_ok=st.token0 == t0, token1_ok=st.token1 == t1,
                fee_ok=st.fee == p.fee, tick_spacing_ok=st.tick_spacing == p.tick_spacing,
                tick=st.tick, sqrt_price_x96=str(st.sqrt_price_x96),
                liquidity=str(st.liquidity), fee_protocol=st.fee_protocol,
                balance0=str(st.balance0), balance1=str(st.balance1), state_block=ref,
            )
            created = find_pool_creation(c, dep.factory, t0, t1, p.fee, onchain,
                                         dep.factory_deploy_block, ref)
            r.update(created_block=created.block, created_tx=created.tx_hash,
                     created_method=created.method,
                     created_tick_spacing_ok=created.tick_spacing == p.tick_spacing)
        r["verified"] = bool(onchain) and all(
            r.get(k) for k in ("address_matches", "token0_ok", "token1_ok", "fee_ok",
                               "tick_spacing_ok", "created_tick_spacing_ok"))
        results[key] = r
        print(f"{key}: verified={r['verified']} created_block={r.get('created_block')} "
              f"tick={r.get('tick')} fee_protocol={r.get('fee_protocol')}")

    # Other WBTC/USDC fee tiers that might exist (context for the fee-tier choice).
    w = cfg.token(net_name, "WBTC").address
    u = cfg.token(net_name, "USDC").address
    other = {}
    for fee in [int(f) for f in args.extra_fees.split(",") if f]:
        other[fee] = get_pool(c, dep.factory, w, u, fee, ref)
        print(f"WBTC/USDC fee={fee}: {other[fee] or 'no pool'}")

    # Factory deployment block sanity: code exists at deploy block, not before.
    from backend.data_engine.onchain import has_code
    fb = dep.factory_deploy_block
    factory_ok = has_code(c, dep.factory, fb) and not has_code(c, dep.factory, fb - 1)
    print(f"factory_deploy_block {fb}: {'confirmed' if factory_ok else 'MISMATCH'}")

    out = {"head": head, "reference_block": ref, "endpoint": c.name,
           "factory_deploy_block_confirmed": factory_ok,
           "pools": results, "other_wbtc_usdc_fee_tiers": other}
    path = ROOT / "data" / "pool_verification.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(f"written {path.relative_to(ROOT)}  (rpc requests: {c.requests_sent})")


if __name__ == "__main__":
    main()
