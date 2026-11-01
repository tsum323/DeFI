"""Robustness score v1 and walk-forward selection (SPEC §9, ARCHITECTURE §5).

Inputs: the segment table {(spec_hash, segment, variant): metrics}. All numbers
are relative to the capital so segments of different price levels compare.

score = 100 x (0.30 s_oos + 0.20 s_consistency + 0.15 s_drawdown
               + 0.15 s_neighbours + 0.20 s_sensitivity) x penalties
- s_oos: mean of (share of OOS segments with net > 0, share beating its own HODL,
  sigmoid(mean OOS ROI minus the passive full-range ROI, scale 2%));
- s_consistency: mean of (share of all segments with net > 0, share of market
  regimes (trend x vol class) whose mean ROI is > 0);
- s_drawdown: 1 - min(1, mean max drawdown / 50%);
- s_neighbours: share of grid neighbours (one step in one parameter) whose mean
  OOS ROI is > 0 — a strategy alone on a lucky island scores low;
- s_sensitivity: share of OOS segments still net > 0 with gas x2 and with
  volume x0.5 (mean of both);
- penalties: x(1 - share of ruined segments); x0.8 if SIZE_IMPACT in most
  segments; x(n/target) if fewer usable segments than expected.
The winner is chosen by this score, never by the best in-sample return.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field

from backend.strategies.generator import DEFAULT_SPACE, StrategyParams
from backend.validation.segments import Segment

W = {"oos": 0.30, "consistency": 0.20, "drawdown": 0.15, "neighbours": 0.15, "sensitivity": 0.20}
PASSIVE = StrategyParams(full_range=True, width_pct=0.0).spec_hash


def _sig(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def _roi(m: dict | None) -> float | None:
    return None if m is None else m["net_a"] / m["capital"]


@dataclass
class Report:
    spec_hash: str
    name: str
    score: float
    components: dict = field(default_factory=dict)
    flags: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def grid_coordinates(p: StrategyParams, space: dict = DEFAULT_SPACE) -> tuple | None:
    """Position of a strategy in the grid (width idx, rule idx, ratio idx) or None."""
    c = p.canonical()
    if c.full_range or c.width_vol_mult is not None or c.max_vol_entry is not None:
        return None
    try:
        wi = space["width_pct"].index(c.width_pct)
        ri = space["ratio_token1"].index(c.ratio_token1)
    except ValueError:
        return None
    rule = {k: getattr(c, k) for k in ("rebalance", "rebalance_delay_blocks", "period_seconds",
                                       "edge_fraction")}
    for i, r in enumerate(space["rules"]):
        full = {"rebalance_delay_blocks": 0, "period_seconds": 0, "edge_fraction": 1.0, **r}
        if all(full[k] == rule[k] for k in rule):
            return wi, i, ri
    return None


def _neighbours(coord: tuple, coords: dict[tuple, str], space: dict) -> list[str]:
    wi, ri, ki = coord
    out = []
    for d in (-1, 1):
        for c in ((wi + d, ri, ki), (wi, ri, ki + d)):
            if c in coords:
                out.append(coords[c])
    # rules: neighbours are the adjacent variants of the same rule type
    rules = space["rules"]
    for d in (-1, 1):
        j = ri + d
        if 0 <= j < len(rules) and rules[j]["rebalance"] == rules[ri]["rebalance"]:
            c = (wi, j, ki)
            if c in coords:
                out.append(coords[c])
    return out


def score_strategies(table: dict, params: list[StrategyParams], segments: list[Segment],
                     space: dict = DEFAULT_SPACE) -> list[Report]:
    oos = [s for s in segments if s.role == "OOS"]
    regimes = {s.index: (s.trend, s.vol_class) for s in segments}
    passive_oos = {s.index: _roi(table.get((PASSIVE, s.index, "base"))) for s in oos}
    coords = {}
    for p in params:
        c = grid_coordinates(p, space)
        if c is not None:
            coords[c] = p.spec_hash
    mean_oos = {}
    for p in params:
        r = [_roi(table.get((p.spec_hash, s.index, "base"))) for s in oos]
        r = [x for x in r if x is not None]
        mean_oos[p.spec_hash] = statistics.fmean(r) if r else None
    reports = []
    for p in params:
        h = p.spec_hash
        base = {s.index: table.get((h, s.index, "base")) for s in segments}
        usable = [m for m in base.values() if m is not None]
        if not usable:
            reports.append(Report(h, p.name, 0.0, flags=["no_results"]))
            continue
        o = [base[s.index] for s in oos if base.get(s.index) is not None]
        oos_roi = [_roi(m) for m in o]
        pos_oos = sum(r > 0 for r in oos_roi) / len(oos) if oos else 0.0
        beat_hodl = sum(m["vs_hodl_a"] > 0 for m in o) / len(oos) if oos else 0.0
        vs_passive = [_roi(m) - passive_oos[s.index] for s, m in
                      ((s, base[s.index]) for s in oos)
                      if m is not None and passive_oos.get(s.index) is not None]
        s_oos = (pos_oos + beat_hodl + _sig(statistics.fmean(vs_passive) / 0.02 if vs_passive
                                            else -10)) / 3
        all_roi = [_roi(m) for m in usable]
        by_regime: dict = {}
        for i, m in base.items():
            if m is not None:
                by_regime.setdefault(regimes[i], []).append(_roi(m))
        regime_ok = sum(statistics.fmean(v) > 0 for v in by_regime.values()) / max(1, len(by_regime))
        s_cons = (sum(r > 0 for r in all_roi) / len(segments) + regime_ok) / 2
        mdd = statistics.fmean(m["max_drawdown_a"] for m in usable)
        s_dd = 1 - min(1.0, mdd / 0.5)
        c = grid_coordinates(p, space)
        nb = _neighbours(c, coords, space) if c is not None else []
        nb_vals = [mean_oos[n] for n in nb if mean_oos.get(n) is not None]
        s_nb = sum(v > 0 for v in nb_vals) / len(nb_vals) if nb_vals else 0.5
        sens = []
        for var in ("gas_x2", "volume_x0.5"):
            ms = [table.get((h, s.index, var)) for s in oos]
            sens.append(sum(1 for m in ms if m is not None and m["net_a"] > 0) / len(oos) if oos else 0)
        s_sens = statistics.fmean(sens)
        ruined = sum(m["ruined"] for m in usable) / len(usable)
        size = sum(m["size_impact"] for m in usable) / len(usable)
        pen = (1 - ruined) * (0.8 if size > 0.5 else 1.0) * min(1.0, len(usable) / len(segments))
        comp = {"oos": s_oos, "consistency": s_cons, "drawdown": s_dd, "neighbours": s_nb,
                "sensitivity": s_sens, "penalty": pen}
        score = 100 * sum(W[k] * comp[k] for k in W) * pen
        flags = []
        if ruined:
            flags.append("RUINED_IN_SOME_SEGMENTS")
        if size > 0.5:
            flags.append("SIZE_IMPACT")
        if pos_oos < 0.5:
            flags.append("OOS_MOSTLY_NEGATIVE")
        if len(by_regime) > 1 and regime_ok <= 0.5:
            flags.append("REGIME_DEPENDENT")
        is_roi = [_roi(base[s.index]) for s in segments if s.role == "IS" and base.get(s.index)]
        stats = {"is_mean_roi": statistics.fmean(is_roi) if is_roi else None,
                 "oos_mean_roi": statistics.fmean(oos_roi) if oos_roi else None,
                 "oos_positive": pos_oos, "oos_beat_hodl": beat_hodl,
                 "oos_vs_passive": statistics.fmean(vs_passive) if vs_passive else None,
                 "all_positive": sum(r > 0 for r in all_roi) / len(segments),
                 "regimes": {f"{k[0]}/{k[1]}": statistics.fmean(v) for k, v in by_regime.items()},
                 "mean_mdd": mdd, "neighbours": len(nb_vals),
                 "gas_x2_oos_positive": sens[0], "volume_x0.5_oos_positive": sens[1],
                 "segments": len(usable), "rebalances_total": sum(m["rebalances"] for m in usable)}
        reports.append(Report(h, p.name, score, comp, flags, stats))
    reports.sort(key=lambda r: -r.score)
    return reports


def walk_forward(table: dict, params: list[StrategyParams], segments: list[Segment],
                 train_segments: int = 2) -> dict:
    """Pick the best strategy by mean ROI over the previous `train_segments` and
    record its ROI on the next segment; compare with the passive full-range LP and
    with the median strategy. Tests the selection process, not one strategy."""
    names = {p.spec_hash: p.name for p in params}
    steps = []
    for k in range(train_segments, len(segments)):
        train = segments[k - train_segments:k]
        best, best_v = None, -1e18
        for p in params:
            r = [_roi(table.get((p.spec_hash, s.index, "base"))) for s in train]
            if None in r:
                continue
            v = statistics.fmean(r)
            if v > best_v:
                best, best_v = p.spec_hash, v
        test = segments[k]
        if best is None:
            continue
        chosen = _roi(table.get((best, test.index, "base")))
        all_test = [x for x in (_roi(table.get((p.spec_hash, test.index, "base"))) for p in params)
                    if x is not None]
        steps.append({"test_segment": test.index, "role": test.role, "chosen": names[best],
                      "train_roi": best_v, "test_roi": chosen,
                      "passive_roi": _roi(table.get((PASSIVE, test.index, "base"))),
                      "median_roi": statistics.median(all_test) if all_test else None})
    def total(key):
        vals = [s[key] for s in steps if s[key] is not None]
        return sum(vals), (sum(v > 0 for v in vals) / len(vals) if vals else None)
    return {"steps": steps, "train_segments": train_segments,
            "chosen_sum_roi": total("test_roi")[0], "chosen_positive": total("test_roi")[1],
            "passive_sum_roi": total("passive_roi")[0], "median_sum_roi": total("median_roi")[0],
            "chosen_beats_passive": sum(1 for s in steps if s["test_roi"] is not None and
                                        s["passive_roi"] is not None and s["test_roi"] > s["passive_roi"])
            / max(1, len(steps))}


def nested_selection(table: dict, params: list[StrategyParams], segments: list[Segment],
                     space: dict = DEFAULT_SPACE, inner_oos: int = 4, top: int = 10) -> dict:
    """Selection that never sees the real OOS segments: score on IS only (the last
    `inner_oos` IS segments play the role of OOS), pick the top strategies, then
    report their results on the real OOS segments. Ranking by the full score uses
    the OOS segments and is therefore optimistic; this is the honest estimate."""
    from dataclasses import replace
    is_segs = [s for s in segments if s.role == "IS"]
    real_oos = [s for s in segments if s.role == "OOS"]
    inner = [replace(s, role="OOS" if k >= len(is_segs) - inner_oos else "IS")
             for k, s in enumerate(is_segs)]
    picked = score_strategies(table, params, inner, space)[:top]
    rows = []
    for r in picked:
        rois = [_roi(table.get((r.spec_hash, s.index, "base"))) for s in real_oos]
        rois = [x for x in rois if x is not None]
        rows.append({"name": r.name, "inner_score": r.score,
                     "oos_mean_roi": statistics.fmean(rois) if rois else None,
                     "oos_positive": sum(x > 0 for x in rois) / len(real_oos) if real_oos else None})
    passive = [_roi(table.get((PASSIVE, s.index, "base"))) for s in real_oos]
    passive = [x for x in passive if x is not None]
    vals = [r["oos_mean_roi"] for r in rows if r["oos_mean_roi"] is not None]
    return {"inner_oos_segments": inner_oos, "picked": rows,
            "picked_mean_oos_roi": statistics.fmean(vals) if vals else None,
            "passive_mean_oos_roi": statistics.fmean(passive) if passive else None}
