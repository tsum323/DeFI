"""Stage 7: one AI research cycle (Claude proposes, Python tests and judges).

  python -m scripts.run_research [--n 5]                       # Claude via API key
  python -m scripts.run_research --context-out ctx.json        # write the IS-only context
  python -m scripts.run_research --hypotheses-file hyp.json --author "..." [--oos-seen]
  python -m scripts.run_research --attach-commentary CYCLE_ID --commentary-file c.json

Needs ANTHROPIC_API_KEY (config/lab.yaml: ai_researcher.api_key_env). The model sees only
in-sample results; verdicts are computed on the OOS segments with pre-registered criteria
(backend/ai_researcher/research.py). Report: data_snapshots/research/cycle_<id>.md
"""

from __future__ import annotations

import argparse
import sys

from backend.ai_researcher.adapter import AnthropicAdapter, FileAdapter, LLMError
from backend.ai_researcher.research import (attach_commentary, build_context, run_cycle,
                                            tested_hypotheses)
from backend.ai_researcher.schema import Commentary
from backend.validation.segments import load_segment_table
from backend.backtesting.dataset import ROOT, load_dataset
from backend.core.config import load_config
from backend.database import schema as S
from backend.strategies.generator import DEFAULT_SPACE, generate
from backend.strategies.mass_test import code_version, relabel_commit_versions
from backend.validation.study import setup_study


def main() -> None:
    cfg = load_config()
    ai = cfg.lab["ai_researcher"]
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=ai.get("max_hypotheses_per_cycle", 5))
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--context-out", help="write the in-sample context for a human/assistant author")
    ap.add_argument("--hypotheses-file", help="use hypotheses from this JSON file instead of the API")
    ap.add_argument("--author", default="in-session assistant", help="recorded as created_by_model")
    ap.add_argument("--oos-seen", action="store_true",
                    help="the author has already seen OOS results (verdicts flagged as contaminated)")
    ap.add_argument("--attach-commentary", metavar="CYCLE_ID")
    ap.add_argument("--commentary-file")
    args = ap.parse_args()
    engine = S.make_engine(cfg.lab["database"]["url"])
    out_dir = ROOT / "data_snapshots" / "research"
    if args.attach_commentary:
        c = Commentary.model_validate_json(open(args.commentary_file).read())
        print(attach_commentary(engine, args.attach_commentary, c, out_dir))
        return
    adapter = None
    if args.hypotheses_file:
        adapter = FileAdapter(args.hypotheses_file, f"{args.author} (file)", args.oos_seen)
    elif not args.context_out:
        try:
            adapter = AnthropicAdapter(ai["model"], ai.get("api_key_env", "ANTHROPIC_API_KEY"),
                                       args.effort)
        except LLMError as exc:
            print(f"AI researcher cannot start: {exc}.\n"
                  "Add the key in the environment settings (environment menu -> Edit -> environment "
                  "variables / secrets) and start a new session, or use --hypotheses-file.",
                  file=sys.stderr)
            sys.exit(2)
    ds = load_dataset(cfg)
    study = setup_study(cfg, ds)
    relabel_commit_versions(engine)
    version = code_version()
    if args.context_out:
        table = load_segment_table(engine, study.base, study.segments, version)
        open(args.context_out, "w").write(
            build_context(table, generate(DEFAULT_SPACE), study, tested_hypotheses(engine)))
        print(f"in-sample context written to {args.context_out}")
        return
    try:
        summary = run_cycle(engine, ds, study, adapter, generate(DEFAULT_SPACE), n=args.n,
                            version=version, out_dir=out_dir)
    except LLMError as exc:
        print(f"AI research cycle failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"cycle {summary['cycle_id']}: {summary['proposed']} proposed, {summary['tested']} tested, "
          f"{summary['confirmed']} confirmed ({summary['seconds']}s)")
    for h in summary["hypotheses"]:
        print(f"- [{h['verdict'] or h['status']}/{h['confidence'] or '-'}] {h['title']}: {h['conclusion'] or ''}")


if __name__ == "__main__":
    main()
