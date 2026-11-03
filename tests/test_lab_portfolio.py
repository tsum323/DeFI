"""UNISWAP AI LAB virtual portfolio: sizing, opening, valuation, closing rules (SYNTHETIC)."""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

from sqlalchemy import select

from backend.core.config import load_config
from backend.database import schema as S
from backend.lab import portfolio as pf
from backend.lab.scanner import ScanPool, position
from backend.strategies.generator import StrategyParams

WBTC_USDC = ScanPool("wbtc_usdc_500", "0x9a772018FbD77fcD2d25657e5C547BAfF3Fd7D16", "ethereum", "WBTC", "USDC", 8, 6, 500, "WBTC")


def cfg_with(capital=10_000):
    cfg = load_config()
    cfg.lab["portfolio"] = {"virtual_capital_usd": capital, "max_open": 8, "max_days": 30,
                            "close_after_hours_out_of_ranking": 72,
                            "allocation": {"CONSERVATIVE": 0.3, "MODERATE": 0.15, "EXPERIMENTAL": 0.05}}
    return cfg


def card(p, tier="MODERATE"):
    return {"tier": tier, "strategy": p.name, "spec_hash": p.spec_hash, "pool": "wbtc_usdc_3000",
            "pair": "WBTC/USDC", "current_price": 80_000.0, "range": {"price_lower": 70_000.0, "price_upper": 90_000.0},
            "allocation": {"deposit_usd": 10_000.0, "token0": 0.06, "token1": 5_000.0},
            "expected_fees_usd": [40.0, 80.0], "expected_il_usd": -20.0,
            "costs_usd": {"gas_open": 3.0, "gas_close": 2.0, "swap": 10.0, "total": 15.0},
            "expected_net_vs_hodl_usd": [5.0, 45.0], "exit_probability": 0.1, "horizon_days": 7,
            "rebalance_rule": p.rebalance, "sigma_horizon": 0.05, "variants": []}


def test_scale_card_keeps_gas_fixed():
    c = pf.scale_card(card(StrategyParams(width_pct=0.1)), 1_500.0)
    assert c["expected_fees_usd"] == [6.0, 12.0] and math.isclose(c["expected_il_usd"], -3.0)
    assert math.isclose(c["costs_usd"]["swap"], 1.5) and c["costs_usd"]["gas_open"] == 3.0
    assert [round(x, 6) for x in c["expected_net_vs_hodl_usd"]] == [-3.5, 2.5]  # gas weighs more on small size


def test_open_value_close_and_history(tmp_path):
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    cfg = cfg_with(10_000)
    a, b = StrategyParams(width_pct=0.1), StrategyParams(width_pct=0.2)
    ranking = {"block": 100, "tiers": {"CONSERVATIVE": [], "MODERATE": [card(a)], "EXPERIMENTAL": [card(b, "EXPERIMENTAL")]}}
    opened = pf.open_from_ranking(engine, cfg, ranking, {a.spec_hash: a, b.spec_hash: b}, 100, 6, now=1_000)
    assert len(opened) == 2 and pf.open_from_ranking(engine, cfg, ranking, {a.spec_hash: a, b.spec_hash: b}, 100, 6, now=1_100) == []
    assert math.isclose(pf.cash(engine, cfg), 10_000 - 1_500 - 500)
    with engine.connect() as c:
        rows = c.execute(select(S.portfolio_positions)).all()
        fts = c.execute(select(S.forward_tests)).all()
        fcs = c.execute(select(S.forecasts)).all()
    assert {int(f.capital_token1) for f in fts} == {1_500 * 10 ** 6, 500 * 10 ** 6} and len(fcs) == 2
    # the simulation behind position 1 reports a result
    res = {"days": 2.0, "net_a": 12 * 10 ** 6, "fees_a": 20 * 10 ** 6, "il": -5 * 10 ** 6, "gas_cost": 3 * 10 ** 6,
           "swap_cost": 0, "vs_hodl_a": 12 * 10 ** 6, "now": {"in_range": True, "price": 81_000.0}, "time_in_range": 1.0}
    with engine.begin() as c:
        c.execute(S.forward_tests.update().where(S.forward_tests.c.id == rows[0].forward_test_id)
                  .values(result_json=json.dumps(res)))
    pf.update_positions(engine, cfg, ranking, {}, {"ETH": 3000, "BTC": 80_000}, now=2_000)
    s = pf.summary(engine, cfg, now=2_000)
    assert math.isclose(s["pnl_usd"], 12.0) and math.isclose(s["balance_usd"], 10_012.0) and len(s["open"]) == 2
    pf.snapshot_day(engine, s, now=2_000)
    # strategy b leaves the ranking -> closed after 72 h; everything closes after 30 days
    r2 = {"block": 101, "tiers": {"MODERATE": [card(a)]}}
    pf.update_positions(engine, cfg, r2, {}, {"ETH": 3000, "BTC": 80_000}, now=3_000)
    out = pf.update_positions(engine, cfg, r2, {}, {"ETH": 3000, "BTC": 80_000}, now=3_000 + 72 * 3600)
    assert len(out["closed"]) == 1 and "рейтинге" in out["closed"][0]
    out = pf.update_positions(engine, cfg, r2, {}, {"ETH": 3000, "BTC": 80_000}, now=1_000 + 30 * 86_400)
    assert len(out["closed"]) == 1 and "срок" in out["closed"][0]
    s = pf.summary(engine, cfg, now=1_000 + 30 * 86_400)
    assert len(s["closed"]) == 2 and not s["open"] and len(s["history"]) == 1
    assert rows[0].forward_test_id in pf.closed_forward_tests(engine)


def test_estimated_position_fees_and_value():
    sp = int(math.sqrt(80_000 * 10 ** (6 - 8)) * 2 ** 96)
    pos = position(WBTC_USDC, sp, 0.1, 500, 80_000.0, 1.0)
    entry = {"price": 80_000.0, "range": [pos["price_lower"], pos["price_upper"]], "width": 0.1,
             "usd": {"ETH": 3000.0, "BTC": 80_000.0}, "gas_usd": 2.0, "swap_usd": 0.1, "since_hour": 3600}
    row = SimpleNamespace(entry_json=json.dumps(entry), allocation_usd=500.0)
    h = [SimpleNamespace(hour_ts=3600 * k, close_sqrt_price=str(sp), close_liquidity=str(int(pos["L"])),
                         fee0=0.0, fee1=10.0) for k in (1, 2, 3)]
    m = pf.mark_estimated(row, WBTC_USDC, h, {"ETH": 3000.0, "BTC": 80_000.0})
    assert math.isclose(m["fees_usd"], 15.0, rel_tol=1e-6)   # half of each hour's fees
    assert abs(m["il_usd"]) < 1e-6 and m["in_range"] and m["time_in_range"] == 1.0
    assert math.isclose(m["net_usd"], 15.0 - 2.1, rel_tol=1e-6) and m["label"] == "ESTIMATED"


def test_strategy_names_in_plain_russian():
    from backend.lab.plain import describe, describe_static
    p = StrategyParams(width_pct=0.3, ratio_token1=0.7, rebalance="on_exit", rebalance_delay_blocks=300)
    assert describe(p) == ("диапазон ±30% от цены; 70% в USDC, 30% в WBTC; если цена выйдет из диапазона — "
                           "новый диапазон вокруг цены через ~1 ч")
    assert "не меняется" in describe(StrategyParams(width_pct=0.2)) and "поровну" in describe(StrategyParams(width_pct=0.2))
    assert describe_static("static ±10%") == "диапазон ±10% от цены; диапазон не меняется"
