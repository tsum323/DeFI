"""Integer Uniswap v3 math, ported 1:1 from v3-core / v3-periphery (Solidity 0.7.6).

Libraries: FullMath, UnsafeMath, LiquidityMath, TickMath, SqrtPriceMath,
SwapMath, Tick (fee-growth parts), Position, LiquidityAmounts.

Rules:
- Python ints (arbitrary precision); every uint/int width and rounding
  direction of the contracts is reproduced explicitly.
- Overflow that would revert in Solidity raises MathError (message = the
  contract's revert string where it has one). Arithmetic the contracts do
  "unchecked" on purpose (fee growth deltas, tokensOwed) wraps modulo 2^N.
- No floats anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass

Q96 = 1 << 96
Q128 = 1 << 128
MAX_UINT128 = (1 << 128) - 1
MAX_UINT160 = (1 << 160) - 1
MAX_UINT256 = (1 << 256) - 1
MAX_INT128 = (1 << 127) - 1
MIN_INT128 = -(1 << 127)
MAX_INT256 = (1 << 255) - 1

MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342

FEE_DENOMINATOR = 1_000_000  # fees are in hundredths of a bip (pips)


class MathError(ArithmeticError):
    """Equivalent of a Solidity revert inside the math libraries."""


def _u256(x: int) -> int:
    if not 0 <= x <= MAX_UINT256:
        raise MathError("uint256 overflow")
    return x


def _to_uint160(x: int) -> int:
    if not 0 <= x <= MAX_UINT160:
        raise MathError("uint160 overflow")
    return x


def _to_uint128(x: int) -> int:
    if not 0 <= x <= MAX_UINT128:
        raise MathError("uint128 overflow")
    return x


def _to_int256(x: int) -> int:
    if x > MAX_INT256:
        raise MathError("int256 overflow")
    return x


# --------------------------------------------------------------------- FullMath
def mul_div(a: int, b: int, denominator: int) -> int:
    """floor(a*b/denominator); reverts if denominator == 0 or result >= 2^256."""
    _u256(a), _u256(b), _u256(denominator)
    if denominator == 0:
        raise MathError("mulDiv: denominator is zero")
    return _u256(a * b // denominator)


def mul_div_rounding_up(a: int, b: int, denominator: int) -> int:
    result = mul_div(a, b, denominator)
    if a * b % denominator:
        if result >= MAX_UINT256:
            raise MathError("mulDivRoundingUp overflow")
        result += 1
    return result


# -------------------------------------------------------------------- UnsafeMath
def div_rounding_up(x: int, y: int) -> int:
    """UnsafeMath.divRoundingUp; division by zero returns 0 in the EVM."""
    if y == 0:
        return 0
    return x // y + (1 if x % y else 0)


# ----------------------------------------------------------------- LiquidityMath
def add_delta(x: int, y: int) -> int:
    """uint128 x + int128 y with the contract's checks."""
    if y < 0:
        z = x - (-y)
        if not z < x or z < 0:
            raise MathError("LS")
    else:
        z = x + y
        if not z >= x or z > MAX_UINT128:
            raise MathError("LA")
    return z


# ---------------------------------------------------------------------- TickMath
_TICK_FACTORS = (
    (0x2, 0xfff97272373d413259a46990580e213a),
    (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
    (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0),
    (0x10, 0xffcb9843d60f6159c9db58835c926644),
    (0x20, 0xff973b41fa98c081472e6896dfb254c0),
    (0x40, 0xff2ea16466c96a3843ec78b326b52861),
    (0x80, 0xfe5dee046a99a2a811c461f1969c3053),
    (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
    (0x200, 0xf987a7253ac413176f2b074cf7815e54),
    (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
    (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9),
    (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
    (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5),
    (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
    (0x8000, 0x31be135f97d08fd981231505542fcfa6),
    (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
    (0x20000, 0x5d6af8dedb81196699c329225ee604),
    (0x40000, 0x2216e584f5fa1ea926041bedfe98),
    (0x80000, 0x48a170391f7dc42444e8fa2),
)


def get_sqrt_ratio_at_tick(tick: int) -> int:
    """sqrt(1.0001^tick) * 2^96 as a Q64.96, exactly as TickMath.getSqrtRatioAtTick."""
    abs_tick = -tick if tick < 0 else tick
    if abs_tick > MAX_TICK:
        raise MathError("T")
    ratio = 0xfffcb933bd6fad37aa2d162d1a594001 if abs_tick & 0x1 else 1 << 128
    for bit, factor in _TICK_FACTORS:
        if abs_tick & bit:
            ratio = (ratio * factor) >> 128
    if tick > 0:
        ratio = MAX_UINT256 // ratio
    # round up when dividing by 2^32 so that getTickAtSqrtRatio(result) == tick
    return (ratio >> 32) + (0 if ratio % (1 << 32) == 0 else 1)


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """Greatest tick such that getSqrtRatioAtTick(tick) <= sqrtPriceX96."""
    if not MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO:
        raise MathError("R")
    ratio = sqrt_price_x96 << 32
    msb = ratio.bit_length() - 1
    r = ratio >> (msb - 127) if msb >= 128 else ratio << (127 - msb)
    log_2 = (msb - 128) << 64
    for shift in range(63, 49, -1):
        r = (r * r) >> 127
        f = r >> 128
        log_2 |= f << shift
        r >>= f
    log_sqrt10001 = log_2 * 255738958999603826347141  # 128.128 number
    tick_low = (log_sqrt10001 - 3402992956809132418596140100660247210) >> 128
    tick_hi = (log_sqrt10001 + 291339464771989622907027621153398088495) >> 128
    if tick_low == tick_hi:
        return tick_low
    return tick_hi if get_sqrt_ratio_at_tick(tick_hi) <= sqrt_price_x96 else tick_low


# ----------------------------------------------------------------- SqrtPriceMath
def get_next_sqrt_price_from_amount0_rounding_up(sqrt_p: int, liquidity: int, amount: int,
                                                 add: bool) -> int:
    if amount == 0:
        return sqrt_p
    numerator1 = liquidity << 96
    product = (amount * sqrt_p) & MAX_UINT256  # Solidity 0.7 unchecked mul
    no_overflow = product // amount == sqrt_p
    if add:
        if no_overflow:
            denominator = (numerator1 + product) & MAX_UINT256
            if denominator >= numerator1:
                return mul_div_rounding_up(numerator1, sqrt_p, denominator)
        return div_rounding_up(numerator1, _u256(numerator1 // sqrt_p + amount))
    if not (no_overflow and numerator1 > product):
        raise MathError("getNextSqrtPriceFromAmount0RoundingUp: price underflow")
    denominator = numerator1 - product
    return _to_uint160(mul_div_rounding_up(numerator1, sqrt_p, denominator))


def get_next_sqrt_price_from_amount1_rounding_down(sqrt_p: int, liquidity: int, amount: int,
                                                   add: bool) -> int:
    if add:
        quotient = ((amount << 96) // liquidity if amount <= MAX_UINT160
                    else mul_div(amount, Q96, liquidity))
        return _to_uint160(_u256(sqrt_p + quotient))
    quotient = (div_rounding_up(amount << 96, liquidity) if amount <= MAX_UINT160
                else mul_div_rounding_up(amount, Q96, liquidity))
    if not sqrt_p > quotient:
        raise MathError("getNextSqrtPriceFromAmount1RoundingDown: price underflow")
    return sqrt_p - quotient


def get_next_sqrt_price_from_input(sqrt_p: int, liquidity: int, amount_in: int,
                                   zero_for_one: bool) -> int:
    if sqrt_p <= 0 or liquidity <= 0:
        raise MathError("getNextSqrtPriceFromInput: zero price or liquidity")
    if zero_for_one:
        return get_next_sqrt_price_from_amount0_rounding_up(sqrt_p, liquidity, amount_in, True)
    return get_next_sqrt_price_from_amount1_rounding_down(sqrt_p, liquidity, amount_in, True)


def get_next_sqrt_price_from_output(sqrt_p: int, liquidity: int, amount_out: int,
                                    zero_for_one: bool) -> int:
    if sqrt_p <= 0 or liquidity <= 0:
        raise MathError("getNextSqrtPriceFromOutput: zero price or liquidity")
    if zero_for_one:
        return get_next_sqrt_price_from_amount1_rounding_down(sqrt_p, liquidity, amount_out, False)
    return get_next_sqrt_price_from_amount0_rounding_up(sqrt_p, liquidity, amount_out, False)


def get_amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    """Token0 between two prices for unsigned liquidity (SqrtPriceMath.getAmount0Delta)."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_a <= 0:
        raise MathError("getAmount0Delta: zero price")
    numerator1 = liquidity << 96
    numerator2 = sqrt_b - sqrt_a
    if round_up:
        return div_rounding_up(mul_div_rounding_up(numerator1, numerator2, sqrt_b), sqrt_a)
    return mul_div(numerator1, numerator2, sqrt_b) // sqrt_a


def get_amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if round_up:
        return mul_div_rounding_up(liquidity, sqrt_b - sqrt_a, Q96)
    return mul_div(liquidity, sqrt_b - sqrt_a, Q96)


def get_amount0_delta_signed(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    """Signed version used by mint (L>0, rounds up) and burn (L<0, rounds down)."""
    if liquidity < 0:
        return -_to_int256(get_amount0_delta(sqrt_a, sqrt_b, -liquidity, False))
    return _to_int256(get_amount0_delta(sqrt_a, sqrt_b, liquidity, True))


def get_amount1_delta_signed(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if liquidity < 0:
        return -_to_int256(get_amount1_delta(sqrt_a, sqrt_b, -liquidity, False))
    return _to_int256(get_amount1_delta(sqrt_a, sqrt_b, liquidity, True))


# ---------------------------------------------------------------------- SwapMath
@dataclass(frozen=True)
class SwapStep:
    sqrt_ratio_next_x96: int
    amount_in: int
    amount_out: int
    fee_amount: int


def compute_swap_step(sqrt_ratio_current_x96: int, sqrt_ratio_target_x96: int, liquidity: int,
                      amount_remaining: int, fee_pips: int) -> SwapStep:
    """SwapMath.computeSwapStep. amount_remaining > 0: exact input; < 0: exact output."""
    cur, target = sqrt_ratio_current_x96, sqrt_ratio_target_x96
    zero_for_one = cur >= target
    exact_in = amount_remaining >= 0
    amount_in = amount_out = 0
    if exact_in:
        remaining_less_fee = mul_div(amount_remaining, FEE_DENOMINATOR - fee_pips, FEE_DENOMINATOR)
        amount_in = (get_amount0_delta(target, cur, liquidity, True) if zero_for_one
                     else get_amount1_delta(cur, target, liquidity, True))
        if remaining_less_fee >= amount_in:
            nxt = target
        else:
            nxt = get_next_sqrt_price_from_input(cur, liquidity, remaining_less_fee, zero_for_one)
    else:
        amount_out = (get_amount1_delta(target, cur, liquidity, False) if zero_for_one
                      else get_amount0_delta(cur, target, liquidity, False))
        if -amount_remaining >= amount_out:
            nxt = target
        else:
            nxt = get_next_sqrt_price_from_output(cur, liquidity, -amount_remaining, zero_for_one)
    reached = target == nxt
    if zero_for_one:
        if not (reached and exact_in):
            amount_in = get_amount0_delta(nxt, cur, liquidity, True)
        if not (reached and not exact_in):
            amount_out = get_amount1_delta(nxt, cur, liquidity, False)
    else:
        if not (reached and exact_in):
            amount_in = get_amount1_delta(cur, nxt, liquidity, True)
        if not (reached and not exact_in):
            amount_out = get_amount0_delta(cur, nxt, liquidity, False)
    if not exact_in and amount_out > -amount_remaining:
        amount_out = -amount_remaining
    if exact_in and nxt != target:
        fee_amount = amount_remaining - amount_in
    else:
        fee_amount = mul_div_rounding_up(amount_in, fee_pips, FEE_DENOMINATOR - fee_pips)
    return SwapStep(nxt, amount_in, amount_out, fee_amount)


# -------------------------------------------------------------------------- Tick
def tick_spacing_to_max_liquidity_per_tick(tick_spacing: int) -> int:
    # Solidity int division truncates toward zero
    min_tick = int(MIN_TICK / tick_spacing) * tick_spacing
    max_tick = int(MAX_TICK / tick_spacing) * tick_spacing
    num_ticks = (max_tick - min_tick) // tick_spacing + 1
    return MAX_UINT128 // num_ticks


@dataclass
class TickInfo:
    liquidity_gross: int = 0
    liquidity_net: int = 0
    fee_growth_outside0_x128: int = 0
    fee_growth_outside1_x128: int = 0
    initialized: bool = False


def get_fee_growth_inside(ticks: dict[int, TickInfo], tick_lower: int, tick_upper: int,
                          tick_current: int, fee_growth_global0_x128: int,
                          fee_growth_global1_x128: int) -> tuple[int, int]:
    """Tick.getFeeGrowthInside; all subtraction is modulo 2^256 like the contract."""
    lower = ticks.get(tick_lower) or TickInfo()
    upper = ticks.get(tick_upper) or TickInfo()
    g0, g1 = fee_growth_global0_x128, fee_growth_global1_x128
    if tick_current >= tick_lower:
        below0, below1 = lower.fee_growth_outside0_x128, lower.fee_growth_outside1_x128
    else:
        below0 = (g0 - lower.fee_growth_outside0_x128) & MAX_UINT256
        below1 = (g1 - lower.fee_growth_outside1_x128) & MAX_UINT256
    if tick_current < tick_upper:
        above0, above1 = upper.fee_growth_outside0_x128, upper.fee_growth_outside1_x128
    else:
        above0 = (g0 - upper.fee_growth_outside0_x128) & MAX_UINT256
        above1 = (g1 - upper.fee_growth_outside1_x128) & MAX_UINT256
    return (g0 - below0 - above0) & MAX_UINT256, (g1 - below1 - above1) & MAX_UINT256


def tick_update(ticks: dict[int, TickInfo], tick: int, tick_current: int, liquidity_delta: int,
                fee_growth_global0_x128: int, fee_growth_global1_x128: int, upper: bool,
                max_liquidity: int) -> bool:
    """Tick.update (fee-growth part; oracle accumulators are not needed for fees).
    Returns True if the tick flipped between initialized and uninitialized."""
    info = ticks.setdefault(tick, TickInfo())
    before = info.liquidity_gross
    after = add_delta(before, liquidity_delta)
    if after > max_liquidity:
        raise MathError("LO")
    flipped = (after == 0) != (before == 0)
    if before == 0:
        # by convention all growth before initialization happened below the tick
        if tick <= tick_current:
            info.fee_growth_outside0_x128 = fee_growth_global0_x128
            info.fee_growth_outside1_x128 = fee_growth_global1_x128
        info.initialized = True
    info.liquidity_gross = after
    net = info.liquidity_net - liquidity_delta if upper else info.liquidity_net + liquidity_delta
    if not MIN_INT128 <= net <= MAX_INT128:
        raise MathError("int128 overflow")
    info.liquidity_net = net
    return flipped


def tick_clear(ticks: dict[int, TickInfo], tick: int) -> None:
    ticks.pop(tick, None)


def tick_cross(ticks: dict[int, TickInfo], tick: int, fee_growth_global0_x128: int,
               fee_growth_global1_x128: int) -> int:
    """Tick.cross: flips fee growth outside, returns liquidityNet."""
    info = ticks.setdefault(tick, TickInfo())
    info.fee_growth_outside0_x128 = (fee_growth_global0_x128 - info.fee_growth_outside0_x128) & MAX_UINT256
    info.fee_growth_outside1_x128 = (fee_growth_global1_x128 - info.fee_growth_outside1_x128) & MAX_UINT256
    return info.liquidity_net


# ---------------------------------------------------------------------- Position
@dataclass
class PositionInfo:
    liquidity: int = 0
    fee_growth_inside0_last_x128: int = 0
    fee_growth_inside1_last_x128: int = 0
    tokens_owed0: int = 0
    tokens_owed1: int = 0


def position_update(pos: PositionInfo, liquidity_delta: int, fee_growth_inside0_x128: int,
                    fee_growth_inside1_x128: int) -> tuple[int, int]:
    """Position.update. Returns the fees credited by this update (tokensOwed delta)."""
    if liquidity_delta == 0:
        if pos.liquidity <= 0:
            raise MathError("NP")  # disallow pokes for 0 liquidity positions
        liquidity_next = pos.liquidity
    else:
        liquidity_next = add_delta(pos.liquidity, liquidity_delta)
    d0 = (fee_growth_inside0_x128 - pos.fee_growth_inside0_last_x128) & MAX_UINT256
    d1 = (fee_growth_inside1_x128 - pos.fee_growth_inside1_last_x128) & MAX_UINT256
    owed0 = mul_div(d0, pos.liquidity, Q128) & MAX_UINT128  # uint128() cast truncates
    owed1 = mul_div(d1, pos.liquidity, Q128) & MAX_UINT128
    if liquidity_delta != 0:
        pos.liquidity = liquidity_next
    pos.fee_growth_inside0_last_x128 = fee_growth_inside0_x128
    pos.fee_growth_inside1_last_x128 = fee_growth_inside1_x128
    if owed0 > 0 or owed1 > 0:
        # overflow is acceptable in the contract (must withdraw before 2^128)
        pos.tokens_owed0 = (pos.tokens_owed0 + owed0) & MAX_UINT128
        pos.tokens_owed1 = (pos.tokens_owed1 + owed1) & MAX_UINT128
    return owed0, owed1


# ------------------------------------------------------------- LiquidityAmounts
def get_liquidity_for_amount0(sqrt_a: int, sqrt_b: int, amount0: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    intermediate = mul_div(sqrt_a, sqrt_b, Q96)
    return _to_uint128(mul_div(amount0, intermediate, sqrt_b - sqrt_a))


def get_liquidity_for_amount1(sqrt_a: int, sqrt_b: int, amount1: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return _to_uint128(mul_div(amount1, Q96, sqrt_b - sqrt_a))


def get_liquidity_for_amounts(sqrt_x: int, sqrt_a: int, sqrt_b: int, amount0: int,
                              amount1: int) -> int:
    """Max liquidity for the given amounts at price sqrt_x (LiquidityAmounts.sol)."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_x <= sqrt_a:
        return get_liquidity_for_amount0(sqrt_a, sqrt_b, amount0)
    if sqrt_x < sqrt_b:
        return min(get_liquidity_for_amount0(sqrt_x, sqrt_b, amount0),
                   get_liquidity_for_amount1(sqrt_a, sqrt_x, amount1))
    return get_liquidity_for_amount1(sqrt_a, sqrt_b, amount1)


def get_amount0_for_liquidity(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return mul_div(liquidity << 96, sqrt_b - sqrt_a, sqrt_b) // sqrt_a


def get_amount1_for_liquidity(sqrt_a: int, sqrt_b: int, liquidity: int) -> int:
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    return mul_div(liquidity, sqrt_b - sqrt_a, Q96)


def get_amounts_for_liquidity(sqrt_x: int, sqrt_a: int, sqrt_b: int,
                              liquidity: int) -> tuple[int, int]:
    """Token amounts of a position (rounded down, as reported by the periphery)."""
    if sqrt_a > sqrt_b:
        sqrt_a, sqrt_b = sqrt_b, sqrt_a
    if sqrt_x <= sqrt_a:
        return get_amount0_for_liquidity(sqrt_a, sqrt_b, liquidity), 0
    if sqrt_x < sqrt_b:
        return (get_amount0_for_liquidity(sqrt_x, sqrt_b, liquidity),
                get_amount1_for_liquidity(sqrt_a, sqrt_x, liquidity))
    return 0, get_amount1_for_liquidity(sqrt_a, sqrt_b, liquidity)


def mint_amounts(sqrt_x: int, tick_current: int, tick_lower: int, tick_upper: int,
                 liquidity: int) -> tuple[int, int]:
    """Tokens the pool takes for +liquidity (UniswapV3Pool._modifyPosition, rounded up)
    or returns for -liquidity (negative values, rounded down)."""
    sa, sb = get_sqrt_ratio_at_tick(tick_lower), get_sqrt_ratio_at_tick(tick_upper)
    if tick_current < tick_lower:
        return get_amount0_delta_signed(sa, sb, liquidity), 0
    if tick_current < tick_upper:
        return (get_amount0_delta_signed(sqrt_x, sb, liquidity),
                get_amount1_delta_signed(sa, sqrt_x, liquidity))
    return 0, get_amount1_delta_signed(sa, sb, liquidity)
