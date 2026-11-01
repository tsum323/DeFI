"""Stage 5: mass-test the strategy grid on one period of the primary pool.

  python -m scripts.run_grid --from 2025-01-01 --to 2026-11-01 [--space default|small]
                             [--mode grid|random --n 100] [--capital 10000] [--batch 40]

Stores every experiment in the DB (deduplicated by strategy + dataset + code
version), exports metrics to data_snapshots/experiments/ and prints the
in-sample leaderboard. In-sample results are NOT a verdict (stage 6 validates).
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timezone

from backend.backtesting.dataset import ROOT, block_at_or_after, load_dataset
from backend.core.config import load_config
from backend.database import schema as S
from backend.strategies.generator import DEFAULT_SPACE, SMALL_SPACE, generate
from backend.strategies.mass_test import dataset_descriptor, load_results, run_mass_test


def _ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--space", default="default", choices=["default", "small"])
    ap.add_argument("--mode", default="grid", choices=["grid", "random"])
    ap.add_argument("--n", type=int)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--gas", default="median", choices=["median", "p90"])
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    cfg = load_config()
    p = cfg.pools[cfg.primary_pool]
    t0 = time.monotonic()
    ds = load_dataset(cfg)
    start, end = block_at_or_after(ds, _ts(args.start)), block_at_or_after(ds, _ts(args.end)) - 1
    params = generate(DEFAULT_SPACE if args.space == "default" else SMALL_SPACE,
                      mode=args.mode, n=args.n, seed=args.seed)
    gas_set = "gas_units" if args.gas == "median" else "gas_units_p90"
    desc = dataset_descriptor(ds, cfg.primary_pool, p.address, start, end,
                              int(args.capital * 10 ** ds.decimals1), gas_set,
                              cfg.lab["costs"]["priority_fee_gwei"])
    engine = S.make_engine(cfg.lab["database"]["url"])
    print(f"{len(params)} strategies, blocks {start}..{end}", flush=True)
    stats = run_mass_test(engine, ds, params, desc, batch=args.batch,
                          export_dir=ROOT / "data_snapshots" / "experiments",
                          progress=lambda d, n: print(f"  {d}/{n} {time.monotonic() - t0:.0f}s",
                                                      flush=True))
    print(stats)
    rows = [r for r in load_results(engine, stats["dataset_hash"], stats["code_version"])
            if r["status"] == "done"]
    usd = 10 ** ds.decimals1
    rows.sort(key=lambda r: -r["metrics"]["net_a"])
    print(f"\nIN-SAMPLE leaderboard by net (model A, conservative), {len(rows)} strategies:")
    print(f"{'strategy':34s} {'net A':>9s} {'net B':>9s} {'vsHODL A':>9s} {'fees A':>8s} {'gas':>7s} "
          f"{'IL real':>9s} {'reb':>5s} {'inrange':>7s} {'MDD':>6s}")
    for r in rows[:args.top] + ([{"name": "...", "metrics": None}] if len(rows) > args.top else []) \
            + [r for r in rows if r["name"] in ("passive_full_range", "w0.5_static")]:
        m = r["metrics"]
        if m is None:
            print("  ...")
            continue
        print(f"{r['name']:34s} {m['net_a'] / usd:9,.0f} {m['net_b'] / usd:9,.0f} "
              f"{m['vs_hodl_a'] / usd:9,.0f} {m['fees_a'] / usd:8,.0f} {m['gas_cost'] / usd:7,.0f} "
              f"{m['il_realized'] / usd:9,.0f} {m['rebalances']:5d} {m['time_in_range']:7.1%} "
              f"{m['max_drawdown_a']:6.1%}" + ("  RUINED" if m.get("ruined") else ""))
    ruined = sum(1 for r in rows if r["metrics"].get("ruined"))
    print(f"\nruined strategies (principal < 1% of capital): {ruined}")
    print("vsHODL = result minus holding that strategy's own opening deposit (token mix differs)")


if __name__ == "__main__":
    main()
