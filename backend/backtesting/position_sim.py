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
import math
from dataclasses import asdict, dataclass, field

from backend.backtesting import uniswap_math as um
from backend.backtesting.events import PoolEvent
from backend.backtesting.fee_meter import FeeMeter
from backend.backtesting.pool_replay import PoolReplay

LONDON_BLOCK = 12_965_000
GWEI = 10 ** 9


class SimError(RuntimeError):
    pass


@dataclass(frozen=True)
class BacktestSpec:
    start_block: int
    end_block: int
    capital_token1: int                  # starting capital in token1 raw units (USDC: 1e6 = $1)
    width_pct: float                     # half-width of the range around the price, e.g. 0.05 = +-5%
    rebalance: str = "none"              # "none" | "on_exit"
    rebalance_delay_blocks: int = 0      # out of range for at least this many blocks before acting
    gas_set: str = "gas_units"           # "gas_units" (median) | "gas_units_p90"
    priority_fee_gwei: float = 1.0
    mark_every_seconds: int = 86_400     # time-series resolution


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
    def __init__(self, ds: Dataset, spec: BacktestSpec):
        if spec.start_block < LONDON_BLOCK:
            raise SimError("backtests before the London fork need a gas price assumption "
                           "(no baseFeePerGas); start at or after block 12965000")
        if spec.rebalance not in ("none", "on_exit"):
            raise SimError(f"unknown rebalance rule {spec.rebalance!r}")
        self.ds, self.spec = ds, spec
        self.replay = PoolReplay(ds.fee, ds.tick_spacing)
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
        self.out_since: int | None = None
        self.blocks_in_range = self.blocks_total = 0
        self.max_share = 0.0
        self.next_mark = 0
        self.last_block = None

    # ------------------------------------------------------------ helpers
    def _gas_cost_token1(self, block: int, units: int) -> int:
        meta = self.ds.blocks.get(block)
        if meta is None or meta[1] is None:
            raise SimError(f"no baseFeePerGas for block {block}")
        wei = units * (meta[1] + int(self.spec.priority_fee_gwei * GWEI))
        i = bisect.bisect_right(self.eth_blocks, block) - 1
        if i < 0:
            raise SimError(f"no ETH/USD sample at or before block {block}")
        answer = self.ds.eth_usd[i][2]  # USD * 1e8 per ETH
        # wei * USD/ETH -> token1 raw units (USDC 6 decimals): wei/1e18 * answer/1e8 * 10^d1
        return wei * answer * 10 ** self.ds.decimals1 // (10 ** 18 * 10 ** 8)

    def _range(self, tick: int) -> tuple[int, int]:
        sp = self.ds.tick_spacing
        half = max(1, round(math.log(1 + self.spec.width_pct) / math.log(1.0001)))
        lower = math.floor((tick - half) / sp) * sp
        upper = math.ceil((tick + half) / sp) * sp
        if upper <= lower:
            upper = lower + sp
        return max(lower, -887220), min(upper, 887220)

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
        self.actions.append(Action(block, self.ds.blocks[block][0], kind, s.tick, lower, upper, liq,
                                   swap_in, swap_cost, gas_units, gas_cost))

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
        fees_a = _value1(self.fees_a[0] + ua[0], self.fees_a[1] + ua[1], s.sqrt_price)
        fees_b = _value1(self.fees_b[0] + ub[0], self.fees_b[1] + ub[1], s.sqrt_price)
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
    def on_block_end(self, block: int) -> None:
        spec, s = self.spec, self.replay.s
        if block < spec.start_block or block > spec.end_block:
            return
        ts = self.ds.blocks[block][0]
        if not self.started:
            self.started = True
            self.y = spec.capital_token1
            self._open(block, "open", self.gas["mint_new_position"])
            self.hodl = (self.pos_deposit[0] + self.x, self.pos_deposit[1] + self.y)
            self.next_mark = ts
        if self.last_block is not None:
            span = block - self.last_block
            self.blocks_total += span
            if self.pos and self.pos.tick_lower <= self.prev_tick < self.pos.tick_upper:
                self.blocks_in_range += span
        self.last_block, self.prev_tick = block, s.tick
        in_range = self.pos.tick_lower <= s.tick < self.pos.tick_upper
        if in_range:
            self.out_since = None
        elif self.out_since is None:
            self.out_since = block
        if (spec.rebalance == "on_exit" and not in_range
                and block - self.out_since >= spec.rebalance_delay_blocks):
            il = self._close()
            self.il_realized += il
            self._open(block, "rebalance", self.gas["decrease_liquidity_and_collect"]
                       + self.gas["mint_new_position"])
            self.actions[-1].il_realized_token1 = il
            self.out_since = None
        if ts >= self.next_mark:
            self.series.append(self._mark(block, ts))
            while self.next_mark <= ts:
                self.next_mark += spec.mark_every_seconds

    def result(self) -> PositionResult:
        if not self.started:
            raise SimError("start block not reached (no pool events in range)")
        end = self._mark(self.last_block, self.ds.blocks[self.last_block][0])
        if not self.series or self.series[-1]["block"] != end["block"]:
            self.series.append(end)
        v0 = self.spec.capital_token1
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
            "max_liquidity_share": self.max_share,
            "size_impact": self.max_share > 0.01,
        }
        return PositionResult(asdict(self.spec), metrics, self.actions, self.series)


def simulate(ds: Dataset, spec: BacktestSpec) -> PositionResult:
    sim = _Sim(ds, spec)
    sim.replay.run(ds.events, until_block=spec.end_block, on_block_end=sim.on_block_end)
    return sim.result()
