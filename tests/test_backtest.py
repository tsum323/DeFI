"""Stage 4: backtesting engine tests on a SYNTHETIC pool history.

The synthetic history is generated with the replay engine itself (so every
Swap event is internally consistent) and is marked SYNTHETIC; it is never used
for research results. Real-history validation lives in scripts/validate_*.py.
"""

from __future__ import annotations

import random

import pytest

from backend.backtesting import uniswap_math as um
from backend.backtesting.events import PoolEvent
from backend.backtesting.pool_replay import PoolReplay
from backend.backtesting.position_sim import BacktestSpec, Dataset, SimError, simulate

B0 = 13_000_000
T0 = 1_630_000_000
LP = "0x" + "11" * 20
GAS = {"gas_units": {"mint_new_position": 436_000, "increase_liquidity": 200_000,
                     "decrease_liquidity_and_collect": 200_000, "swap_for_rebalance": 127_000},
       "gas_units_p90": {"mint_new_position": 488_000, "increase_liquidity": 245_000,
                         "decrease_liquidity_and_collect": 222_000, "swap_for_rebalance": 155_000}}


def synthetic_history(n_swaps: int = 400, seed: int = 1, drift: float = 0.0, liq: int = 10 ** 15,
                      lp_range=(-12000, 12000)):
    """SYNTHETIC: one big LP position + random swaps; returns (events, lp_fees_inside)."""
    rng = random.Random(seed)
    gen = PoolReplay(3000, 60)
    events, block = [], B0

    def emit(name, f):
        nonlocal block
        events.append(PoolEvent(block, 0, name, "0x" + "00" * 32, f))
        block += 1
    gen.initialize(um.get_sqrt_ratio_at_tick(0))
    emit("Initialize", {"sqrtPriceX96": gen.s.sqrt_price, "tick": gen.s.tick})
    for ev in events:
        gen.s.block, gen.s.log_index = ev.block, ev.log_index
    a0, a1 = gen.mint(LP, lp_range[0], lp_range[1], liq)
    emit("Mint", {"sender": LP, "owner": LP, "tickLower": lp_range[0], "tickUpper": lp_range[1],
                  "amount": liq, "amount0": a0, "amount1": a1})
    for _ in range(n_swaps):
        zero_for_one = rng.random() < 0.5 - drift
        amount = rng.randint(10 ** 10, 10 ** 13)
        lim = um.MIN_SQRT_RATIO + 1 if zero_for_one else um.MAX_SQRT_RATIO - 1
        r = gen.plan_swap(zero_for_one, amount, lim)
        if any(abs(t) > 11000 for t in (r.tick,)):
            continue
        gen.commit_swap(zero_for_one, r)
        emit("Swap", {"sender": LP, "recipient": LP, "amount0": r.amount0, "amount1": r.amount1,
                      "sqrtPriceX96": r.sqrt_price, "liquidity": r.liquidity, "tick": r.tick})
    blocks = {b: (T0 + 12 * (b - B0), 20 * 10 ** 9) for b in range(B0, block + 1)}
    return events, blocks


ETH_ANSWER = 1  # SYNTHETIC ETH/USD answer (1e8 = $1) keeping gas proportionate to the tiny capital


def dataset(events, blocks, eth_answer=ETH_ANSWER):
    return Dataset(events=events, blocks=blocks, eth_usd=[[B0 - 1, T0 - 12, eth_answer, T0 - 12]],
                   fee=3000, tick_spacing=60, gas_units=GAS, decimals0=18, decimals1=18)


def spec(**kw):
    base = dict(start_block=B0 + 5, end_block=B0 + 10 ** 6, capital_token1=10 ** 12,
                width_pct=0.5, mark_every_seconds=600)
    base.update(kw)
    return BacktestSpec(**base)


def test_accounting_identities_and_fee_bounds():
    events, blocks = synthetic_history()
    res = simulate(dataset(events, blocks), spec())
    m, first = res.metrics, res.timeseries[0]
    # at open, LP value equals the benchmark up to mint/burn rounding
    assert abs(first["value_lp"] - first["value_hodl"]) <= 4
    # capital = value after the opening swap + its cost (rounding of the dry run)
    assert abs(first["value_hodl"] + res.actions[0].swap_cost_token1 - m["capital"]) <= 4
    for r in res.timeseries:
        assert r["net_a"] == r["value_lp"] + r["fees_a"] - m["capital"] - r["gas_cost"]
        assert r["net_b"] == r["value_lp"] + r["fees_b"] - m["capital"] - r["gas_cost"]
    assert 0 < m["fees_a"] <= m["fees_b"]
    assert m["rebalances"] == 0 and m["time_in_range"] == 1.0
    assert m["gas_cost"] == res.actions[0].gas_cost_token1 > 0
    # gas = (436k mint + 127k swap) * (20 + 1 gwei) * ETH/USD, in 18-decimal token1
    assert res.actions[0].gas_units == 436_000 + 127_000
    assert m["gas_cost"] == (436_000 + 127_000) * 21 * 10 ** 9 * ETH_ANSWER * 10 ** 18 // 10 ** 26
    assert m["gas_cost"] < m["capital"] // 100


def test_fee_model_b_equals_share_of_pool_fees():
    # SYNTHETIC: our range equals the only LP's range, so model B must pay exactly
    # L_p / L of every LP fee after we open.
    events, blocks = synthetic_history()
    ds = dataset(events, blocks)
    res = simulate(ds, spec(width_pct=10.0))  # clamps far outside; covers the LP range
    a = res.actions[0]
    gen = PoolReplay(3000, 60)
    total0 = total1 = n_steps = 0
    for ev in events:
        if ev.block > a.block and ev.name == "Swap":
            zf1 = ev.f["amount0"] > 0
            r = gen.plan_swap(zf1, ev.f["amount0"] if zf1 else ev.f["amount1"],
                              um.MIN_SQRT_RATIO + 1 if zf1 else um.MAX_SQRT_RATIO - 1)
            for st in r.steps:
                n_steps += 1
                if zf1:
                    total0 += st.fee_lp * a.liquidity // st.liquidity
                else:
                    total1 += st.fee_lp * a.liquidity // st.liquidity
        gen.apply(ev) if ev.name != "Swap" or ev.block <= a.block else gen.replay_swap(ev)
        gen.s.block, gen.s.log_index = ev.block, ev.log_index
    sqrt_end = res.timeseries[-1]["sqrt_price"]
    expected = um.mul_div(um.mul_div(total0, sqrt_end, um.Q96), sqrt_end, um.Q96) + total1
    # the reference floors each step; the meter keeps Q128 growth like the contract
    # and floors once, so it may exceed the reference by < 1 unit per step
    assert 0 <= res.metrics["fees_b"] - expected <= n_steps + 2


def test_out_of_range_position_stops_earning():
    events, blocks = synthetic_history(n_swaps=600, drift=0.3, seed=3)
    res = simulate(dataset(events, blocks), spec(width_pct=0.01))
    m = res.metrics
    assert m["time_in_range"] < 1.0 and m["rebalances"] == 0
    # after the price leaves the range for good, accrued fees stay constant
    last_in = max(i for i, r in enumerate(res.timeseries) if r["in_range"])
    after = res.timeseries[last_in + 1:]
    assert after and len({r["fees_b"] for r in after[1:]}) <= 1


def test_rebalance_on_exit():
    events, blocks = synthetic_history(n_swaps=600, drift=0.3, seed=3)
    res = simulate(dataset(events, blocks), spec(width_pct=0.01, rebalance="on_exit"))
    m = res.metrics
    assert m["rebalances"] >= 1
    rb = [a for a in res.actions if a.kind == "rebalance"]
    assert all(a.gas_units >= 200_000 + 436_000 for a in rb)
    assert m["il_realized"] == sum(a.il_realized_token1 for a in rb)
    assert m["time_in_range"] > simulate(dataset(events, blocks),
                                         spec(width_pct=0.01)).metrics["time_in_range"]


def test_no_look_ahead():
    # identical history up to block X, different afterwards -> identical marks up to X
    ev1, blocks = synthetic_history(n_swaps=500, seed=5)
    ev2, _ = synthetic_history(n_swaps=500, seed=6)
    cut = B0 + 250
    mixed = [e for e in ev1 if e.block <= cut] + [e for e in ev2 if e.block > cut]
    # the tail of ev2 is not consistent with ev1's state: replay must refuse it, which
    # proves state never leaks backwards; marks before the cut must be identical
    r1 = simulate(dataset(ev1, blocks), spec(end_block=cut, rebalance="on_exit", width_pct=0.02))
    with pytest.raises(Exception):
        simulate(dataset(mixed, blocks), spec(rebalance="on_exit", width_pct=0.02))
    r3 = simulate(dataset(ev1, blocks), spec(rebalance="on_exit", width_pct=0.02))
    pre1 = [r for r in r1.timeseries if r["block"] <= cut and r["block"] != r1.metrics["end_block"]]
    pre3 = [r for r in r3.timeseries if r["block"] <= cut][:len(pre1)]
    assert pre1 == pre3 and pre1
    assert [a for a in r3.actions if a.block <= cut] == r1.actions


def test_pre_london_start_rejected():
    events, blocks = synthetic_history(n_swaps=10)
    with pytest.raises(SimError):
        simulate(dataset(events, blocks), spec(start_block=12_000_000))


def _strip(res):
    return res.metrics, res.actions, res.timeseries


def test_simulate_many_equals_individual_runs_and_start_state():
    from backend.backtesting.position_sim import simulate_many, state_at
    events, blocks = synthetic_history(n_swaps=500, drift=0.2, seed=8)
    ds = dataset(events, blocks)
    specs = [spec(width_pct=0.02, rebalance="on_exit"), spec(width_pct=0.3),
             spec(width_pct=0.05, rebalance="periodic", period_seconds=600),
             spec(width_pct=0.05, rebalance="edge", edge_fraction=0.5, start_block=B0 + 40),
             spec(full_range=True, width_pct=0)]
    together = simulate_many(ds, specs)
    for sp, r in zip(specs, together):
        assert _strip(r) == _strip(simulate(ds, sp))
    st = state_at(ds, B0 + 4)
    from_state = simulate_many(ds, specs, start_state=st)
    assert [_strip(r) for r in from_state] == [_strip(r) for r in together]
    with pytest.raises(SimError):
        simulate_many(ds, specs, start_state=state_at(ds, B0 + 100))


def test_periodic_edge_and_cooldown_rules():
    events, blocks = synthetic_history(n_swaps=600, drift=0.3, seed=3)
    ds = dataset(events, blocks)
    per = simulate(ds, spec(width_pct=0.5, rebalance="periodic", period_seconds=1200))
    opens = [a.timestamp for a in per.actions]
    assert len(opens) > 2 and all(b - a >= 1200 for a, b in zip(opens, opens[1:]))
    edge = simulate(ds, spec(width_pct=0.02, rebalance="edge", edge_fraction=0.5))
    exit_ = simulate(ds, spec(width_pct=0.02, rebalance="on_exit"))
    assert edge.metrics["rebalances"] >= exit_.metrics["rebalances"] >= 1
    cool = simulate(ds, spec(width_pct=0.02, rebalance="on_exit", cooldown_seconds=3600))
    ts = [a.timestamp for a in cool.actions]
    assert all(b - a >= 3600 for a, b in zip(ts, ts[1:]))
    assert cool.metrics["rebalances"] <= exit_.metrics["rebalances"]


def test_asymmetric_ratio_and_full_range():
    events, blocks = synthetic_history(n_swaps=50)
    ds = dataset(events, blocks)
    for target in (0.2, 0.5, 0.8):
        r = simulate(ds, spec(width_pct=0.2, ratio_token1=target))
        assert abs(r.actions[0].ratio_token1_actual - target) < 0.03  # tick rounding
    lo = simulate(ds, spec(width_pct=0.2, ratio_token1=0.2)).actions[0]
    hi = simulate(ds, spec(width_pct=0.2, ratio_token1=0.8)).actions[0]
    assert hi.tick_lower < lo.tick_lower and hi.tick_upper < lo.tick_upper  # range below price
    full = simulate(ds, spec(full_range=True, width_pct=0)).actions[0]
    assert (full.tick_lower, full.tick_upper) == (-887220, 887220)
    with pytest.raises(SimError):
        simulate(ds, spec(full_range=True, width_pct=0, rebalance="on_exit"))
    with pytest.raises(SimError):
        simulate(ds, spec(width_pct=0.1, rebalance="periodic"))


def test_ruinous_strategy_stops_instead_of_failing(monkeypatch):
    import backend.backtesting.position_sim as ps
    monkeypatch.setattr(ps, "RUIN_DIVISOR", 1)  # SYNTHETIC: treat any loss as ruin
    events, blocks = synthetic_history(n_swaps=600, drift=0.3, seed=3)
    r = simulate(dataset(events, blocks), spec(width_pct=0.01, rebalance="on_exit"))
    assert r.metrics["ruined"] and r.metrics["ruined_block"] is not None
    rb = r.metrics["ruined_block"]
    assert all(a.block < rb for a in r.actions)  # nothing is opened after ruin
    assert r.timeseries[-1]["value_lp"] > 0 and not r.timeseries[-1]["in_range"]


def test_gas_can_not_drive_equity_far_below_zero():
    # SYNTHETIC: absurd gas price makes every rebalance unaffordable quickly
    events, blocks = synthetic_history(n_swaps=600, drift=0.3, seed=3)
    pricey = {b: (ts, 10 ** 15) for b, (ts, _) in blocks.items()}  # 1e6 gwei
    r = simulate(dataset(events, pricey), spec(width_pct=0.01, rebalance="on_exit"))
    m = r.metrics
    assert m["ruined"]
    # loss is bounded by capital plus at most one action's gas
    one_action = max(a.gas_cost_token1 for a in r.actions)
    assert m["net_a"] >= -m["capital"] - one_action
