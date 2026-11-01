"""Exact reconstruction of a Uniswap v3 pool's state from its event history.

Mirrors UniswapV3Pool.sol (v3-core, 0.7.6): slot0 price/tick/feeProtocol,
active liquidity, feeGrowthGlobal{0,1}, protocol fees, every tick
(liquidityGross/Net, feeGrowthOutside), the TickBitmap (so swap steps stop
at the same word boundaries as on-chain, which matters for fee rounding)
and every real position (liquidity, feeGrowthInsideLast, tokensOwed).

Swaps: an event does not say whether the swap was exact-input or exact-output,
nor its price limit. Each Swap is re-executed in the first mode that
reproduces the event bit-for-bit (amount0, amount1, sqrtPriceX96, liquidity,
tick): exact-input of the paid amount, exact-output of the received amount,
then price-targeted (limit = event price). Anything else is a data error.

Invariants (any violation raises ReplayError — no silent continuation):
- Mint/Burn amounts equal the contract's SqrtPriceMath deltas;
- every Swap matches as above;
- Collect never exceeds tokensOwed of the position;
- CollectProtocol never exceeds accumulated protocol fees.
Listeners receive every swap step and flash so that hypothetical positions
(position_sim) can be valued on the exact same path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from backend.backtesting import uniswap_math as um
from backend.backtesting.events import PoolEvent

MAX_INT256 = um.MAX_INT256


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Step:
    """One computeSwapStep on the real path (price moves sqrt_start -> sqrt_end at
    active liquidity `liquidity`). fee_lp is the LP share of the step fee in the
    input token (after protocol fee)."""
    sqrt_start: int
    sqrt_end: int
    liquidity: int
    amount_in: int
    amount_out: int
    fee_amount: int
    fee_lp: int


@dataclass
class SwapResult:
    amount0: int
    amount1: int
    sqrt_price: int
    tick: int
    liquidity: int
    fee_growth_global: int
    protocol_fee: int
    steps: list[Step]
    crossings: list[tuple[int, int, int]]  # (tick, fg0, fg1) passed to Tick.cross


@dataclass
class ReplayStats:
    events: int = 0
    swaps_exact_in: int = 0
    swaps_exact_out: int = 0
    swaps_price_target: int = 0
    swaps_zero: int = 0
    mints: int = 0
    burns: int = 0
    pokes: int = 0
    collects: int = 0
    flashes: int = 0
    tick_crossings: int = 0
    collect_residual_zero: int = 0  # collects that emptied tokensOwed exactly


@dataclass
class PoolState:
    fee: int
    tick_spacing: int
    sqrt_price: int = 0
    tick: int = 0
    fee_protocol: int = 0  # uint8: token0 = low 4 bits, token1 = high 4 bits
    liquidity: int = 0
    fee_growth_global0: int = 0
    fee_growth_global1: int = 0
    protocol_fees0: int = 0
    protocol_fees1: int = 0
    ticks: dict[int, um.TickInfo] = field(default_factory=dict)
    bitmap: dict[int, int] = field(default_factory=dict)  # wordPos -> 256-bit word
    positions: dict[tuple[str, int, int], um.PositionInfo] = field(default_factory=dict)
    initialized: bool = False
    block: int = 0
    log_index: int = -1


class PoolReplay:
    def __init__(self, fee: int, tick_spacing: int):
        self.s = PoolState(fee=fee, tick_spacing=tick_spacing)
        self.max_liquidity_per_tick = um.tick_spacing_to_max_liquidity_per_tick(tick_spacing)
        self.stats = ReplayStats()
        self.swap_listeners: list[Callable] = []
        self.flash_listeners: list[Callable] = []

    # ------------------------------------------------------------ TickBitmap
    def _flip_tick(self, tick: int) -> None:
        if tick % self.s.tick_spacing:
            raise ReplayError(f"tick {tick} not spaced")
        compressed = tick // self.s.tick_spacing
        word, bit = compressed >> 8, compressed & 0xFF
        self.s.bitmap[word] = self.s.bitmap.get(word, 0) ^ (1 << bit)

    def next_initialized_tick_within_one_word(self, tick: int, lte: bool) -> tuple[int, bool]:
        spacing = self.s.tick_spacing
        compressed = tick // spacing  # floor == Solidity's compressed-- for negatives
        if lte:
            word, bit = compressed >> 8, compressed & 0xFF
            mask = (1 << bit) - 1 + (1 << bit)
            masked = self.s.bitmap.get(word, 0) & mask
            if masked:
                return (compressed - (bit - (masked.bit_length() - 1))) * spacing, True
            return (compressed - bit) * spacing, False
        word, bit = (compressed + 1) >> 8, (compressed + 1) & 0xFF
        mask = ~((1 << bit) - 1) & ((1 << 256) - 1)
        masked = self.s.bitmap.get(word, 0) & mask
        if masked:
            lsb = (masked & -masked).bit_length() - 1
            return (compressed + 1 + (lsb - bit)) * spacing, True
        return (compressed + 1 + (255 - bit)) * spacing, False

    # --------------------------------------------------------------- actions
    def initialize(self, sqrt_price: int) -> None:
        if self.s.initialized:
            raise ReplayError("AI: already initialized")
        self.s.sqrt_price = sqrt_price
        self.s.tick = um.get_tick_at_sqrt_ratio(sqrt_price)
        self.s.initialized = True

    def _modify_position(self, owner: str, lower: int, upper: int, delta: int) -> tuple[int, int]:
        s = self.s
        if not (lower < upper and lower >= um.MIN_TICK and upper <= um.MAX_TICK):
            raise ReplayError(f"bad ticks {lower} {upper}")
        key = (owner, lower, upper)
        pos = s.positions.get(key)
        if pos is None:
            pos = s.positions[key] = um.PositionInfo()
        flipped_lower = flipped_upper = False
        if delta != 0:
            flipped_lower = um.tick_update(s.ticks, lower, s.tick, delta, s.fee_growth_global0,
                                           s.fee_growth_global1, False, self.max_liquidity_per_tick)
            flipped_upper = um.tick_update(s.ticks, upper, s.tick, delta, s.fee_growth_global0,
                                           s.fee_growth_global1, True, self.max_liquidity_per_tick)
            if flipped_lower:
                self._flip_tick(lower)
            if flipped_upper:
                self._flip_tick(upper)
        inside0, inside1 = um.get_fee_growth_inside(s.ticks, lower, upper, s.tick,
                                                    s.fee_growth_global0, s.fee_growth_global1)
        um.position_update(pos, delta, inside0, inside1)
        if delta < 0:
            if flipped_lower:
                um.tick_clear(s.ticks, lower)
            if flipped_upper:
                um.tick_clear(s.ticks, upper)
        amount0 = amount1 = 0
        if delta != 0:
            amount0, amount1 = um.mint_amounts(s.sqrt_price, s.tick, lower, upper, delta)
            if lower <= s.tick < upper:
                s.liquidity = um.add_delta(s.liquidity, delta)
        return amount0, amount1

    def mint(self, owner: str, lower: int, upper: int, amount: int) -> tuple[int, int]:
        if amount <= 0:
            raise ReplayError("mint amount must be > 0")
        return self._modify_position(owner, lower, upper, amount)

    def burn(self, owner: str, lower: int, upper: int, amount: int) -> tuple[int, int]:
        a0, a1 = self._modify_position(owner, lower, upper, -amount)
        a0, a1 = -a0, -a1
        if a0 > 0 or a1 > 0:
            pos = self.s.positions[(owner, lower, upper)]
            pos.tokens_owed0 = (pos.tokens_owed0 + a0) & um.MAX_UINT128
            pos.tokens_owed1 = (pos.tokens_owed1 + a1) & um.MAX_UINT128
        return a0, a1

    def collect(self, owner: str, lower: int, upper: int, amount0: int, amount1: int) -> None:
        pos = self.s.positions.get((owner, lower, upper))
        if pos is None and amount0 == 0 and amount1 == 0:
            return  # the contract allows collecting from an empty slot (returns 0)
        if pos is None or amount0 > pos.tokens_owed0 or amount1 > pos.tokens_owed1:
            owed = (pos.tokens_owed0, pos.tokens_owed1) if pos else None
            raise ReplayError(f"Collect {(amount0, amount1)} exceeds tokensOwed {owed} of "
                              f"{(owner, lower, upper)}")
        pos.tokens_owed0 -= amount0
        pos.tokens_owed1 -= amount1

    def collect_protocol(self, amount0: int, amount1: int) -> None:
        if amount0 > self.s.protocol_fees0 or amount1 > self.s.protocol_fees1:
            raise ReplayError(f"CollectProtocol {(amount0, amount1)} exceeds protocol fees "
                              f"{(self.s.protocol_fees0, self.s.protocol_fees1)}")
        self.s.protocol_fees0 -= amount0
        self.s.protocol_fees1 -= amount1

    def set_fee_protocol(self, fp0: int, fp1: int) -> None:
        self.s.fee_protocol = fp0 + (fp1 << 4)

    def flash(self, paid0: int, paid1: int) -> tuple[int, int]:
        s = self.s
        if s.liquidity <= 0:
            raise ReplayError("L: flash with zero liquidity")
        lp0 = lp1 = 0
        if paid0 > 0:
            fp0 = s.fee_protocol % 16
            fees0 = paid0 // fp0 if fp0 else 0
            s.protocol_fees0 = (s.protocol_fees0 + fees0) & um.MAX_UINT128
            lp0 = paid0 - fees0
            s.fee_growth_global0 = (s.fee_growth_global0 + um.mul_div(lp0, um.Q128, s.liquidity)) \
                & um.MAX_UINT256
        if paid1 > 0:
            fp1 = s.fee_protocol >> 4
            fees1 = paid1 // fp1 if fp1 else 0
            s.protocol_fees1 = (s.protocol_fees1 + fees1) & um.MAX_UINT128
            lp1 = paid1 - fees1
            s.fee_growth_global1 = (s.fee_growth_global1 + um.mul_div(lp1, um.Q128, s.liquidity)) \
                & um.MAX_UINT256
        return lp0, lp1

    def plan_swap(self, zero_for_one: bool, amount_specified: int, sqrt_price_limit: int) -> SwapResult:
        """UniswapV3Pool.swap without mutating state (dry run)."""
        s = self.s
        if amount_specified == 0:
            raise ReplayError("AS")
        if zero_for_one:
            if not (s.sqrt_price > sqrt_price_limit > um.MIN_SQRT_RATIO):
                raise ReplayError("SPL")
        elif not (s.sqrt_price < sqrt_price_limit < um.MAX_SQRT_RATIO):
            raise ReplayError("SPL")
        fee_protocol = s.fee_protocol % 16 if zero_for_one else s.fee_protocol >> 4
        remaining, calculated = amount_specified, 0
        price, tick, liq = s.sqrt_price, s.tick, s.liquidity
        fg = s.fee_growth_global0 if zero_for_one else s.fee_growth_global1
        protocol_fee = 0
        exact_input = amount_specified > 0
        steps: list[Step] = []
        crossings: list[tuple[int, int, int]] = []
        while remaining != 0 and price != sqrt_price_limit:
            start = price
            tick_next, initialized = self.next_initialized_tick_within_one_word(tick, zero_for_one)
            tick_next = max(um.MIN_TICK, min(um.MAX_TICK, tick_next))
            sqrt_next = um.get_sqrt_ratio_at_tick(tick_next)
            if (sqrt_next < sqrt_price_limit) if zero_for_one else (sqrt_next > sqrt_price_limit):
                target = sqrt_price_limit
            else:
                target = sqrt_next
            st = um.compute_swap_step(price, target, liq, remaining, s.fee)
            price = st.sqrt_ratio_next_x96
            if exact_input:
                remaining -= st.amount_in + st.fee_amount
                calculated -= st.amount_out
            else:
                remaining += st.amount_out
                calculated += st.amount_in + st.fee_amount
            fee_lp = st.fee_amount
            if fee_protocol > 0:
                delta = fee_lp // fee_protocol
                fee_lp -= delta
                protocol_fee += delta
            if liq > 0:
                fg = (fg + um.mul_div(fee_lp, um.Q128, liq)) & um.MAX_UINT256
            steps.append(Step(start, price, liq, st.amount_in, st.amount_out, st.fee_amount, fee_lp))
            if price == sqrt_next:
                if initialized:
                    g0 = fg if zero_for_one else s.fee_growth_global0
                    g1 = s.fee_growth_global1 if zero_for_one else fg
                    crossings.append((tick_next, g0, g1))
                    net = s.ticks[tick_next].liquidity_net
                    liq = um.add_delta(liq, -net if zero_for_one else net)
                tick = tick_next - 1 if zero_for_one else tick_next
            elif price != start:
                tick = um.get_tick_at_sqrt_ratio(price)
        if zero_for_one == exact_input:
            a0, a1 = amount_specified - remaining, calculated
        else:
            a0, a1 = calculated, amount_specified - remaining
        return SwapResult(a0, a1, price, tick, liq, fg, protocol_fee, steps, crossings)

    def commit_swap(self, zero_for_one: bool, r: SwapResult) -> None:
        s = self.s
        for t, g0, g1 in r.crossings:
            um.tick_cross(s.ticks, t, g0, g1)
        self.stats.tick_crossings += len(r.crossings)
        s.sqrt_price, s.tick, s.liquidity = r.sqrt_price, r.tick, r.liquidity
        if zero_for_one:
            s.fee_growth_global0 = r.fee_growth_global
            s.protocol_fees0 = (s.protocol_fees0 + r.protocol_fee) & um.MAX_UINT128
        else:
            s.fee_growth_global1 = r.fee_growth_global
            s.protocol_fees1 = (s.protocol_fees1 + r.protocol_fee) & um.MAX_UINT128

    def replay_swap(self, ev: PoolEvent) -> SwapResult | None:
        """Find the swap mode that reproduces the Swap event exactly, then apply it."""
        f, s = ev.f, self.s
        a0, a1, target = f["amount0"], f["amount1"], f["sqrtPriceX96"]
        want = (a0, a1, target, f["liquidity"], f["tick"])
        if a0 == 0 and a1 == 0 and target == s.sqrt_price:
            self.stats.swaps_zero += 1  # nothing to do (dust swap)
            return None
        zero_for_one = a0 > 0 if a0 != 0 else a1 < 0
        lim = um.MIN_SQRT_RATIO + 1 if zero_for_one else um.MAX_SQRT_RATIO - 1
        candidates = [("exact_in", a0 if zero_for_one else a1, lim),
                      ("exact_out", a1 if zero_for_one else a0, lim)]
        if target != s.sqrt_price:
            candidates.append(("price_target", MAX_INT256 // 2, target))
        tried = []
        for mode, amount, limit in candidates:
            if amount == 0 or (mode == "exact_in" and amount < 0) or (mode == "exact_out" and amount > 0):
                continue
            try:
                r = self.plan_swap(zero_for_one, amount, limit)
            except (um.MathError, ReplayError) as exc:
                tried.append((mode, str(exc)))
                continue
            got = (r.amount0, r.amount1, r.sqrt_price, r.liquidity, r.tick)
            if got == want:
                self.commit_swap(zero_for_one, r)
                setattr(self.stats, f"swaps_{mode}", getattr(self.stats, f"swaps_{mode}") + 1)
                for cb in self.swap_listeners:
                    cb(ev, zero_for_one, r, s.fee_protocol)
                return r
            tried.append((mode, got))
        raise ReplayError(f"Swap at {ev.block}:{ev.log_index} not reproducible; want {want}, "
                          f"tried {tried}")

    # ------------------------------------------------------------- dispatch
    def apply(self, ev: PoolEvent) -> None:
        s, f, n = self.s, ev.f, ev.name
        if (ev.block, ev.log_index) <= (s.block, s.log_index):
            raise ReplayError(f"events out of order at {ev.block}:{ev.log_index}")
        if n == "Initialize":
            self.initialize(f["sqrtPriceX96"])
            if s.tick != f["tick"]:
                raise ReplayError("Initialize tick mismatch")
        elif not s.initialized:
            raise ReplayError(f"{n} before Initialize")
        elif n == "Swap":
            self.replay_swap(ev)
        elif n == "Mint":
            got = self.mint(f["owner"], f["tickLower"], f["tickUpper"], f["amount"])
            if got != (f["amount0"], f["amount1"]):
                raise ReplayError(f"Mint amounts {got} != event {(f['amount0'], f['amount1'])} "
                                  f"at {ev.block}:{ev.log_index}")
            self.stats.mints += 1
        elif n == "Burn":
            got = self.burn(f["owner"], f["tickLower"], f["tickUpper"], f["amount"])
            if got != (f["amount0"], f["amount1"]):
                raise ReplayError(f"Burn amounts {got} != event at {ev.block}:{ev.log_index}")
            if f["amount"] == 0:
                self.stats.pokes += 1
            else:
                self.stats.burns += 1
        elif n == "Collect":
            self.collect(f["owner"], f["tickLower"], f["tickUpper"], f["amount0"], f["amount1"])
            pos = s.positions.get((f["owner"], f["tickLower"], f["tickUpper"]))
            self.stats.collects += 1
            self.stats.collect_residual_zero += pos is None or (
                pos.tokens_owed0 == 0 and pos.tokens_owed1 == 0)
        elif n == "Flash":
            lp0, lp1 = self.flash(f["paid0"], f["paid1"])
            self.stats.flashes += 1
            for cb in self.flash_listeners:
                cb(ev, lp0, lp1, s.liquidity, s.tick, s.sqrt_price)
        elif n == "SetFeeProtocol":
            if s.fee_protocol != f["feeProtocol0Old"] + (f["feeProtocol1Old"] << 4):
                raise ReplayError("SetFeeProtocol: old value mismatch")
            self.set_fee_protocol(f["feeProtocol0New"], f["feeProtocol1New"])
        elif n == "CollectProtocol":
            self.collect_protocol(f["amount0"], f["amount1"])
        elif n == "IncreaseObservationCardinalityNext":
            pass  # oracle only
        else:
            raise ReplayError(f"unknown event {n}")
        s.block, s.log_index = ev.block, ev.log_index
        self.stats.events += 1

    def run(self, events: Iterable[PoolEvent], until_block: int | None = None,
            on_block_end: Callable[[int], None] | None = None) -> None:
        last = None
        for ev in events:
            if until_block is not None and ev.block > until_block:
                break
            if on_block_end is not None and last is not None and ev.block != last:
                on_block_end(last)
            self.apply(ev)
            last = ev.block
        if on_block_end is not None and last is not None:
            on_block_end(last)
