"""Read-only API of UNISWAP AI LAB: the five sections of the interface.

Every number carries its origin: EXACT (blockchain), MODELED (exact pool replay /
backtest), ESTIMATED (hourly approximation), SCENARIO (forecast). Nothing here writes.
"""

from __future__ import annotations

import json
import math
import time

from sqlalchemy import func, select

from backend.database import schema as S
from backend.lab.activity import recent_activity
from backend.lab.cycle import latest_ranking, latest_scan

RISK_TEXT = {
    "general": ("Предоставление ликвидности может привести к частичной или полной потере вложенных средств. "
                "Риски: движение цены (потеря относительно хранения токенов), ошибки смарт-контрактов, "
                "отвязка токенов (USDC, WBTC), низкая ликвидность, ошибки модели. Расчёты — оценки модели, "
                "не гарантия дохода и не инвестиционная рекомендация. Система ничего не исполняет: решение "
                "и транзакцию делаете только вы."),
}
TIER_SHARE = {"CONSERVATIVE": 0.30, "MODERATE": 0.15, "EXPERIMENTAL": 0.05}


def _strict(engine) -> dict:
    with engine.connect() as c:
        sig = c.execute(select(S.signals).order_by(S.signals.c.id.desc()).limit(1)).first()
        ev = c.execute(select(S.signal_events).where(S.signal_events.c.signal_id == sig.id)
                       .order_by(S.signal_events.c.id.desc()).limit(1)).first() if sig else None
    if sig is None:
        return {"status": "WAIT", "text": "Сигналов ещё не было."}
    p = json.loads(sig.payload_json)
    status = ev.status if ev else sig.signal_type
    return {"status": status, "type": sig.signal_type, "id": sig.id, "created_at": sig.created_at,
            "payload": p if sig.signal_type == "OPEN CANDIDATE" else None,
            "text": ("Строгий сигнал на открытие есть — см. карточку." if sig.signal_type == "OPEN CANDIDATE"
                     and status in ("OPEN CANDIDATE", "HOLD", "REBALANCE") else
                     "Строгого сигнала нет: ни одна стратегия не прошла все проверки. Ниже — лучшие текущие "
                     "варианты с честной оценкой, насколько они проверены.")}


def _exact_card(o: dict) -> dict:
    """Opportunity (exact replay of WBTC/USDC 0.3%) in the common card shape."""
    from backend.lab.plain import describe
    from backend.strategies.generator import params_from_json
    rng, a = o["range"], o["allocation"]
    h = o["historical"]
    return {
        "id": "x-" + o["spec_hash"][:12], "source": "exact", "tier": o["tier"], "pair": o["pair"],
        "network": o["network"], "network_ru": "Ethereum", "pool": o["pool"], "pool_address": o["pool_address"],
        "fee_tier": o["fee_tier"],
        "price": o["current_price"], "price_lower": rng["price_lower"], "price_upper": rng["price_upper"],
        "deposit_usd": a["deposit_usd"], "token0_amount": a["token0"], "token1_amount": a["token1"],
        "quote_share": a.get("usdc_value_share"), "base": "WBTC", "quote": "USDC", "token0": "WBTC", "token1": "USDC",
        "liquidity_raw": a.get("liquidity"),
        "strategy": o["strategy"], "strategy_ru": describe(params_from_json(json.dumps(o["params"])))
        if o.get("params") else o["strategy"], "rule": o["rebalance_rule"], "horizon_days": o["horizon_days"],
        "fees_usd": o["expected_fees_usd"], "il_usd": o["expected_il_usd"],
        "scenarios": [{"change": s["price_change"], "il_usd": s["il_usd"],
                       "position_change_usd": s["net_usd"][0] + o["costs_usd"]["gas_open"] + o["costs_usd"]["gas_close"]
                       - o["expected_fees_usd"][0]}
                      for s in o.get("il_scenarios", []) if s["price_change"] in (-0.2, -0.1, 0.1, 0.2)],
        "gas_usd": o["costs_usd"]["gas_open"] + o["costs_usd"]["gas_close"], "swap_usd": o["costs_usd"]["swap"],
        "net_usd": o["expected_net_vs_hodl_usd"], "exit_probability": o["exit_probability"],
        "risk": o["risk"], "risk_why": o["risk_why"], "confidence": o["confidence"],
        "history": {"robustness": h.get("robustness_score"), "oos_vs_passive": h.get("oos_mean_excess_vs_passive"),
                    "oos_positive": h.get("oos_positive_segments"), "oos_roi": h.get("oos_mean_roi"),
                    "is_vs_passive": h.get("is_mean_excess"), "mean_drawdown": h.get("mean_drawdown"),
                    "max_drawdown": h.get("max_drawdown"), "segments": "21 отрезок по 91 дню, 2021–2026"},
        "why": o["reasons"], "blockers": o["blockers_ru"], "to_improve": o["to_improve_ru"],
        "invalidation": ["цена выйдет за диапазон" + (" (стратегия переставляет диапазон)" if o["rebalance_rule"] != "none" else ""),
                         "стратегия выпадет из рейтинга возможностей", "волатильность вырастет в 1,5 раза"],
        "valid_until": o["valid_until"], "label": "MODELED", "oos_contaminated": o["oos_contaminated"],
        "variants": [v["strategy"] for v in o.get("variants", [])],
    }


def _scan_card(p: dict, scan: dict) -> dict | None:
    b = p.get("best")
    if not b:
        return None
    base, quote = p["pair"].split("/")
    why = []
    v24 = p["volume_usd"]["24h"]
    if v24 >= 100_000:
        why.append(f"активные торги: ${v24:,.0f} за сутки, комиссий ${p['fees_usd']['24h']:,.0f}")
    if p["tvl_usd"] >= 1_000_000:
        why.append(f"в пуле достаточно ликвидности: ${p['tvl_usd']:,.0f}")
    why.append(f"за последние дни такой диапазон зарабатывал бы ${b['fees_usd'][0]:,.0f}–${b['fees_usd'][1]:,.0f} "
               f"комиссий за {scan['horizon_days']} дн. на ${scan['capital_usd']:,.0f}")
    p_exit = b["exit_probability"]
    risk = "high" if p_exit > 0.4 or (p["volatility_7d"] or 0) > 0.8 else "medium" if p_exit > 0.15 else "low"
    return {
        "id": f"s-{p['pool']}", "source": "estimated", "tier": "EXPERIMENTAL", "pair": p["pair"],
        "network": p.get("network", "ethereum"), "network_ru": p.get("network_ru", "Ethereum"),
        "gas_note": p.get("gas_note"),
        "pool": p["pool"], "pool_address": p["address"], "fee_tier": p["fee_tier"], "price": p["price"],
        "price_lower": b["price_lower"], "price_upper": b["price_upper"], "deposit_usd": scan["capital_usd"],
        "token0_amount": b["amount0"], "token1_amount": b["amount1"], "quote_share": b["share_quote_value"],
        "liquidity_share": b.get("liquidity_share"),
        "base": base, "quote": quote, "token0": p.get("token0", base), "token1": p.get("token1", quote),
        "strategy": f"static ±{b['width']:.0%}", "strategy_ru": f"диапазон ±{b['width']:.0%} от цены; диапазон не меняется",
        "rule": "none",
        "horizon_days": scan["horizon_days"], "fees_usd": b["fees_usd"], "il_usd": b["il_usd"],
        "scenarios": b.get("scenarios", []),
        "gas_usd": b["gas_usd"], "swap_usd": b["swap_usd"], "net_usd": b["net_vs_hodl_usd"],
        "exit_probability": p_exit, "risk": risk,
        "risk_why": f"шанс выхода из диапазона за {scan['horizon_days']} дн. ~{p_exit:.0%}; волатильность "
                    f"{(p['volatility_7d'] or 0):.0%} годовых",
        "confidence": "low",
        "history": {"days": p["history_days"], "segments": f"только последние {p['history_days']:.0f} дн. (нет проверки на истории)"},
        "why": why, "blockers": ["нет проверки на многолетней истории и на «невиденных» данных"] + p["problems"] + p["data_quality"],
        "to_improve": ["накопить результат в виртуальном портфеле", "загрузить длинную историю пула для полной проверки"],
        "invalidation": ["цена выйдет за диапазон", "объём торгов упадёт ниже $100 000 за сутки",
                         "волатильность вырастет в 1,5 раза"],
        "valid_until": scan["created_at"] + scan["horizon_days"] * 86_400, "label": "ESTIMATED",
        "token_risks": p["token_risks"] + ([p["network_risk"]] if p.get("network_risk") else []),
        "fee_calibration": (scan.get("fee_calibration") or {}).get("factor"),
        "variants": [],
    }


def signals(engine) -> dict:
    ranking = latest_ranking(engine) or {}
    scan = latest_scan(engine) or {}
    cards = [_exact_card(o) for t in ("CONSERVATIVE", "MODERATE", "EXPERIMENTAL") for o in ranking.get("tiers", {}).get(t, [])]
    for p in scan.get("pools", []):
        if p["pool"] == ranking.get("pool"):
            continue
        c = _scan_card(p, scan)
        if c and p.get("score") is not None and c["net_usd"][1] > 0:
            cards.append(c)
    add_entry_prices(cards, scan)
    act = int((ranking.get("market") or {}).get("active_liquidity") or 0)
    for c in cards:   # our share of the liquidity earning at the current price, for the card's size
        if c["source"] == "exact" and act and c.get("liquidity_raw"):
            mine = int(c["liquidity_raw"])
            c["liquidity_share"] = mine / (act + mine)
    for c in cards:
        c["amount_guide"] = amount_guide(c)
    bad = [{"pair": p["pair"], "fee_tier": p["fee_tier"], "problems": p["problems"] + p["data_quality"]}
           for p in scan.get("pools", []) if p["problems"] or p["data_quality"]]
    return {"strict": _strict(engine), "cards": cards, "best_today": best_today(cards), "tier_share": TIER_SHARE,
            "market": ranking.get("market"), "market_checks": ranking.get("market_checks", []),
            "diagnostics": ranking.get("diagnostics", []), "pools_with_problems": bad,
            "scan_at": scan.get("created_at"), "ranking_at": ranking.get("created_at"),
            "risk_warning": RISK_TEXT["general"]}


TIER_ORDER = {"CONSERVATIVE": 0, "MODERATE": 1, "EXPERIMENTAL": 2}


def _mid(c: dict) -> float:
    return (c["net_usd"][0] + c["net_usd"][1]) / 2


def best_today(cards: list[dict], n: int = 6) -> list[str]:
    """Ids of the day's best choices: the best (most proven, then highest expected result
    with a positive lower bound preferred) card of every pair; for the exactly modelled
    pool also the best of a second tier, so there is a safer and a bolder option."""
    out, seen = [], set()
    ordered = sorted(cards, key=lambda c: (TIER_ORDER[c["tier"]], c["net_usd"][0] <= 0, -_mid(c)))
    for c in ordered:   # one per (network, pair)
        key = (c.get("network", "ethereum"), c["pair"])
        if key not in seen and _mid(c) > 0:
            seen.add(key)
            out.append(c)
    exact = [c for c in ordered if c["source"] == "exact" and c not in out and _mid(c) > 0]
    first = next((c for c in out if c["source"] == "exact"), None)
    second = next((c for c in exact if first is None or c["tier"] != first["tier"]), None)
    if second:
        out.append(second)
    return [c["id"] for c in sorted(out, key=lambda c: (TIER_ORDER[c["tier"]], -_mid(c)))[:n]]


GAS_SHARE_MAX = 0.10      # gas should take at most 10% of the expected fees
POOL_SHARE_MAX = 0.02     # our liquidity at most 2% of the liquidity earning now


def _ru(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _nice_up(x: float) -> float:
    step = 100 if x < 2_000 else 500 if x < 10_000 else 1_000 if x < 50_000 else 5_000
    return math.ceil(x / step) * step


def _nice_down(x: float) -> float:
    step = 100 if x < 2_000 else 500 if x < 10_000 else 1_000 if x < 50_000 else 5_000
    return max(step, math.floor(x / step) * step)


def amount_guide(c: dict) -> dict:
    """Recommended amount for a card, from two limits (no invented numbers):
    min  — gas (fixed per position) takes at most GAS_SHARE_MAX of the expected fees;
    max  — our liquidity stays below POOL_SHARE_MAX of the liquidity earning now (beyond
           that our own deposit dilutes the fees and moves the price; the model is less reliable).
    Fees, loss vs holding and the swap scale with the amount; gas does not."""
    dep = c["deposit_usd"]
    fees_mid = (c["fees_usd"][0] + c["fees_usd"][1]) / 2 / dep            # per dollar, horizon
    edge = fees_mid + (c["il_usd"] - c["swap_usd"]) / dep                 # per dollar before gas
    share = c.get("liquidity_share")
    max_amt = dep * POOL_SHARE_MAX / share if share else None
    if fees_mid <= 0 or edge <= 0:
        return {"recommended": None, "min": None, "max": max_amt, "min_profit": None, "breakeven": None,
                "why": "по расчёту модели этот вариант не окупается ни при какой сумме — лучше подождать других вариантов"}
    min_amt = c["gas_usd"] / (GAS_SHARE_MAX * fees_mid)
    breakeven = c["gas_usd"] / edge          # expected result vs holding = 0 at this amount
    min_profit = _nice_up(breakeven * 1.01)
    rec = _nice_up(min_amt)
    if max_amt is not None and rec > max_amt:
        return {"recommended": None, "min": min_amt, "max": max_amt, "min_profit": None, "breakeven": breakeven,
                "why": f"чтобы газ был не больше 10% комиссий, нужно от ~${_ru(min_amt)}, но больше ~${_ru(max_amt)} "
                       "в этот пул ставить не стоит — вариант слишком мелкий для такого газа; лучше подождать"}
    if max_amt is not None and min_profit > max_amt:
        min_profit = None
    return {"recommended": rec, "min": min_amt, "max": _nice_down(max_amt) if max_amt else None,
            "min_profit": min_profit, "breakeven": breakeven,
            "why": (f"от ~${_ru(min_amt)} газ (${c['gas_usd']:.2f}) занимает не больше 10% ожидаемых комиссий; "
                    + (f"больше ~${_ru(max_amt)} ставить не стоит: ваша доля превысит 2% работающей ликвидности пула"
                       if max_amt else "верхний предел по ликвидности пула не рассчитан"))}


def add_entry_prices(cards: list[dict], scan: dict) -> None:
    """Entry price guide per card from the pair's most traded pool (EXACT swap sums):
    7-day average price weighted by volume, weekly low/high, and how far this pool's price
    is from that reference (a large gap would mean buying at a worse price than elsewhere)."""
    pools = scan.get("pools", [])
    ref: dict = {}
    for p in pools:
        k = (p.get("network", "ethereum"), p["pair"])
        if p.get("vwap_7d") and (k not in ref or p["volume_usd"]["7d"] > ref[k]["volume_usd"]["7d"]):
            ref[k] = p
    by_pool = {p["pool"]: p for p in pools}
    for c in cards:
        r, own = ref.get((c.get("network", "ethereum"), c["pair"])), by_pool.get(c["pool"])
        c["base_usd"] = own.get("base_usd") if own else None
        c["quote_usd"] = own.get("quote_usd") if own else None
        if r is None:
            c["entry"] = None
            continue
        avg = r["vwap_7d"]
        dev = c["price"] / r["price"] - 1
        rec = min(c["price"], avg)
        c["entry"] = {
            "now": c["price"], "average_7d": avg, "low_7d": r["low_7d"], "high_7d": r["high_7d"],
            "recommended": rec, "reference_pool": f"{r['pair']} {r['fee_tier']:.2%}", "reference_price": r["price"],
            "pool_vs_market": dev,
            "verdict": ("сейчас цена ниже средней за неделю — входить по текущей цене выгоднее средней"
                        if c["price"] <= avg else
                        f"сейчас цена выше средней за неделю на {c['price'] / avg - 1:.1%} — выгоднее подождать "
                        f"снижения до ~{avg:,.4g} (не гарантия, что цена туда вернётся)"),
            "assumption": f"считаем, что вы входите с {c['quote']} и докупаете {c['base']} — тогда выгоднее входить, когда {c['base']} дешевле",
            "label": "EXACT"}


def portfolio(engine, cfg) -> dict:
    from backend.lab.portfolio import summary
    return summary(engine, cfg)


def research(engine) -> dict:
    from backend.lab.learning import REASON_RU, fee_calibration
    from backend.lab.scanner import get_state
    with engine.connect() as c:
        hyps = c.execute(select(S.hypotheses).order_by(S.hypotheses.c.id.desc()).limit(40)).all()
        lessons = c.execute(select(S.lessons).order_by(S.lessons.c.id.desc()).limit(30)).all()
        fcs = c.execute(select(S.forecasts).order_by(S.forecasts.c.id.desc()).limit(40)).all()
        n_exp = c.execute(select(func.count()).select_from(S.experiments).where(S.experiments.c.status == "done")).scalar()
        n_str = c.execute(select(func.count()).select_from(S.strategies)).scalar()
    out_h = []
    for h in hyps:
        r = json.loads(h.result_json) if h.result_json else {}
        out_h.append({"id": h.id, "created_at": h.created_at, "title": h.title, "description": h.description,
                      "rationale": h.rationale, "status": h.status, "verdict": h.verdict, "confidence": h.confidence,
                      "conclusion": h.conclusion, "by": h.created_by_model,
                      "strategies": [{"strategy": s.get("strategy"), "verdict": s.get("verdict"),
                                      "oos_vs_passive": s.get("mean_excess"), "positive": s.get("positive_excess_segments")}
                                     for s in r.get("strategies", [])],
                      "oos_contaminated": r.get("oos_contaminated")})
    return {
        "foundation": {"experiments": n_exp, "strategies": n_str, "period": "2021-08-06 … 2026-11-01",
                       "pool": "WBTC/USDC 0,3% (полная история с мая 2021)",
                       "note": "историческое исследование сохранено и не пересчитывается; новые стратегии считаются только для себя"},
        "hypotheses": out_h,
        "confirmed": [h for h in out_h if h["verdict"] == "confirmed"],
        "rejected": [h for h in out_h if h["verdict"] == "rejected"],
        "lessons": [{"at": le.created_at, "strategy": le.strategy,
                     "reasons": [REASON_RU.get(r, r) for r in json.loads(le.reasons_json)], "action": le.action}
                    for le in lessons],
        "forecasts": [{"id": f.id, "created_at": f.created_at, "strategy": f.strategy, "tier": f.tier,
                       "status": f.status, "forecast": json.loads(f.forecast_json),
                       "checkpoint": json.loads(f.checkpoint_json) if f.checkpoint_json else None,
                       "review": json.loads(f.review_json) if f.review_json else None,
                       "due": f.created_at + f.horizon_days * 86_400} for f in fcs],
        "fee_calibration_forecasts": fee_calibration(engine),
        "scanner_calibration": get_state(engine, "scanner_fee_calibration"),
        "next_tasks": (get_state(engine, "next_tasks") or {}).get("items", []),
        "reason_names": REASON_RU,
    }


def _bot_brief(engine, cfg) -> dict:
    from backend.lab.bot import summary
    from backend.lab.scanner import NETWORK_RU
    b = summary(engine, cfg)
    cur = b["current"]
    return {"balance_usd": b["balance_usd"], "pnl_usd": b["pnl_usd"], "start_usd": b["start_usd"],
            "where": (f"{cur['pair']} · {NETWORK_RU.get(cur['network'], cur['network'])}" if cur
                      else "ждёт подходящего варианта")}


def bot(engine, cfg) -> dict:
    from backend.lab.bot import summary
    return summary(engine, cfg)


def activity(engine) -> dict:
    return {"items": recent_activity(engine, 300)}


def dashboard(engine, cfg) -> dict:
    from backend.lab.portfolio import summary
    sig = signals(engine)
    acts = recent_activity(engine, 300)
    now = int(time.time())
    day = [a for a in acts if a["at"] >= now - 86_400]
    errors = [a for a in day if a["status"] == "error"]
    last = acts[0]["at"] if acts else None
    pf = summary(engine, cfg)
    with engine.connect() as c:
        ss = c.execute(select(S.sync_state)).first()
        sc = c.execute(select(S.scan_state)).first()
    ordered = sorted(sig["cards"], key=lambda c: ({"CONSERVATIVE": 0, "MODERATE": 1, "EXPERIMENTAL": 2}[c["tier"]],
                                                  -(c["net_usd"][0] + c["net_usd"][1]) / 2))
    best, seen = [], set()
    for c in ordered:   # the best variant of each pair / source
        key = (c["pair"], c["source"])
        if key not in seen:
            seen.add(key)
            best.append(c)
    best = best[:4]
    stale = last is None or now - last > 3 * 3600
    return {
        "status": ("error" if sum(1 for e in errors if not any(a["kind"] == e["kind"] and a["status"] == "ok"
                                                                and a["at"] > e["at"] for a in day)) >= 1
                   else "stale" if stale else "ok"),
        "status_text": ("Система работает: последнее действие " if not stale else "Давно не было действий: ") ,
        "last_action_at": last,
        # an error is "resolved" when the same step has since run successfully
        "errors_24h": [{"at": e["at"], "message": e["message"],
                        "resolved": any(a["kind"] == e["kind"] and a["status"] == "ok" and a["at"] > e["at"] for a in day)}
                       for e in errors][:6],
        "data": {"pool_block": ss.last_block if ss else None, "scanner_block": sc.last_block if sc else None},
        "strict": sig["strict"], "counts": {t: sum(1 for c in sig["cards"] if c["tier"] == t)
                                             for t in ("CONSERVATIVE", "MODERATE", "EXPERIMENTAL")},
        "best": best,
        "portfolio": {k: pf[k] for k in ("initial_usd", "balance_usd", "pnl_usd", "pnl_pct", "fees_usd",
                                         "day_change_usd")} | {"open": len(pf["open"]), "closed": len(pf["closed"])},
        "bot": _bot_brief(engine, cfg),
        "today": [{"at": a["at"], "status": a["status"], "message": a["message"]} for a in day[:12]],
        "next_tasks": (research(engine)["next_tasks"])[:5],
    }
