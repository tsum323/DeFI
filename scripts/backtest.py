"""Run one LP backtest on the real replayed history of the primary pool.

  python -m scripts.backtest --from 2025-01-01 --to 2026-11-01 --capital 10000 \
      --width 0.10 --rebalance on_exit [--delay-blocks 0] [--gas p90] [--json out.json]

Prints the metrics (USDC); fees, costs and PnL are MODELED (both fee bounds A/B).
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone

from backend.backtesting.dataset import block_at_or_after, load_dataset
from backend.backtesting.position_sim import BacktestSpec, simulate
from backend.core.config import load_config


def _ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--capital", type=float, default=10_000.0, help="USDC")
    ap.add_argument("--width", type=float, required=True, help="half-width, 0.10 = +-10%%")
    ap.add_argument("--rebalance", default="none", choices=["none", "on_exit"])
    ap.add_argument("--delay-blocks", type=int, default=0)
    ap.add_argument("--gas", default="median", choices=["median", "p90"])
    ap.add_argument("--json")
    args = ap.parse_args()
    cfg = load_config()
    t0 = time.monotonic()
    ds = load_dataset(cfg)
    start, end = block_at_or_after(ds, _ts(args.start)), block_at_or_after(ds, _ts(args.end)) - 1
    spec = BacktestSpec(start_block=start, end_block=end,
                        capital_token1=int(args.capital * 10 ** ds.decimals1),
                        width_pct=args.width, rebalance=args.rebalance,
                        rebalance_delay_blocks=args.delay_blocks,
                        gas_set="gas_units" if args.gas == "median" else "gas_units_p90",
                        priority_fee_gwei=cfg.lab["costs"]["priority_fee_gwei"])
    res = simulate(ds, spec)
    m = res.metrics
    usd = 10 ** ds.decimals1
    print(f"blocks {m['start_block']}..{m['end_block']} ({m['days']:.0f} days), "
          f"width +-{args.width:.1%}, rebalance={args.rebalance}  [{time.monotonic() - t0:.0f}s]")
    for k in ("capital", "value_lp", "value_hodl", "fees_a", "fees_b", "gas_cost", "swap_cost",
              "il_realized", "il_unrealized", "net_a", "net_b", "vs_hodl_a", "vs_hodl_b"):
        print(f"  {k:14s} {m[k] / usd:14,.2f} USDC")
    for k in ("roi_a", "roi_b", "apr_a", "apr_b", "max_drawdown_a", "time_in_range"):
        print(f"  {k:14s} {m[k]:14.2%}")
    print(f"  rebalances     {m['rebalances']:14d}")
    print(f"  max L_p/L      {m['max_liquidity_share']:14.4%}  size_impact={m['size_impact']}")
    if args.json:
        with open(args.json, "w") as f:
            json.dump({"spec": res.spec, "metrics": m, "labels": res.labels,
                       "actions": [asdict(a) for a in res.actions],
                       "timeseries": res.timeseries}, f, indent=1)


if __name__ == "__main__":
    main()
