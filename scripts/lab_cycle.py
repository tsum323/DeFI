"""One tick of the UNISWAP AI LAB routine (backend/lab/cycle.py): scanner, virtual
portfolio, forecast reviews; once a day calibration, research and the daily report.

  python -m scripts.lab_cycle [--no-daily] [--force-daily]
"""

from __future__ import annotations

import argparse

from backend.backtesting.dataset import ROOT, load_dataset
from backend.core.config import load_config
from backend.data_engine.rpc import build_clients
from backend.database import schema as S
from backend.lab.cycle import run_tick
from backend.lab.scanner import set_state
from backend.strategies.mass_test import code_version
from backend.validation.study import setup_study


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-daily", action="store_true")
    ap.add_argument("--force-daily", action="store_true", help="run the daily research now")
    args = ap.parse_args()
    cfg = load_config()
    engine = S.make_engine(cfg.lab["database"]["url"])
    clients = {c.name: c for c in build_clients(cfg.networks["ethereum"])[0]}
    client = clients.get(cfg.lab["scanner"]["endpoint"]) or next(iter(clients.values()))
    ds = load_dataset(cfg)
    study = setup_study(cfg, ds)
    if args.force_daily:
        set_state(engine, "daily_research", {"day": None})
    out = run_tick(engine, cfg, client=client, ds=ds, study=study, version=code_version(),
                   report_dir=ROOT / "data_snapshots" / "reports", allow_daily=not args.no_daily)
    print("lab cycle:", out)


if __name__ == "__main__":
    main()
