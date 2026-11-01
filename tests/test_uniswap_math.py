"""Stage 3: tests of the integer Uniswap v3 math core.

Three kinds of evidence:
- reference vectors transcribed from Uniswap v3-core tests (TickMath.spec.ts,
  SqrtPriceMath.spec.ts, SwapMath.spec.ts);
- independent high-precision Decimal arithmetic (property checks);
- EXACT real mainnet events (tests/fixtures/real_pool_logs.json): Mint/Burn
  amounts and swap steps must be reproduced by the integer formulas.
Synthetic scenarios are marked SYNTHETIC.
"""

from __future__ import annotations

import json
import random
from decimal import ROUND_FLOOR, Decimal, getcontext
from pathlib import Path

import pytest

from backend.backtesting import uniswap_math as um

getcontext().prec = 100
FIXTURE = Path(__file__).parent / "fixtures" / "real_pool_logs.json"
E18 = 10 ** 18


def encode_price_sqrt(reserve1: int, reserve0: int) -> int:
    """v3-core test helper: floor(sqrt(reserve1/reserve0) * 2^96)."""
    v = (Decimal(reserve1) / Decimal(reserve0)).sqrt() * Decimal(2 ** 96)
    return int(v.to_integral_value(rounding=ROUND_FLOOR))


def exact_sqrt_ratio(tick: int) -> Decimal:
    return Decimal("1.0001") ** (Decimal(tick) / 2) * Decimal(2 ** 96)


# ------------------------------------------------------------------ TickMath
def test_tick_math_reference_vectors():
    assert um.get_sqrt_ratio_at_tick(um.MIN_TICK) == um.MIN_SQRT_RATIO == 4295128739
    assert um.get_sqrt_ratio_at_tick(um.MIN_TICK + 1) == 4295343490
    assert um.get_sqrt_ratio_at_tick(um.MAX_TICK - 1) == \
        1461373636630004318706518188784493106690254656249
    assert um.get_sqrt_ratio_at_tick(um.MAX_TICK) == um.MAX_SQRT_RATIO
    assert um.get_sqrt_ratio_at_tick(0) == 2 ** 96
    assert um.get_tick_at_sqrt_ratio(um.MIN_SQRT_RATIO) == um.MIN_TICK
    assert um.get_tick_at_sqrt_ratio(4295343490) == um.MIN_TICK + 1
    assert um.get_tick_at_sqrt_ratio(1461373636630004318706518188784493106690254656249) == \
        um.MAX_TICK - 1
    assert um.get_tick_at_sqrt_ratio(um.MAX_SQRT_RATIO - 1) == um.MAX_TICK - 1
    for bad in (um.MIN_TICK - 1, um.MAX_TICK + 1):
        with pytest.raises(um.MathError):
            um.get_sqrt_ratio_at_tick(bad)
    for bad in (um.MIN_SQRT_RATIO - 1, um.MAX_SQRT_RATIO):
        with pytest.raises(um.MathError):
            um.get_tick_at_sqrt_ratio(bad)


def _ticks_to_check():
    rng = random.Random(1)
    ticks = {0, 1, -1, um.MIN_TICK, um.MAX_TICK, um.MIN_TICK + 1, um.MAX_TICK - 1}
    for bit in range(20):  # every magic constant is used by +-2^bit
        ticks |= {1 << bit, -(1 << bit), (1 << bit) - 1, -(1 << bit) + 1}
    ticks |= {rng.randint(um.MIN_TICK, um.MAX_TICK) for _ in range(1500)}
    ticks |= {rng.randint(40_000, 90_000) for _ in range(500)}  # WBTC/USDC region
    return sorted(t for t in ticks if um.MIN_TICK <= t <= um.MAX_TICK)


def test_get_sqrt_ratio_at_tick_matches_high_precision():
    for t in _ticks_to_check():
        got, exact = um.get_sqrt_ratio_at_tick(t), exact_sqrt_ratio(t)
        # the contract multiplies Q128 factors of 1.0001^(-|t|/2); each truncation costs
        # <= 1 unit of that ratio, i.e. relative error ~ 1.0001^(|t|/2) / 2^128 per step
        rel = 40 * Decimal("1.0001") ** (Decimal(abs(t)) / 2) / Decimal(2 ** 128)
        assert abs(Decimal(got) - exact) <= 2 + exact * rel, t


def test_tick_sqrt_roundtrip_and_floor_property():
    for t in _ticks_to_check():
        s = um.get_sqrt_ratio_at_tick(t)
        if s < um.MAX_SQRT_RATIO:
            assert um.get_tick_at_sqrt_ratio(s) == t
        if s - 1 >= um.MIN_SQRT_RATIO:
            assert um.get_tick_at_sqrt_ratio(s - 1) == t - 1
    rng = random.Random(2)
    for _ in range(2000):  # random prices: greatest tick with ratio(tick) <= price
        s = rng.randint(um.MIN_SQRT_RATIO, um.MAX_SQRT_RATIO - 1)
        t = um.get_tick_at_sqrt_ratio(s)
        assert um.get_sqrt_ratio_at_tick(t) <= s
        assert t == um.MAX_TICK or um.get_sqrt_ratio_at_tick(t + 1) > s


# -------------------------------------------------------------- SqrtPriceMath
def test_sqrt_price_math_reference_vectors():
    p1, p121 = encode_price_sqrt(1, 1), encode_price_sqrt(121, 100)
    assert um.get_amount0_delta(p1, p121, E18, True) == 90909090909090910
    assert um.get_amount0_delta(p1, p121, E18, False) == 90909090909090909
    assert um.get_amount1_delta(p1, p121, E18, True) == 100000000000000000
    assert um.get_amount1_delta(p1, p121, E18, False) == 99999999999999999
    assert um.get_next_sqrt_price_from_input(p1, E18, E18 // 10, False) == \
        87150978765690771352898345369
    assert um.get_next_sqrt_price_from_input(p1, E18, E18 // 10, True) == \
        72025602285694852357767227579
    assert um.get_next_sqrt_price_from_output(p1, E18, E18 // 10, False) == \
        88031291682515930659493278152
    assert um.get_next_sqrt_price_from_output(p1, E18, E18 // 10, True) == \
        71305346262837903834189555302
    assert um.get_next_sqrt_price_from_input(p1, E18, 0, True) == p1
    with pytest.raises(um.MathError):
        um.get_next_sqrt_price_from_input(0, 1, E18 // 10, False)
    with pytest.raises(um.MathError):
        um.get_next_sqrt_price_from_input(1, 0, E18 // 10, True)
    # sqrtP = 256 (price 65536) with L = 1024: virtual reserves are
    # token0 = L / sqrtP = 4 and token1 = L * sqrtP = 262144 -> taking all (or more) reverts
    sp = 20282409603651670423947251286016
    assert sp == 256 * 2 ** 96
    with pytest.raises(um.MathError):
        um.get_next_sqrt_price_from_output(sp, 1024, 4, False)
    with pytest.raises(um.MathError):
        um.get_next_sqrt_price_from_output(sp, 1024, 262145, True)
    with pytest.raises(um.MathError):
        um.get_next_sqrt_price_from_output(sp, 1024, 262144, True)
    assert um.get_next_sqrt_price_from_output(sp, 1024, 262143, True) == 77371252455336267181195264


def test_amount_deltas_match_high_precision_and_round_correctly():
    rng = random.Random(3)
    for _ in range(1000):
        a, b = sorted((rng.randint(um.MIN_SQRT_RATIO, 2 ** 130), rng.randint(um.MIN_SQRT_RATIO, 2 ** 130)))
        if a == b:
            continue
        liq = rng.randint(1, um.MAX_UINT128)
        x0 = Decimal(liq) * Decimal(2 ** 96) * (Decimal(b) - Decimal(a)) / (Decimal(a) * Decimal(b))
        x1 = Decimal(liq) * (Decimal(b) - Decimal(a)) / Decimal(2 ** 96)
        dn0, up0 = um.get_amount0_delta(a, b, liq, False), um.get_amount0_delta(a, b, liq, True)
        dn1, up1 = um.get_amount1_delta(a, b, liq, False), um.get_amount1_delta(a, b, liq, True)
        assert dn0 <= x0 <= up0 and up0 - dn0 <= 2
        assert dn1 <= x1 <= up1 and up1 - dn1 <= 1
        assert um.get_amount0_delta(b, a, liq, True) == up0  # argument order irrelevant


# ------------------------------------------------------------------- SwapMath
def test_swap_math_reference_vectors():
    price, target = encode_price_sqrt(1, 1), encode_price_sqrt(101, 100)
    # exact amount in that gets capped at price target in one for zero
    st = um.compute_swap_step(price, target, 2 * E18, E18, 600)
    assert (st.amount_in, st.fee_amount, st.amount_out) == \
        (9975124224178055, 5988667735148, 9925619580021728)
    assert st.amount_in + st.fee_amount < E18 and st.sqrt_ratio_next_x96 == target
    # exact amount out that gets capped at price target in one for zero
    st = um.compute_swap_step(price, target, 2 * E18, -E18, 600)
    assert (st.amount_in, st.fee_amount, st.amount_out) == \
        (9975124224178055, 5988667735148, 9925619580021728)
    assert st.sqrt_ratio_next_x96 == target
    # exact amount in that is fully spent in one for zero
    target = encode_price_sqrt(1000, 100)
    st = um.compute_swap_step(price, target, 2 * E18, E18, 600)
    assert (st.amount_in, st.fee_amount, st.amount_out) == \
        (999400000000000000, 600000000000000, 666399946655997866)
    assert st.amount_in + st.fee_amount == E18 and st.sqrt_ratio_next_x96 < target
    # exact amount out that is fully received in one for zero
    target = encode_price_sqrt(10000, 100)
    st = um.compute_swap_step(price, target, 2 * E18, -E18, 600)
    assert (st.amount_in, st.fee_amount, st.amount_out) == \
        (2000000000000000000, 1200720432259356, E18)
    # amount out is capped at the desired amount out
    st = um.compute_swap_step(417332158212080721273783715441582, 1452870262520218020823638996,
                              159344665391607089467575320103, -1, 1)
    assert (st.amount_in, st.fee_amount, st.amount_out, st.sqrt_ratio_next_x96) == \
        (1, 1, 1, 417332158212080721273783715441581)
    # target price of 1 uses partial input amount
    st = um.compute_swap_step(2, 1, 1, 3915081100057732413702495386755767, 1)
    assert (st.amount_in, st.fee_amount, st.amount_out, st.sqrt_ratio_next_x96) == \
        (39614081257132168796771975168, 39614120871253040049813, 0, 1)
    # entire input amount taken as fee
    st = um.compute_swap_step(2413, 79887613182836312, 1985041575832132834610021537970, 10, 1872)
    assert (st.amount_in, st.fee_amount, st.amount_out, st.sqrt_ratio_next_x96) == (0, 10, 0, 2413)
    # intermediate insufficient liquidity, zero for one exact output
    sp = 20282409603651670423947251286016
    st = um.compute_swap_step(sp, sp * 11 // 10, 1024, -4, 3000)
    assert (st.amount_out, st.sqrt_ratio_next_x96, st.amount_in, st.fee_amount) == \
        (0, sp * 11 // 10, 26215, 79)
    # intermediate insufficient liquidity, one for zero exact output
    st = um.compute_swap_step(sp, sp * 9 // 10, 1024, -263000, 3000)
    assert (st.amount_out, st.sqrt_ratio_next_x96, st.amount_in, st.fee_amount) == \
        (26214, sp * 9 // 10, 1, 1)


def test_swap_step_properties():
    # SYNTHETIC random steps: conservation and fee bounds
    rng = random.Random(4)
    for _ in range(1500):
        cur = rng.randint(2 ** 80, 2 ** 110)
        target = cur + rng.choice([-1, 1]) * rng.randint(1, cur // 10)
        liq = rng.randint(10 ** 6, 10 ** 24)
        amt = rng.choice([1, -1]) * rng.randint(1, 10 ** 22)
        fee = rng.choice([100, 500, 3000, 10000])
        st = um.compute_swap_step(cur, target, liq, amt, fee)
        lo, hi = sorted((cur, target))
        assert lo <= st.sqrt_ratio_next_x96 <= hi
        if amt > 0:
            assert st.amount_in + st.fee_amount <= amt
        else:
            assert st.amount_out <= -amt
        if st.sqrt_ratio_next_x96 == target or amt < 0:
            assert st.fee_amount == um.mul_div_rounding_up(st.amount_in, fee, 10 ** 6 - fee)


# --------------------------------------------------------- LiquidityAmounts
def test_liquidity_amounts_roundtrip_never_exceeds_inputs():
    rng = random.Random(5)
    for _ in range(1500):
        lo, hi = sorted(rng.sample(range(40_000, 90_000), 2))
        sa, sb = um.get_sqrt_ratio_at_tick(lo), um.get_sqrt_ratio_at_tick(hi)
        tx = rng.randint(lo - 2000, hi + 2000)
        sx = um.get_sqrt_ratio_at_tick(tx)
        a0, a1 = rng.randint(0, 10 ** 12), rng.randint(0, 10 ** 15)
        liq = um.get_liquidity_for_amounts(sx, sa, sb, a0, a1)
        b0, b1 = um.get_amounts_for_liquidity(sx, sa, sb, liq)
        assert b0 <= a0 and b1 <= a1
        # a mint charges rounded-up amounts; it fits within the inputs up to 1 wei per side
        m0, m1 = um.mint_amounts(sx, tx, lo, hi, liq) if liq else (0, 0)
        assert m0 <= a0 + 1 and m1 <= a1 + 1
        # one more unit of liquidity would need more than we have on some side
        if liq:
            c0, c1 = um.get_amounts_for_liquidity(sx, sa, sb, liq + 1)
            assert c0 >= b0 and c1 >= b1
        assert um.get_liquidity_for_amounts(sx, sa, sb, a0 * 2, a1 * 2) >= liq  # monotone


def test_mint_rounds_up_burn_rounds_down():
    rng = random.Random(6)
    for _ in range(1000):
        lo, hi = sorted(rng.sample(range(-5000, 5000), 2))
        tick = rng.randint(lo - 100, hi + 100)
        sx = um.get_sqrt_ratio_at_tick(tick) + rng.randint(0, 1000)
        tick = um.get_tick_at_sqrt_ratio(sx)
        liq = rng.randint(1, 10 ** 20)
        m0, m1 = um.mint_amounts(sx, tick, lo, hi, liq)
        b0, b1 = um.mint_amounts(sx, tick, lo, hi, -liq)
        assert m0 >= -b0 >= 0 and m1 >= -b1 >= 0
        assert m0 + b0 <= 1 and m1 + b1 <= 1


# --------------------------------------------------- Tick / Position (fees)
def test_max_liquidity_per_tick_reference():
    # values from v3-core Tick.spec.ts
    assert um.tick_spacing_to_max_liquidity_per_tick(10) == 1917569901783203986719870431555990
    assert um.tick_spacing_to_max_liquidity_per_tick(60) == 11505743598341114571880798222544994
    assert um.tick_spacing_to_max_liquidity_per_tick(200) == 38350317471085141830651933667504588
    assert um.tick_spacing_to_max_liquidity_per_tick(887272) == um.MAX_UINT128 // 3
    assert um.tick_spacing_to_max_liquidity_per_tick(2302) == 441351967472034323558203122479595605


def test_fee_growth_inside_reference_cases():
    # cases from v3-core Tick.spec.ts #getFeeGrowthInside
    T = um.TickInfo
    assert um.get_fee_growth_inside({}, -2, 2, 0, 15, 15) == (15, 15)
    assert um.get_fee_growth_inside({}, -2, 2, 4, 15, 15) == (0, 0)
    assert um.get_fee_growth_inside({}, -2, 2, -4, 15, 15) == (0, 0)
    ticks = {2: T(fee_growth_outside0_x128=2, fee_growth_outside1_x128=3, initialized=True)}
    assert um.get_fee_growth_inside(ticks, -2, 2, 0, 15, 15) == (13, 12)
    ticks = {-2: T(fee_growth_outside0_x128=2, fee_growth_outside1_x128=3, initialized=True)}
    assert um.get_fee_growth_inside(ticks, -2, 2, 0, 15, 15) == (13, 12)
    ticks = {-2: T(fee_growth_outside0_x128=2, fee_growth_outside1_x128=3, initialized=True),
             2: T(fee_growth_outside0_x128=4, fee_growth_outside1_x128=1, initialized=True)}
    assert um.get_fee_growth_inside(ticks, -2, 2, 0, 15, 15) == (9, 11)
    ticks = {-2: T(fee_growth_outside0_x128=um.MAX_UINT256 - 3, fee_growth_outside1_x128=um.MAX_UINT256 - 2,
                   initialized=True),
             2: T(fee_growth_outside0_x128=3, fee_growth_outside1_x128=5, initialized=True)}
    assert um.get_fee_growth_inside(ticks, -2, 2, 0, 15, 15) == (16, 13)


def test_tick_update_and_cross_semantics():
    # SYNTHETIC: initialization convention and flipping, as in Tick.sol
    ticks: dict[int, um.TickInfo] = {}
    maxl = um.tick_spacing_to_max_liquidity_per_tick(60)
    assert um.tick_update(ticks, 0, 1, 3, 10, 20, False, maxl) is True
    assert (ticks[0].fee_growth_outside0_x128, ticks[0].fee_growth_outside1_x128) == (10, 20)
    assert um.tick_update(ticks, 60, 1, 5, 10, 20, True, maxl) is True
    assert ticks[60].fee_growth_outside0_x128 == 0  # tick above current: growth assumed below
    assert ticks[60].liquidity_net == -5 and ticks[0].liquidity_net == 3
    assert um.tick_update(ticks, 0, 1, 1, 50, 50, False, maxl) is False
    assert ticks[0].liquidity_gross == 4 and ticks[0].fee_growth_outside0_x128 == 10
    assert um.tick_update(ticks, 0, 1, -4, 50, 50, False, maxl) is True
    with pytest.raises(um.MathError):
        um.tick_update(ticks, 120, 1, maxl + 1, 0, 0, False, maxl)
    with pytest.raises(um.MathError):
        um.tick_update(ticks, 180, 1, -1, 0, 0, False, maxl)
    assert um.tick_cross(ticks, 60, 100, 200) == -5
    assert (ticks[60].fee_growth_outside0_x128, ticks[60].fee_growth_outside1_x128) == (100, 200)
    um.tick_cross(ticks, 60, 130, 260)
    assert (ticks[60].fee_growth_outside0_x128, ticks[60].fee_growth_outside1_x128) == (30, 60)


def test_position_fees_scenario():
    # SYNTHETIC end-to-end: a position earns exactly its share of fee growth
    # while the price is inside its range, nothing while outside.
    ticks: dict[int, um.TickInfo] = {}
    maxl = um.tick_spacing_to_max_liquidity_per_tick(60)
    g0 = g1 = 0
    cur = 30
    pos = um.PositionInfo()
    liq = 10 ** 18
    um.tick_update(ticks, 0, cur, liq, g0, g1, False, maxl)
    um.tick_update(ticks, 60, cur, liq, g0, g1, True, maxl)
    um.position_update(pos, liq, *um.get_fee_growth_inside(ticks, 0, 60, cur, g0, g1))
    # 1000 token0 of LP fees accrue while in range: growth += fee * 2^128 / L
    g0 += 1000 * um.Q128 // liq
    # price moves above the range -> cross tick 60
    um.tick_cross(ticks, 60, g0, g1)
    cur = 70
    # fees accrue while out of range: not ours
    g0 += 5000 * um.Q128 // liq
    g1 += 7000 * um.Q128 // liq
    owed = um.position_update(pos, 0, *um.get_fee_growth_inside(ticks, 0, 60, cur, g0, g1))
    assert owed == (999, 0)  # floor of 1000 (growth itself was floored)
    assert (pos.tokens_owed0, pos.tokens_owed1) == (999, 0)
    with pytest.raises(um.MathError):
        um.position_update(um.PositionInfo(), 0, 0, 0)  # "NP": poke of empty position


def test_position_update_wraps_like_solidity():
    # fee growth is a modular counter: inside can wrap past 2^256
    pos = um.PositionInfo(liquidity=um.Q128, fee_growth_inside0_last_x128=um.MAX_UINT256 - 9)
    assert um.position_update(pos, 0, 5, 0) == (15, 0)
    assert um.add_delta(5, -5) == 0
    with pytest.raises(um.MathError):
        um.add_delta(5, -6)
    with pytest.raises(um.MathError):
        um.add_delta(um.MAX_UINT128, 1)


def test_full_math():
    assert um.mul_div(um.Q128, 50 * um.Q128 // 100, 150 * um.Q128 // 100) == um.Q128 // 3
    assert um.mul_div_rounding_up(um.Q128, 1000 * um.Q128, 3000 * um.Q128) == um.Q128 // 3 + 1
    with pytest.raises(um.MathError):
        um.mul_div(um.Q128, 5, 0)
    with pytest.raises(um.MathError):
        um.mul_div(um.MAX_UINT256, um.MAX_UINT256, um.MAX_UINT256 - 1)
    with pytest.raises(um.MathError):
        um.mul_div_rounding_up(535006138814359, 432862656469423142931042426214547535783388063929571229938474969,
                               2)
    assert um.div_rounding_up(7, 2) == 4 and um.div_rounding_up(6, 2) == 3


# ------------------------------------------------------- real mainnet data
@pytest.mark.skipif(not FIXTURE.exists(), reason="no real fixtures")
def test_real_mint_and_burn_amounts_are_reproduced_exactly():
    data = json.loads(FIXTURE.read_text())
    assert data["mint_burn_cases"]
    for c in data["mint_burn_cases"]:
        liq = int(c["liquidity"]) * (1 if c["event"] == "Mint" else -1)
        d0, d1 = um.mint_amounts(int(c["sqrt_price"]), c["tick"], c["tick_lower"], c["tick_upper"], liq)
        sign = 1 if c["event"] == "Mint" else -1
        assert (d0, d1) == (sign * int(c["amount0"]), sign * int(c["amount1"])), c


@pytest.mark.skipif(not FIXTURE.exists(), reason="no real fixtures")
def test_real_swaps_reproduced_by_swap_step():
    data = json.loads(FIXTURE.read_text())
    assert data["swap_pairs"]
    exact = 0
    for c in data["swap_pairs"]:
        before, after, liq = int(c["sqrt_before"]), int(c["sqrt_after"]), int(c["liquidity"])
        st = um.compute_swap_step(before, after, liq, 1 << 250, data["fee"])
        assert st.sqrt_ratio_next_x96 == after
        a0, a1 = int(c["amount0"]), int(c["amount1"])
        ein, eout = (a0, -a1) if after < before else (a1, -a0)
        assert abs(ein - st.amount_in - st.fee_amount) <= 2 and abs(eout - st.amount_out) <= 2, c
        exact += ein == st.amount_in + st.fee_amount and eout == st.amount_out
    assert exact >= len(data["swap_pairs"]) * 0.9
