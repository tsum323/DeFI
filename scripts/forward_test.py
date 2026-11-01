"""Stage 9: forward tests — watch candidate strategies on data that did not exist yet.

  python -m scripts.forward_test --add-research-candidates   # register stage-7 candidates now
  python -m scripts.forward_test                             # update and print results

Registration fixes the parameters and the start block; results are MODELED and are
research, not signals. Run regularly (each session) so new blocks get included.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import select

from backend.backtesting.dataset import cached_state_at, load_dataset
from backend.core.config import load_config
from backend.database import schema as S
from backend.signal_engine.market import latest_event_block
from backend.strategies.generator import BASE_STRATEGIES, params_from_json
from backend.virtual_portfolio.portfolio import add_forward_test, mark_forward_tests

RESEARCH_CANDIDATES = ("w0.75_r1=0.4_exit_d7200_cool24h", "w1.5_r1=0.45_static")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--add-research-candidates", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    ds = load_dataset(cfg)
    engine = S.make_engine(cfg.lab["database"]["url"])
    last = latest_event_block(ds)
    capital = int(cfg.lab["signal_engine"]["capital_usd"] * 10 ** ds.decimals1)
    if args.add_research_candidates:
        with engine.connect() as c:
            found = {}
            for pj in c.execute(select(S.hypotheses.c.params_json)).scalars():
                for d in json.loads(pj):
                    p = params_from_json(json.dumps(d))
                    if p.name in RESEARCH_CANDIDATES:
                        found[p.name] = p
        for name in RESEARCH_CANDIDATES:
            if name in found:
                fid = add_forward_test(engine, found[name], last, capital,
                                       "stage-7 candidate: beat passive LP in IS and OOS (OOS contaminated)")
                print(f"registered #{fid} {name} from block {last}")
        for name in ("static_wide_50",):
            fid = add_forward_test(engine, BASE_STRATEGIES[name], last, capital, "base strategy (control)")
            print(f"registered #{fid} {BASE_STRATEGIES[name].name} from block {last} (control)")
    res = mark_forward_tests(engine, ds, lambda b: cached_state_at(ds, b), last,
                             cfg.lab["costs"]["priority_fee_gwei"])
    usd = 10 ** ds.decimals1
    if not res:
        print("no forward test has new data yet (they start at the current block)")
    for r in res:
        if "error" in r:
            print(f"#{r['id']}: error {r['error']}")
            continue
        print(f"#{r['id']} {r['strategy']}: {r['days']:.1f} days, net ${r['net_a'] / usd:,.2f}, "
              f"vs passive LP ${(r['vs_passive_a'] or 0) / usd:,.2f}, rebalances {r['rebalances']}")


if __name__ == "__main__":
    main()
