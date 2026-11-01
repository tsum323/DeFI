"""Stage 2: choose the primary WBTC/USDC fee tier from real activity.

For each candidate pool, over the last N days of finalized blocks:
swap count, USDC-side volume, LP fee revenue (volume * fee), TVL (pool token
balances valued at the pool price) and fee/TVL (annualized). Only Swap logs are
fetched. Output: data/fee_tier_comparison.json.

Selection rule (documented in docs/DATA_SOURCES.md §5): the primary pool is the
one with the higher LP fee revenue in USDC over the window, provided it also
has >= 1000 swaps in the window (enough events for statistically meaningful
backtests). Ties / close calls (<20% difference) are flagged for review.

Run: python -m scripts.compare_fee_tiers [--days 30]
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, getcontext
from pathlib import Path

from backend.core.config import load_config
from backend.data_engine.abi import POOL_EVENTS
from backend.data_engine.onchain import read_pool
from backend.data_engine.rpc import RangeTooLarge, select_client

getcontext().prec = 60
ROOT = Path(__file__).resolve().parents[1]
SWAP = next(e for e in POOL_EVENTS if e.name == "Swap")


def price_usdc_per_wbtc(sqrt_price_x96: int, dec0: int, dec1: int) -> Decimal:
    raw = (Decimal(sqrt_price_x96) / (Decimal(2) ** 96)) ** 2
    return raw * (Decimal(10) ** (dec0 - dec1))


def fetch_swaps(c, address: str, lo: int, hi: int, chunk: int) -> list[dict]:
    out, cur = [], lo
    while cur <= hi:
        end = min(cur + chunk - 1, hi)
        try:
            out += c.get_logs(address, cur, end, [SWAP.topic0])
            cur = end + 1
        except RangeTooLarge:
            chunk = max(50, chunk // 2)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--pools", default="wbtc_usdc_3000,wbtc_usdc_500")
    args = ap.parse_args()

    cfg = load_config()
    keys = args.pools.split(",")
    net = cfg.networks[cfg.pools[keys[0]].network]
    c = select_client(net, cfg.lab["data_engine"])
    hi = c.block_number() - cfg.lab["data_engine"]["confirmations"]
    lo = hi - int(args.days * 86400 / net.block_time_seconds)
    print(f"window: blocks {lo}..{hi} (~{args.days} days)")

    res = {}
    for key in keys:
        p = cfg.pools[key]
        d0 = cfg.token(p.network, p.token0).decimals
        d1 = cfg.token(p.network, p.token1).decimals
        assert p.token1 == "USDC", "volume is measured on the USDC side"
        st = read_pool(c, p.address, hi)
        px = price_usdc_per_wbtc(st.sqrt_price_x96, d0, d1)
        swaps = fetch_swaps(c, p.address, lo, hi, cfg.lab["data_engine"]["logs_chunk_blocks"])
        vol1 = sum(abs(SWAP.decode(lg)["amount1"]) for lg in swaps)
        volume = Decimal(vol1) / Decimal(10) ** d1
        fees = volume * Decimal(p.fee) / Decimal(1_000_000)
        tvl = Decimal(st.balance0) / Decimal(10) ** d0 * px + Decimal(st.balance1) / Decimal(10) ** d1
        res[key] = {
            "address": p.address, "fee": p.fee, "swaps": len(swaps),
            "volume_usdc": float(volume), "lp_fees_usdc": float(fees),
            "tvl_usdc": float(tvl), "price_usdc_per_wbtc": float(px),
            "active_liquidity": str(st.liquidity), "fee_protocol": st.fee_protocol,
            "fee_apr_on_tvl": float(fees / tvl * Decimal(365) / Decimal(args.days)) if tvl else None,
        }
        r = res[key]
        print(f"{key}: swaps={r['swaps']} volume=${r['volume_usdc']:,.0f} "
              f"fees=${r['lp_fees_usdc']:,.0f} TVL=${r['tvl_usdc']:,.0f} "
              f"fee/TVL APR={r['fee_apr_on_tvl'] or 0:.2%} price=${r['price_usdc_per_wbtc']:,.0f}")

    eligible = {k: v for k, v in res.items() if v["swaps"] >= 1000}
    pool_set = eligible or res
    winner = max(pool_set, key=lambda k: pool_set[k]["lp_fees_usdc"])
    fees_sorted = sorted((v["lp_fees_usdc"] for v in res.values()), reverse=True)
    close = len(fees_sorted) > 1 and fees_sorted[1] > 0.8 * fees_sorted[0]
    print(f"primary fee tier by rule: {winner}" + ("  (CLOSE CALL — review)" if close else ""))
    out = {"window_blocks": [lo, hi], "days": args.days, "endpoint": c.name,
           "pools": res, "selected": winner, "close_call": close}
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "fee_tier_comparison.json").write_text(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
