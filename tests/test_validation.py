"""Stage 6: validation logic on SYNTHETIC result tables and a SYNTHETIC pool."""

from __future__ import annotations

from backend.strategies.generator import DEFAULT_SPACE, StrategyParams
from backend.validation.robustness import PASSIVE, grid_coordinates, score_strategies, walk_forward
from backend.validation.segments import Segment, make_segments
from tests.test_backtest import B0, T0, dataset, synthetic_history

CAP = 10 ** 6


def seg(i, role, trend="flat", vol="low"):
    return Segment(i, 100 * i, 100 * i + 99, i, i, role, 0.0, 0.5, trend, vol)


def m(roi, vs_hodl=1, mdd=0.1, ruined=False, size=False):
    return {"net_a": int(roi * CAP), "capital": CAP, "vs_hodl_a": vs_hodl, "max_drawdown_a": mdd,
            "ruined": ruined, "size_impact": size, "rebalances": 1}


def table_for(segs, rois_by_strategy):
    t = {}
    for h, rois in rois_by_strategy.items():
        for s, r in zip(segs, rois):
            for var in ("base", "gas_x2", "volume_x0.5", "volume_x1.5"):
                t[(h, s.index, var)] = m(r)
    return t


def test_consistent_strategy_beats_one_lucky_segment():
    segs = [seg(i, "IS") for i in range(6)] + [seg(i, "OOS") for i in range(6, 9)]
    steady = StrategyParams(width_pct=0.3)
    lucky = StrategyParams(width_pct=0.05)
    passive = StrategyParams(full_range=True, width_pct=0.0)
    t = table_for(segs, {steady.spec_hash: [0.01] * 9,
                         lucky.spec_hash: [0.9, -0.05, -0.05, -0.05, -0.05, -0.05, -0.02, -0.02, -0.02],
                         passive.spec_hash: [0.0] * 9})
    reps = {r.name: r for r in score_strategies(t, [steady, lucky, passive], segs)}
    assert reps["w0.3_static"].score > reps["w0.05_static"].score
    assert reps["w0.05_static"].stats["is_mean_roi"] > reps["w0.3_static"].stats["is_mean_roi"]
    assert "OOS_MOSTLY_NEGATIVE" in reps["w0.05_static"].flags
    assert PASSIVE == passive.spec_hash


def test_ruin_and_size_penalties_and_regime_flag():
    segs = [seg(0, "IS", "up"), seg(1, "IS", "down"), seg(2, "OOS", "up"), seg(3, "OOS", "down")]
    a, b = StrategyParams(width_pct=0.3), StrategyParams(width_pct=0.5)
    t = table_for(segs, {a.spec_hash: [0.05, -0.05, 0.05, -0.05], b.spec_hash: [0.05, -0.05, 0.05, -0.05]})
    for s in segs:
        t[(b.spec_hash, s.index, "base")] = m(0.05 if s.trend == "up" else -0.05, ruined=s.index == 1,
                                               size=True)
    reps = {r.spec_hash: r for r in score_strategies(t, [a, b], segs)}
    assert reps[b.spec_hash].score < reps[a.spec_hash].score
    assert {"RUINED_IN_SOME_SEGMENTS", "SIZE_IMPACT"} <= set(reps[b.spec_hash].flags)
    assert "REGIME_DEPENDENT" in reps[a.spec_hash].flags


def test_grid_neighbours():
    p = StrategyParams(width_pct=0.1, rebalance="on_exit", rebalance_delay_blocks=300)
    assert grid_coordinates(p, DEFAULT_SPACE) == (3, 2, 0)
    assert grid_coordinates(StrategyParams(width_pct=0.123), DEFAULT_SPACE) is None


def test_walk_forward_uses_only_previous_segments():
    segs = [seg(i, "IS") for i in range(5)]
    a, b = StrategyParams(width_pct=0.3), StrategyParams(width_pct=0.5)
    passive = StrategyParams(full_range=True, width_pct=0.0)
    # a is best in segments 0-1, b in 2-3; the test segment results are what follows
    t = table_for(segs, {a.spec_hash: [0.1, 0.1, -0.1, -0.1, 0.3],
                         b.spec_hash: [0.0, 0.0, 0.2, 0.2, -0.2],
                         passive.spec_hash: [0.0] * 5})
    wf = walk_forward(t, [a, b, passive], segs, train_segments=2)
    chosen = [s["chosen"] for s in wf["steps"]]
    # k=2: train 0-1 -> a; k=3: train 1-2 (a 0.0, b 0.1) -> b; k=4: train 2-3 -> b
    assert chosen == ["w0.3_static", "w0.5_static", "w0.5_static"]
    assert [s["test_roi"] for s in wf["steps"]] == [-0.1, 0.2, -0.2]


def test_make_segments_roles_and_regimes():
    events, blocks = synthetic_history(n_swaps=900, drift=0.25, seed=9)
    blocks = {b: (T0 + 600 * (b - B0), bf) for b, (_, bf) in blocks.items()}
    ds = dataset(events, blocks)
    segs = make_segments(ds, T0, T0 + 600 * 900, days=1, oos_segments=2)
    assert len(segs) >= 4 and [s.role for s in segs][-2:] == ["OOS", "OOS"]
    assert all(a.end_block < b.start_block for a, b in zip(segs, segs[1:]))
    assert {s.vol_class for s in segs} <= {"high", "low"}
    assert {s.trend for s in segs} <= {"up", "down", "flat"}
