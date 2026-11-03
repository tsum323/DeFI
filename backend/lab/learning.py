"""Self-learning (UNISWAP AI LAB): compare forecasts with what actually happened, find the
reasons for the errors, and turn them into new hypotheses that are then tested.

What may change automatically: hypotheses, strategy parameters, experiments, rankings,
conclusions, and the fee-forecast calibration factor (a parameter, shown in the UI).
What never changes automatically: the math engine, the backtester, the quality filters.

Cycle:
  1. record_forecast()  — when a virtual position opens from an opportunity, the card's
     numbers (fees, loss vs holding, chance to leave the range, result) are frozen.
  2. review_forecasts() — daily: interim comparison pro rata; at the horizon (7 days) the
     final review with the reasons for every material error -> `lessons`.
  3. LearningAdapter    — rule-based researcher (the same interface as the Claude adapter):
     proposes hypotheses from lessons, near-miss strategies and the market regime; the
     existing research loop runs them on the stored history and records verdicts.
A strategy is never called successful because of a few good days: verdicts come only from
the historical validation, and the forward results are shown as they are.
"""

from __future__ import annotations

import json
import math
import statistics
import time

from sqlalchemy import insert, select, update

from backend.ai_researcher.schema import (
    Commentary,
    HypothesisBatch,
    HypothesisDraft,
    StrategyProposal,
)
from backend.database import schema as S
from backend.strategies.generator import StrategyParams, params_from_json

DAY = 86_400
MIN_REVIEWS_FOR_CALIBRATION = 3

REASON_RU = {
    "price_faster": "цена двигалась быстрее ожиданий",
    "vol_higher": "волатильность оказалась выше прогноза",
    "vol_lower": "волатильность оказалась ниже прогноза",
    "fees_lower": "комиссии оказались ниже прогноза",
    "fees_higher": "комиссии оказались выше прогноза",
    "out_of_range": "позиция долго находилась вне диапазона",
    "costs_ate": "расходы на перестановки съели доходность",
    "il_worse": "потеря относительно хранения токенов больше прогноза",
}


# ---------------------------------------------------------------- forecasts
def record_forecast(engine, forward_test_id: int, card: dict, now: int | None = None) -> int:
    """Freeze the opportunity card's predictions for a new virtual position."""
    f = {"fees_usd": card["expected_fees_usd"], "il_usd": card["expected_il_usd"],
         "costs_usd": card["costs_usd"]["gas_open"] + card["costs_usd"]["gas_close"] + card["costs_usd"]["swap"],
         "net_vs_hodl_usd": card["expected_net_vs_hodl_usd"], "exit_probability": card["exit_probability"],
         "entry_price": card["current_price"], "range": [card["range"]["price_lower"], card["range"]["price_upper"]],
         "sigma_horizon": card.get("sigma_horizon"), "capital_usd": card["allocation"]["deposit_usd"]}
    with engine.begin() as conn:
        row = conn.execute(select(S.forecasts.c.id).where(S.forecasts.c.forward_test_id == forward_test_id)).first()
        if row:
            return row[0]
        return conn.execute(insert(S.forecasts).values(
            created_at=now or int(time.time()), forward_test_id=forward_test_id, spec_hash=card["spec_hash"],
            strategy=card["strategy"], tier=card["tier"], horizon_days=card["horizon_days"],
            forecast_json=json.dumps(f), status="open")).inserted_primary_key[0]


def compare(forecast: dict, horizon_days: int, actual: dict, usd: float = 1e6) -> dict:
    """Forecast vs actual so far (pro rata for interim checks). `actual` is a forward-test
    result document (token1 raw units, USDC -> divide by `usd`)."""
    days = actual.get("days") or 0
    k = min(1.0, days / horizon_days) if horizon_days else 1.0
    fees_fc = [x * k for x in forecast["fees_usd"]]
    fees = actual["fees_a"] / usd
    il = actual.get("il", 0) / usd
    costs = (actual.get("gas_cost", 0) + actual.get("swap_cost", 0)) / usd
    out = {"days": days, "fraction": k, "fees_actual": fees, "fees_forecast": fees_fc,
           "il_actual": il, "il_forecast": forecast["il_usd"] * k, "costs_actual": costs,
           "time_in_range": actual.get("time_in_range"), "rebalances": actual.get("rebalances"),
           "vs_hodl_actual": actual.get("vs_hodl_a", 0) / usd,
           "fee_ratio": fees / ((fees_fc[0] + fees_fc[1]) / 2) if sum(fees_fc) > 0 else None}
    entry = (actual.get("entry") or {}).get("price") or forecast.get("entry_price")
    now_p = (actual.get("now") or {}).get("price")
    if entry and now_p:
        out["price_change"] = now_p / entry - 1
    return out


def reasons_for(cmp: dict, forecast: dict) -> list[str]:
    """Material reasons why the result differs from the forecast (deterministic rules)."""
    r = []
    sig = forecast.get("sigma_horizon") or 0
    if sig and cmp.get("price_change") is not None and abs(cmp["price_change"]) > 1.5 * sig * math.sqrt(max(cmp["fraction"], 1e-9)):
        r.append("price_faster")
    lo, hi = cmp["fees_forecast"]
    if cmp["fees_actual"] < 0.7 * lo:
        r.append("fees_lower")
    elif cmp["fees_actual"] > 1.3 * hi and hi > 0:
        r.append("fees_higher")
    if cmp.get("time_in_range") is not None and cmp["time_in_range"] < 0.8:
        r.append("out_of_range")
    if cmp["costs_actual"] > 0.5 * max(cmp["fees_actual"], 1e-9) and (cmp.get("rebalances") or 0) > 0:
        r.append("costs_ate")
    if cmp["il_actual"] < 1.5 * cmp["il_forecast"] - 1:  # both negative: more loss than expected
        r.append("il_worse")
    return r


def review_forecasts(engine, now: int | None = None) -> dict:
    """Interim checkpoints for open forecasts; final reviews (+ lessons) at the horizon."""
    now = now or int(time.time())
    with engine.connect() as conn:
        open_fc = conn.execute(select(S.forecasts).where(S.forecasts.c.status == "open")).all()
        fts = {f.id: f for f in conn.execute(select(S.forward_tests))}
    out = {"checked": 0, "reviewed": 0, "lessons": []}
    for fc in open_fc:
        ft = fts.get(fc.forward_test_id)
        if ft is None or not ft.result_json:
            continue
        res = json.loads(ft.result_json)
        if "error" in res:
            continue
        forecast = json.loads(fc.forecast_json)
        cmp = compare(forecast, fc.horizon_days, res)
        reasons = reasons_for(cmp, forecast)
        cmp["reasons"] = reasons
        final = (res.get("days") or 0) >= fc.horizon_days
        with engine.begin() as conn:
            vals = {"checkpoint_json": json.dumps(cmp)}
            if final:
                vals.update(status="reviewed", review_json=json.dumps(cmp), reviewed_at=now)
                action = plan_for(reasons, params_from_json(ft.params_json))
                conn.execute(insert(S.lessons).values(
                    created_at=now, forecast_id=fc.id, spec_hash=fc.spec_hash, strategy=fc.strategy,
                    reasons_json=json.dumps(reasons), action=action["text"]))
                out["lessons"].append({"strategy": fc.strategy, "reasons": reasons, "action": action["text"]})
                out["reviewed"] += 1
            conn.execute(update(S.forecasts).where(S.forecasts.c.id == fc.id).values(**vals))
        out["checked"] += 1
    return out


def fee_calibration(engine) -> dict:
    """Median ratio actual / forecast fees over final reviews (a forecast parameter)."""
    with engine.connect() as conn:
        rows = [json.loads(r) for r in conn.execute(select(S.forecasts.c.review_json)
                                                    .where(S.forecasts.c.status == "reviewed")).scalars() if r]
    ratios = [r["fee_ratio"] for r in rows if r.get("fee_ratio") is not None]
    if len(ratios) < MIN_REVIEWS_FOR_CALIBRATION:
        return {"factor": None, "reviews": len(ratios),
                "note": f"нужно минимум {MIN_REVIEWS_FOR_CALIBRATION} завершённых проверки прогноза"}
    return {"factor": statistics.median(ratios), "reviews": len(ratios),
            "note": "медиана отношения фактических комиссий к прогнозу"}


# ---------------------------------------------------------------- new hypotheses
def _proposal(p: StrategyParams, **over) -> StrategyProposal:
    d = {"width_pct": p.width_pct, "rebalance": p.rebalance, "rebalance_delay_blocks": p.rebalance_delay_blocks,
         "period_days": p.period_seconds // DAY, "edge_fraction": p.edge_fraction,
         "cooldown_hours": p.cooldown_seconds // 3600, "ratio_token1": p.ratio_token1,
         "full_range": p.full_range, "width_vol_mult": p.width_vol_mult, "max_vol_entry": p.max_vol_entry,
         "vol_lookback_days": max(3, p.vol_lookback_seconds // DAY)}
    d.update(over)
    return StrategyProposal(**d)


def plan_for(reasons: list[str], p: StrategyParams) -> dict:
    """What to try next because of these reasons (parameters only)."""
    w = p.width_pct
    props, why = [], []
    if {"out_of_range", "price_faster", "vol_higher"} & set(reasons):
        props += [_proposal(p, width_pct=round(min(2.0, w * 1.5), 3)),
                  _proposal(p, width_pct=round(min(2.0, w * 1.5), 3), rebalance="on_exit",
                            rebalance_delay_blocks=7200, period_days=0)]
        why.append(f"диапазон ±{w:.0%} слишком узкий для такого движения → проверить ±{min(2.0, w * 1.5):.0%}")
    if "costs_ate" in reasons:
        props += [_proposal(p, cooldown_hours=72), _proposal(p, rebalance="none", rebalance_delay_blocks=0,
                                                               period_days=0, cooldown_hours=0)]
        why.append("перестановки слишком дорогие → реже переставлять (72 ч) или не переставлять")
    if "fees_lower" in reasons and "out_of_range" not in reasons:
        props += [_proposal(p, width_pct=round(max(0.02, w * 0.75), 3))]
        why.append(f"комиссий меньше прогноза при цене внутри диапазона → проверить более узкий ±{max(0.02, w * 0.75):.0%} "
                   "и учесть калибровку прогноза комиссий")
    text = "; ".join(why) or "прогноз в пределах ожиданий — без изменений"
    return {"text": text, "proposals": props}


class LearningAdapter:
    """Deterministic researcher with the LLMAdapter interface (no paid API needed).
    `oos_seen` = True: its ideas are built from results that include the OOS segments, so
    the research loop marks them OOS-contaminated (honest confidence)."""
    model = "rule-based-learning-v1"
    oos_seen = True

    def __init__(self, engine, tested: set[str], near_misses: list[dict], vol_now: float | None,
                 vol_median: float | None):
        self.engine, self.tested = engine, tested
        self.near, self.vol_now, self.vol_median = near_misses, vol_now, vol_median
        self.queue: list[dict] = []   # ideas not run today -> "next research tasks"

    def _fresh(self, props: list[StrategyProposal]) -> list[StrategyProposal]:
        out, seen = [], set()
        for pr in props:
            h = pr.to_params().spec_hash
            if h not in self.tested and h not in seen:
                seen.add(h)
                out.append(pr)
        return out[:4]

    def propose(self, system: str, context: str, n: int) -> HypothesisBatch:
        drafts: list[HypothesisDraft] = []
        with self.engine.connect() as conn:
            lessons = conn.execute(select(S.lessons).order_by(S.lessons.c.id.desc()).limit(20)).all()
            strategies = {h: params_from_json(pj) for h, pj in
                          conn.execute(select(S.strategies.c.spec_hash, S.strategies.c.params_json))}
        for le in lessons:  # 1. errors of the virtual portfolio
            p = strategies.get(le.spec_hash)
            reasons = json.loads(le.reasons_json)
            if p is None or not reasons:
                continue
            plan = plan_for(reasons, p)
            props = self._fresh(plan["proposals"])
            if props:
                drafts.append(HypothesisDraft(
                    title=f"Исправление после ошибки прогноза: {le.strategy}",
                    description="; ".join(REASON_RU.get(r, r) for r in reasons),
                    rationale=plan["text"], strategies=props,
                    test_plan="все 21 отрезок истории (IS/OOS) + виртуальный портфель",
                    expected_outcome="меньше выходов из диапазона/расходов при сопоставимых комиссиях"))
        for nm in self.near:  # 2. near misses: neighbours of the best moderate strategies
            p = strategies.get(nm["spec_hash"])
            if p is None or p.full_range:
                continue
            r = p.ratio_token1
            props = self._fresh([
                _proposal(p, width_pct=round(p.width_pct + 0.05, 3)),
                _proposal(p, width_pct=round(max(0.03, p.width_pct - 0.05), 3)),
                *([_proposal(p, ratio_token1=round(min(0.9, r + 0.1), 2)),
                   _proposal(p, ratio_token1=round(max(0.1, r - 0.1), 2))] if r is not None else [])])
            if props:
                drafts.append(HypothesisDraft(
                    title=f"Соседи перспективной стратегии {nm['strategy']}",
                    description="проверить, устойчив ли результат к небольшому изменению диапазона и доли USDC",
                    rationale=f"не хватает: {'; '.join(nm.get('blockers_ru', [])) or '—'}",
                    strategies=props, test_plan="все 21 отрезок истории (IS/OOS)",
                    expected_outcome="если соседи тоже лучше пассивного — результат не случайный"))
        if self.vol_now and self.vol_median and self.near:  # 3. regime: volatility-aware width
            p = strategies.get(self.near[0]["spec_hash"])
            if p is not None and not p.full_range:
                mult = 3.0 if self.vol_now < self.vol_median else 4.0
                props = self._fresh([_proposal(p, width_vol_mult=mult, vol_lookback_days=7)])
                if props:
                    drafts.append(HypothesisDraft(
                        title="Диапазон по текущей волатильности",
                        description=f"ширина = {mult:g} × недельная волатильность вместо постоянной",
                        rationale=f"волатильность сейчас {self.vol_now:.0%} годовых, медиана истории {self.vol_median:.0%}",
                        strategies=props, test_plan="все 21 отрезок истории (IS/OOS)",
                        expected_outcome="меньше выходов в нервном рынке, больше комиссий в спокойном"))
        self.queue = [{"title": d.title, "strategies": len(d.strategies)} for d in drafts[n:]]
        return HypothesisBatch(hypotheses=drafts[:n])

    def comment(self, system: str, results: str) -> Commentary:
        doc = json.loads(results)["results"]
        tested = [h for h in doc if h["status"] == "tested"]
        conf = [h["title"] for h in tested if h["verdict"] == "confirmed"]
        rej = [h["title"] for h in tested if h["verdict"] == "rejected"]
        return Commentary(
            summary=f"Проверено гипотез: {len(tested)}; подтверждено {len(conf)}, отклонено {len(rej)}, "
                    f"без вывода {len(tested) - len(conf) - len(rej)}. Выводы посчитаны Python по истории.",
            patterns=[h["conclusion"] for h in tested if h.get("conclusion")][:5],
            ineffective_configurations=rej, next_directions=[q["title"] for q in self.queue][:5])
