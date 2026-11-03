"""Current LP opportunities (SCENARIO), built on the stored historical study.

The historical research (segment table, robustness scores) is NOT recomputed here:
every strategy is judged by its stored results, and only the market-dependent part is
evaluated now — range at the current price, a fee estimate replayed over the recent
past on that exact range, impermanent loss at +-1 sigma, gas, the chance of leaving the
range, and the expected result versus holding the tokens.

Tiers (never mixed with the strict signal, which keeps all its filters):
  CONSERVATIVE  passes every signal filter (SPEC 11.4) and the OOS check is clean
  MODERATE      clean OOS evidence: beats the passive LP on unseen segments, positive
                OOS ROI, survives gas x2, acceptable drawdown; may fail stability across
                periods or the current-regime fit
  EXPERIMENTAL  promising but unconfirmed: OOS-contaminated (designed after seeing OOS),
                or a thinner OOS record (mean excess > 0 with fewer positive segments)
A strategy enters a tier only if the expected result versus HODL for the horizon is
positive after costs; otherwise it is listed in the diagnostics with the reasons.
"""

from __future__ import annotations

import math

from backend.signal_engine.engine import Candidate, build_open_candidate, pool_checks
from backend.signal_engine.market import MarketSnapshot

TIERS = ("CONSERVATIVE", "MODERATE", "EXPERIMENTAL")
OOS_CORE = ("sufficient_history", "oos_beats_passive", "oos_positive_roi", "acceptable_drawdown",
            "costs_included_robust")
PER_TIER = 6          # forecasts computed per tier (the replay is the costly part)
VIRTUAL_PER_TIER = 3  # best of each tier are followed in the virtual portfolio

# plain-language diagnostics (Signal Diagnostics)
WHY_RU = {
    "data_current": "данные пула не свежие",
    "pool_activity": "недостаточный объём торгов в пуле",
    "market_data_complete": "не хватает рыночных данных (волатильность, газ, ETH/USD)",
    "sufficient_history": "мало истории: нет результатов на всех периодах",
    "oos_beats_passive": "на данных, которые стратегия не видела, не лучше простого размещения",
    "oos_positive_roi": "на данных, которые стратегия не видела, в минусе",
    "stable_across_periods": "нестабильна: в периодах, на которых её подбирали, проигрывала простому размещению",
    "acceptable_drawdown": "слишком глубокие исторические просадки",
    "costs_included_robust": "не выдерживает рост газа ×2 или разорялась",
    "fits_current_regime": "в похожем рынке исторически не зарабатывала",
    "oos_contaminated": "параметры придуманы после просмотра проверочных данных — нужна проверка на новых данных",
    "negative_net": "после расходов ожидается хуже, чем просто хранить токены",
    "gas_heavy": "газ съедает больше четверти ожидаемых комиссий",
    "exit_risk": "высокий риск выхода цены из диапазона за горизонт",
    "high_volatility": "высокая волатильность рынка сейчас",
    "low_active_liquidity": "низкая активная ликвидность в пуле",
}
CHANGE_RU = {
    "stable_across_periods": "новые отрезки истории (каждые 91 день) должны подтвердить преимущество",
    "fits_current_regime": "рынок должен перейти в режим, где стратегия раньше зарабатывала",
    "oos_contaminated": "положительный результат в виртуальном портфеле на новых данных",
    "pool_activity": "объём торгов в пуле должен вырасти",
    "data_current": "дождаться загрузки новых блоков",
    "negative_net": "больше комиссий (объём) или ниже газ",
    "gas_heavy": "газ в сети Ethereum должен снизиться или капитал быть больше",
    "exit_risk": "волатильность должна снизиться",
    "high_volatility": "волатильность должна снизиться",
}


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def exit_probability(price: float, lower: float, upper: float, sigma_h: float) -> float | None:
    """Chance that the price touches a range bound within the horizon (driftless
    log-Brownian motion, reflection principle; an approximation, SCENARIO)."""
    if sigma_h <= 0 or not lower < price < upper:
        return None if sigma_h <= 0 else 1.0
    up, down = math.log(upper / price), math.log(price / lower)
    return min(1.0, 2 * (1 - _phi(up / sigma_h)) + 2 * (1 - _phi(down / sigma_h)))


def tier_of(c: Candidate) -> tuple[str | None, list[str]]:
    """Tier from the stored historical evidence, and what keeps it from a higher one."""
    failed = [x.name for x in c.checks if not x.passed]
    blockers = list(failed) + (["oos_contaminated"] if c.contaminated else [])
    if not failed and not c.contaminated:
        return "CONSERVATIVE", blockers
    if not c.contaminated and not any(f in OOS_CORE for f in failed):
        return "MODERATE", blockers
    st = c.stats
    thin_ok = (st.get("mean_excess") or -1) > 0 and (st.get("oos_mean_roi") or -1) > 0
    if not any(f in ("sufficient_history", "costs_included_robust") for f in failed) and thin_ok:
        return "EXPERIMENTAL", blockers
    return None, blockers


def _risk(p_exit: float | None, max_mdd: float, mean_mdd: float) -> tuple[str, str]:
    """Risk level from the chance of leaving the range now and the historical drawdowns
    (worst segment and average). Returns (level, explanation)."""
    p = 1.0 if p_exit is None else p_exit
    why = (f"выход из диапазона за горизонт ~{p:.0%}; просадка в истории: средняя "
           f"{mean_mdd:.0%}, худшая {max_mdd:.0%}")
    if p > 0.6 or max_mdd > 0.45 or mean_mdd > 0.2:
        return "high", why
    if p > 0.3 or max_mdd > 0.25 or mean_mdd > 0.1:
        return "medium", why
    return "low", why


def opportunity(ds, state, snap: MarketSnapshot, cand: Candidate, tier: str, blockers: list[str],
                cfg: dict, state_at_fn, now_ts: int, pool_meta: dict, robustness: float | None,
                hist: dict) -> dict:
    """One opportunity card: the open-candidate content plus risk, expected result vs
    HODL and the reasons."""
    p = build_open_candidate(ds, state, snap, cand, cfg, state_at_fn, now_ts, pool_meta)
    sigma = p["sigma_horizon"]
    at_sigma = [s for s in p["il_scenarios"] if abs(abs(s["price_change"]) - round(sigma, 4)) < 1e-9]
    il_exp = sum(s["il_usd"] for s in at_sigma) / len(at_sigma) if at_sigma else 0.0
    fees = p["expected_fees_usd"]
    gas = p["expected_costs_usd"]["gas_open"] + p["expected_costs_usd"]["gas_close"]
    swap = p["expected_costs_usd"]["swap"]
    net_vs_hodl = [fees[0] + il_exp - gas - swap, fees[1] + il_exp - gas - swap]
    rng = p["range"]
    p_exit = exit_probability(snap.price, rng["price_lower"], rng["price_upper"], sigma)
    risk, risk_why = _risk(p_exit, cand.stats["max_mdd"], cand.stats["mean_mdd"])
    issues = []
    if net_vs_hodl[1] <= 0:
        issues.append("negative_net")
    if fees[1] > 0 and gas > 0.25 * fees[1]:
        issues.append("gas_heavy")
    if p_exit is not None and p_exit > 0.6:
        issues.append("exit_risk")
    mid = (net_vs_hodl[0] + net_vs_hodl[1]) / 2
    capital = int(p["capital_token1"]) / 10 ** ds.decimals1
    score = None
    if robustness is not None:
        score = round(robustness * 100 * (1 - (p_exit or 0) * 0.5) * (1 if mid > 0 else 0.5), 1)
    reasons = []
    st = cand.stats
    reasons.append(f"на данных, которые стратегия раньше не видела, была лучше простого размещения в "
                   f"{st['positive_excess_segments']} из 7 периодов по 3 месяца, в среднем на {st['mean_excess'] * 100:+.1f}%")
    if (st.get("oos_mean_roi") or 0) > 0:
        reasons.append(f"на этих данных в среднем зарабатывала {st['oos_mean_roi'] * 100:+.1f}% за 3 месяца")
    if "fits_current_regime" not in blockers and st.get("regime_segments"):
        reasons.append(f"зарабатывала при похожем рынке ({st['regime_segments']} периода)")
    if fees[0] > 0:
        reasons.append(f"за последние дни такой диапазон собрал бы ${fees[0]:,.0f}–${fees[1]:,.0f} комиссий "
                       f"за {p['horizon_days']} дн.")
    return {
        "tier": tier, "strategy": cand.params.name, "spec_hash": cand.params.spec_hash,
        "pool": pool_meta["pool"], "pair": pool_meta["pair"], "pool_address": pool_meta["pool_address"],
        "network": pool_meta["network"], "fee_tier": pool_meta["fee_tier"],
        "current_price": snap.price, "range": rng, "allocation": p["allocation"],
        "rebalance_rule": cand.params.rebalance, "horizon_days": p["horizon_days"],
        "expected_fees_usd": fees, "expected_il_usd": il_exp, "costs_usd": p["expected_costs_usd"],
        "expected_net_vs_hodl_usd": net_vs_hodl, "expected_net_mid_usd": mid,
        "expected_net_mid_pct": mid / capital if capital else None,
        "net_forecast_usd": p["net_forecast_usd"], "il_scenarios": p["il_scenarios"],
        "exit_probability": p_exit, "risk": risk, "risk_why": risk_why, "sigma_horizon": sigma,
        "confidence": ("high" if tier == "CONSERVATIVE" and cand.confidence == "high" else
                       "medium" if tier in ("CONSERVATIVE", "MODERATE") else "low"),
        "historical": {**p["historical"], "robustness_score": robustness, **hist},
        "reasons": reasons, "blockers": blockers + issues,
        "blockers_ru": [WHY_RU.get(b, b) for b in blockers + issues],
        "to_improve_ru": [CHANGE_RU[b] for b in blockers + issues if b in CHANGE_RU],
        "perspective_score": score, "oos_contaminated": cand.contaminated,
        "valid_until": p["valid_until"], "label": "SCENARIO",
    }


def market_diagnostics(snap: MarketSnapshot, cfg: dict, vol_hist: list[float], d0: int = 8,
                       d1: int = 6) -> list[dict]:
    out = [{"name": c.name, "passed": c.passed, "value": c.value, "threshold": c.threshold,
            "ru": WHY_RU.get(c.name, c.name)} for c in pool_checks(snap, cfg)]
    if snap.vol_7d is not None and vol_hist:
        hi = sorted(vol_hist)[int(0.8 * (len(vol_hist) - 1))]
        out.append({"name": "high_volatility", "passed": snap.vol_7d <= hi,
                    "value": round(snap.vol_7d, 4), "threshold": f"<= {hi:.2f} (80% исторических)",
                    "ru": WHY_RU["high_volatility"]})
    act = active_liquidity_usd(snap, d0, d1)
    if snap.tvl_usd:
        share = act / snap.tvl_usd
        out.append({"name": "low_active_liquidity", "passed": share >= 0.05, "value": round(share, 4),
                    "threshold": ">= 5% TVL в пределах ±2% от цены", "ru": WHY_RU["low_active_liquidity"]})
    return out


def active_liquidity_usd(snap: MarketSnapshot, d0: int, d1: int, band: float = 0.02) -> float:
    """USD value of the liquidity active at the current tick, counted over +-band of the
    price (the current tick's L assumed constant over the band; EXACT L, MODELED value)."""
    L = int(snap.active_liquidity)
    sp = snap.sqrt_price / 2 ** 96
    sa, sb = sp / math.sqrt(1 + band), sp * math.sqrt(1 + band)
    a0 = L * (1 / sp - 1 / sb) / 10 ** d0
    a1 = L * (sp - sa) / 10 ** d1
    return a0 * snap.price + a1


def rank_opportunities(ds, state, snap: MarketSnapshot, cands: list[Candidate], table: dict, study,
                       cfg: dict, state_at_fn, now_ts: int, pool_meta: dict,
                       robustness: dict[str, float]) -> dict:
    oos = [s for s in study.segments if s.role == "OOS"]
    pool_ok = all(c.passed for c in pool_checks(snap, cfg))
    by_tier: dict[str, list] = {t: [] for t in TIERS}
    diagnostics = []
    for c in cands:
        tier, blockers = tier_of(c)
        if tier is None:
            if len(diagnostics) < 15:
                diagnostics.append({"strategy": c.params.name, "spec_hash": c.params.spec_hash,
                                    "tier": None, "blockers": blockers,
                                    "blockers_ru": [WHY_RU.get(b, b) for b in blockers],
                                    "oos_mean_excess": c.stats["mean_excess"],
                                    "robustness_score": robustness.get(c.params.spec_hash)})
            continue
        by_tier[tier].append((c, blockers))
    out: dict = {t: [] for t in TIERS}
    if pool_ok:
        for t in TIERS:
            picks = sorted(by_tier[t], key=lambda x: -(robustness.get(x[0].params.spec_hash) or 0)
                           - (x[0].stats["mean_excess"] or 0))[:PER_TIER * 2]
            for c, blockers in picks:
                h = c.params.spec_hash
                ms = [table.get((h, s.index, "base")) for s in oos]
                hist = {"oos_time_out_of_range": _mean(m.get("time_out_of_range") for m in ms if m),
                        "oos_rebalances": _mean(m.get("rebalances") for m in ms if m)}
                o = opportunity(ds, state, snap, c, t, blockers, cfg, state_at_fn, now_ts, pool_meta,
                                robustness.get(h), hist)
                if "negative_net" in o["blockers"]:
                    diagnostics.insert(0, {**{k: o[k] for k in ("strategy", "spec_hash", "blockers",
                                                                 "blockers_ru")},
                                           "tier": t, "oos_mean_excess": c.stats["mean_excess"],
                                           "robustness_score": robustness.get(h),
                                           "expected_net_mid_usd": o["expected_net_mid_usd"]})
                    continue
                out[t].append(o)
            out[t] = _group_same_position(sorted(out[t], key=lambda o: -(o["perspective_score"] or 0)))
    vol_hist = [s.realized_vol for s in study.segments]
    return {"created_at": now_ts, "block": snap.block, "pool": pool_meta["pool"],
            "market": {**snap.to_dict(),
                       "active_liquidity_usd_2pct": active_liquidity_usd(snap, ds.decimals0, ds.decimals1),
                       "gas_gwei": snap.base_fee_wei / 1e9 if snap.base_fee_wei else None},
            "tiers": out,
            "market_checks": market_diagnostics(snap, cfg, vol_hist, ds.decimals0, ds.decimals1),
            "diagnostics": diagnostics[:20], "counts": {t: len(by_tier[t]) for t in TIERS},
            "strict_signal_filters_unchanged": True, "label": "SCENARIO"}


def _group_same_position(opps: list[dict]) -> list[dict]:
    """Strategies that open the very same position now (same range and deposit) differ only
    in their later rebalance rule: show one card, list the others as variants."""
    out, seen = [], {}
    for o in opps:
        key = (o["range"]["tick_lower"], o["range"]["tick_upper"], round(o["allocation"]["token1"], 2))
        if key in seen:
            seen[key]["variants"].append({"strategy": o["strategy"], "spec_hash": o["spec_hash"],
                                          "rebalance_rule": o["rebalance_rule"],
                                          "perspective_score": o["perspective_score"]})
            continue
        o["variants"] = []
        seen[key] = o
        out.append(o)
    return out[:PER_TIER]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


# ---------------------------------------------------------------- storage and history
def _tiers_of(payload: dict) -> dict[str, tuple[str, str]]:
    out = {}
    for tier, opps in payload.get("tiers", {}).items():
        for o in opps:
            out[o["spec_hash"]] = (tier, o["strategy"])
            for v in o.get("variants", []):
                out.setdefault(v.get("spec_hash", v["strategy"]), (tier, v["strategy"]))
    return out


def save_ranking(engine, ranking: dict) -> list[dict]:
    """Keep only the latest ranking; log every tier change as an event. Returns the events."""
    import json

    from sqlalchemy import delete, insert, select

    from backend.database import schema as S
    with engine.begin() as conn:
        prev = conn.execute(select(S.opportunity_rankings.c.payload_json).where(
            S.opportunity_rankings.c.pool == ranking["pool"])
            .order_by(S.opportunity_rankings.c.id.desc()).limit(1)).scalar()
        before = _tiers_of(json.loads(prev)) if prev else {}
        after = _tiers_of(ranking)
        events = []
        for h in sorted(set(before) | set(after)):
            tf = before.get(h, (None, None))[0]
            tt = after.get(h, (None, None))[0]
            if tf != tt:
                name = (after.get(h) or before.get(h))[1]
                events.append({"spec_hash": h, "strategy": name, "tier_from": tf, "tier_to": tt})
        if events:
            conn.execute(insert(S.opportunity_events), [dict(
                created_at=ranking["created_at"], block=ranking["block"], pool=ranking["pool"],
                details_json=None, **e) for e in events])
        conn.execute(delete(S.opportunity_rankings).where(S.opportunity_rankings.c.pool == ranking["pool"]))
        conn.execute(insert(S.opportunity_rankings).values(
            created_at=ranking["created_at"], block=ranking["block"], pool=ranking["pool"],
            payload_json=json.dumps(ranking, default=str)))
    return events


def load_robustness(engine, version: str) -> dict[str, float]:
    """Stored robustness scores (0..1) of the historical validation, latest per strategy."""
    from sqlalchemy import select

    from backend.database import schema as S
    with engine.connect() as c:
        return {h: s / 10_000 for h, s in c.execute(
            select(S.validation_reports.c.spec_hash, S.validation_reports.c.score)
            .where(S.validation_reports.c.code_version == version).order_by(S.validation_reports.c.id))}
