"""Backtest of a hypothetical LP position on the exact replayed pool history.

simulate(dataset, spec) -> PositionResult

- The pool is replayed event by event (pool_replay, EXACT). The hypothetical
  position never changes the real path; its fees come from FeeMeter with both
  bounds (A: fixed volume, conservative; B: fixed price path, optimistic).
- Decisions are taken at the end of block b using only state <= b and are
  executed against the state at the start of b+1 (= end of b): no look-ahead.
- Opening / rebalancing: the portfolio is swapped to the range's token ratio
  with a dry-run swap through the real pool state (pool fee + real price
  impact), then minted with the contract's rounding (mint rounds up, burn down).
- Costs: gas units (config, calibrated) x (baseFeePerGas of the decision block
  + priority fee) x ETH/USD (Chainlink sample at or before the block).
- Collected fees are held aside in tokens (not re-invested), so the principal
  path is identical for fee models A and B and both are reported.
- Values are in USDC (token1) units; USDC is taken as 1 USD (assumption).
Labels: prices/state EXACT; fees, costs, PnL MODELED.
"""

from __future__ import annotations

import bisect
import copy
import math
from dataclasses import asdict, dataclass, field

from backend.backtesting import uniswap_math as um
from backend.backtesting.events import PoolEvent
from backend.backtesting.fee_meter import FeeMeter
from backend.analytics.metrics import series_metrics
from backend.backtesting.market_view import MarketView
from backend.backtesting.pool_replay import PoolReplay, PoolState

LONDON_BLOCK = 12_965_000
GWEI = 10 ** 9
RUIN_DIVISOR = 100  # equity below 1% of capital -> strategy stops ("ruined")


class SimError(RuntimeError):
    pass


@dataclass(frozen=True)
class BacktestSpec:
    start_block: int
    end_block: int
    capital_token1: int                  # starting capital in token1 raw units (USDC: 1e6 = $1)
    width_pct: float                     # half-width: range = price/(1+w) .. price*(1+w)
    rebalance: str = "none"              # "none" | "on_exit" | "periodic" | "edge"
    rebalance_delay_blocks: int = 0      # trigger must persist this many blocks (on_exit, edge)
    period_seconds: int = 0              # periodic: re-center every period
    edge_fraction: float = 1.0           # edge: act when the price has moved this fraction of
                                         # the way from the open tick to the boundary
    cooldown_seconds: int = 0            # minimum time between two rebalances (all rules)
    ratio_token1: float | None = None    # target value share of token1 at open (None: symmetric)
    full_range: bool = False             # passive full-range benchmark (ignores width/ratio)
    width_vol_mult: float | None = None  # width = mult x weekly realized vol (past only)
    vol_lookback_seconds: int = 7 * 86_400
    max_vol_entry: float | None = None   # annualized vol above which the position is closed
                                         # and not (re)opened until vol falls back below
    fee_multiplier: float = 1.0          # sensitivity: scale fees (trading volume x k)
    gas_multiplier: float = 1.0          # sensitivity: scale gas prices
    gas_set: str = "gas_units"           # "gas_units" (median) | "gas_units_p90"
    priority_fee_gwei: float = 1.0
    mark_every_seconds: int = 86_400     # time-series resolution

    def validate(self) -> None:
        if self.rebalance not in RULES:
            raise SimError(f"unknown rebalance rule {self.rebalance!r}")
        if self.rebalance == "periodic" and self.period_seconds <= 0:
            raise SimError("periodic rebalance needs period_seconds > 0")
        if not 0 < self.edge_fraction <= 1:
            raise SimError("edge_fraction must be in (0, 1]")
        if self.ratio_token1 is not None and not 0 < self.ratio_token1 < 1:
            raise SimError("ratio_token1 must be in (0, 1)")
        if self.width_pct <= 0 and not self.full_range:
            raise SimError("width_pct must be > 0")
        if self.full_range and self.rebalance != "none":
            raise SimError("a full-range position never leaves its range")
        if self.width_vol_mult is not None and self.width_vol_mult <= 0:
            raise SimError("width_vol_mult must be > 0")
        if self.max_vol_entry is not None and self.max_vol_entry <= 0:
            raise SimError("max_vol_entry must be > 0")
        if self.fee_multiplier < 0 or self.gas_multiplier < 0:
            raise SimError("multipliers must be >= 0")


RULES = ("none", "on_exit", "periodic", "edge")


@dataclass
class Dataset:
    events: list[PoolEvent]
    blocks: dict[int, tuple[int, int | None]]   # block -> (timestamp, baseFeePerGas)
    eth_usd: list[list[int]]                    # [block, ts, answer_1e8, updated_at], sorted
    fee: int
    tick_spacing: int
    gas_units: dict[str, dict[str, int]]        # {"gas_units": {...}, "gas_units_p90": {...}}
    decimals0: int
    decimals1: int


@dataclass
class Action:
    block: int
    timestamp: int
    kind: str                 # open | rebalance
    tick: int
    tick_lower: int
    tick_upper: int
    liquidity: int
    swap_in: int              # signed: >0 token0 sold, <0 token1 sold (raw units)
    swap_cost_token1: int     # value lost in the swap vs mid price (fee + impact)
    gas_units: int
    gas_cost_token1: int
    ratio_token1_actual: float = 0.0  # value share of token1 actually deposited (after rounding)
    il_realized_token1: int = 0


@dataclass
class PositionResult:
    spec: dict
    metrics: dict
    actions: list[Action]
    timeseries: list[dict]
    labels: dict = field(default_factory=lambda: {
        "state": "EXACT", "fees": "MODELED", "costs": "MODELED", "pnl": "MODELED"})


def _value1(amount0: int, amount1: int, sqrt_price: int) -> int:
    """Value in token1 raw units at sqrtPriceX96 (rounded down)."""
    return um.mul_div(um.mul_div(amount0, sqrt_price, um.Q96), sqrt_price, um.Q96) + amount1


class _Sim:
    def __init__(self, ds: Dataset, spec: BacktestSpec, replay: PoolReplay,
                 market: MarketView | None = None, final_block: int | None = None):
        if spec.start_block < LONDON_BLOCK:
            raise SimError("backtests before the London fork need a gas price assumption "
                           "(no baseFeePerGas); start at or after block 12965000")
        spec.validate()
        self.ds, self.spec = ds, spec
        self.replay = replay
        self.market = market or MarketView()
        self.final_block = final_block
        self.finished = False
        self.end_mark: dict | None = None
        self.now_ts = 0
        self.waiting = False  # out of the market because of the volatility filter
        self.last_open_ts = 0
        self.open_tick = 0
        self.ruined_block: int | None = None
        self.gas = ds.gas_units[spec.gas_set]
        self.eth_blocks = [r[0] for r in ds.eth_usd]
        self.started = False
        self.x = self.y = 0            # idle tokens (token0, token1)
        self.pos: FeeMeter | None = None
        self.pos_deposit = (0, 0)      # tokens deposited into the current position
        self.hodl = (0, 0)             # tokens of the initial deposit (benchmark)
        self.fees_a = [0, 0]           # collected fees held aside, model A / B
        self.fees_b = [0, 0]
        self.gas_cost = self.swap_cost = self.il_realized = 0
        self.actions: list[Action] = []
        self.series: list[dict] = []
        self.trigger_since: int | None = None
        self.blocks_in_range = self.blocks_total = 0
        self.max_share = 0.0
        self.next_mark = 0
        self.last_block = None

    # ------------------------------------------------------------ helpers
    def _gas_cost_token1(self, block: int, units: int) -> int:
        meta = self.ds.blocks.get(block)
        if meta is None or meta[1] is None:
            raise SimError(f"no baseFeePerGas for block {block}")
        wei = int(units * (meta[1] + int(self.spec.priority_fee_gwei * GWEI)) * self.spec.gas_multiplier)
        i = bisect.bisect_right(self.eth_blocks, block) - 1
        if i < 0:
            raise SimError(f"no ETH/USD sample at or before block {block}")
        answer = self.ds.eth_usd[i][2]  # USD * 1e8 per ETH
        # wei * USD/ETH -> token1 raw units (USDC 6 decimals): wei/1e18 * answer/1e8 * 10^d1
        return wei * answer * 10 ** self.ds.decimals1 // (10 ** 18 * 10 ** 8)

    def _range(self, tick: int) -> tuple[int, int]:
        sp = self.ds.tick_spacing
        tmin, tmax = -(um.MAX_TICK // sp) * sp, (um.MAX_TICK // sp) * sp
        if self.spec.full_range:
            return tmin, tmax
        if self.spec.ratio_token1 is None:
            half = max(1, round(math.log(1 + self._width()) / math.log(1.0001)))
            lo_t, hi_t = tick - half, tick + half
        else:
            lo_t, hi_t = self._asymmetric_ticks()
        lower = math.floor(lo_t / sp) * sp
        upper = math.ceil(hi_t / sp) * sp
        if upper <= lower:
            upper = lower + sp
        return max(lower, tmin), min(upper, tmax)

    def _vol(self) -> float | None:
        return self.market.vol_annual(self.now_ts, self.spec.vol_lookback_seconds)

    def _width(self) -> float:
        if self.spec.width_vol_mult is None:
            return self.spec.width_pct
        vol = self._vol()
        if vol is None:  # not enough history yet: fall back to the fixed width
            return self.spec.width_pct
        return min(3.0, max(0.005, self.spec.width_vol_mult * vol * math.sqrt(7 / 365)))

    def _vol_blocks_entry(self) -> bool:
        """Entry needs a known volatility at or below the threshold (unknown -> wait)."""
        if self.spec.max_vol_entry is None:
            return False
        vol = self._vol()
        return vol is None or vol > self.spec.max_vol_entry

    def _vol_forces_exit(self) -> bool:
        """Exit only on a known volatility above the threshold."""
        if self.spec.max_vol_entry is None:
            return False
        vol = self._vol()
        return vol is not None and vol > self.spec.max_vol_entry

    def _asymmetric_ticks(self) -> tuple[float, float]:
        """MATH_CORE §2: range with sqrt-width (1+w) whose token1 value share at the
        current price equals ratio_token1: w1 = (s-sa) / ((s-sa) + (sb-s)*s/sb).
        Float bisection is fine here: the result is rounded to tickSpacing and the
        actual deposited ratio is recorded."""
        s = self.replay.s.sqrt_price / 2 ** 96
        k = 1 + self._width()
        target = self.spec.ratio_token1

        def w1(sa: float) -> float:
            sb = sa * k
            return (s - sa) / ((s - sa) + (sb - s) * s / sb)
        lo, hi = s / k, s  # w1(lo) = 1, w1(hi) = 0 (decreasing)
        for _ in range(200):
            mid = (lo + hi) / 2
            if w1(mid) > target:
                lo = mid
            else:
                hi = mid
        sa = (lo + hi) / 2
        # tick = log_1.0001(price) = 2 * ln(sqrtPrice) / ln(1.0001)
        return 2 * math.log(sa) / math.log(1.0001), 2 * math.log(sa * k) / math.log(1.0001)

    def _swap_to_ratio(self, lower: int, upper: int) -> tuple[int, int]:
        """Dry-run swap through the real pool so that idle tokens match the range ratio.
        Returns (signed amount sold, value lost vs mid price)."""
        s = self.replay.s
        sa, sb = um.get_sqrt_ratio_at_tick(lower), um.get_sqrt_ratio_at_tick(upper)
        r0, r1 = um.get_amounts_for_liquidity(s.sqrt_price, sa, sb, 10 ** 24)
        before = _value1(self.x, self.y, s.sqrt_price)

        def excess0(x: int, y: int) -> int:  # >0: too much token0 for the ratio
            return x * r1 - y * r0
        if excess0(self.x, self.y) == 0 or before == 0:
            return 0, 0
        zero_for_one = excess0(self.x, self.y) > 0
        lo, hi = 0, (self.x if zero_for_one else self.y)
        best = None
        for _ in range(80):
            if hi - lo <= 1:
                break
            mid = (lo + hi) // 2
            r = self.replay.plan_swap(zero_for_one, mid, um.MIN_SQRT_RATIO + 1 if zero_for_one
                                      else um.MAX_SQRT_RATIO - 1)
            nx = self.x - r.amount0  # amounts are signed from the pool's side
            ny = self.y - r.amount1
            e = excess0(nx, ny)
            if (e > 0) == zero_for_one:
                lo, best = mid, (mid, r)
            else:
                hi = mid
        if best is None or best[0] == 0:
            return 0, 0
        mid, r = best
        self.x -= r.amount0
        self.y -= r.amount1
        after = _value1(self.x, self.y, s.sqrt_price)
        return (mid if zero_for_one else -mid), max(0, before - after)

    def _open(self, block: int, kind: str, gas_units: int) -> None:
        s = self.replay.s
        lower, upper = self._range(s.tick)
        swap_in, swap_cost = self._swap_to_ratio(lower, upper)
        if swap_in:
            gas_units += self.gas["swap_for_rebalance"]
        sa, sb = um.get_sqrt_ratio_at_tick(lower), um.get_sqrt_ratio_at_tick(upper)
        liq = um.get_liquidity_for_amounts(s.sqrt_price, sa, sb, self.x, self.y)
        while liq > 0:
            a0, a1 = um.mint_amounts(s.sqrt_price, s.tick, lower, upper, liq)
            if a0 <= self.x and a1 <= self.y:
                break
            liq -= max(1, liq // 10 ** 12)
        if liq <= 0:
            raise SimError(f"cannot mint at block {block}: no liquidity from {self.x, self.y}")
        self.x -= a0
        self.y -= a1
        self.pos_deposit = (a0, a1)
        self.pos = FeeMeter(lower, upper, liq)
        self.replay.swap_listeners.append(self.pos.on_swap)
        self.replay.flash_listeners.append(self.pos.on_flash)
        gas_cost = self._gas_cost_token1(block, gas_units)
        self.gas_cost += gas_cost
        self.swap_cost += swap_cost
        deposit_value = _value1(a0, a1, s.sqrt_price)
        ratio = a1 / deposit_value if deposit_value else 0.0
        ts = self.ds.blocks[block][0]
        self.last_open_ts, self.open_tick = ts, s.tick
        self.actions.append(Action(block, ts, kind, s.tick, lower, upper, liq,
                                   swap_in, swap_cost, gas_units, gas_cost, ratio))

    def _close(self) -> int:
        """Burn + collect the current position into idle tokens; returns realized IL."""
        s, m = self.replay.s, self.pos
        b0, b1 = um.mint_amounts(s.sqrt_price, s.tick, m.tick_lower, m.tick_upper, -m.liquidity)
        out0, out1 = -b0, -b1
        fa, fb = m.fees_a(), m.fees_b()
        self.fees_a[0] += fa[0]
        self.fees_a[1] += fa[1]
        self.fees_b[0] += fb[0]
        self.fees_b[1] += fb[1]
        self.max_share = max(self.max_share, m.max_share)
        self.replay.swap_listeners.remove(m.on_swap)
        self.replay.flash_listeners.remove(m.on_flash)
        self.x += out0
        self.y += out1
        il = _value1(out0, out1, s.sqrt_price) - _value1(*self.pos_deposit, s.sqrt_price)
        self.pos = None
        return il

    def _position_now(self) -> tuple[int, int]:
        s, m = self.replay.s, self.pos
        if m is None:
            return 0, 0
        b0, b1 = um.mint_amounts(s.sqrt_price, s.tick, m.tick_lower, m.tick_upper, -m.liquidity)
        return -b0, -b1

    def _mark(self, block: int, ts: int) -> dict:
        s = self.replay.s
        p0, p1 = self._position_now()
        ua, ub = (self.pos.fees_a(), self.pos.fees_b()) if self.pos else ((0, 0), (0, 0))
        v_lp = _value1(p0 + self.x, p1 + self.y, s.sqrt_price)
        k = self.spec.fee_multiplier
        fees_a = int(_value1(self.fees_a[0] + ua[0], self.fees_a[1] + ua[1], s.sqrt_price) * k)
        fees_b = int(_value1(self.fees_b[0] + ub[0], self.fees_b[1] + ub[1], s.sqrt_price) * k)
        v_hodl = _value1(*self.hodl, s.sqrt_price)
        il_unreal = (_value1(p0, p1, s.sqrt_price) - _value1(*self.pos_deposit, s.sqrt_price)
                     if self.pos else 0)
        costs = self.gas_cost + self.swap_cost
        v0 = self.spec.capital_token1
        row = {"block": block, "timestamp": ts, "sqrt_price": s.sqrt_price, "tick": s.tick,
               "price": self.price_human(s.sqrt_price),
               "in_range": bool(self.pos and self.pos.tick_lower <= s.tick < self.pos.tick_upper),
               "value_lp": v_lp, "fees_a": fees_a, "fees_b": fees_b, "value_hodl": v_hodl,
               "il_unrealized": il_unreal, "il_realized": self.il_realized,
               "gas_cost": self.gas_cost, "swap_cost": self.swap_cost,
               "net_a": v_lp + fees_a - v0 - self.gas_cost,
               "net_b": v_lp + fees_b - v0 - self.gas_cost,
               "costs": costs}
        return row

    def price_human(self, sqrt_price: int) -> float:
        p = (sqrt_price / 2 ** 96) ** 2
        return p * 10 ** (self.ds.decimals0 - self.ds.decimals1)

    # --------------------------------------------------------- per block
    def _equity_after(self, block: int, units: int) -> int:
        """Account equity (idle tokens + collected fees A - gas paid) minus the gas of
        the next action; conservative model A."""
        sp = self.replay.s.sqrt_price
        fees = int(_value1(self.fees_a[0], self.fees_a[1], sp) * self.spec.fee_multiplier)
        return (_value1(self.x, self.y, sp) + fees - self.gas_cost
                - self._gas_cost_token1(block, units))

    def _affordable(self, block: int, units: int) -> bool:
        floor = self.spec.capital_token1 // RUIN_DIVISOR
        return (self._equity_after(block, units) >= floor
                and _value1(self.x, self.y, self.replay.s.sqrt_price) >= floor)

    def _exit(self, block: int, ts: int) -> None:
        il = self._close()
        self.il_realized += il
        units = self.gas["decrease_liquidity_and_collect"]
        cost = self._gas_cost_token1(block, units)
        self.gas_cost += cost
        self.actions.append(Action(block, ts, "exit", self.replay.s.tick, 0, 0, 0, 0, 0, units,
                                   cost, 0.0, il))

    def on_block_end(self, block: int) -> None:
        spec, s = self.spec, self.replay.s
        if self.finished or block < spec.start_block or block > spec.end_block:
            return
        ts = self.ds.blocks[block][0]
        self.now_ts = ts
        if not self.started:
            self.started = True
            self.y = spec.capital_token1
            if self._vol_blocks_entry():
                self.waiting = True
                self.hodl = (0, self.y)
            else:
                self._open(block, "open", self.gas["mint_new_position"])
                self.hodl = (self.pos_deposit[0] + self.x, self.pos_deposit[1] + self.y)
            self.next_mark = ts
        if self.last_block is not None:
            span = block - self.last_block
            self.blocks_total += span
            if self.pos and self.pos.tick_lower <= self.prev_tick < self.pos.tick_upper:
                self.blocks_in_range += span
        self.last_block, self.prev_tick = block, s.tick
        if self.ruined_block is None:
            if self.pos is None and self.waiting:
                if not self._vol_blocks_entry():
                    units = self.gas["mint_new_position"] + self.gas["swap_for_rebalance"]
                    if self._affordable(block, units):
                        self._open(block, "reenter", self.gas["mint_new_position"])
                        self.waiting = False
                    else:
                        self.ruined_block = block
            elif self.pos is not None and self._vol_forces_exit():
                self._exit(block, ts)
                self.waiting = True
                self.trigger_since = None
            elif (self.pos is not None and spec.rebalance != "none"
                  and self._should_rebalance(block, ts)):
                il = self._close()
                self.il_realized += il
                units = (self.gas["decrease_liquidity_and_collect"] + self.gas["mint_new_position"]
                         + self.gas["swap_for_rebalance"])
                if not self._affordable(block, units):
                    # the account could not pay for another rebalance: stop instead of
                    # going below zero (principal + collected fees - gas, model A)
                    self.ruined_block = block
                else:
                    self._open(block, "rebalance", self.gas["decrease_liquidity_and_collect"]
                               + self.gas["mint_new_position"])
                    self.actions[-1].il_realized_token1 = il
                self.trigger_since = None
        if ts >= self.next_mark:
            self.series.append(self._mark(block, ts))
            while self.next_mark <= ts:
                self.next_mark += spec.mark_every_seconds
        if block == self.final_block:
            self.finish(block, ts)

    def finish(self, block: int, ts: int) -> None:
        """Freeze the end-of-period mark and stop listening to the replay."""
        self.end_mark = self._mark(block, ts)
        if self.pos is not None:
            self.max_share = max(self.max_share, self.pos.max_share)
        self.detach_listeners()
        self.finished = True

    def detach_listeners(self) -> None:
        if self.pos is not None:
            for lst, cb in ((self.replay.swap_listeners, self.pos.on_swap),
                            (self.replay.flash_listeners, self.pos.on_flash)):
                if cb in lst:
                    lst.remove(cb)

    def detach(self) -> None:
        self.detach_listeners()
        self.pos = None

    def _should_rebalance(self, block: int, ts: int) -> bool:
        spec, tick, m = self.spec, self.replay.s.tick, self.pos
        if ts - self.last_open_ts < spec.cooldown_seconds:
            return False
        if spec.rebalance == "periodic":
            return ts - self.last_open_ts >= spec.period_seconds
        if spec.rebalance == "on_exit":
            triggered = not (m.tick_lower <= tick < m.tick_upper)
        else:  # edge: moved edge_fraction of the way from the open tick to a boundary
            if tick >= self.open_tick:
                triggered = tick - self.open_tick >= spec.edge_fraction * (m.tick_upper - self.open_tick)
            else:
                triggered = self.open_tick - tick >= spec.edge_fraction * (self.open_tick - m.tick_lower)
        if not triggered:
            self.trigger_since = None
            return False
        if self.trigger_since is None:
            self.trigger_since = block
        return block - self.trigger_since >= spec.rebalance_delay_blocks

    def result(self) -> PositionResult:
        if not self.started:
            raise SimError("start block not reached (no pool events in range)")
        end = self.end_mark or self._mark(self.last_block, self.ds.blocks[self.last_block][0])
        if not self.series or self.series[-1]["block"] != end["block"]:
            self.series.append(end)
        v0 = self.spec.capital_token1
        if not self.finished:
            self.max_share = max(self.max_share, self.pos.max_share if self.pos else 0.0)
        peak, mdd = -1 << 62, 0
        for r in self.series:
            eq = v0 + r["net_a"]
            peak = max(peak, eq)
            mdd = max(mdd, (peak - eq) / peak if peak > 0 else 0)
        days = max(1e-9, (end["timestamp"] - self.series[0]["timestamp"]) / 86_400)
        metrics = {
            "start_block": self.series[0]["block"], "end_block": end["block"], "days": days,
            "capital": v0, "value_lp": end["value_lp"], "value_hodl": end["value_hodl"],
            "fees_a": end["fees_a"], "fees_b": end["fees_b"],
            "gas_cost": self.gas_cost, "swap_cost": self.swap_cost,
            "il_realized": self.il_realized, "il_unrealized": end["il_unrealized"],
            "net_a": end["net_a"], "net_b": end["net_b"],
            "vs_hodl_a": end["value_lp"] + end["fees_a"] - self.gas_cost - end["value_hodl"],
            "vs_hodl_b": end["value_lp"] + end["fees_b"] - self.gas_cost - end["value_hodl"],
            "roi_a": end["net_a"] / v0, "roi_b": end["net_b"] / v0,
            "apr_a": end["net_a"] / v0 * 365 / days, "apr_b": end["net_b"] / v0 * 365 / days,
            "max_drawdown_a": mdd,
            "time_in_range": self.blocks_in_range / self.blocks_total if self.blocks_total else 1.0,
            "rebalances": sum(a.kind == "rebalance" for a in self.actions),
            "ruined": self.ruined_block is not None, "ruined_block": self.ruined_block,
            "max_liquidity_share": self.max_share,
            "size_impact": self.max_share > 0.01,
            "time_out_of_range": 1 - (self.blocks_in_range / self.blocks_total
                                      if self.blocks_total else 1.0),
            "exits": sum(a.kind == "exit" for a in self.actions),
        }
        metrics.update(series_metrics([v0 + r["net_a"] for r in self.series], days,
                                      end["net_a"], v0, mdd))
        return PositionResult(asdict(self.spec), metrics, self.actions, self.series)


def simulate_many(ds: Dataset, specs: list[BacktestSpec], *,
                  start_state: PoolState | None = None,
                  return_exceptions: bool = False) -> list:
    """Run many strategies during ONE replay pass. Hypothetical positions never
    change the real path, so they can share the pool state: each strategy only
    adds its FeeMeter listener and dry-run swaps. `start_state` (state at the end
    of some block before every start) skips replaying the earlier history."""
    replay = PoolReplay(ds.fee, ds.tick_spacing)
    if start_state is not None:
        if start_state.block >= min(sp.start_block for sp in specs):
            raise SimError("start_state is not before every strategy start")
        replay.s = copy.deepcopy(start_state)
    event_blocks = [e.block for e in ds.events]
    market = MarketView()
    if start_state is not None:
        _prefill_market(market, ds, start_state.block)

    def final_block(sp: BacktestSpec) -> int | None:
        i = bisect.bisect_right(event_blocks, sp.end_block) - 1
        return event_blocks[i] if i >= 0 else None
    sims = [_Sim(ds, sp, replay, market, final_block(sp)) for sp in specs]
    errors: dict[int, Exception] = {}
    end = max(sp.end_block for sp in specs)
    after = replay.s.block
    first = bisect.bisect_right(event_blocks, after) if start_state else 0
    # only strategies whose window contains the block are called (thousands of
    # short segments can share one pass)
    pending = sorted(range(len(sims)), key=lambda i: specs[i].start_block)
    nxt = 0
    active: list[int] = []

    def on_block_end(block: int) -> None:
        nonlocal nxt, active
        market.update(ds.blocks[block][0], replay.s.tick)
        while nxt < len(pending) and specs[pending[nxt]].start_block <= block:
            active.append(pending[nxt])
            nxt += 1
        done = False
        for i in active:
            sim = sims[i]
            try:
                sim.on_block_end(block)
            except (SimError, um.MathError) as exc:  # isolate one strategy's failure
                errors[i] = exc
                sim.detach()
            done = done or sim.finished or i in errors
        if done:
            active = [i for i in active if not sims[i].finished and i not in errors]
    replay.run(ds.events[first:], until_block=end, on_block_end=on_block_end)
    out: list = []
    for i, sim in enumerate(sims):
        if i not in errors:
            try:
                out.append(sim.result())
                continue
            except SimError as exc:
                errors[i] = exc
        if not return_exceptions:
            raise errors[i]
        out.append(errors[i])
    return out


def _prefill_market(market: MarketView, ds: Dataset, upto_block: int,
                    lookback_seconds: int = 35 * 86_400) -> None:
    """Hourly tick samples before a cached start state, so volatility rules see the
    same past as a full replay would. The pool tick only changes on Swap (and
    Initialize), so the last Swap tick of a block is that block's end tick."""
    end_ts = ds.blocks[upto_block][0] if upto_block in ds.blocks else None
    if end_ts is None:
        return
    blocks = [e.block for e in ds.events]
    i = bisect.bisect_right(blocks, upto_block)
    j = i
    while j > 0 and ds.blocks[ds.events[j - 1].block][0] >= end_ts - lookback_seconds:
        j -= 1
    tick = None
    for k in range(j - 1, -1, -1):  # tick in force at the window start
        e = ds.events[k]
        if e.name in ("Swap", "Initialize"):
            tick = e.f["tick"]
            break
    last_block = None
    for e in ds.events[j:i]:
        if last_block is not None and e.block != last_block and tick is not None:
            market.update(ds.blocks[last_block][0], tick)
        if e.name in ("Swap", "Initialize"):
            tick = e.f["tick"]
        last_block = e.block
    if last_block is not None and tick is not None:
        market.update(ds.blocks[last_block][0], tick)


def simulate(ds: Dataset, spec: BacktestSpec, *, start_state: PoolState | None = None) -> PositionResult:
    return simulate_many(ds, [spec], start_state=start_state)[0]


def state_at(ds: Dataset, block: int) -> PoolState:
    """Pool state at the end of `block` (replays the history up to it)."""
    replay = PoolReplay(ds.fee, ds.tick_spacing)
    replay.run(ds.events, until_block=block)
    return replay.s
