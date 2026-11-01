"""Strict schemas for what the language model may return (SPEC §10).

The model proposes hypotheses and writes commentary; it never returns numbers
that are used as results. Every proposed strategy is converted into
StrategyParams and validated by Python before anything is run.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from backend.strategies.generator import DAY, StrategyParams


class StrategyProposal(BaseModel):
    """One concrete strategy configuration (all fields required, null = not used)."""
    width_pct: float = Field(description="half-width of the range, 0.10 = +-10%; ignored if full_range")
    rebalance: Literal["none", "on_exit", "periodic", "edge"]
    rebalance_delay_blocks: int = Field(description="on_exit/edge: trigger must persist N blocks (~12 s each)")
    period_days: int = Field(description="periodic: re-center every N days; 0 otherwise")
    edge_fraction: float = Field(description="edge: act after this fraction of the way to a boundary (0.3-1)")
    cooldown_hours: int = Field(description="minimum hours between two rebalances")
    ratio_token1: float | None = Field(description="target USDC value share at open (0.2-0.8) or null")
    full_range: bool
    width_vol_mult: float | None = Field(description="width = mult x weekly realized vol, or null")
    max_vol_entry: float | None = Field(description="annualized vol above which to exit/stay out, or null")
    vol_lookback_days: int = Field(description="volatility lookback in days (>= 3)")

    def to_params(self) -> StrategyParams:
        return StrategyParams(
            width_pct=self.width_pct, rebalance=self.rebalance,
            rebalance_delay_blocks=self.rebalance_delay_blocks,
            period_seconds=self.period_days * DAY, edge_fraction=self.edge_fraction,
            cooldown_seconds=self.cooldown_hours * 3600, ratio_token1=self.ratio_token1,
            full_range=self.full_range, width_vol_mult=self.width_vol_mult,
            max_vol_entry=self.max_vol_entry, vol_lookback_seconds=self.vol_lookback_days * DAY,
        ).canonical()


class HypothesisDraft(BaseModel):
    title: str
    description: str = Field(description="what pattern is claimed")
    rationale: str = Field(description="why, referring only to the provided in-sample evidence")
    strategies: list[StrategyProposal] = Field(description="1 to 4 configurations that test the claim")
    test_plan: str
    expected_outcome: str = Field(description="what would confirm or refute the claim")


class HypothesisBatch(BaseModel):
    hypotheses: list[HypothesisDraft]


class Commentary(BaseModel):
    summary: str = Field(description="what the computed results show, citing only given numbers")
    patterns: list[str]
    ineffective_configurations: list[str]
    next_directions: list[str]


def validate_proposal(p: StrategyProposal) -> tuple[StrategyParams | None, str | None]:
    """Python-side validation; returns (params, None) or (None, reason)."""
    from backend.backtesting.position_sim import SimError
    if p.vol_lookback_days < 3 and (p.width_vol_mult is not None or p.max_vol_entry is not None):
        return None, "vol_lookback_days must be >= 3 (needs >= 24 hourly returns)"
    if not p.full_range and not 0.001 <= p.width_pct <= 5:
        return None, f"width_pct {p.width_pct} outside [0.001, 5]"
    if p.rebalance == "periodic" and p.period_days < 1:
        return None, "periodic needs period_days >= 1"
    if p.rebalance_delay_blocks < 0 or p.cooldown_hours < 0:
        return None, "negative delay/cooldown"
    try:
        params = p.to_params()
        params.to_spec(1, 2, 1).validate()
    except (SimError, ValueError, TypeError) as exc:
        return None, str(exc)
    return params, None
