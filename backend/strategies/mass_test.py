"""Mass testing of many strategies on one dataset (stage 5).

Every experiment is stored once per (strategy hash, dataset hash, code version)
in the `experiments` table; re-running a study skips what is already done.
Results are MODELED and in-sample: ranking/validation is stage 6.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import insert, select

from backend.backtesting.dataset import cached_state_at
from backend.backtesting.position_sim import Dataset, PositionResult, simulate_many
from backend.database import schema as S
from backend.strategies.generator import StrategyParams

ROOT = Path(__file__).resolve().parents[2]


# Code that determines backtest results. Edits elsewhere (optimizer, scripts, docs,
# scoring) do not invalidate stored experiments.
RESULT_PATHS = ("backend/backtesting", "backend/strategies/generator.py", "backend/analytics",
                "backend/core", "backend/validation/segments.py")


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True,
                                   stderr=subprocess.DEVNULL).strip()


def code_version(rev: str = "HEAD") -> str:
    """Hash of the git objects of RESULT_PATHS at `rev` (+ '-dirty' if they have
    uncommitted changes and rev is HEAD)."""
    try:
        ids = [_git("rev-parse", f"{rev}:{p}") for p in RESULT_PATHS]
        version = "sim-" + hashlib.sha256(" ".join(ids).encode()).hexdigest()[:12]
        if rev == "HEAD" and subprocess.run(["git", "diff", "--quiet", "HEAD", "--", *RESULT_PATHS],
                                            cwd=ROOT).returncode != 0:
            version += "-dirty"
        return version
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def relabel_commit_versions(engine) -> dict:
    """Older rows were labelled with a commit hash. Relabel them to the result-code
    version when that commit's RESULT_PATHS are identical to it (checked by git);
    rows whose code differs keep their label and are recomputed if needed."""
    out = {}
    with engine.begin() as conn:
        labels = [v for v in conn.execute(select(S.experiments.c.code_version).distinct()).scalars()
                  if v and not v.startswith("sim-") and not v.endswith("-dirty") and v != "unknown"]
        for old in labels:
            try:
                new = code_version(old)
            except Exception:  # noqa: BLE001 - unknown commit
                continue
            if new == "unknown":
                continue
            rows = conn.execute(select(S.experiments.c.id, S.experiments.c.spec_hash,
                                       S.experiments.c.dataset_hash)
                                .where(S.experiments.c.code_version == old)).all()
            existing = set(conn.execute(select(S.experiments.c.dedup_key).where(
                S.experiments.c.code_version == new)).scalars())
            n = 0
            for rid, sh, dh in rows:
                key = _hash([sh, dh, new])
                if key in existing:
                    conn.execute(S.experiments.delete().where(S.experiments.c.id == rid))
                    continue
                conn.execute(S.experiments.update().where(S.experiments.c.id == rid)
                             .values(code_version=new, dedup_key=key))
                existing.add(key)
                n += 1
            out[old] = (new, n)
    return out


def dataset_descriptor(ds: Dataset, pool_key: str, address: str, start: int, end: int,
                       capital_token1: int, gas_set: str, priority_fee_gwei: float) -> dict:
    upto = [e for e in ds.events if e.block <= end]
    eth = [r for r in ds.eth_usd if r[0] <= end]
    return {"pool": pool_key, "address": address, "from_block": start, "to_block": end,
            "events_upto_end": len(upto), "last_event": [upto[-1].block, upto[-1].log_index],
            "eth_usd_rows": len(eth), "gas_units": ds.gas_units[gas_set], "gas_set": gas_set,
            "priority_fee_gwei": priority_fee_gwei, "capital_token1": capital_token1}


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def run_mass_test(engine, ds: Dataset, params: list[StrategyParams], descriptor: dict, *,
                  batch: int = 40, progress=None, export_dir: Path | None = None) -> dict:
    dset_hash = _hash(descriptor)
    version = code_version()
    with engine.begin() as conn:
        if conn.execute(select(S.datasets.c.dataset_hash).where(
                S.datasets.c.dataset_hash == dset_hash)).first() is None:
            conn.execute(insert(S.datasets).values(
                dataset_hash=dset_hash, pool=descriptor["pool"], from_block=descriptor["from_block"],
                to_block=descriptor["to_block"], description_json=json.dumps(descriptor)))
        for p in params:
            if conn.execute(select(S.strategies.c.spec_hash).where(
                    S.strategies.c.spec_hash == p.spec_hash)).first() is None:
                conn.execute(insert(S.strategies).values(
                    spec_hash=p.spec_hash, name=p.name, params_json=json.dumps(asdict(p))))
        done = set(conn.execute(select(S.experiments.c.dedup_key).where(
            S.experiments.c.dataset_hash == dset_hash, S.experiments.c.code_version == version,
            S.experiments.c.status == "done")).scalars())
    todo = [p for p in params if _hash([p.spec_hash, dset_hash, version]) not in done]
    stats = {"dataset_hash": dset_hash, "code_version": version, "requested": len(params),
             "skipped_done": len(params) - len(todo), "done": 0, "failed": 0}
    start, end = descriptor["from_block"], descriptor["to_block"]
    state = cached_state_at(ds, start - 1) if todo else None
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        specs = [p.to_spec(start, end, descriptor["capital_token1"], descriptor["gas_set"],
                           descriptor["priority_fee_gwei"]) for p in chunk]
        t0 = int(time.time())
        results = simulate_many(ds, specs, start_state=state, return_exceptions=True)
        rows = []
        for p, r in zip(chunk, results):
            key = _hash([p.spec_hash, dset_hash, version])
            ok = isinstance(r, PositionResult)
            rows.append(dict(
                dedup_key=key, spec_hash=p.spec_hash, dataset_hash=dset_hash, code_version=version,
                status="done" if ok else "failed",
                rejection_reason=None if ok else f"{type(r).__name__}: {r}"[:2000],
                metrics_json=json.dumps(r.metrics) if ok else None,
                actions_json=json.dumps([asdict(a) for a in r.actions]) if ok else None,
                label="MODELED", started_at=t0, finished_at=int(time.time())))
            stats["done" if ok else "failed"] += 1
        with engine.begin() as conn:
            for row in rows:
                conn.execute(S.experiments.delete().where(S.experiments.c.dedup_key == row["dedup_key"]))
            conn.execute(insert(S.experiments), rows)
        if progress:
            progress(min(i + batch, len(todo)), len(todo))
    if export_dir is not None:
        export_results(engine, dset_hash, export_dir)
    return stats


def load_results(engine, dataset_hash: str, code_version_: str | None = None) -> list[dict]:
    q = (select(S.experiments.c.spec_hash, S.strategies.c.name, S.strategies.c.params_json,
                S.experiments.c.metrics_json, S.experiments.c.code_version, S.experiments.c.status,
                S.experiments.c.rejection_reason)
         .join(S.strategies, S.strategies.c.spec_hash == S.experiments.c.spec_hash)
         .where(S.experiments.c.dataset_hash == dataset_hash))
    if code_version_:
        q = q.where(S.experiments.c.code_version == code_version_)
    with engine.connect() as conn:
        return [{"spec_hash": r[0], "name": r[1], "params": json.loads(r[2]),
                 "metrics": json.loads(r[3]) if r[3] else None, "code_version": r[4],
                 "status": r[5], "error": r[6]} for r in conn.execute(q)]


def export_results(engine, dataset_hash: str, out_dir: Path) -> Path:
    """Persist study results (metrics only) as gzip JSON lines in the repo snapshot."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with engine.connect() as conn:
        desc = conn.execute(select(S.datasets.c.description_json).where(
            S.datasets.c.dataset_hash == dataset_hash)).scalar_one()
    path = out_dir / f"study_{dataset_hash[:16]}.jsonl.gz"
    with gzip.open(path, "wt") as f:
        f.write(json.dumps({"dataset_hash": dataset_hash, "dataset": json.loads(desc)}) + "\n")
        for r in sorted(load_results(engine, dataset_hash), key=lambda r: r["spec_hash"]):
            f.write(json.dumps(r) + "\n")
    return path
