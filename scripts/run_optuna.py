"""Stage 6: Bayesian optimization (Optuna TPE) on in-sample segments, then a single
out-of-sample validation of the best trials.

  python -m scripts.run_optuna [--trials 120] [--batch 24] [--top 5]

Uses the same segments as scripts.run_validation (IS: first segments, OOS: last 7).
Resumable: the study is stored in data/optuna.sqlite; finished experiments are reused.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone

from sqlalchemy import insert

from backend.backtesting.dataset import ROOT, load_dataset
from backend.core.config import load_config
from backend.database import schema as S
from backend.optimizer.optuna_study import best_params, optimize
from backend.strategies.generator import BASE_STRATEGIES
from backend.strategies.mass_test import dataset_descriptor, relabel_commit_versions
from backend.validation.robustness import score_strategies
from backend.validation.segments import load_segment_table, make_segments, run_segment_table


def _ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2021-08-06")
    ap.add_argument("--to", dest="end", default="2026-11-01")
    ap.add_argument("--segment-days", type=int, default=91)
    ap.add_argument("--oos", type=int, default=7)
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--trials", type=int, default=120)
    ap.add_argument("--batch", type=int, default=24)
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    cfg = load_config()
    p = cfg.pools[cfg.primary_pool]
    t0 = time.monotonic()
    ds = load_dataset(cfg)
    segs = make_segments(ds, _ts(args.start), _ts(args.end), args.segment_days, args.oos)
    base = dataset_descriptor(ds, cfg.primary_pool, p.address, segs[0].start_block, segs[-1].end_block,
                              int(args.capital * 10 ** ds.decimals1), "gas_units",
                              cfg.lab["costs"]["priority_fee_gwei"])
    base.pop("from_block"), base.pop("to_block")
    base["segment_days"] = args.segment_days
    engine = S.make_engine(cfg.lab["database"]["url"])
    relabel_commit_versions(engine)
    print(f"optimizing on {sum(s.role == 'IS' for s in segs)} IS segments; OOS hidden", flush=True)
    study = optimize(engine, ds, segs, base, n_trials=args.trials, batch=args.batch, seed=args.seed,
                     progress=lambda d, n, v, name: print(
                         f"  trials {d}/{n}  best IS objective {v:+.4f}  ({name})  "
                         f"{time.monotonic() - t0:.0f}s", flush=True))
    top = best_params(study, args.top)
    passive = BASE_STRATEGIES["passive_full_range"]
    cands = top + [passive]
    print(f"\nrevealing OOS once for the top {len(top)} trials ...", flush=True)
    stats = run_segment_table(engine, ds, cands, segs, base, batch=len(cands))
    version = stats["code_version"]
    table = load_segment_table(engine, base, segs, version)
    reports = {r.spec_hash: r for r in score_strategies(table, cands, segs)}
    key = "optuna:" + study.study_name
    with engine.begin() as conn:
        conn.execute(S.validation_reports.delete().where(S.validation_reports.c.study_key == key))
        conn.execute(insert(S.validation_reports), [dict(
            study_key=key, spec_hash=r.spec_hash, code_version=version, score=round(r.score * 100),
            components_json=json.dumps({**r.components, **r.stats}), flags_json=json.dumps(r.flags),
            created_at=int(time.time())) for r in reports.values()])
    out = ROOT / "data_snapshots" / "validation" / f"{study.study_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    trials = [{"number": t.number, "value": t.value, "params": t.params, **t.user_attrs}
              for t in study.trials if t.value is not None]
    out.write_text(json.dumps({"study": study.study_name, "code_version": version, "dataset": base,
                               "objective": "mean(IS ROI) - 0.5*std(IS ROI)", "label": "MODELED",
                               "trials": trials,
                               "top": [{"params": asdict(c), **asdict(reports[c.spec_hash])}
                                       for c in cands]}, indent=1) + "\n")

    def pct(x):
        return "   n/a" if x is None else f"{x:+6.1%}"
    print(f"{'strategy':48s} IS obj  IS ROI OOS ROI vsPass OOS+ beatH score flags")
    by_obj = {t.user_attrs.get("spec_hash"): t.value for t in study.trials if t.value is not None}
    for c in cands:
        r = reports[c.spec_hash]
        st = r.stats
        print(f"{c.name:48s} {by_obj.get(c.spec_hash, float('nan')):+.3f} {pct(st.get('is_mean_roi'))} "
              f"{pct(st.get('oos_mean_roi'))} {pct(st.get('oos_vs_passive'))} "
              f"{st.get('oos_positive', 0):4.0%} {st.get('oos_beat_hodl', 0):5.0%} {r.score:5.1f} "
              f"{','.join(r.flags)}")
    print(f"\nwritten {out.relative_to(ROOT)}  ({time.monotonic() - t0:.0f}s)")


if __name__ == "__main__":
    main()
