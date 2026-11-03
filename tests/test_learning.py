"""UNISWAP AI LAB self-learning: forecast vs result, reasons, lessons, new hypotheses (SYNTHETIC)."""

from __future__ import annotations

import json

from sqlalchemy import insert, select

from backend.database import schema as S
from backend.lab.learning import (
    LearningAdapter,
    compare,
    fee_calibration,
    plan_for,
    reasons_for,
    record_forecast,
    review_forecasts,
)
from backend.strategies.generator import StrategyParams

FC = {"fees_usd": [40.0, 80.0], "il_usd": -20.0, "costs_usd": 10.0, "net_vs_hodl_usd": [10.0, 50.0],
      "exit_probability": 0.05, "entry_price": 80_000.0, "range": [64_000.0, 96_000.0], "sigma_horizon": 0.05}


def result(days, fees, tir=1.0, price=80_000.0, il=-20.0, gas=10.0, reb=0):
    return {"days": days, "fees_a": fees * 1e6, "il": il * 1e6, "gas_cost": gas * 1e6, "swap_cost": 0,
            "time_in_range": tir, "rebalances": reb, "vs_hodl_a": (fees + il - gas) * 1e6,
            "entry": {"price": 80_000.0}, "now": {"price": price}}


def test_compare_pro_rata_and_reasons():
    c = compare(FC, 7, result(3.5, 30.0, il=-10.0))
    assert c["fraction"] == 0.5 and c["fees_forecast"] == [20.0, 40.0] and abs(c["fee_ratio"] - 1.0) < 1e-9
    assert reasons_for(c, FC) == []
    bad = compare(FC, 7, result(7, 10.0, tir=0.4, price=92_000.0, il=-120.0))
    assert set(reasons_for(bad, FC)) == {"price_faster", "fees_lower", "out_of_range", "il_worse"}
    costly = compare(FC, 7, result(7, 60.0, gas=50.0, reb=3))
    assert "costs_ate" in reasons_for(costly, FC)


def test_plan_widens_after_exits_and_calms_after_costs():
    p = StrategyParams(width_pct=0.1, rebalance="on_exit")
    wide = plan_for(["out_of_range"], p)
    assert {pr.width_pct for pr in wide["proposals"]} == {0.15} and "±15%" in wide["text"]
    calm = plan_for(["costs_ate"], p)
    assert {pr.cooldown_hours for pr in calm["proposals"]} >= {72}
    assert plan_for([], p)["proposals"] == []


def _forward_test(engine, p, res):
    with engine.begin() as conn:
        return conn.execute(insert(S.forward_tests).values(
            created_at=0, start_block=1, spec_hash=p.spec_hash, params_json=json.dumps(p.__dict__),
            capital_token1=str(10 ** 10), reason="opportunity: MODERATE",
            result_json=json.dumps(res))).inserted_primary_key[0]


def card(p):
    return {"spec_hash": p.spec_hash, "strategy": p.name, "tier": "MODERATE", "horizon_days": 7,
            "expected_fees_usd": FC["fees_usd"], "expected_il_usd": FC["il_usd"],
            "costs_usd": {"gas_open": 6.0, "gas_close": 4.0, "swap": 0.0},
            "expected_net_vs_hodl_usd": FC["net_vs_hodl_usd"], "exit_probability": 0.05,
            "current_price": 80_000.0, "range": {"price_lower": 64_000.0, "price_upper": 96_000.0},
            "allocation": {"deposit_usd": 10_000.0}, "sigma_horizon": 0.05}


def test_review_cycle_lessons_calibration_and_new_hypotheses(tmp_path):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    p = StrategyParams(width_pct=0.1, rebalance="on_exit")
    with engine.begin() as conn:
        conn.execute(insert(S.strategies).values(spec_hash=p.spec_hash, name=p.name,
                                                 params_json=json.dumps(p.__dict__)))
    fid = _forward_test(engine, p, result(2, 15.0))
    assert record_forecast(engine, fid, card(p)) == record_forecast(engine, fid, card(p))  # once
    r = review_forecasts(engine)
    assert r == {"checked": 1, "reviewed": 0, "lessons": []}           # interim only
    with engine.begin() as conn:
        conn.execute(S.forward_tests.update().values(result_json=json.dumps(result(7, 10.0, tir=0.4))))
    r = review_forecasts(engine)
    assert r["reviewed"] == 1 and "out_of_range" in r["lessons"][0]["reasons"]
    assert review_forecasts(engine)["checked"] == 0                    # reviewed only once
    assert fee_calibration(engine)["factor"] is None                   # needs >= 3 reviews
    ad = LearningAdapter(engine, tested={p.spec_hash}, near_misses=[], vol_now=None, vol_median=None)
    batch = ad.propose("", "", 5)
    assert batch.hypotheses and all(s.width_pct == 0.15 for s in batch.hypotheses[0].strategies)
    ad2 = LearningAdapter(engine, tested={p.spec_hash} | {s.to_params().spec_hash for s in batch.hypotheses[0].strategies},
                          near_misses=[], vol_now=None, vol_median=None)
    assert ad2.propose("", "", 5).hypotheses == []                    # nothing proposed twice
    with engine.connect() as conn:
        assert conn.execute(select(S.lessons)).first().action.startswith("диапазон ±10%")
