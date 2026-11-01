"""Signal engine (SPEC §11): choose a validated strategy for the current market, or WAIT.

1. Pool-level checks: data is current, pool activity is sufficient.
2. Every strategy with complete segment results is checked against the quality
   filters (SPEC 11.4); each check is reported with its value and threshold.
3. If a strategy passes, an OPEN CANDIDATE is built with the same code the
   backtester uses (range rule, dry-run swap through the real pool state, gas),
   a fee estimate replayed over the recent past on that exact range (A/B bounds),
   impermanent-loss scenarios computed exactly, and a net forecast as a RANGE.
4. If nothing passes: WAIT with the reasons. No artificial signals.
5. An active signal is monitored instead of issuing a new one (SPEC 11.5):
   HOLD / REBALANCE / CLOSE CANDIDATE / INVALIDATED, every change stored.
All forecasts are SCENARIO; historical statistics are MODELED.
"""

from __future__ import annotations

import bisect
import copy
import json
import math
import statistics
import time
from dataclasses import asdict, dataclass, field

from sqlalchemy import insert, select

from backend.ai_researcher.research import evaluate_strategy
from backend.backtesting import uniswap_math as um
from backend.backtesting.fee_meter import FeeMeter
from backend.backtesting.market_view import MarketView
from backend.backtesting.pool_replay import PoolReplay, PoolState
from backend.backtesting.position_sim import Dataset, _prefill_market, _Sim, _value1
from backend.database import schema as S
from backend.signal_engine.market import DAY, MarketSnapshot, human_price
from backend.strategies.generator import StrategyParams, params_from_json
from backend.validation.robustness import PASSIVE
from backend.validation.study import Study

ACTIVE = ("OPEN CANDIDATE", "HOLD", "REBALANCE")
TERMINAL = ("CLOSE CANDIDATE", "INVALIDATED")


@dataclass
class Check:
    name: str
    passed: bool
    value: object
    threshold: object


@dataclass
class Candidate:
    params: StrategyParams
    checks: list[Check]
    stats: dict
    confidence: str = "low"
    contaminated: bool = False

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


@dataclass
class Decision:
    decision: str
    signal_id: int | None
    snapshot: dict
    reasons: list
    payload: dict = field(default_factory=dict)


def _roi(m):
    return None if m is None else m["net_a"] / m["capital"]


# ---------------------------------------------------------------- filters
def pool_checks(snap: MarketSnapshot, cfg: dict) -> list[Check]:
    return [Check("data_current", snap.data_age_seconds <= cfg["max_data_age_minutes"] * 60,
                  round(snap.data_age_seconds / 60, 1), f"<= {cfg['max_data_age_minutes']} min"),
            Check("pool_activity", snap.volume_24h_usd >= cfg["min_volume_24h_usd"],
                  round(snap.volume_24h_usd), f">= {cfg['min_volume_24h_usd']} USD/24h"),
            Check("market_data_complete", snap.vol_7d is not None and snap.eth_usd is not None
                  and snap.base_fee_wei is not None, "vol_7d/eth_usd/base_fee", "present")]


def evaluate_candidates(table: dict, strategies: dict[str, StrategyParams], study: Study,
                        snap: MarketSnapshot, cfg: dict, contaminated: set[str]) -> list[Candidate]:
    segs = study.segments
    oos = [s for s in segs if s.role == "OOS"]
    out = []
    for h, p in strategies.items():
        if not all(table.get((h, s.index, "base")) for s in oos):
            continue
        r = evaluate_strategy(table, p, study)
        base = [(s, table.get((h, s.index, "base"))) for s in segs]
        have = [(s, m) for s, m in base if m]
        abs_oos = statistics.fmean(_roi(table[(h, s.index, "base")]) for s in oos)
        mdd = statistics.fmean(m["max_drawdown_a"] for _, m in have)
        regime = [(s, m) for s, m in have if s.trend == snap.trend and s.vol_class == snap.vol_class
                  and table.get((PASSIVE, s.index, "base"))]
        reg_ex = [_roi(m) - _roi(table[(PASSIVE, s.index, "base")]) for s, m in regime]
        checks = [
            Check("sufficient_history", len(have) == len(segs), len(have), f"{len(segs)} segments"),
            Check("oos_beats_passive", (r["mean_excess"] or -1) > 0 and
                  r["positive_excess_segments"] >= cfg["min_oos_positive_segments"],
                  {"mean_excess": r["mean_excess"], "positive": r["positive_excess_segments"]},
                  f"> 0 and >= {cfg['min_oos_positive_segments']}/{len(oos)}"),
            Check("oos_positive_roi", abs_oos > 0 or not cfg["require_positive_oos_roi"],
                  round(abs_oos, 4), "> 0"),
            Check("stable_across_periods", bool(r["consistent_with_is"]),
                  {"is_mean_excess": r["is_mean_excess"], "is_positive": r["is_positive_excess_segments"]},
                  "IS excess > 0 in >= half of segments"),
            Check("acceptable_drawdown", mdd <= cfg["max_mean_drawdown"], round(mdd, 4),
                  f"<= {cfg['max_mean_drawdown']}"),
            Check("costs_included_robust", not r["ruined"] and (r["mean_excess_gas_x2"] or -1) > 0,
                  {"ruined": r["ruined"], "gas_x2_excess": r["mean_excess_gas_x2"]}, "no ruin, gas x2 > 0"),
            Check("fits_current_regime", bool(reg_ex) and statistics.fmean(reg_ex) >= 0,
                  {"regime": f"{snap.trend}/{snap.vol_class}", "segments": len(reg_ex),
                   "mean_excess": round(statistics.fmean(reg_ex), 4) if reg_ex else None},
                  "history in this regime with excess >= 0"),
        ]
        c = Candidate(p, checks, {**r, "oos_mean_roi": abs_oos, "mean_mdd": mdd,
                                  "max_mdd": max(m["max_drawdown_a"] for _, m in have),
                                  "regime_segments": len(reg_ex)}, contaminated=h in contaminated)
        if c.passed:
            strong = r["positive_excess_segments"] >= 6 and (r["mean_excess"] or 0) > 0.01
            c.confidence = ("low" if c.contaminated or len(reg_ex) < 2
                            else "high" if strong else "medium")
        out.append(c)
    out.sort(key=lambda c: (c.passed, sum(x.passed for x in c.checks), c.stats["mean_excess"] or -9),
             reverse=True)
    return out


# ---------------------------------------------------------------- signal content
def _fee_estimate(ds: Dataset, state_at_fn, lower: int, upper: int, liq: int, block: int,
                  days: int) -> dict:
    """Fees the exact range would have earned over the last `days` (A/B), replayed."""
    ts = ds.blocks[block][0]
    blocks = sorted(b for b in ds.blocks if b <= block)
    start_i = bisect.bisect_left([ds.blocks[b][0] for b in blocks], ts - days * DAY)
    start_block = blocks[max(0, start_i - 1)]
    replay = PoolReplay(ds.fee, ds.tick_spacing)
    replay.s = copy.deepcopy(state_at_fn(start_block))
    meter = FeeMeter(lower, upper, liq)
    replay.swap_listeners.append(meter.on_swap)
    replay.flash_listeners.append(meter.on_flash)
    ev_blocks = [e.block for e in ds.events]
    replay.run(ds.events[bisect.bisect_right(ev_blocks, start_block):], until_block=block)
    span_days = (ts - ds.blocks[start_block][0]) / DAY
    return {"days": round(span_days, 2), "a": meter.fees_a(), "b": meter.fees_b(),
            "in_range_steps": meter.steps_in_range}


def build_open_candidate(ds: Dataset, state: PoolState, snap: MarketSnapshot, cand: Candidate,
                         cfg: dict, state_at_fn, now_ts: int, pool_meta: dict) -> dict:
    d0, d1 = ds.decimals0, ds.decimals1
    capital = int(cfg["capital_usd"] * 10 ** d1)
    spec = cand.params.to_spec(state.block + 1, state.block + 2, capital)
    replay = PoolReplay(ds.fee, ds.tick_spacing)
    replay.s = copy.deepcopy(state)
    market = MarketView()
    _prefill_market(market, ds, state.block)
    sim = _Sim(ds, spec, replay, market)
    sim.now_ts = snap.timestamp
    sim.y = capital
    lower, upper = sim._range(state.tick)
    swap_in, swap_cost = sim._swap_to_ratio(lower, upper)
    sa, sb = um.get_sqrt_ratio_at_tick(lower), um.get_sqrt_ratio_at_tick(upper)
    sp = state.sqrt_price
    liq = um.get_liquidity_for_amounts(sp, sa, sb, sim.x, sim.y)
    a0, a1 = um.mint_amounts(sp, state.tick, lower, upper, liq)
    while a0 > sim.x or a1 > sim.y:
        liq -= max(1, liq // 10 ** 12)
        a0, a1 = um.mint_amounts(sp, state.tick, lower, upper, liq)
    deposit_value = _value1(a0, a1, sp)
    gas = ds.gas_units["gas_units"]
    open_units = gas["mint_new_position"] + (gas["swap_for_rebalance"] if swap_in else 0)
    close_units = gas["decrease_liquidity_and_collect"]
    gas_open = sim._gas_cost_token1(state.block, open_units)
    gas_close = sim._gas_cost_token1(state.block, close_units)
    gas_total = gas_open + gas_close
    # the swap cost is already inside the post-swap holdings (sim.x, sim.y); only gas is
    # subtracted from the forecasts, the swap cost is reported separately
    costs = gas_total
    H = cfg["validity_days"]
    fee_rates = []
    windows = {}
    for days in cfg["fee_lookback_days"]:
        est = _fee_estimate(ds, state_at_fn, lower, upper, liq, state.block, days)
        va, vb = _value1(*est["a"], sp), _value1(*est["b"], sp)
        windows[f"{days}d"] = {"days": est["days"], "fees_a_usd": va / 10 ** d1, "fees_b_usd": vb / 10 ** d1}
        if est["days"] > 0:
            fee_rates += [va / est["days"], vb / est["days"]]
    fees_lo, fees_hi = (min(fee_rates) * H, max(fee_rates) * H) if fee_rates else (0, 0)
    sigma = (snap.vol_7d or 0) * math.sqrt(H / 365)
    scen = []
    for ch in sorted(set(cfg["price_scenarios"]) | {-round(sigma, 4), round(sigma, 4)}):
        sp2 = int(sp * math.sqrt(max(1e-9, 1 + ch)))
        p0, p1 = um.get_amounts_for_liquidity(sp2, sa, sb, liq)
        lp = _value1(p0 + sim.x - a0, p1 + sim.y - a1, sp2)
        hodl = _value1(sim.x, sim.y, sp2)  # holding the post-swap tokens
        il = _value1(p0, p1, sp2) - _value1(a0, a1, sp2)
        scen.append({"price_change": ch, "price": human_price(sp2, d0, d1),
                     "il_usd": il / 10 ** d1,
                     "net_vs_hodl_usd": [(il + fees_lo - costs) / 10 ** d1, (il + fees_hi - costs) / 10 ** d1],
                     "net_usd": [(lp + fees_lo - capital - costs) / 10 ** d1,
                                 (lp + fees_hi - capital - costs) / 10 ** d1],
                     "hodl_usd": (hodl - capital) / 10 ** d1})
    at_sigma = [x for x in scen if abs(abs(x["price_change"]) - round(sigma, 4)) < 1e-9]
    net_lo = min(x["net_usd"][0] for x in at_sigma) if at_sigma else None
    net_hi = max(x["net_usd"][1] for x in at_sigma) if at_sigma else None
    st = cand.stats
    rebal = cand.params.rebalance != "none"
    return {
        "type": "OPEN CANDIDATE", "created_at": now_ts, "block": state.block, **pool_meta,
        "current_price": snap.price, "strategy": cand.params.name, "spec_hash": cand.params.spec_hash,
        "params": asdict(cand.params),
        "range": {"tick_lower": lower, "tick_upper": upper,
                  "price_lower": human_price(sa, d0, d1), "price_upper": human_price(sb, d0, d1)},
        "allocation": {"token0": a0 / 10 ** d0, "token1": a1 / 10 ** d1,
                       "usdc_value_share": a1 / deposit_value if deposit_value else None,
                       "deposit_usd": deposit_value / 10 ** d1,
                       "idle_usd": _value1(sim.x - a0, sim.y - a1, sp) / 10 ** d1,
                       "swap_needed": swap_in != 0, "liquidity": str(liq)},
        "expected_fees_usd": [fees_lo / 10 ** d1, fees_hi / 10 ** d1], "horizon_days": H,
        "fee_windows": windows,
        "expected_costs_usd": {"gas_open": gas_open / 10 ** d1, "gas_close": gas_close / 10 ** d1,
                               "swap": swap_cost / 10 ** d1,
                               "total": (gas_total + swap_cost) / 10 ** d1},
        "il_scenarios": scen, "sigma_horizon": sigma,
        "net_forecast_usd": [net_lo, net_hi],
        "historical": {"max_drawdown": st["max_mdd"], "mean_drawdown": st["mean_mdd"],
                       "oos_mean_excess_vs_passive": st["mean_excess"],
                       "oos_positive_segments": st["positive_excess_segments"],
                       "oos_mean_roi": st["oos_mean_roi"], "is_mean_excess": st["is_mean_excess"]},
        "confidence": cand.confidence, "oos_contaminated": cand.contaminated,
        "reasons": [asdict(c) for c in cand.checks],
        "invalidation": {
            "price_outside_range": not rebal,
            "rebalance_rule": cand.params.rebalance,
            "max_vol_7d": round(max(1.5 * (snap.vol_7d or 0), 0.3), 4),
            "trend_must_stay": snap.trend,
            "max_data_age_minutes": cfg["max_data_age_minutes"]},
        "valid_until": now_ts + H * DAY,
        "label": "SCENARIO",
    }


# ---------------------------------------------------------------- monitoring
def monitor(payload: dict, snap: MarketSnapshot, cand: Candidate | None, now_ts: int) -> tuple[str, str]:
    inv = payload["invalidation"]
    rng = payload["range"]
    if now_ts > payload["valid_until"]:
        return "INVALIDATED", "signal expired (valid_until passed)"
    if snap.vol_7d is not None and snap.vol_7d > inv["max_vol_7d"]:
        return "INVALIDATED", f"7d volatility {snap.vol_7d:.1%} above {inv['max_vol_7d']:.1%}"
    if snap.trend != inv["trend_must_stay"]:
        return "INVALIDATED", f"market regime changed: trend {inv['trend_must_stay']} -> {snap.trend}"
    if cand is None or not cand.passed:
        failed = [c.name for c in cand.checks if not c.passed] if cand else ["no results"]
        return "CLOSE CANDIDATE", "strategy no longer passes quality filters: " + ", ".join(failed)
    inside = rng["tick_lower"] <= snap.tick < rng["tick_upper"]
    if not inside:
        if inv["rebalance_rule"] == "none":
            return "CLOSE CANDIDATE", "price left the range of a non-rebalancing strategy"
        return "REBALANCE", "price left the range; the strategy re-centers"
    return "HOLD", "conditions still hold"


# ---------------------------------------------------------------- orchestration
def contaminated_specs(engine) -> set[str]:
    out = set()
    with engine.connect() as c:
        for hid, rj in c.execute(select(S.hypotheses.c.id, S.hypotheses.c.result_json)):
            if rj and json.loads(rj).get("oos_contaminated"):
                for (h,) in c.execute(select(S.hypothesis_experiments.c.spec_hash).where(
                        S.hypothesis_experiments.c.hypothesis_id == hid)):
                    out.add(h)
    return out


def _latest_signal(engine, pool: str):
    with engine.connect() as c:
        sig = c.execute(select(S.signals).where(S.signals.c.pool == pool)
                        .order_by(S.signals.c.id.desc()).limit(1)).first()
        if sig is None:
            return None, None
        ev = c.execute(select(S.signal_events).where(S.signal_events.c.signal_id == sig.id)
                       .order_by(S.signal_events.c.id.desc()).limit(1)).first()
    return sig, ev


def evaluate_market(engine, ds: Dataset, state: PoolState, snap: MarketSnapshot, table: dict,
                    strategies: dict[str, StrategyParams], study: Study, cfg: dict, pool_meta: dict,
                    state_at_fn, now_ts: int | None = None) -> Decision:
    now_ts = now_ts or int(time.time())
    pc = pool_checks(snap, cfg)
    cands = evaluate_candidates(table, strategies, study, snap, cfg, contaminated_specs(engine))
    by_hash = {c.params.spec_hash: c for c in cands}
    sig, ev = _latest_signal(engine, pool_meta["pool"])
    status = ev.status if ev else None
    reasons: list = []
    decision, signal_id, payload = "WAIT", None, {}
    with engine.begin() as conn:
        if sig is not None and sig.signal_type == "OPEN CANDIDATE" and status in ACTIVE:
            p = json.loads(sig.payload_json)
            if not all(c.passed for c in pc):
                new, why = status, "market data not current/complete: status not reassessed"
            else:
                new, why = monitor(p, snap, by_hash.get(sig.spec_hash), now_ts)
            if new != status or why != ev.reason:
                conn.execute(insert(S.signal_events).values(
                    signal_id=sig.id, created_at=now_ts, block=snap.block, status=new, reason=why,
                    details_json=json.dumps({"price": snap.price, "tick": snap.tick})))
            decision, signal_id, reasons, payload = new, sig.id, [why], p
        else:
            passed = [c for c in cands if c.passed] if all(c.passed for c in pc) else []
            if passed:
                best = passed[0]
                payload = build_open_candidate(ds, state, snap, best, cfg, state_at_fn, now_ts, pool_meta)
                signal_id = conn.execute(insert(S.signals).values(
                    created_at=now_ts, block=snap.block, pool=pool_meta["pool"],
                    signal_type="OPEN CANDIDATE", spec_hash=best.params.spec_hash,
                    payload_json=json.dumps(payload), label="SCENARIO",
                    parent_id=sig.id if sig else None)).inserted_primary_key[0]
                conn.execute(insert(S.signal_events).values(
                    signal_id=signal_id, created_at=now_ts, block=snap.block, status="OPEN CANDIDATE",
                    reason="strategy passed all quality filters", details_json=None))
                decision, reasons = "OPEN CANDIDATE", [asdict(c) for c in best.checks]
            else:
                reasons = [asdict(c) for c in pc if not c.passed] or [{
                    "name": "no_validated_strategy", "passed": False,
                    "value": f"0 of {len(cands)} strategies pass all filters",
                    "threshold": "all SPEC 11.4 checks"}]
                closest = [{"strategy": c.params.name,
                            "failed": [x.name for x in c.checks if not x.passed],
                            "oos_mean_excess": c.stats["mean_excess"], "oos_mean_roi": c.stats["oos_mean_roi"]}
                           for c in cands[:5]]
                payload = {"type": "WAIT", "created_at": now_ts, "block": snap.block, **pool_meta,
                           "current_price": snap.price, "reasons": reasons, "closest_candidates": closest,
                           "label": "SCENARIO"}
                key = json.dumps([r["name"] for r in reasons])
                same = (sig is not None and sig.signal_type == "WAIT" and status == "WAIT"
                        and ev.reason == key)
                if not same:  # a new WAIT only when the situation changes (no spam)
                    signal_id = conn.execute(insert(S.signals).values(
                        created_at=now_ts, block=snap.block, pool=pool_meta["pool"], signal_type="WAIT",
                        spec_hash=None, payload_json=json.dumps(payload), label="SCENARIO",
                        parent_id=sig.id if sig else None)).inserted_primary_key[0]
                    conn.execute(insert(S.signal_events).values(
                        signal_id=signal_id, created_at=now_ts, block=snap.block, status="WAIT",
                        reason=key, details_json=json.dumps(closest)))
                    if sig is not None and status in ACTIVE:
                        conn.execute(insert(S.signal_events).values(
                            signal_id=sig.id, created_at=now_ts, block=snap.block, status="INVALIDATED",
                            reason="superseded by WAIT", details_json=None))
                else:
                    signal_id = sig.id
        conn.execute(insert(S.market_evaluations).values(
            created_at=now_ts, block=snap.block, decision=decision,
            snapshot_json=json.dumps(snap.to_dict()), reasons_json=json.dumps(reasons, default=str)))
    return Decision(decision, signal_id, snap.to_dict(), reasons, payload)


def load_strategies(engine) -> dict[str, StrategyParams]:
    with engine.connect() as c:
        return {h: params_from_json(pj) for h, pj in
                c.execute(select(S.strategies.c.spec_hash, S.strategies.c.params_json))}
