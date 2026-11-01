"""Stage 7: AI research loop with a SYNTHETIC stand-in for the language model.

The fake adapter returns fixed hypotheses; everything else (validation, backtests,
verdicts, storage, report) is the real code path on a SYNTHETIC pool.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from backend.ai_researcher.research import build_context, conclude, evaluate_strategy, run_cycle
from backend.ai_researcher.schema import (Commentary, HypothesisBatch, HypothesisDraft,
                                          StrategyProposal, validate_proposal)
from backend.database import schema as S
from backend.strategies.generator import BASE_STRATEGIES, StrategyParams
from backend.validation.segments import load_segment_table, make_segments, run_segment_table
from backend.validation.study import Study
from tests.test_backtest import B0, T0, dataset, synthetic_history


def prop(**kw):
    base = dict(width_pct=0.3, rebalance="none", rebalance_delay_blocks=0, period_days=0,
                edge_fraction=1.0, cooldown_hours=0, ratio_token1=None, full_range=False,
                width_vol_mult=None, max_vol_entry=None, vol_lookback_days=7)
    base.update(kw)
    return StrategyProposal(**base)


class FakeAdapter:
    model = "fake-model (SYNTHETIC)"

    def __init__(self, batch):
        self.batch, self.contexts, self.comment_inputs = batch, [], []

    def propose(self, system, context, n):
        self.contexts.append(context)
        return self.batch

    def comment(self, system, results):
        self.comment_inputs.append(results)
        return Commentary(summary="synthetic commentary", patterns=[], ineffective_configurations=[],
                          next_directions=[])


def setup(tmp_path):
    events, blocks = synthetic_history(n_swaps=900, drift=0.1, seed=12)
    blocks = {b: (T0 + 600 * (b - B0), bf) for b, (_, bf) in blocks.items()}
    ds = dataset(events, blocks)
    segs = make_segments(ds, T0 + 3600, T0 + 600 * 900, days=1, oos_segments=2)
    study = Study(segs, {"pool": "synthetic", "capital_token1": 10 ** 12, "gas_set": "gas_units",
                         "priority_fee_gwei": 1.0})
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    grid = [StrategyParams(width_pct=w) for w in (0.05, 0.3)] + [BASE_STRATEGIES["passive_full_range"]]
    st = run_segment_table(engine, ds, grid, segs, study.base, batch=10)
    return ds, study, engine, grid, st["code_version"]


def test_validate_proposal_rejects_bad_parameters():
    assert validate_proposal(prop())[0] is not None
    assert "vol_lookback" in validate_proposal(prop(max_vol_entry=0.8, vol_lookback_days=1))[1]
    assert validate_proposal(prop(rebalance="periodic", period_days=0))[0] is None
    assert validate_proposal(prop(width_pct=50))[0] is None
    # a rebalance rule is meaningless for a full-range position: canonicalized to the passive LP
    full = validate_proposal(prop(full_range=True, rebalance="on_exit"))[0]
    assert full.spec_hash == BASE_STRATEGIES["passive_full_range"].spec_hash


def test_context_is_in_sample_only(tmp_path):
    ds, study, engine, grid, version = setup(tmp_path)
    table = load_segment_table(engine, study.base, study.segments, version)
    ctx = build_context(table, grid, study, [])
    # poison every OOS result: the context must not change
    oos = {s.index for s in study.segments if s.role == "OOS"}
    poisoned = {k: ({**v, "net_a": -10 ** 30} if k[1] in oos and v else v) for k, v in table.items()}
    assert build_context(poisoned, grid, study, []) == ctx
    doc = json.loads(ctx)
    assert {s["index"] for s in doc["in_sample_segments"]}.isdisjoint(oos)


def test_research_cycle_end_to_end(tmp_path):
    ds, study, engine, grid, version = setup(tmp_path)
    batch = HypothesisBatch(hypotheses=[
        HypothesisDraft(title="wide asymmetric", description="d", rationale="r",
                        strategies=[prop(width_pct=0.5, ratio_token1=0.6), prop(width_pct=0.5)],
                        test_plan="t", expected_outcome="e"),
        HypothesisDraft(title="all invalid", description="d", rationale="r",
                        strategies=[prop(width_pct=99)], test_plan="t", expected_outcome="e"),
    ])
    fake = FakeAdapter(batch)
    s1 = run_cycle(engine, ds, study, fake, grid, n=5, version=version, out_dir=tmp_path / "rep")
    assert s1["proposed"] == 2 and s1["tested"] == 1
    h = {x["title"]: x for x in s1["hypotheses"]}
    assert h["all invalid"]["status"] == "invalid" and h["all invalid"]["invalid_reasons"]
    assert h["wide asymmetric"]["verdict"] in ("confirmed", "rejected")
    assert len(h["wide asymmetric"]["strategies"]) == 2
    with engine.connect() as c:
        rows = c.execute(select(S.hypotheses.c.status, S.hypotheses.c.ai_commentary,
                                S.hypotheses.c.created_by_model)).all()
        links = c.execute(select(S.hypothesis_experiments)).all()
    assert {r.status for r in rows} == {"tested", "invalid"} and len(links) == 2
    assert all(r.created_by_model == fake.model for r in rows)
    assert (tmp_path / "rep" / f"cycle_{s1['cycle_id']}.md").exists()
    # second cycle: the same strategies are reported as already tested, verdicts withheld
    s2 = run_cycle(engine, ds, study, fake, grid, n=5, version=version)
    assert {x["title"]: x["status"] for x in s2["hypotheses"]}["wide asymmetric"] == "duplicate"
    ctx2 = json.loads(fake.contexts[-1])
    tested = ctx2["already_tested_hypotheses"]
    assert tested and "verdict" not in tested[0]


def test_verdict_rules():
    from backend.validation.segments import Segment
    segs = [Segment(i, i, i, i, i, "OOS", 0, 0.5, "flat", "low") for i in range(7)]
    study = Study(segs, {})
    p = StrategyParams(width_pct=0.3)
    passive = BASE_STRATEGIES["passive_full_range"].spec_hash

    def mm(r):
        return {"net_a": int(r * 10 ** 6), "capital": 10 ** 6, "ruined": False}
    t = {}
    for i, ex in enumerate([0.02, 0.02, 0.02, 0.02, 0.02, 0.02, -0.01]):
        for var in ("base", "gas_x2"):
            t[(p.spec_hash, i, var)], t[(passive, i, var)] = mm(ex), mm(0.0)
    r = evaluate_strategy(t, p, study)
    assert r["verdict"] == "confirmed" and r["confidence"] == "high"
    t[(p.spec_hash, 0, "base")]["ruined"] = True
    assert evaluate_strategy(t, p, study)["verdict"] == "rejected"
    assert conclude([])[0] == "inconclusive"
