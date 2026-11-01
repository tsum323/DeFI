"""Stage 5: strategy generator and mass testing (SYNTHETIC pool from test_backtest)."""

from __future__ import annotations

import json

from backend.database import schema as S
from backend.strategies.generator import (BASE_STRATEGIES, DEFAULT_SPACE, StrategyParams,
                                          generate, params_from_json)
from backend.strategies.mass_test import dataset_descriptor, load_results, run_mass_test
from tests.test_backtest import B0, dataset, synthetic_history


def test_canonical_hash_ignores_irrelevant_parameters():
    a = StrategyParams(width_pct=0.1, rebalance="none", period_seconds=99, edge_fraction=0.3,
                       rebalance_delay_blocks=5, cooldown_seconds=7)
    assert a.spec_hash == StrategyParams(width_pct=0.1).spec_hash
    assert StrategyParams(width_pct=0.1, rebalance="on_exit").spec_hash != a.spec_hash
    assert StrategyParams(full_range=True, width_pct=0.3).spec_hash == \
        BASE_STRATEGIES["passive_full_range"].spec_hash
    p = StrategyParams(width_pct=0.2, rebalance="edge", edge_fraction=0.8, ratio_token1=0.3)
    assert params_from_json(json.dumps(p.__dict__)) == p


def test_generate_grid_and_random_are_deduplicated_and_valid():
    g = generate()
    hashes = [p.spec_hash for p in g]
    assert len(hashes) == len(set(hashes))
    n_grid = len(DEFAULT_SPACE["width_pct"]) * len(DEFAULT_SPACE["rules"]) * len(DEFAULT_SPACE["ratio_token1"])
    assert n_grid <= len(g) <= n_grid + len(BASE_STRATEGIES)
    for p in g:
        p.to_spec(B0 + 5, B0 + 100, 10 ** 12).validate()
    r1, r2 = generate(mode="random", n=20, seed=1), generate(mode="random", n=20, seed=1)
    assert [p.spec_hash for p in r1] == [p.spec_hash for p in r2]


def test_mass_test_stores_and_skips_done_experiments():
    events, blocks = synthetic_history(n_swaps=300, drift=0.2, seed=4)
    ds = dataset(events, blocks)
    engine = S.make_engine("sqlite:///:memory:")
    params = generate({"width_pct": [0.02, 0.3], "rules": [{"rebalance": "none"},
                                                            {"rebalance": "on_exit"}],
                       "ratio_token1": [None]}, include_base=False)
    params.append(StrategyParams(width_pct=1e-9))  # degenerate width: valid but tiny range
    desc = dataset_descriptor(ds, "synthetic", "0x" + "00" * 20, B0 + 5, B0 + 290, 10 ** 12,
                              "gas_units", 1.0)
    st = run_mass_test(engine, ds, params, desc, batch=2)
    assert st["done"] + st["failed"] == len(params) and st["done"] >= 4
    again = run_mass_test(engine, ds, params, desc, batch=2)
    assert again["skipped_done"] == st["done"] and again["done"] == 0
    rows = load_results(engine, st["dataset_hash"])
    assert {r["spec_hash"] for r in rows} == {p.spec_hash for p in params}
    done = [r for r in rows if r["status"] == "done"]
    assert all(r["metrics"]["net_a"] == r["metrics"]["value_lp"] + r["metrics"]["fees_a"]
               - r["metrics"]["capital"] - r["metrics"]["gas_cost"] for r in done)
