"""Save / restore the synced pool data as compressed files inside the repo.

  python -m scripts.data_snapshot export   [--pool wbtc_usdc_3000]
  python -m scripts.data_snapshot restore  [--pool wbtc_usdc_3000]

export: data/lab.sqlite -> data_snapshots/<pool>/ (xz CSV parts + manifest.json)
restore: data_snapshots/<pool>/ -> data/lab.sqlite, after sha256 checks; then
         `python -m scripts.sync_pool` continues from the snapshot's last block.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

from backend.core.config import load_config
from backend.data_engine.snapshot import export_pool, restore_pool
from backend.database import schema as S

ROOT = Path(__file__).resolve().parent.parent
SIDE_FILES = ("pool_verification.json", "fee_tier_comparison.json", "gas_calibration.json",
              "math_validation.json", "replay_checkpoints.json")


def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["export", "restore"])
    ap.add_argument("--pool", default=cfg.primary_pool)
    args = ap.parse_args()
    p = cfg.pools[args.pool]
    chain_id = cfg.networks[p.network].chain_id
    snap = ROOT / "data_snapshots" / args.pool
    engine = S.make_engine(cfg.lab["database"]["url"])
    t0 = time.monotonic()
    if args.action == "export":
        m = export_pool(engine, chain_id, p.address, snap)
        for name in SIDE_FILES:
            if (ROOT / "data" / name).exists():
                shutil.copy2(ROOT / "data" / name, snap / name)
        total = sum(f["bytes"] for f in m["files"])
        print(f"exported {m['counts']} coverage={m['coverage']} "
              f"files={len(m['files'])} size={total / 1e6:.1f} MB in {time.monotonic() - t0:.0f}s")
    else:
        res = restore_pool(engine, snap)
        for name in SIDE_FILES:
            if (snap / name).exists() and not (ROOT / "data" / name).exists():
                shutil.copy2(snap / name, ROOT / "data" / name)
        print(json.dumps(res, indent=2))
        print(f"restored in {time.monotonic() - t0:.0f}s")


if __name__ == "__main__":
    main()
