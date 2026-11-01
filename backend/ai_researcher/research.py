"""AI research cycle (stage 7): context -> hypotheses -> experiments -> verdicts -> report.

Division of labour (SPEC §10):
- the language model reads an IN-SAMPLE summary and proposes structured
  hypotheses, later it writes commentary on results computed by Python;
- Python validates every proposal, runs the backtests, and decides the verdict
  and confidence with criteria fixed in advance (below), on the OUT-OF-SAMPLE
  segments the model never saw. The model cannot change code or results.

Pre-registered verdict for a strategy (excess = ROI - passive full-range ROI on
the same OOS segment):
  confirmed    mean excess > 0, excess > 0 in >= 5 of 7 OOS segments, never
               ruined, and mean excess still > 0 with gas x2;
  rejected     otherwise;
  inconclusive results missing.
A hypothesis is confirmed if at least one of its strategies is.
Confidence: high = confirmed with >= 6/7 positive and mean excess > 1%, or
rejected with mean excess < 0 and <= 2/7 positive; medium = other confirmed /
rejected with mean excess < 0; low = the rest.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from dataclasses import asdict, replace
from pathlib import Path

from sqlalchemy import insert, select

from backend.ai_researcher.schema import validate_proposal
from backend.data_engine.sync import _insert_ignore
from backend.database import schema as S
from backend.strategies.generator import BASE_STRATEGIES, StrategyParams, params_from_json
from backend.validation.robustness import PASSIVE, score_strategies
from backend.validation.segments import load_segment_table, run_segment_table
from backend.validation.study import Study

ROOT = Path(__file__).resolve().parents[2]
MIN_POSITIVE_OOS = 5

SYSTEM_PROPOSE = """You are the research assistant of DeFi Liquidity Lab, a read-only research system \
that backtests Uniswap v3 liquidity-provision (LP) strategies on the WBTC/USDC 0.3% pool on Ethereum.

Your job: read the IN-SAMPLE evidence below and propose new, testable hypotheses about which LP \
configurations could beat the passive full-range LP after all costs (gas, swap fees, price impact, \
impermanent loss), consistently across market regimes - not just in one lucky period.

Rules:
- Use only the evidence provided. Do not invent numbers; you will not see out-of-sample data, and the \
verdict is computed by Python on data you never see.
- Each hypothesis states a pattern, a rationale grounded in the evidence, 1-4 concrete strategy \
configurations that test it (a test and, where useful, a control), a test plan and the outcome that \
would confirm or refute it.
- Prefer diverse hypotheses (range width, asymmetry, rebalance rules, cooldowns, volatility scaling \
and volatility filters) over small variations of one idea. Do not repeat strategies listed as already tested.

Strategy fields: width_pct (half-width, 0.10 = +-10%), rebalance in none|on_exit|periodic|edge, \
rebalance_delay_blocks (blocks of ~12 s; 300 ~ 1 hour, 7200 ~ 1 day), period_days (periodic), \
edge_fraction (edge: 0.3-1.0 of the way from the open price to a boundary), cooldown_hours, \
ratio_token1 (target USDC value share at open, 0.2-0.8, or null for symmetric), full_range, \
width_vol_mult (width = mult x weekly realized volatility, or null), max_vol_entry (annualized \
volatility above which the position is closed and not reopened until volatility falls, or null), \
vol_lookback_days (>= 3). Fields unused by a rule are ignored."""

SYSTEM_COMMENT = """You are the research assistant of DeFi Liquidity Lab. Below are hypotheses and the \
results Python computed for them on out-of-sample data. Write a short analysis: what the results show, \
recurring patterns, configurations that look ineffective, and directions worth testing next. Cite only \
numbers that appear in the results; never introduce new numbers, never overrule the verdicts, and do \
not describe any strategy as profitable or safe beyond what the verdicts state."""


def _roi(m):
    return None if m is None else m["net_a"] / m["capital"]


def _is_only(table: dict, study: Study) -> dict:
    is_idx = {s.index for s in study.segments if s.role == "IS"}
    return {k: v for k, v in table.items() if k[1] in is_idx}


def build_context(table: dict, params: list[StrategyParams], study: Study,
                  tested: list[dict], top: int = 15) -> str:
    """In-sample-only evidence for the model (the OOS segments are excluded)."""
    is_segs = [s for s in study.segments if s.role == "IS"]
    inner = [replace(s, role="OOS" if k >= len(is_segs) - 4 else "IS") for k, s in enumerate(is_segs)]
    t = _is_only(table, study)
    reps = score_strategies(t, params, inner)
    rows = []
    for r in reps:
        st = r.stats
        rows.append({"strategy": r.name, "inner_score": round(r.score, 1),
                     "is_mean_roi": _r(st.get("is_mean_roi")), "inner_oos_mean_roi": _r(st.get("oos_mean_roi")),
                     "inner_oos_vs_passive": _r(st.get("oos_vs_passive")),
                     "share_segments_positive": _r(st.get("all_positive")),
                     "mean_roi_by_regime": {k: _r(v) for k, v in st.get("regimes", {}).items()},
                     "mean_max_drawdown": _r(st.get("mean_mdd")), "rebalances_total": st.get("rebalances_total"),
                     "flags": r.flags})

    def group(key):
        acc: dict = {}
        for p, r in zip(params, reps_by_hash(reps, params)):
            if r is None or r.stats.get("oos_vs_passive") is None:
                continue
            acc.setdefault(key(p), []).append(r.stats["oos_vs_passive"])
        return {str(k): {"n": len(v), "mean_inner_oos_vs_passive": _r(statistics.fmean(v))}
                for k, v in sorted(acc.items(), key=lambda kv: str(kv[0]))}
    passive = {s.index: _r(_roi(t.get((PASSIVE, s.index, "base")))) for s in is_segs}
    ctx = {
        "data": "WBTC/USDC 0.3% Uniswap v3, Ethereum; capital 10,000 USDC per segment; each strategy is "
                "re-opened at the start of every segment; ROI = net result / capital after gas, swap "
                "costs and IL (fees model A, conservative). All values below are IN-SAMPLE.",
        "in_sample_segments": [{"index": s.index, "start": time.strftime("%Y-%m-%d", time.gmtime(s.start_ts)),
                                "days": round((s.end_ts - s.start_ts) / 86400), "price_change": _r(s.price_change),
                                "realized_vol": _r(s.realized_vol), "regime": f"{s.trend}/{s.vol_class}",
                                "passive_full_range_roi": passive[s.index]} for s in is_segs],
        "inner_validation": "the last 4 in-sample segments act as an inner hold-out for 'inner_*' fields",
        "top_strategies": rows[:top],
        "bottom_strategies": rows[-10:],
        "by_rebalance_rule": group(lambda p: p.rebalance if not p.full_range else "full_range"),
        "by_width": group(lambda p: "full" if p.full_range else p.width_pct),
        "by_ratio_token1": group(lambda p: p.ratio_token1),
        "already_tested_hypotheses": tested,
    }
    return json.dumps(ctx, indent=1)


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def reps_by_hash(reps, params):
    by = {r.spec_hash: r for r in reps}
    return [by.get(p.spec_hash) for p in params]


def evaluate_strategy(table: dict, p: StrategyParams, study: Study) -> dict:
    oos = [s for s in study.segments if s.role == "OOS"]
    ex, ex_gas, rois, ruined, missing = [], [], [], False, 0
    for s in oos:
        m, pm = table.get((p.spec_hash, s.index, "base")), table.get((PASSIVE, s.index, "base"))
        mg, pg = table.get((p.spec_hash, s.index, "gas_x2")), table.get((PASSIVE, s.index, "gas_x2"))
        if m is None or pm is None:
            missing += 1
            continue
        ruined = ruined or bool(m.get("ruined"))
        rois.append(_roi(m))
        ex.append(_roi(m) - _roi(pm))
        if mg is not None and pg is not None:
            ex_gas.append(_roi(mg) - _roi(pg))
    is_ex = []
    for s in study.segments:
        if s.role != "IS":
            continue
        m, pm = table.get((p.spec_hash, s.index, "base")), table.get((PASSIVE, s.index, "base"))
        if m is not None and pm is not None:
            is_ex.append(_roi(m) - _roi(pm))
    out = {"strategy": p.name, "spec_hash": p.spec_hash, "oos_segments": len(oos), "missing": missing,
           # IS context (not part of the verdict): does the edge also exist in-sample?
           "is_mean_excess": _r(statistics.fmean(is_ex)) if is_ex else None,
           "is_positive_excess_segments": sum(x > 0 for x in is_ex), "is_segments": len(is_ex),
           "consistent_with_is": bool(is_ex) and statistics.fmean(is_ex) > 0
           and sum(x > 0 for x in is_ex) * 2 >= len(is_ex),
           "oos_excess_vs_passive": [_r(x) for x in ex], "oos_roi": [_r(x) for x in rois],
           "mean_excess": _r(statistics.fmean(ex)) if ex else None,
           "positive_excess_segments": sum(x > 0 for x in ex),
           "mean_excess_gas_x2": _r(statistics.fmean(ex_gas)) if ex_gas else None, "ruined": ruined}
    if missing or not ex:
        out["verdict"], out["confidence"] = "inconclusive", "low"
        return out
    mean, pos = statistics.fmean(ex), out["positive_excess_segments"]
    ok = (mean > 0 and pos >= MIN_POSITIVE_OOS and not ruined
          and (out["mean_excess_gas_x2"] or -1) > 0)
    out["verdict"] = "confirmed" if ok else "rejected"
    if ok:
        out["confidence"] = "high" if pos >= 6 and mean > 0.01 else "medium"
    else:
        out["confidence"] = "high" if mean < 0 and pos <= 2 else "medium" if mean < 0 else "low"
    return out


def conclude(results: list[dict]) -> tuple[str, str, str]:
    """Hypothesis verdict, confidence and a deterministic conclusion from strategy results."""
    if not results:
        return "inconclusive", "low", "no valid strategy to test"
    conf = [r for r in results if r["verdict"] == "confirmed"]
    if conf:
        best = max(conf, key=lambda r: r["mean_excess"])
        return ("confirmed", best["confidence"],
                f"{best['strategy']} beat the passive LP in {best['positive_excess_segments']}/"
                f"{best['oos_segments']} OOS segments, mean excess {best['mean_excess']:+.2%} per segment "
                f"(gas x2: {best['mean_excess_gas_x2']:+.2%}).")
    if any(r["verdict"] == "inconclusive" for r in results):
        return "inconclusive", "low", "some strategies have missing results"
    best = max(results, key=lambda r: r["mean_excess"])
    conf_ = "high" if all(r["confidence"] == "high" for r in results) else "medium" \
        if all(r["mean_excess"] < 0 for r in results) else "low"
    return ("rejected", conf_,
            f"no strategy met the criteria; best {best['strategy']}: mean excess "
            f"{best['mean_excess']:+.2%} per OOS segment, positive in {best['positive_excess_segments']}/"
            f"{best['oos_segments']}.")


def tested_hypotheses(engine) -> list[dict]:
    """Titles and strategies of earlier hypotheses (verdicts withheld: they encode OOS results)."""
    out = []
    with engine.connect() as conn:
        for title, pj in conn.execute(select(S.hypotheses.c.title, S.hypotheses.c.params_json)
                                      .where(S.hypotheses.c.status == "tested")):
            out.append({"title": title, "strategies": [params_from_json(json.dumps(d)).name
                                                       for d in json.loads(pj)]})
    return out


def run_cycle(engine, ds, study: Study, adapter, grid_params: list[StrategyParams], *,
              n: int = 5, version: str, out_dir: Path | None = None) -> dict:
    t0 = time.time()
    table = load_segment_table(engine, study.base, study.segments, version)
    context = build_context(table, grid_params, study, tested_hypotheses(engine))
    cycle_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + \
        hashlib.sha256(context.encode()).hexdigest()[:8]
    batch = adapter.propose(SYSTEM_PROPOSE, context, n)
    known = {h for t in tested_hypotheses(engine) for h in t["strategies"]}
    records = []
    for d in batch.hypotheses[:n]:
        params, reasons = [], []
        for prop in d.strategies[:4]:
            p, why = validate_proposal(prop)
            (params.append(p) if p is not None else reasons.append(why))
        status = "proposed" if params else "invalid"
        if params and all(p.name in known for p in params):
            status = "duplicate"
        records.append({"draft": d, "params": params, "invalid_reasons": reasons, "status": status})
    to_run = list({p.spec_hash: p for r in records if r["status"] == "proposed" for p in r["params"]}
                  .values())
    if to_run:
        run_segment_table(engine, ds, to_run + [BASE_STRATEGIES["passive_full_range"]],
                          study.segments, study.base, batch=len(to_run) + 1)
    table = load_segment_table(engine, study.base, study.segments, version)
    prior_tests = len(tested_hypotheses(engine))
    for r in records:
        if r["status"] != "proposed":
            continue
        r["results"] = [evaluate_strategy(table, p, study) for p in r["params"]]
        r["verdict"], r["confidence"], r["conclusion"] = conclude(r["results"])
        r["status"] = "tested"
    results_doc = [{"title": r["draft"].title, "description": r["draft"].description,
                    "status": r["status"], "verdict": r.get("verdict"), "confidence": r.get("confidence"),
                    "conclusion": r.get("conclusion"), "strategies": r.get("results", []),
                    "invalid_reasons": r["invalid_reasons"]} for r in records]
    oos_seen = bool(getattr(adapter, "oos_seen", False))
    commentary = None
    if any(r["status"] == "tested" for r in records):
        commentary = adapter.comment(SYSTEM_COMMENT, json.dumps(
            {"verdict_rules": __doc__.split("Pre-registered")[1], "results": results_doc}, indent=1))
    now = int(time.time())
    with engine.begin() as conn:
        for r in records:
            d = r["draft"]
            hid = conn.execute(insert(S.hypotheses).values(
                cycle_id=cycle_id, created_at=now, title=d.title, description=d.description,
                rationale=d.rationale, params_json=json.dumps([asdict(p) for p in r["params"]]),
                test_plan=d.test_plan, expected_outcome=d.expected_outcome, status=r["status"],
                verdict=r.get("verdict"), confidence=r.get("confidence"),
                result_json=json.dumps({"strategies": r.get("results", []),
                                        "invalid_reasons": r["invalid_reasons"],
                                        "oos_contaminated": oos_seen}),
                conclusion=r.get("conclusion"),
                ai_commentary=commentary.model_dump_json() if commentary else None,
                created_by_model=adapter.model, code_version=version)).inserted_primary_key[0]
            _insert_ignore(conn, S.hypothesis_experiments,
                           [{"hypothesis_id": hid, "spec_hash": p.spec_hash} for p in r["params"]])
    summary = {"cycle_id": cycle_id, "model": adapter.model, "code_version": version,
               "proposed": len(records), "tested": sum(r["status"] == "tested" for r in records),
               "confirmed": sum(r.get("verdict") == "confirmed" for r in records),
               "oos_reuse_before_cycle": prior_tests, "oos_contaminated": oos_seen,
               "seconds": round(time.time() - t0, 1),
               "hypotheses": results_doc,
               "commentary": commentary.model_dump() if commentary else None}
    if out_dir is not None:
        write_report(summary, out_dir)
    return summary


def write_report(summary: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"cycle_{summary['cycle_id']}.json").write_text(json.dumps(summary, indent=1) + "\n")
    seen = summary.get("oos_contaminated")
    lines = [f"# Цикл AI-исследования {summary['cycle_id']}", "",
             f"Модель: `{summary['model']}` · версия кода `{summary['code_version']}` · "
             f"гипотез: {summary['proposed']}, проверено: {summary['tested']}, "
             f"подтверждено: {summary['confirmed']}", "",
             "Вердикты и цифры посчитаны Python на OOS-отрезках по заранее заданным правилам (MODELED); "
             + ("автор гипотез эти отрезки уже видел. " if seen else "автор гипотез их не видел. ")
             +
             f"До этого цикла на тех же OOS-данных уже проверено гипотез: "
             f"{summary['oos_reuse_before_cycle']} — чем больше проверок, тем выше шанс случайного "
             "«подтверждения»; подтверждённое требует проверки на новых данных (этапы 8–9).", ""]
    if summary.get("oos_contaminated"):
        lines += ["> **OOS-проверка не чистая:** автор гипотез уже видел OOS-результаты до их "
                  "написания. Вердикты ниже — справочные; «подтверждено» здесь не доказательство.", ""]
    for h in summary["hypotheses"]:
        lines += [f"## {h['title']}", "", h["description"], "",
                  f"**Статус:** {h['status']} · **вердикт:** {h['verdict'] or '—'} · "
                  f"**уверенность:** {h['confidence'] or '—'}", ""]
        if h["conclusion"]:
            lines += [f"Вывод: {h['conclusion']}", ""]
        for s in h["strategies"]:
            me = s["mean_excess"]
            lines.append(f"- `{s['strategy']}` — среднее превышение над пассивным LP "
                         f"{'n/a' if me is None else f'{me:+.2%}'} за OOS-отрезок, лучше в "
                         f"{s['positive_excess_segments']}/{s['oos_segments']}, газ×2: "
                         f"{'n/a' if s['mean_excess_gas_x2'] is None else format(s['mean_excess_gas_x2'], '+.2%')}"
                         f"{', разорение' if s['ruined'] else ''} → {s['verdict']}"
                         + ("" if s.get("is_mean_excess") is None else
                            f"; на IS: {s['is_mean_excess']:+.2%}, лучше в "
                            f"{s['is_positive_excess_segments']}/{s['is_segments']}"
                            + ("" if s.get("consistent_with_is") else " — **на IS преимущества нет**")))
        for why in h["invalid_reasons"]:
            lines.append(f"- отклонено при проверке параметров: {why}")
        lines.append("")
    c = summary.get("commentary")
    if c:
        lines += ["## Комментарий модели (цифры выше посчитаны Python)", "", c["summary"], ""]
        for title, key in (("Закономерности", "patterns"), ("Неэффективные конфигурации",
                                                            "ineffective_configurations"),
                           ("Что проверить дальше", "next_directions")):
            if c.get(key):
                lines += [f"**{title}:**", *[f"- {x}" for x in c[key]], ""]
    path = out_dir / f"cycle_{summary['cycle_id']}.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def attach_commentary(engine, cycle_id: str, commentary, out_dir: Path | None = None) -> dict:
    """Attach a commentary (Commentary schema) written after the results to a stored cycle."""
    with engine.begin() as conn:
        n = conn.execute(S.hypotheses.update().where(S.hypotheses.c.cycle_id == cycle_id)
                         .values(ai_commentary=commentary.model_dump_json())).rowcount
    if not n:
        raise ValueError(f"no hypotheses for cycle {cycle_id}")
    summary = None
    if out_dir is not None:
        path = out_dir / f"cycle_{cycle_id}.json"
        if path.exists():
            summary = json.loads(path.read_text())
            summary["commentary"] = commentary.model_dump()
            write_report(summary, out_dir)
    return {"cycle_id": cycle_id, "rows": n, "report_updated": summary is not None}
