"""Read-only queries behind the dashboard API (stage 10).

Every value carries its reliability label (ARCHITECTURE §2): EXACT (reconstructed
on-chain data), MODELED (backtests / hypothetical positions), SCENARIO (forecasts).
Nothing here writes to the chain; only the details cache is written to data/cache.
"""

from __future__ import annotations

import json
import os
import statistics
import threading
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select

from backend.backtesting import uniswap_math as um
from backend.database import schema as S
from backend.strategies.generator import params_from_json
from backend.strategies.mass_test import code_version
from backend.validation.robustness import PASSIVE
from backend.validation.segments import Segment, load_segment_table

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "data" / "cache" / "details"
_lock = threading.Lock()


def _v(value, label: str):
    return {"value": value, "label": label}


def latest_validation_file() -> Path | None:
    files = sorted((ROOT / "data_snapshots" / "validation").glob("validation_*.json"),
                   key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


@lru_cache(maxsize=1)
def study_from_report() -> tuple[dict, list[Segment], str] | tuple[None, None, None]:
    path = latest_validation_file()
    if path is None:
        return None, None, None
    doc = json.loads(path.read_text())
    segs = [Segment(**s) for s in doc["segments"]]
    return doc["dataset"], segs, doc["code_version"]


def segment_table(engine) -> dict:
    base, segs, version = study_from_report()
    if base is None:
        return {}
    return load_segment_table(engine, base, segs, version)


def _roi(m):
    return None if m is None else m["net_a"] / m["capital"]


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return statistics.fmean(xs) if xs else None


def strategies_map(engine) -> dict:
    with engine.connect() as c:
        return {h: (n, params_from_json(pj)) for h, n, pj in
                c.execute(select(S.strategies.c.spec_hash, S.strategies.c.name, S.strategies.c.params_json))}


def ranking(engine, cfg) -> dict:
    base, segs, version = study_from_report()
    if base is None:
        return {"rows": [], "segments": []}
    t = segment_table(engine)
    strategies = strategies_map(engine)
    p = cfg.pools[cfg.primary_pool]
    with engine.connect() as c:
        rep = {}
        for sh, score, comp, flags in c.execute(
                select(S.validation_reports.c.spec_hash, S.validation_reports.c.score,
                       S.validation_reports.c.components_json, S.validation_reports.c.flags_json)
                .where(S.validation_reports.c.code_version == version)
                .order_by(S.validation_reports.c.id)):
            rep[sh] = (score / 100, json.loads(comp), json.loads(flags))
    oos = [s for s in segs if s.role == "OOS"]
    rows = []
    for h, (name, prm) in strategies.items():
        ms = [t.get((h, s.index, "base")) for s in segs]
        if not any(ms) or not all(t.get((h, s.index, "base")) for s in oos):
            continue
        have = [m for m in ms if m]
        cap = have[0]["capital"]
        score, comp, flags = rep.get(h, (None, {}, []))
        oos_m = [t[(h, s.index, "base")] for s in oos]
        pas = [t.get((PASSIVE, s.index, "base")) for s in oos]
        rows.append({
            "spec_hash": h, "name": name, "pair": f"{p.token0}/{p.token1}",
            "range": "весь" if prm.full_range else f"±{prm.width_pct * 100:g}%"
                     + (f" (волат.×{prm.width_vol_mult:g})" if prm.width_vol_mult else ""),
            "rule": prm.rebalance, "allocation_usdc": prm.ratio_token1,
            "fees": _mean(m["fees_a"] / cap for m in have),
            "il": _mean((m["il_realized"] + m["il_unrealized"]) / cap for m in have),
            "net": _mean(m["net_a"] for m in have) / 10 ** 6,
            "roi": _mean(_roi(m) for m in have),
            "max_drawdown": max(m["max_drawdown_a"] for m in have),
            "sharpe": _mean(m.get("sharpe_a") for m in have),
            "oos_roi": _mean(_roi(m) for m in oos_m),
            "oos_vs_passive": _mean(_roi(m) - _roi(pm) for m, pm in zip(oos_m, pas) if pm),
            "segments": len(have), "score": score, "flags": flags,
            "ruined_segments": sum(1 for m in have if m.get("ruined")),
        })
    rows.sort(key=lambda r: -(r["score"] if r["score"] is not None else -1))
    return {"rows": rows, "label": "MODELED", "code_version": version,
            "per": "среднее за 91-дневный отрезок; капитал 10 000 USDC открывается заново на каждом",
            "segments": [{"index": s.index, "role": s.role, "start_ts": s.start_ts, "end_ts": s.end_ts,
                          "trend": s.trend, "vol_class": s.vol_class, "price_change": s.price_change}
                         for s in segs]}


def overview(engine, cfg) -> dict:
    with engine.connect() as c:
        n_exp = c.execute(select(func.count()).select_from(S.experiments)
                          .where(S.experiments.c.status == "done")).scalar_one()
        n_str = c.execute(select(func.count(func.distinct(S.experiments.c.spec_hash)))).scalar_one()
        n_sig = c.execute(select(func.count()).select_from(S.signals)).scalar_one()
        last_eval = c.execute(select(S.market_evaluations).order_by(
            S.market_evaluations.c.id.desc()).limit(1)).first()
        last_cycle = c.execute(select(S.hypotheses.c.cycle_id, S.hypotheses.c.created_at,
                                      S.hypotheses.c.created_by_model)
                               .order_by(S.hypotheses.c.id.desc()).limit(1)).first()
        n_hyp = c.execute(select(func.count()).select_from(S.hypotheses)).scalar_one()
    rk = ranking(engine, cfg)["rows"]
    active = signals(engine)["active"]
    best = max(rk, key=lambda r: r["oos_roi"] or -9) if rk else None
    # "promising" = the signal engine's two core filters: beats the passive LP on OOS
    # (mean > 0, >= 5/7 segments) AND in-sample (mean > 0, >= half of the segments)
    from backend.ai_researcher.research import evaluate_strategy
    from backend.validation.study import Study
    base, segs, _ = study_from_report()
    t, strategies = segment_table(engine), strategies_map(engine)
    promising = []
    for r in rk:
        e = evaluate_strategy(t, strategies[r["spec_hash"]][1], Study(segs, base))
        if (e["mean_excess"] or -1) > 0 and e["positive_excess_segments"] >= 5 and e["consistent_with_is"]:
            promising.append(r["name"])
    healthy = [r for r in rk if not r["ruined_segments"]]
    return {
        "experiments": _v(n_exp, "MODELED"), "strategies_tested": _v(n_str, "MODELED"),
        "best_oos": _v({"name": best["name"], "oos_roi": best["oos_roi"],
                        "oos_vs_passive": best["oos_vs_passive"]} if best else None, "MODELED"),
        "mean_net_roi": _v(_mean(r["roi"] for r in rk), "MODELED"),
        "max_drawdown": _v(max((r["max_drawdown"] for r in healthy), default=None), "MODELED"),
        "ruined_strategies": _v(len(rk) - len(healthy), "MODELED"),
        "total_fees_per_segment_mean": _v(_mean(r["fees"] for r in rk), "MODELED"),
        "promising_strategies": _v(len(promising), "MODELED"), "promising_names": promising[:10],
        "active_signals": _v(len(active), "SCENARIO"), "signals_total": _v(n_sig, "SCENARIO"),
        "last_decision": _v(last_eval.decision if last_eval else None, "SCENARIO"),
        "last_decision_at": last_eval.created_at if last_eval else None,
        "ai_researcher": {"hypotheses": n_hyp,
                          "last_cycle": last_cycle.cycle_id if last_cycle else None,
                          "last_cycle_at": last_cycle.created_at if last_cycle else None,
                          "author": last_cycle.created_by_model if last_cycle else None,
                          "api_key_configured": bool(os.environ.get(
                              cfg.lab["ai_researcher"].get("api_key_env", "ANTHROPIC_API_KEY")))},
    }


def research(engine) -> dict:
    with engine.connect() as c:
        rows = c.execute(select(S.hypotheses).order_by(S.hypotheses.c.id.desc())).all()
    out, cycles = [], {}
    for r in rows:
        res = json.loads(r.result_json) if r.result_json else {}
        out.append({"id": r.id, "cycle_id": r.cycle_id, "created_at": r.created_at, "title": r.title,
                    "description": r.description, "rationale": r.rationale, "test_plan": r.test_plan,
                    "expected_outcome": r.expected_outcome, "status": r.status, "verdict": r.verdict,
                    "confidence": r.confidence, "conclusion": r.conclusion,
                    "strategies": res.get("strategies", []),
                    "invalid_reasons": res.get("invalid_reasons", []),
                    "oos_contaminated": res.get("oos_contaminated", False),
                    "author": r.created_by_model})
        if r.ai_commentary:
            cycles[r.cycle_id] = json.loads(r.ai_commentary)
    return {"hypotheses": out, "commentary": cycles, "label": "MODELED"}


def signals(engine) -> dict:
    with engine.connect() as c:
        sigs = c.execute(select(S.signals).order_by(S.signals.c.id.desc()).limit(200)).all()
        evs = c.execute(select(S.signal_events).order_by(S.signal_events.c.id)).all()
    hist: dict = {}
    for e in evs:
        hist.setdefault(e.signal_id, []).append({"at": e.created_at, "block": e.block,
                                                 "status": e.status, "reason": e.reason})
    rows = []
    for s in sigs:
        h = hist.get(s.id, [])
        rows.append({"id": s.id, "created_at": s.created_at, "block": s.block, "type": s.signal_type,
                     "status": h[-1]["status"] if h else s.signal_type, "history": h,
                     "payload": json.loads(s.payload_json)})
    active = [r for r in rows if r["type"] == "OPEN CANDIDATE"
              and r["status"] in ("OPEN CANDIDATE", "HOLD", "REBALANCE")]
    return {"signals": rows, "active": active, "watch": watch(engine), "label": "SCENARIO"}


def watch(engine) -> dict:
    """Watch level: strategies one filter short of a signal, followed on new data."""
    from backend.virtual_portfolio.portfolio import WATCH_PREFIX
    with engine.connect() as c:
        ev = c.execute(select(S.market_evaluations).order_by(S.market_evaluations.c.id.desc()).limit(1)).first()
        fts = c.execute(select(S.forward_tests).where(S.forward_tests.c.reason.startswith(WATCH_PREFIX))
                        .order_by(S.forward_tests.c.id)).all()
    current = json.loads(ev.snapshot_json).get("watch", []) if ev else []
    on_list = {w["spec_hash"] for w in current}
    tests = [{"id": f.id, "spec_hash": f.spec_hash, "start_block": f.start_block, "created_at": f.created_at,
              "reason": f.reason.removeprefix(WATCH_PREFIX), "name": params_from_json(f.params_json).name,
              "on_list_now": f.spec_hash in on_list,
              "result": json.loads(f.result_json) if f.result_json else None} for f in fts]
    return {"current": current, "evaluated_at": ev.created_at if ev else None, "tests": tests,
            "label": "MODELED"}


def portfolio(engine) -> dict:
    from backend.virtual_portfolio.portfolio import journal
    with engine.connect() as c:
        res = c.execute(select(S.virtual_results)).all()
        pos = {p.id: p for p in c.execute(select(S.virtual_positions))}
        fts = c.execute(select(S.forward_tests).order_by(S.forward_tests.c.id)).all()
    positions = [{"position_id": r.position_id, "signal_id": pos[r.position_id].signal_id,
                  "status": r.status, "outcome": r.outcome, "close_reason": r.close_reason,
                  **json.loads(r.result_json)} for r in res if r.position_id in pos]
    forward = [{"id": f.id, "start_block": f.start_block, "created_at": f.created_at, "reason": f.reason,
                "params": json.loads(f.params_json), "result": json.loads(f.result_json) if f.result_json
                else None} for f in fts]
    return {"journal": journal(engine), "positions": positions, "forward_tests": forward,
            "label": "MODELED"}


def datasources(engine, cfg) -> dict:
    p = cfg.pools[cfg.primary_pool]
    net = cfg.networks[p.network]
    with engine.connect() as c:
        st = c.execute(select(S.sync_state)).all()
        gaps = c.execute(select(S.sync_gaps).order_by(S.sync_gaps.c.id.desc()).limit(20)).all()
        counts = {name: c.execute(select(func.count()).select_from(t)).scalar_one()
                  for name, t in S.event_tables.items()}
        last_block = c.execute(select(func.max(S.blocks.c.number))).scalar_one()
        last_ts = c.execute(select(S.blocks.c.timestamp).where(S.blocks.c.number == last_block)).scalar_one() \
            if last_block else None
        srcs = c.execute(select(S.raw_logs.c.source, func.count()).group_by(S.raw_logs.c.source)).all()
    return {
        "network": p.network, "chain_id": net.chain_id, "pool": cfg.primary_pool, "pool_address": p.address,
        "endpoints": [{"name": e["name"], "archive": e.get("archive", False),
                       "max_logs_blocks": e.get("max_logs_blocks"), "configured": bool(e.get("url") or
                       os.environ.get(e.get("url_env", ""), ""))} for e in net.rpc],
        "sync": [{"start_block": s.start_block, "last_block": s.last_block, "updated_at": s.updated_at}
                 for s in st],
        "last_block": last_block, "last_block_ts": last_ts, "events": counts,
        "logs_by_source": {s: n for s, n in srcs},
        "gaps": [{"from_block": g.from_block, "to_block": g.to_block, "error": g.error[:300],
                  "source": g.source, "resolved": g.resolved} for g in gaps],
        "open_gaps": sum(1 for g in gaps if not g.resolved), "label": "EXACT",
    }


# ---------------------------------------------------------------- strategy details
_ds = None


def _dataset(cfg):
    global _ds
    if _ds is None:
        from backend.backtesting.dataset import load_dataset
        _ds = load_dataset(cfg)
    return _ds


def strategy_details(engine, cfg, spec_hash: str) -> dict | None:
    strategies = strategies_map(engine)
    if spec_hash not in strategies:
        return None
    name, prm = strategies[spec_hash]
    base, segs, version = study_from_report()
    t = segment_table(engine)
    seg_rows = []
    for s in segs or []:
        m = t.get((spec_hash, s.index, "base"))
        pm = t.get((PASSIVE, s.index, "base"))
        seg_rows.append({"index": s.index, "role": s.role, "start_ts": s.start_ts, "trend": s.trend,
                         "vol_class": s.vol_class, "roi": _roi(m), "passive_roi": _roi(pm),
                         "fees": m["fees_a"] / m["capital"] if m else None,
                         "max_drawdown": m["max_drawdown_a"] if m else None,
                         "rebalances": m["rebalances"] if m else None,
                         "ruined": m.get("ruined") if m else None})
    oos = [s for s in (segs or []) if s.role == "OOS"]
    series = _details_series(cfg, prm, spec_hash, oos[0].start_block, oos[-1].end_block,
                             version, base) if oos else None
    return {"spec_hash": spec_hash, "name": name, "params": prm.__dict__, "segments": seg_rows,
            "series": series, "label": "MODELED"}


def _details_series(cfg, prm, spec_hash, start, end, version, base) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{spec_hash[:16]}_{start}_{end}_{version}.json"
    if path.exists():
        return json.loads(path.read_text())
    with _lock:
        if path.exists():
            return json.loads(path.read_text())
        from backend.backtesting.dataset import cached_state_at
        from backend.backtesting.position_sim import simulate
        ds = _dataset(cfg)
        sp = prm.to_spec(start, end, base["capital_token1"], base["gas_set"], base["priority_fee_gwei"])
        r = simulate(ds, sp, start_state=cached_state_at(ds, start - 1))
        acts = r.actions
        d0, d1 = ds.decimals0, ds.decimals1
        pts = []
        for row in r.timeseries:
            # position amounts at the mark (liquidity of the action in force; idle excluded)
            a = [x for x in acts if x.block <= row["block"]][-1] if acts else None
            if a is not None and a.liquidity:
                x0, x1 = um.get_amounts_for_liquidity(
                    int(row["sqrt_price"]), um.get_sqrt_ratio_at_tick(a.tick_lower),
                    um.get_sqrt_ratio_at_tick(a.tick_upper), a.liquidity)
            else:
                x0 = x1 = 0
            pts.append({"t": row["timestamp"], "price": row["price"], "value_lp": row["value_lp"] / 10 ** d1,
                        "value_hodl": row["value_hodl"] / 10 ** d1, "fees_a": row["fees_a"] / 10 ** d1,
                        "fees_b": row["fees_b"] / 10 ** d1, "net_a": row["net_a"] / 10 ** d1,
                        "gas": row["gas_cost"] / 10 ** d1, "in_range": row["in_range"],
                        "token0": x0 / 10 ** d0, "token1": x1 / 10 ** d1})
        from backend.signal_engine.market import human_price
        actions = [{"t": a.timestamp, "kind": a.kind,
                    "price_lower": human_price(um.get_sqrt_ratio_at_tick(a.tick_lower), d0, d1) if a.liquidity else None,
                    "price_upper": human_price(um.get_sqrt_ratio_at_tick(a.tick_upper), d0, d1) if a.liquidity else None,
                    "gas": a.gas_cost_token1 / 10 ** d1, "swap_cost": a.swap_cost_token1 / 10 ** d1,
                    "il_realized": a.il_realized_token1 / 10 ** d1} for a in acts]
        m = r.metrics
        doc = {"start_block": start, "end_block": end, "points": pts, "actions": actions,
               "metrics": {k: (v / 10 ** d1 if k in ("capital", "value_lp", "value_hodl", "fees_a", "fees_b",
                                                     "gas_cost", "swap_cost", "il_realized", "il_unrealized",
                                                     "net_a", "net_b", "vs_hodl_a", "vs_hodl_b", "avg_daily_net_a") else v)
                           for k, v in m.items()},
               "note": "continuous run over the OOS period; token amounts exclude idle tokens",
               "label": "MODELED"}
        path.write_text(json.dumps(doc))
        return doc
