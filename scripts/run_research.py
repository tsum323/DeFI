"""Stage 7: one AI research cycle (Claude proposes, Python tests and judges).

  python -m scripts.run_research [--n 5]

Needs ANTHROPIC_API_KEY (config/lab.yaml: ai_researcher.api_key_env). The model sees only
in-sample results; verdicts are computed on the OOS segments with pre-registered criteria
(backend/ai_researcher/research.py). Report: data_snapshots/research/cycle_<id>.md
"""

from __future__ import annotations

import argparse
import sys

from backend.ai_researcher.adapter import AnthropicAdapter, LLMError
from backend.ai_researcher.research import run_cycle
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
    args = ap.parse_args()
    try:
        adapter = AnthropicAdapter(ai["model"], ai.get("api_key_env", "ANTHROPIC_API_KEY"), args.effort)
    except LLMError as exc:
        print(f"AI researcher cannot start: {exc}.\n"
              "Add the key in the environment settings (environment menu -> Edit -> environment "
              "variables / secrets) and start a new session.", file=sys.stderr)
        sys.exit(2)
    ds = load_dataset(cfg)
    study = setup_study(cfg, ds)
    engine = S.make_engine(cfg.lab["database"]["url"])
    relabel_commit_versions(engine)
    version = code_version()
    try:
        summary = run_cycle(engine, ds, study, adapter, generate(DEFAULT_SPACE), n=args.n,
                            version=version, out_dir=ROOT / "data_snapshots" / "research")
    except LLMError as exc:
        print(f"AI research cycle failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print(f"cycle {summary['cycle_id']}: {summary['proposed']} proposed, {summary['tested']} tested, "
          f"{summary['confirmed']} confirmed ({summary['seconds']}s)")
    for h in summary["hypotheses"]:
        print(f"- [{h['verdict'] or h['status']}/{h['confidence'] or '-'}] {h['title']}: {h['conclusion'] or ''}")


if __name__ == "__main__":
    main()
