"""Second strategy search (Nov 2026).

Kept out of generator.py on purpose: generator.py is part of the result-code version,
and adding a search space must not invalidate the stored experiments.
"""

from __future__ import annotations

from backend.strategies.generator import DAY, StrategyParams, generate

# Second search (Nov 2026): around the near-misses of the first grid (w0.2-0.3, USDC-heavy),
# plus volatility-adaptive widths and a high-volatility exit. The space was chosen AFTER
# seeing OOS results, so these strategies count as OOS-contaminated (signal engine).
EXPLORE_SPACE = {
    "width_pct": [0.15, 0.2, 0.25, 0.3, 0.4],
    "rules": [
        {"rebalance": "none"},
        {"rebalance": "on_exit", "rebalance_delay_blocks": 7200},
        {"rebalance": "on_exit", "rebalance_delay_blocks": 7200, "cooldown_seconds": 3 * DAY},
        {"rebalance": "edge", "edge_fraction": 0.8, "cooldown_seconds": DAY},
    ],
    "ratio_token1": [0.6, 0.7, 0.8, 0.9],
}


def explore_params() -> list[StrategyParams]:
    out = generate(EXPLORE_SPACE, include_base=False)
    for mult in (2.0, 3.0, 4.0):
        for ratio in (None, 0.7, 0.8):
            for rule in ({"rebalance": "none"}, {"rebalance": "on_exit", "rebalance_delay_blocks": 7200}):
                out.append(StrategyParams(width_pct=0.2, width_vol_mult=mult, ratio_token1=ratio, **rule))
    for vmax in (0.6, 0.8):
        for w in (0.2, 0.3):
            for rule in ({"rebalance": "none"}, {"rebalance": "on_exit", "rebalance_delay_blocks": 7200}):
                out.append(StrategyParams(width_pct=w, ratio_token1=0.7, max_vol_entry=vmax, **rule))
    seen, res = set(), []
    for p in out:
        c = p.canonical()
        if c.spec_hash not in seen:
            seen.add(c.spec_hash)
            res.append(c)
    return res

