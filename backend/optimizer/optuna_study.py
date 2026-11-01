"""Bayesian optimization (Optuna TPE) of strategy parameters on IN-SAMPLE segments only.

- Search space covers range width, target token ratio, rebalance rule and its
  parameters, cooldown (re-entry), volatility-scaled width and a volatility
  threshold for exit / re-entry (SPEC §7).
- Objective = mean IS segment ROI - 0.5 x std of IS segment ROI (consistency
  matters, not one lucky segment); ruined or failed segments count as -100%.
  OOS segments are never evaluated during the search (ARCHITECTURE §4).
- Trials run in batches through run_segment_table (one replay pass per batch);
  identical strategies are taken from the experiments table, never re-run.
- The study lives in an Optuna RDB storage, so it resumes after a restart.
"""

from __future__ import annotations

import statistics
import warnings
from pathlib import Path

import optuna

from backend.strategies.generator import DAY, StrategyParams
from backend.strategies.mass_test import _hash
from backend.validation.segments import Segment, load_segment_table, run_segment_table

ROOT = Path(__file__).resolve().parents[2]
STORAGE = f"sqlite:///{ROOT / 'data' / 'optuna.sqlite'}"


def params_from_trial(trial: optuna.trial.BaseTrial) -> StrategyParams:
    rule = trial.suggest_categorical("rebalance", ["none", "on_exit", "periodic", "edge"])
    kw: dict = {"rebalance": rule,
                "width_pct": trial.suggest_float("width_pct", 0.01, 2.0, log=True)}
    if trial.suggest_categorical("asymmetric", [False, True]):
        kw["ratio_token1"] = round(trial.suggest_float("ratio_token1", 0.2, 0.8), 2)
    if rule in ("on_exit", "edge"):
        kw["rebalance_delay_blocks"] = trial.suggest_categorical(
            "rebalance_delay_blocks", [0, 300, 1800, 7200, 21600])
    if rule == "periodic":
        kw["period_seconds"] = trial.suggest_int("period_days", 1, 60, log=True) * DAY
    if rule == "edge":
        kw["edge_fraction"] = round(trial.suggest_float("edge_fraction", 0.3, 1.0), 2)
    if rule != "none":
        kw["cooldown_seconds"] = trial.suggest_categorical("cooldown_hours", [0, 6, 24, 72]) * 3600
        if trial.suggest_categorical("vol_width", [False, True]):
            kw["width_vol_mult"] = round(trial.suggest_float("width_vol_mult", 0.3, 5.0, log=True), 3)
    if trial.suggest_categorical("vol_filter", [False, True]):
        kw["max_vol_entry"] = round(trial.suggest_float("max_vol_entry", 0.3, 1.5), 2)
    if "width_vol_mult" in kw or "max_vol_entry" in kw:
        kw["vol_lookback_seconds"] = trial.suggest_categorical("vol_lookback_days", [1, 3, 7, 14, 30]) * DAY
    kw["width_pct"] = round(kw["width_pct"], 4)
    return StrategyParams(**kw).canonical()


def is_objective(table: dict, spec_hash: str, is_segments: list[Segment]) -> float:
    rois = []
    for s in is_segments:
        m = table.get((spec_hash, s.index, "base"))
        if m is None or m.get("ruined"):
            rois.append(-1.0)
        else:
            rois.append(m["net_a"] / m["capital"])
    if not rois:
        return -1.0
    sd = statistics.stdev(rois) if len(rois) > 1 else 0.0
    return statistics.fmean(rois) - 0.5 * sd


def study_name(base_descriptor: dict, is_segments: list[Segment], version: str) -> str:
    return "lp_" + _hash([base_descriptor, [s.index for s in is_segments],
                          [s.start_block for s in is_segments], version])[:20]


def optimize(engine, ds, segments: list[Segment], base_descriptor: dict, *, n_trials: int,
             batch: int = 24, seed: int = 0, storage: str = STORAGE, progress=None) -> optuna.Study:
    from backend.strategies.mass_test import code_version
    is_segments = [s for s in segments if s.role == "IS"]
    if not is_segments:
        raise ValueError("no in-sample segments")
    version = code_version()
    Path(storage.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    with warnings.catch_warnings():  # multivariate/group TPE is flagged experimental by Optuna
        warnings.simplefilter("ignore", optuna.exceptions.ExperimentalWarning)
        sampler = optuna.samplers.TPESampler(seed=seed, multivariate=True, group=True)
    study = optuna.create_study(study_name=study_name(base_descriptor, is_segments, version),
                                storage=storage, direction="maximize", load_if_exists=True,
                                sampler=sampler)
    # trials left RUNNING by a crash are closed as failed (resume)
    for t in study.get_trials(deepcopy=False, states=(optuna.trial.TrialState.RUNNING,)):
        study.tell(t.number, state=optuna.trial.TrialState.FAIL)
    done = sum(1 for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE)
    while done < n_trials:
        k = min(batch, n_trials - done)
        trials = [study.ask() for _ in range(k)]
        params = [params_from_trial(t) for t in trials]
        for t, p in zip(trials, params):
            t.set_user_attr("spec_hash", p.spec_hash)
            t.set_user_attr("name", p.name)
        uniq = list({p.spec_hash: p for p in params}.values())
        run_segment_table(engine, ds, uniq, is_segments, base_descriptor, variants_is=("base",),
                          variants_oos=(), batch=len(uniq))
        table = load_segment_table(engine, base_descriptor, is_segments)
        for t, p in zip(trials, params):
            study.tell(t, is_objective(table, p.spec_hash, is_segments))
        done += k
        if progress:
            progress(done, n_trials, study.best_value, study.best_trial.user_attrs.get("name"))
    return study


def best_params(study: optuna.Study, k: int) -> list[StrategyParams]:
    """Top-k distinct strategies of the study by IS objective."""
    seen, out = set(), []
    trials = sorted((t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE),
                    key=lambda t: -t.value)
    for t in trials:
        p = params_from_trial(optuna.trial.FixedTrial(t.params))
        if p.spec_hash not in seen:
            seen.add(p.spec_hash)
            out.append(p)
        if len(out) == k:
            break
    return out
