"""Persist research state (experiments, reports, hypotheses, signals, virtual and forward
tests) in the repository, because the database lives only inside an ephemeral session.

Each table -> data_snapshots/research_db/<table>.jsonl.xz (one JSON row per line) +
manifest.json with row counts and sha256. Restore inserts missing rows (idempotent).
Derived tables (virtual_marks, virtual_results) are recomputed, not stored.
"""

from __future__ import annotations

import hashlib
import json
import lzma
import shutil
from pathlib import Path

from sqlalchemy import select

from backend.data_engine.sync import _insert_ignore
from backend.database import schema as S

TABLES = ("strategies", "datasets", "experiments", "validation_reports", "hypotheses",
          "hypothesis_experiments", "signals", "signal_events", "market_evaluations",
          "virtual_positions", "forward_tests", "job_runs", "opportunity_rankings",
          "opportunity_events", "pool_hours", "scan_state", "scan_results", "activity", "forecasts", "lessons",
          "portfolio_positions", "portfolio_history", "lab_state",
          "bot_positions", "bot_history", "tracked_positions")
SKIP_COLUMNS = {"experiments": {"actions_json"}}  # large and not needed for research


def export_research(engine, out_dir: Path, optuna_db: Path | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"tables": {}}
    with engine.connect() as conn:
        for name in TABLES:
            t = getattr(S, name)
            cols = [c for c in t.c if c.name not in SKIP_COLUMNS.get(name, set())]
            rows = conn.execute(select(*cols).order_by(*t.primary_key.columns)).all()
            blob = "\n".join(json.dumps(dict(r._mapping), sort_keys=True) for r in rows).encode()
            path = out_dir / f"{name}.jsonl.xz"
            path.write_bytes(lzma.compress(blob, preset=6))
            manifest["tables"][name] = {"rows": len(rows), "sha256": hashlib.sha256(blob).hexdigest()}
    if optuna_db is not None and optuna_db.exists():
        shutil.copy2(optuna_db, out_dir / "optuna.sqlite")
        manifest["optuna"] = "optuna.sqlite"
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


# Research results: only ever added to (by sessions and the daily research). Everything else
# is LIVE state that changes in place (positions, marks, scan progress, learned parameters).
RESEARCH_TABLES = ("strategies", "datasets", "experiments", "validation_reports", "hypotheses",
                   "hypothesis_experiments")


def restore_research(engine, src_dir: Path, optuna_db: Path | None = None, *, mirror_live: bool = False,
                     only: tuple[str, ...] | None = None) -> dict:
    """Merge a saved state into the database.
    Research tables: rows are added, existing ones kept (append-only history).
    Live tables: with `mirror_live` the saved state REPLACES what the database holds (it is the
    newer one: rows that changed or were deleted since must not survive from an older copy);
    without it, rows are only added (used for an older snapshot, e.g. the committed one)."""
    manifest = json.loads((src_dir / "manifest.json").read_text())
    out = {}
    with engine.begin() as conn:
        for name, meta in manifest["tables"].items():
            if only is not None and name not in only:
                continue
            blob = lzma.decompress((src_dir / f"{name}.jsonl.xz").read_bytes())
            if hashlib.sha256(blob).hexdigest() != meta["sha256"]:
                raise ValueError(f"{name}: checksum mismatch")
            rows = [json.loads(line) for line in blob.decode().splitlines() if line]
            t = getattr(S, name)
            if mirror_live and name not in RESEARCH_TABLES:
                conn.execute(t.delete())
            for i in range(0, len(rows), 2000):
                _insert_ignore(conn, t, rows[i:i + 2000])
            out[name] = len(rows)
    # the Optuna study file is optional (git ignores *.sqlite); only needed for optimization
    if (optuna_db is not None and manifest.get("optuna") and not optuna_db.exists()
            and (src_dir / manifest["optuna"]).exists()):
        optuna_db.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_dir / manifest["optuna"], optuna_db)
        out["optuna"] = "restored"
    return out
