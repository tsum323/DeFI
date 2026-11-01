"""Stage 6: robustness validation of the strategy grid on the real history.

  python -m scripts.run_validation [--space default|small] [--segment-days 91] [--oos 7]

1. Splits 2021-08-06 .. 2026-11-01 into contiguous segments (last --oos are OOS).
2. Runs every strategy on every segment (IS: base; OOS: base, gas x2, volume x0.5/x1.5).
   Resumable: finished (strategy, segment, variant, code version) rows are skipped.
3. Scores robustness (backend/validation/robustness.py), runs walk-forward selection,
   stores validation_reports and exports data_snapshots/validation/.
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
from backend.strategies.generator import DEFAULT_SPACE, SMALL_SPACE, generate
from backend.strategies.mass_test import _hash, dataset_descriptor, relabel_commit_versions
from backend.validation.robustness import score_strategies, walk_forward
from backend.validation.segments import load_segment_table, make_segments, run_segment_table


def _ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2021-08-06")
    ap.add_argument("--to", dest="end", default="2026-11-01")
    ap.add_argument("--space", default="default", choices=["default", "small"])
    ap.add_argument("--segment-days", type=int, default=91)
    ap.add_argument("--oos", type=int, default=7)
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--batch", type=int, default=45, help="strategies per replay pass")
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    cfg = load_config()
    p = cfg.pools[cfg.primary_pool]
    t0 = time.monotonic()
    ds = load_dataset(cfg)
    segs = make_segments(ds, _ts(args.start), _ts(args.end), args.segment_days, args.oos)
    space = DEFAULT_SPACE if args.space == "default" else SMALL_SPACE
    params = generate(space)
    base = dataset_descriptor(ds, cfg.primary_pool, p.address, segs[0].start_block, segs[-1].end_block,
                              int(args.capital * 10 ** ds.decimals1), "gas_units",
                              cfg.lab["costs"]["priority_fee_gwei"])
    base.pop("from_block"), base.pop("to_block")
    base["segment_days"] = args.segment_days
    engine = S.make_engine(cfg.lab["database"]["url"])
    relabeled = relabel_commit_versions(engine)
    if relabeled:
        print("relabeled stored experiments:", relabeled)
    print(f"{len(segs)} segments x {args.segment_days}d ({sum(s.role == 'OOS' for s in segs)} OOS), "
          f"{len(params)} strategies", flush=True)
    for s in segs:
        print(f"  seg {s.index:2d} {s.role:3s} {datetime.fromtimestamp(s.start_ts, timezone.utc):%Y-%m-%d}"
              f"  price {s.price_change:+6.1%}  vol {s.realized_vol:5.1%}  {s.trend}/{s.vol_class}")
    stats = run_segment_table(engine, ds, params, segs, base, batch=args.batch,
                              progress=lambda d, n: print(f"  pass {d}/{n} strategies "
                                                          f"{time.monotonic() - t0:.0f}s", flush=True))
    print(stats, flush=True)
    version = stats["code_version"]  # the version the table was computed with
    table = load_segment_table(engine, base, segs, version)
    reports = score_strategies(table, params, segs, space)
    wf = walk_forward(table, params, segs)
    study_key = _hash([base, [asdict(s) for s in segs], sorted(x.spec_hash for x in params), version])
    now = int(time.time())
    with engine.begin() as conn:
        conn.execute(S.validation_reports.delete().where(S.validation_reports.c.study_key == study_key))
        conn.execute(insert(S.validation_reports), [dict(
            study_key=study_key, spec_hash=r.spec_hash, code_version=version, score=round(r.score * 100),
            components_json=json.dumps({**r.components, **r.stats}), flags_json=json.dumps(r.flags),
            created_at=now) for r in reports])
    out_dir = ROOT / "data_snapshots" / "validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"validation_{study_key[:16]}.json"
    out.write_text(json.dumps({
        "study_key": study_key, "code_version": version, "dataset": base,
        "segments": [asdict(s) for s in segs], "label": "MODELED",
        "reports": [asdict(r) for r in reports], "walk_forward": wf}, indent=1) + "\n")

    def pct(x):
        return "   n/a" if x is None else f"{x:+6.1%}"
    print(f"\nRANKING BY ROBUSTNESS (top {args.top}):")
    print(f"{'strategy':34s} score  IS ROI OOS ROI vsPass  OOS+ beatH gas2+ vol½+ regOK  MDD  flags")
    for r in reports[:args.top]:
        st, c = r.stats, r.components
        print(f"{r.name:34s} {r.score:5.1f} {pct(st.get('is_mean_roi'))} {pct(st.get('oos_mean_roi'))} "
              f"{pct(st.get('oos_vs_passive'))} {st.get('oos_positive', 0):5.0%} "
              f"{st.get('oos_beat_hodl', 0):5.0%} {st.get('gas_x2_oos_positive', 0):5.0%} "
              f"{st.get('volume_x0.5_oos_positive', 0):5.0%} {c.get('consistency', 0):5.2f} "
              f"{st.get('mean_mdd', 0):5.1%}  {','.join(r.flags)}")
    by_is = sorted([r for r in reports if r.stats.get("is_mean_roi") is not None],
                   key=lambda r: -r.stats["is_mean_roi"])[:5]
    print("\nfor contrast — best by IN-SAMPLE ROI only:")
    for r in by_is:
        print(f"  {r.name:34s} IS {pct(r.stats['is_mean_roi'])}  OOS {pct(r.stats.get('oos_mean_roi'))}"
              f"  score {r.score:5.1f}  rank {reports.index(r) + 1}")
    print(f"\nWALK-FORWARD (choose best of previous {wf['train_segments']} segments, test next):")
    print(f"  chosen strategy: sum ROI {wf['chosen_sum_roi']:+.1%}, positive in "
          f"{(wf['chosen_positive'] or 0):.0%} of test segments, beats passive in "
          f"{wf['chosen_beats_passive']:.0%}")
    print(f"  passive full range: sum ROI {wf['passive_sum_roi']:+.1%}; "
          f"median strategy: {wf['median_sum_roi']:+.1%}")
    print(f"\nwritten {out.relative_to(ROOT)}  ({time.monotonic() - t0:.0f}s)")


if __name__ == "__main__":
    main()
