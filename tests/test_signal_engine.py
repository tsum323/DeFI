"""Stage 8: signal engine on a SYNTHETIC pool and SYNTHETIC result tables."""

from __future__ import annotations

from dataclasses import replace

from sqlalchemy import select

from backend.backtesting.position_sim import state_at
from backend.database import schema as S
from backend.signal_engine.engine import Candidate, Check, evaluate_market, monitor
from backend.signal_engine.market import snapshot
from backend.strategies.generator import BASE_STRATEGIES, StrategyParams
from backend.validation.segments import make_segments
from backend.validation.study import Study
from tests.test_backtest import B0, T0, dataset, synthetic_history

CFG = {"validity_days": 7, "max_data_age_minutes": 10 ** 9, "min_volume_24h_usd": 0,
       "min_oos_positive_segments": 5, "require_positive_oos_roi": True, "max_mean_drawdown": 0.3,
       "capital_usd": 10 ** -6, "fee_lookback_days": [1, 3],
       "price_scenarios": [-0.2, -0.1, 0.0, 0.1, 0.2]}
META = {"pool": "synthetic", "pair": "T0/T1", "network": "test", "pool_address": "0x0", "fee_tier": 0.003}


def setup(tmp_path):
    events, blocks = synthetic_history(n_swaps=1500, drift=0.05, seed=21)
    blocks = {b: (T0 + 600 * (b - B0), bf) for b, (_, bf) in blocks.items()}
    ds = dataset(events, blocks)
    last = events[-1].block
    st = state_at(ds, last)
    snap = snapshot(ds, st, blocks[last][0] + 60, vol_median=1e9)
    segs = make_segments(ds, T0 + 3600, T0 + 600 * 1400, days=1, oos_segments=7)
    segs = [replace(s, trend=snap.trend, vol_class=snap.vol_class) for s in segs]
    study = Study(segs, {})
    engine = S.make_engine(f"sqlite:///{tmp_path / 'lab.sqlite'}")
    return ds, st, snap, study, engine


def metrics(roi, mdd=0.1, ruined=False):
    return {"net_a": int(roi * 10 ** 12), "capital": 10 ** 12, "ruined": ruined,
            "max_drawdown_a": mdd, "vs_hodl_a": 0}


def table_with(study, good: StrategyParams | None, bad: StrategyParams):
    t, P = {}, BASE_STRATEGIES["passive_full_range"].spec_hash
    for s in study.segments:
        for var in ("base", "gas_x2"):
            t[(P, s.index, var)] = metrics(0.0)
            t[(bad.spec_hash, s.index, var)] = metrics(-0.01)
            if good is not None:
                t[(good.spec_hash, s.index, var)] = metrics(0.02)
    return t


def strategies(*ps):
    return {p.spec_hash: p for p in ps + (BASE_STRATEGIES["passive_full_range"],)}


def test_open_candidate_has_all_signal_fields_then_hold_then_invalidated(tmp_path):
    ds, st, snap, study, engine = setup(tmp_path)
    good, bad = StrategyParams(width_pct=0.3), StrategyParams(width_pct=0.05, rebalance="on_exit")
    t = table_with(study, good, bad)
    now = snap.timestamp + 60
    d = evaluate_market(engine, ds, st, snap, t, strategies(good, bad), study, CFG, META,
                        lambda b: state_at(ds, b), now)
    assert d.decision == "OPEN CANDIDATE"
    p = d.payload
    for key in ("current_price", "range", "allocation", "strategy", "expected_fees_usd",
                "expected_costs_usd", "il_scenarios", "net_forecast_usd", "historical",
                "confidence", "reasons", "invalidation", "valid_until", "pool_address", "fee_tier"):
        assert key in p, key
    assert p["range"]["tick_lower"] < snap.tick < p["range"]["tick_upper"]
    lo, hi = p["expected_fees_usd"]
    assert 0 <= lo <= hi and p["net_forecast_usd"][0] <= p["net_forecast_usd"][1]
    zero = [x for x in p["il_scenarios"] if x["price_change"] == 0.0][0]
    assert abs(zero["il_usd"]) < 1e-6  # no price move -> no impermanent loss
    assert all(x["il_usd"] <= 1e-6 for x in p["il_scenarios"])
    # next evaluation monitors instead of issuing a new signal
    d2 = evaluate_market(engine, ds, st, snap, t, strategies(good, bad), study, CFG, META,
                         lambda b: state_at(ds, b), now + 3600)
    assert (d2.decision, d2.signal_id) == ("HOLD", d.signal_id)
    d3 = evaluate_market(engine, ds, st, snap, t, strategies(good, bad), study, CFG, META,
                         lambda b: state_at(ds, b), now + 8 * 86400)
    assert d3.decision == "INVALIDATED"
    with engine.connect() as c:
        statuses = [r.status for r in c.execute(select(S.signal_events).where(
            S.signal_events.c.signal_id == d.signal_id).order_by(S.signal_events.c.id))]
        n_signals = c.execute(select(S.signals)).all()
        n_evals = c.execute(select(S.market_evaluations)).all()
    assert statuses == ["OPEN CANDIDATE", "HOLD", "INVALIDATED"]
    assert len(n_signals) == 1 and len(n_evals) == 3


def test_wait_when_nothing_passes_and_no_duplicate_waits(tmp_path):
    ds, st, snap, study, engine = setup(tmp_path)
    bad = StrategyParams(width_pct=0.05, rebalance="on_exit")
    t = table_with(study, None, bad)
    args = (engine, ds, st, snap, t, strategies(bad), study, CFG, META, lambda b: state_at(ds, b))
    d1 = evaluate_market(*args, snap.timestamp + 60)
    d2 = evaluate_market(*args, snap.timestamp + 120)
    assert d1.decision == d2.decision == "WAIT" and d1.signal_id == d2.signal_id
    assert d1.payload["closest_candidates"][0]["failed"]


def test_stale_data_gives_wait(tmp_path):
    ds, st, snap, study, engine = setup(tmp_path)
    good, bad = StrategyParams(width_pct=0.3), StrategyParams(width_pct=0.05)
    t = table_with(study, good, bad)
    stale = replace(snap, data_age_seconds=10 ** 6)
    d = evaluate_market(engine, ds, st, stale, t, strategies(good, bad), study,
                        {**CFG, "max_data_age_minutes": 60}, META, lambda b: state_at(ds, b),
                        snap.timestamp + 60)
    assert d.decision == "WAIT" and d.reasons[0]["name"] == "data_current"


def test_monitor_transitions(tmp_path):
    ds, st, snap, study, engine = setup(tmp_path)
    ok = Candidate(StrategyParams(width_pct=0.3), [Check("x", True, 1, 1)], {})
    bad = Candidate(StrategyParams(width_pct=0.3), [Check("x", False, 1, 1)], {})
    payload = {"valid_until": snap.timestamp + 1000,
               "range": {"tick_lower": snap.tick - 600, "tick_upper": snap.tick + 600},
               "invalidation": {"max_vol_7d": 10.0, "trend_must_stay": snap.trend,
                                "rebalance_rule": "none"}}
    assert monitor(payload, snap, ok, snap.timestamp)[0] == "HOLD"
    assert monitor(payload, snap, ok, snap.timestamp + 2000)[0] == "INVALIDATED"
    assert monitor(payload, snap, bad, snap.timestamp)[0] == "CLOSE CANDIDATE"
    out = {**payload, "range": {"tick_lower": snap.tick + 60, "tick_upper": snap.tick + 600}}
    assert monitor(out, snap, ok, snap.timestamp)[0] == "CLOSE CANDIDATE"
    reb = {**out, "invalidation": {**payload["invalidation"], "rebalance_rule": "on_exit"}}
    assert monitor(reb, snap, ok, snap.timestamp)[0] == "REBALANCE"
    other = {**payload, "invalidation": {**payload["invalidation"], "trend_must_stay": "nope"}}
    assert monitor(other, snap, ok, snap.timestamp)[0] == "INVALIDATED"


def test_virtual_portfolio_from_signal(tmp_path):
    import json

    from backend.backtesting.position_sim import simulate
    from backend.virtual_portfolio.portfolio import journal, mark_all, open_new_positions
    ds, st, snap, study, engine = setup(tmp_path)
    # signal from the middle of the SYNTHETIC history, so there is a future to mark against
    mid = sorted(ds.blocks)[len(ds.blocks) // 2]
    st_mid = state_at(ds, mid)
    snap_mid = snapshot(ds, st_mid, ds.blocks[mid][0] + 60, vol_median=1e9)
    good, bad = StrategyParams(width_pct=0.3), StrategyParams(width_pct=0.05, rebalance="on_exit")
    segs = [replace(s, trend=snap_mid.trend, vol_class=snap_mid.vol_class) for s in study.segments]
    study = Study(segs, {})
    t = table_with(study, good, bad)
    d = evaluate_market(engine, ds, st_mid, snap_mid, t, strategies(good, bad), study,
                        {**CFG, "validity_days": 1}, META, lambda b: state_at(ds, b),
                        snap_mid.timestamp + 60)
    assert d.decision == "OPEN CANDIDATE"
    [pid] = open_new_positions(engine)
    assert open_new_positions(engine) == [pid]  # idempotent
    last = ds.events[-1].block
    early = sorted(b for b in ds.blocks if b > mid)[50]
    [m1] = mark_all(engine, ds, lambda b: state_at(ds, b), early)
    assert m1["status"] == "open" and m1["opening_matches_signal"]
    assert {"passive_full_range", "static_wide_50", "exit_rebalance_10"} <= set(m1["benchmarks"])
    # the mark equals a plain backtest of the same strategy from the signal block
    ref = simulate(ds, good.to_spec(mid, early, int(d.payload["capital_token1"])),
                   start_state=state_at(ds, mid - 1))
    assert m1["metrics"]["net_a"] == ref.metrics["net_a"]
    [m2] = mark_all(engine, ds, lambda b: state_at(ds, b), last)  # validity (1 day) has ended
    assert m2["status"] == "closed" and m2["outcome"] in ("success", "failure")
    j = journal(engine)
    assert j["counts"]["open_candidates"] == 1 and j["counts"]["active_positions"] == 0
    assert j["counts"]["successful"] + j["counts"]["failed"] == 1
    assert j["cumulative_net_a"] == m2["metrics"]["net_a"] and j["max_drawdown"] >= 0
    # editing the signal afterwards is detected
    with engine.begin() as c:
        c.execute(S.signals.update().where(S.signals.c.id == d.signal_id)
                  .values(payload_json=json.dumps({**d.payload, "range": {}})))
    import pytest
    with pytest.raises(RuntimeError):
        mark_all(engine, ds, lambda b: state_at(ds, b), last)


def test_live_update_notification_text(tmp_path):
    from scripts.live_update import latest_signal, message
    ds, st, snap, study, engine = setup(tmp_path)
    good, bad = StrategyParams(width_pct=0.3), StrategyParams(width_pct=0.05, rebalance="on_exit")
    t = table_with(study, good, bad)
    evaluate_market(engine, ds, st, snap, t, strategies(good, bad), study, CFG, META,
                    lambda b: state_at(ds, b), snap.timestamp + 60)
    sig = latest_signal(engine)
    title, body = message(sig)
    assert "КАНДИДАТ НА ОТКРЫТИЕ" in title
    assert "Диапазон" in body and "МСК" in body and "не инвестиционная рекомендация" in body


def test_watch_level_one_failed_check_goes_to_forward_test(tmp_path):
    # SYNTHETIC: "near" wins everywhere but its drawdown is too deep -> exactly one failed check
    from backend.api.queries import watch
    from backend.virtual_portfolio.portfolio import register_watch
    ds, st, snap, study, engine = setup(tmp_path)
    bad = StrategyParams(width_pct=0.05, rebalance="on_exit")
    near = StrategyParams(width_pct=0.3)
    t = table_with(study, None, bad)
    for s in study.segments:
        for var in ("base", "gas_x2"):
            t[(near.spec_hash, s.index, var)] = metrics(0.02, mdd=0.5)
    strats = strategies(bad, near)
    d = evaluate_market(engine, ds, st, snap, t, strats, study, CFG, META, lambda b: state_at(ds, b),
                        snap.timestamp + 60)
    assert d.decision == "WAIT"
    assert [w["strategy"] for w in d.payload["watch"]] == [near.name]
    assert d.payload["watch"][0]["failed"] == "acceptable_drawdown"
    assert register_watch(engine, d.payload["watch"], strats, snap.block, 10 ** 6) == [near.name]
    assert register_watch(engine, d.payload["watch"], strats, snap.block + 1, 10 ** 6) == []  # once
    w = watch(engine)
    assert [x["name"] for x in w["tests"]] == [near.name] and w["tests"][0]["on_list_now"]
    assert [x["strategy"] for x in w["current"]] == [near.name]


def test_data_age_counts_synced_blocks_without_events(tmp_path):
    # SYNTHETIC: a quiet pool (no event since the last swap) is still current when synced
    ds, st, snap, study, engine = setup(tmp_path)
    now = snap.timestamp + 3 * 3600
    stale = snapshot(ds, st, now, vol_median=1e9)
    fresh = snapshot(ds, st, now, vol_median=1e9, synced_block=st.block + 3 * 3600 // 12 - 5)
    assert stale.data_age_seconds == 3 * 3600 and fresh.data_age_seconds == 60
    assert snapshot(ds, st, now, 1e9, synced_block=st.block + 10 ** 6).data_age_seconds == 0
