"""Fee accrual of a liquidity range on the replayed (real) price path.

A FeeMeter listens to PoolReplay and, for every swap step and flash, credits
the part of the LP fee earned inside [tick_lower, tick_upper):

- model B, fixed price path (optimistic): per-unit growth += fee_seg * 2^128 / L
  where L is the pool's real active liquidity (MATH_CORE §6);
- model A, fixed volume (conservative): per-unit growth += fee_seg * 2^128 / (L + L_p).

fee_seg is the step's LP fee (after protocol fee) times the share of the step's
input that was traded while the price was inside the range. When the range's
ticks are initialized in the real pool, steps never straddle them and model B
reproduces the contract's feeGrowthInside exactly (validated on real positions).
Hypothetical ranges with uninitialized ticks are split pro rata: MODELED.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.backtesting import uniswap_math as um


@dataclass
class FeeMeter:
    tick_lower: int
    tick_upper: int
    liquidity: int  # L_p of the (hypothetical) position, used by model A and size checks
    growth_b0: int = 0  # Q128 per unit of liquidity
    growth_b1: int = 0
    growth_a0: int = 0
    growth_a1: int = 0
    active: bool = True
    max_share: float = 0.0  # max L_p / L seen while in range (float: analytics only)
    steps_in_range: int = 0

    def __post_init__(self):
        self.sa = um.get_sqrt_ratio_at_tick(self.tick_lower)
        self.sb = um.get_sqrt_ratio_at_tick(self.tick_upper)

    # amounts of the input token over [lo, hi] for liquidity L (rounded up like swap steps)
    @staticmethod
    def _input(zero_for_one: bool, lo: int, hi: int, liq: int) -> int:
        if zero_for_one:
            return um.get_amount0_delta(lo, hi, liq, True)
        return um.get_amount1_delta(lo, hi, liq, True)

    def _credit(self, token1: bool, fee: int, pool_liq: int) -> None:
        if fee <= 0 or pool_liq <= 0:
            return
        gb = um.mul_div(fee, um.Q128, pool_liq)
        ga = um.mul_div(fee, um.Q128, pool_liq + self.liquidity)
        if token1:
            self.growth_b1 += gb
            self.growth_a1 += ga
        else:
            self.growth_b0 += gb
            self.growth_a0 += ga
        if self.liquidity:
            self.max_share = max(self.max_share, self.liquidity / pool_liq)

    def on_swap(self, ev, zero_for_one: bool, r, fee_protocol: int) -> None:
        if not self.active:
            return
        for st in r.steps:
            lo, hi = sorted((st.sqrt_start, st.sqrt_end))
            if lo == hi:
                # price did not move (input fully taken as fee): active iff the
                # tick that held the price is inside the range
                tick = um.get_tick_at_sqrt_ratio(lo) if lo < um.MAX_SQRT_RATIO else um.MAX_TICK
                if self.tick_lower <= tick < self.tick_upper and lo != self.sb:
                    self._credit(not zero_for_one, st.fee_lp, st.liquidity)
                    self.steps_in_range += 1
                continue
            a, b = max(lo, self.sa), min(hi, self.sb)
            if a >= b:
                continue
            if a == lo and b == hi:
                fee = st.fee_lp
            else:
                part = self._input(zero_for_one, a, b, st.liquidity)
                fee = st.fee_lp * part // st.amount_in if st.amount_in else 0
            self._credit(not zero_for_one, fee, st.liquidity)
            self.steps_in_range += 1

    def on_flash(self, ev, lp0: int, lp1: int, pool_liq: int, tick: int, sqrt_price: int) -> None:
        if self.active and self.tick_lower <= tick < self.tick_upper:
            self._credit(False, lp0, pool_liq)
            self._credit(True, lp1, pool_liq)

    # ---------------------------------------------------------------- results
    def fees_b(self, liquidity: int | None = None) -> tuple[int, int]:
        liq = self.liquidity if liquidity is None else liquidity
        return (um.mul_div(self.growth_b0, liq, um.Q128) if self.growth_b0 else 0,
                um.mul_div(self.growth_b1, liq, um.Q128) if self.growth_b1 else 0)

    def fees_a(self) -> tuple[int, int]:
        return (um.mul_div(self.growth_a0, self.liquidity, um.Q128) if self.growth_a0 else 0,
                um.mul_div(self.growth_a1, self.liquidity, um.Q128) if self.growth_a1 else 0)

    def reset(self) -> None:
        self.growth_b0 = self.growth_b1 = self.growth_a0 = self.growth_a1 = 0
