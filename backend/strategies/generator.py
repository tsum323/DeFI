"""Strategy parameter space and generator (stage 5).

A StrategyParams is the declarative, period-independent part of a backtest:
range rule (width, target token ratio, full range) and rebalance rule. Each
has a stable hash (canonical JSON), so the same strategy is never tested
twice on the same dataset and code version.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
from dataclasses import asdict, dataclass, fields

from backend.backtesting.position_sim import BacktestSpec

DAY = 86_400


@dataclass(frozen=True)
class StrategyParams:
    width_pct: float = 0.1
    rebalance: str = "none"
    rebalance_delay_blocks: int = 0
    period_seconds: int = 0
    edge_fraction: float = 1.0
    cooldown_seconds: int = 0
    ratio_token1: float | None = None
    full_range: bool = False

    def canonical(self) -> "StrategyParams":
        """Drop parameters that the rule ignores, so equivalent strategies hash equal."""
        d = asdict(self)
        if self.full_range:
            return StrategyParams(full_range=True, width_pct=0.0)
        if self.rebalance != "periodic":
            d["period_seconds"] = 0
        if self.rebalance != "edge":
            d["edge_fraction"] = 1.0
        if self.rebalance not in ("on_exit", "edge"):
            d["rebalance_delay_blocks"] = 0
        if self.rebalance == "none":
            d["cooldown_seconds"] = 0
        return StrategyParams(**d)

    @property
    def spec_hash(self) -> str:
        blob = json.dumps(asdict(self.canonical()), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()

    @property
    def name(self) -> str:
        c = self.canonical()
        if c.full_range:
            return "passive_full_range"
        parts = [f"w{c.width_pct:g}"]
        if c.ratio_token1 is not None:
            parts.append(f"r1={c.ratio_token1:g}")
        if c.rebalance == "none":
            parts.append("static")
        elif c.rebalance == "on_exit":
            parts.append(f"exit_d{c.rebalance_delay_blocks}")
        elif c.rebalance == "periodic":
            parts.append(f"every_{c.period_seconds // DAY}d")
        else:
            parts.append(f"edge{c.edge_fraction:g}_d{c.rebalance_delay_blocks}")
        if c.cooldown_seconds:
            parts.append(f"cool{c.cooldown_seconds // 3600}h")
        return "_".join(parts)

    def to_spec(self, start_block: int, end_block: int, capital_token1: int,
                gas_set: str = "gas_units", priority_fee_gwei: float = 1.0) -> BacktestSpec:
        c = asdict(self.canonical())
        return BacktestSpec(start_block=start_block, end_block=end_block,
                            capital_token1=capital_token1, gas_set=gas_set,
                            priority_fee_gwei=priority_fee_gwei, **c)


# Base strategies (SPEC §5): named presets that every study includes.
BASE_STRATEGIES: dict[str, StrategyParams] = {
    "passive_full_range": StrategyParams(full_range=True, width_pct=0.0),
    "static_wide_50": StrategyParams(width_pct=0.5),
    "static_10": StrategyParams(width_pct=0.1),
    "exit_rebalance_10": StrategyParams(width_pct=0.1, rebalance="on_exit"),
    "exit_rebalance_10_delay_1d": StrategyParams(width_pct=0.1, rebalance="on_exit",
                                                 rebalance_delay_blocks=7200),
    "periodic_weekly_10": StrategyParams(width_pct=0.1, rebalance="periodic", period_seconds=7 * DAY),
    "edge80_10": StrategyParams(width_pct=0.1, rebalance="edge", edge_fraction=0.8),
}

# Default search space (grid): ranges x rebalance rules x token ratio.
DEFAULT_SPACE = {
    "width_pct": [0.01, 0.02, 0.05, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0, 2.0],
    "rules": [
        {"rebalance": "none"},
        {"rebalance": "on_exit", "rebalance_delay_blocks": 0},
        {"rebalance": "on_exit", "rebalance_delay_blocks": 300},     # ~1 hour
        {"rebalance": "on_exit", "rebalance_delay_blocks": 7200},    # ~1 day
        {"rebalance": "periodic", "period_seconds": DAY},
        {"rebalance": "periodic", "period_seconds": 7 * DAY},
        {"rebalance": "periodic", "period_seconds": 30 * DAY},
        {"rebalance": "edge", "edge_fraction": 0.5},
        {"rebalance": "edge", "edge_fraction": 0.8},
    ],
    "ratio_token1": [None, 0.3, 0.7],
}

SMALL_SPACE = {
    "width_pct": [0.05, 0.1, 0.3],
    "rules": [{"rebalance": "none"}, {"rebalance": "on_exit"}],
    "ratio_token1": [None],
}


def generate(space: dict = DEFAULT_SPACE, *, mode: str = "grid", n: int | None = None,
             seed: int = 0, include_base: bool = True) -> list[StrategyParams]:
    """All (grid) or `n` random combinations, de-duplicated by spec hash."""
    combos = [StrategyParams(width_pct=w, ratio_token1=r, **rule)
              for w, rule, r in itertools.product(space["width_pct"], space["rules"],
                                                  space["ratio_token1"])]
    if mode == "random":
        combos = random.Random(seed).sample(combos, min(n or len(combos), len(combos)))
    elif mode != "grid":
        raise ValueError(f"unknown mode {mode!r}")
    out, seen = [], set()
    for p in (list(BASE_STRATEGIES.values()) if include_base else []) + combos:
        c = p.canonical()
        if c.spec_hash not in seen:
            seen.add(c.spec_hash)
            out.append(c)
    return out


def params_from_json(blob: str) -> StrategyParams:
    d = json.loads(blob)
    names = {f.name for f in fields(StrategyParams)}
    return StrategyParams(**{k: v for k, v in d.items() if k in names})
